"""v57「按主机走代理」：只让名单里的主机在**抓取订阅列表**时走代理。

为什么需要它：`yang-1989.xyz` 三个源被该站 Cloudflare 按出口拒绝 —— 2026-10-08 实测，
同一台机器换过两代出口 IPv4（含 IPv6 前缀一起换）后直连一律
`403 Sorry, you have been blocked`；而同一个 URL 经本机代理是 `200 / 119742 B / 461 条`。

这个文件只钉三件事（**不联网、不碰数据库**）：
1. `Settings.proxy_for()` 的名单匹配口径（后缀匹配、不误伤 `…yang-1989.xyz.evil.com`）；
2. `SyncManager._open_source()` 的两条路互不串味 ——
   命中名单：用**代理 client** + `addresses=()`，且**不做**本地解析（解析权交给代理）；
   没命中：仍然走 `self.client` + 已校验并**钉住的 IP**（v42 的 DNS 重绑定修复不能回退）；
3. 代理 client 在 `close()` 里被关掉（否则每轮重启漏连接池）。
"""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace as dataclass_replace
from unittest import mock

from app import sync
from app.config import settings as real_settings

_PROXY = "http://host.docker.internal:7890"
_PROXIED_URL = "https://iptv.yang-1989.xyz/playlist.m3u"
_DIRECT_URL = "https://live.example.test/iptv.m3u"


class ProxyForTests(unittest.TestCase):
    def _settings(self, proxy=_PROXY, hosts="yang-1989.xyz"):
        return dataclass_replace(real_settings, source_proxy=proxy, source_proxy_hosts=hosts)

    def test_matches_host_and_subdomains(self):
        s = self._settings()
        for url in (
            "https://yang-1989.xyz/playlist.m3u",
            "https://iptv.yang-1989.xyz/playlist.m3u",
            "https://migu.yang-1989.xyz/playlist.m3u",
            "https://a.b.yang-1989.xyz/x.m3u",
        ):
            self.assertEqual(s.proxy_for(url), _PROXY, url)

    def test_does_not_match_lookalikes(self):
        s = self._settings()
        for url in (
            "https://yang-1989.xyz.evil.com/x.m3u",   # 后缀比较必须带前导点
            "https://evil-yang-1989.xyz/x.m3u",
            "https://notyang-1989.xyz/x.m3u",
            "https://example.com/yang-1989.xyz.m3u",
        ):
            self.assertEqual(s.proxy_for(url), "", url)

    def test_disabled_when_unset(self):
        for s in (self._settings(proxy=""), self._settings(hosts=""), self._settings(proxy="", hosts="")):
            self.assertEqual(s.proxy_for(_PROXIED_URL), "")

    def test_host_list_tolerates_spaces_and_leading_dots(self):
        s = self._settings(hosts=" .Yang-1989.xyz , other.test ")
        self.assertEqual(s.proxy_for(_PROXIED_URL), _PROXY)
        self.assertEqual(s.proxy_for("https://other.test/x.m3u"), _PROXY)
        self.assertEqual(s.proxy_for("https://nope.test/x.m3u"), "")


class _FakeResponse:
    """只够 _open_source 用：状态码 + headers + aclose()。"""

    def __init__(self, status_code=200):
        self.status_code = status_code
        self.headers: dict[str, str] = {}

    async def aclose(self):
        return None


class OpenSourceProxyTests(unittest.TestCase):
    def setUp(self):
        self.direct = mock.Mock(name="direct_client")
        self.manager = sync.SyncManager.__new__(sync.SyncManager)
        self.manager.client = self.direct
        self.manager._proxy_clients = {}
        self.settings = dataclass_replace(
            real_settings, source_proxy=_PROXY, source_proxy_hosts="yang-1989.xyz"
        )
        self.allowed = mock.patch.object(
            sync, "ALLOWED_SOURCE_HOSTS", {"iptv.yang-1989.xyz", "live.example.test"}
        )
        self.settings_patch = mock.patch.object(sync, "settings", self.settings)
        self.next_redirect_patch = mock.patch.object(sync, "next_redirect", mock.AsyncMock(return_value=None))
        for p in (self.allowed, self.settings_patch, self.next_redirect_patch):
            p.start()
            self.addCleanup(p.stop)

    def _capture_send(self):
        calls: list[tuple] = []

        async def fake_send_pinned(client, url, host, addresses=(), headers=None, **kwargs):
            calls.append((client, url, host, tuple(addresses), dict(headers or {})))
            return _FakeResponse()

        patch = mock.patch.object(sync, "send_pinned", fake_send_pinned)
        patch.start()
        self.addCleanup(patch.stop)
        return calls

    def test_proxied_host_uses_proxy_client_and_skips_pinning(self):
        calls = self._capture_send()
        resolve = mock.Mock(side_effect=AssertionError("走代理时不应做本地解析"))
        patch = mock.patch.object(sync, "resolve_addresses", resolve)
        patch.start()
        self.addCleanup(patch.stop)

        asyncio.run(self.manager._open_source(_PROXIED_URL, {"Accept": "*/*"}))

        self.assertEqual(len(calls), 1)
        client, url, host, addresses, _ = calls[0]
        self.assertIsNot(client, self.direct, "命中名单时必须用代理 client，不能拿直连 client 发")
        self.assertEqual(url, _PROXIED_URL)
        self.assertEqual(host, "iptv.yang-1989.xyz")
        self.assertEqual(addresses, (), "走代理必须传 addresses=()，让 httpx 用 CONNECT 交给代理解析")
        self.assertEqual(resolve.call_count, 0)
        self.assertEqual(list(self.manager._proxy_clients), [_PROXY], "代理 client 应按代理地址缓存")

    def test_unlisted_host_keeps_pinned_direct_path(self):
        calls = self._capture_send()
        patch = mock.patch.object(sync, "resolve_addresses", mock.Mock(return_value=("1.2.3.4",)))
        patch.start()
        self.addCleanup(patch.stop)
        patch2 = mock.patch.object(sync, "restricted_in", mock.Mock(return_value=""))
        patch2.start()
        self.addCleanup(patch2.stop)

        asyncio.run(self.manager._open_source(_DIRECT_URL, {"Accept": "*/*"}))

        client, url, host, addresses, _ = calls[0]
        self.assertIs(client, self.direct, "没命中名单必须仍然用直连 client")
        self.assertEqual(addresses, ("1.2.3.4",), "v42 的钉 IP 行为不能回退成空")
        self.assertEqual(self.manager._proxy_clients, {}, "不该顺手建代理 client")

    def test_close_closes_proxy_clients(self):
        proxy_client = mock.AsyncMock(name="proxy_client")
        self.manager._proxy_clients[_PROXY] = proxy_client
        self.manager._closing = False
        self.manager._queue_guard = asyncio.Lock()
        self.manager._pending = []
        self.manager._full_dirty = ""
        self.manager.current_task = None
        self.direct.aclose = mock.AsyncMock()

        asyncio.run(self.manager.close())

        proxy_client.aclose.assert_awaited()
        self.assertEqual(self.manager._proxy_clients, {})


if __name__ == "__main__":
    unittest.main()
