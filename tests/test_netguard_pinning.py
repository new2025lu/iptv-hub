"""v42 P0-2（DNS 重绑定）回归测试：**校验时的解析结果就是实际连接的目标**。

审计结论：抓取路径原先只"解析一次做校验"，而 httpx 建连时会**自己再解析一次** ——
候选域名解析可控时，校验返回公网 IP、连接时返回 127.0.0.1 即可绕过守卫
（审计已在容器内复现：第 3 次解析返回 127.0.0.1 时成功取回内网内容）。

这里不碰网络：httpx 的 AsyncClient 被替换成只记录请求的替身，DNS 解析函数被
monkeypatch 成"第一次返回公网 IP、以后返回 127.0.0.1"，然后断言最终请求的
目标主机是**校验过的那个公网 IP**（而不是域名、更不是 127.0.0.1），
并且 Host 头仍是原域名（HTTP 语义不能丢）。
"""

from __future__ import annotations

import asyncio
import socket
import unittest
from unittest import mock
from urllib.parse import urlsplit

import httpx

from app import netguard, sync

# 真实的公网地址（example.com 的旧 A 记录）：不能用 203.0.113.x 这类文档网段 ——
# 它们 is_private=True，会把"放行公网"的断言搅浑。
PUBLIC_IP = "93.184.216.34"
ATTACK_IP = "127.0.0.1"

PLAYLIST = (
    b"#EXTM3U\n"
    b"#EXTINF:-1 tvg-id=\"A\",Channel A\n"
    b"http://a.test/1.m3u8\n"
)


class _FakeAsyncClient:
    """httpx.AsyncClient 的最小替身：只记录请求，响应固定是一份合法 m3u。"""

    instances: list["_FakeAsyncClient"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.requests: list[httpx.Request] = []
        _FakeAsyncClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def build_request(self, method, url, headers=None):
        return httpx.Request(method, url, headers=headers)

    async def send(self, request, stream=False):
        self.requests.append(request)
        return httpx.Response(200, content=PLAYLIST, request=request)

    async def aclose(self):
        return None


def _flipping_resolver(host, public=PUBLIC_IP, attack=ATTACK_IP):
    """模拟"校验时解析出公网 IP、之后再解析就变成 127.0.0.1"的攻击者 DNS。"""
    calls: list[str] = []

    def resolve(name):
        calls.append(name)
        return (public,) if len(calls) == 1 else (attack,)

    resolve.calls = calls        # type: ignore[attr-defined]
    return resolve


def _attack_getaddrinfo(host, *args, **kwargs):
    """模拟 httpx 建连时自己那次解析：无论问什么都答 127.0.0.1。

    修复生效时它**根本不会被调用**（连接目标是 IP 字面量，无需解析）；
    一旦被调用就说明又回到了"让 httpx 再解析一次"的老路。
    """
    _attack_getaddrinfo.calls.append(host)       # type: ignore[attr-defined]
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ATTACK_IP, 0))]


_attack_getaddrinfo.calls = []                   # type: ignore[attr-defined]


class PinTargetTests(unittest.TestCase):
    """pin_target：把 host 换成已校验 IP，Host 头保留原样（含端口/IPv6）。"""

    def test_default_port_has_no_port_in_host_header(self):
        pinned, host_header = netguard.pin_target(urlsplit("https://example.test/a.m3u"), "example.test", PUBLIC_IP)
        self.assertEqual(pinned, f"https://{PUBLIC_IP}/a.m3u")
        self.assertEqual(host_header, "example.test")

    def test_explicit_port_survives_in_url_and_host_header(self):
        pinned, host_header = netguard.pin_target(urlsplit("http://example.test:1905/u/x"), "example.test", PUBLIC_IP)
        self.assertEqual(pinned, f"http://{PUBLIC_IP}:1905/u/x")
        self.assertEqual(host_header, "example.test:1905")

    def test_ipv6_literal_gets_brackets(self):
        pinned, host_header = netguard.pin_target(urlsplit("http://[2001:db8::1]:8080/x"), "2001:db8::1", "2001:db8::2")
        self.assertEqual(pinned, "http://[2001:db8::2]:8080/x")
        self.assertEqual(host_header, "[2001:db8::1]:8080")

    def test_no_ip_returns_url_unchanged(self):
        pinned, host_header = netguard.pin_target(urlsplit("https://example.test/a.m3u"), "example.test", None)
        self.assertEqual(pinned, "https://example.test/a.m3u")
        self.assertEqual(host_header, "example.test")


class ProbeManualSourceRebindingTests(unittest.TestCase):
    """手动加源探测：DNS 翻转后请求仍打在已校验的公网 IP 上。"""

    def setUp(self):
        _FakeAsyncClient.instances.clear()
        _attack_getaddrinfo.calls = []

    def test_request_goes_to_validated_ip_not_rebound_host(self):
        resolver = _flipping_resolver("rebind.test")
        with mock.patch.object(sync, "resolve_addresses", resolver), \
             mock.patch.object(socket, "getaddrinfo", _attack_getaddrinfo), \
             mock.patch.object(sync.httpx, "AsyncClient", _FakeAsyncClient):
            probe = asyncio.run(sync.probe_manual_source("http://rebind.test/list.m3u"))

        self.assertEqual(probe["entries"], 1)
        client = _FakeAsyncClient.instances[-1]
        self.assertEqual(len(client.requests), 1, "只该发一次请求")
        request = client.requests[0]
        # 关键断言：连接目标是**校验过的公网 IP**，不是被翻转后的 127.0.0.1
        self.assertEqual(request.url.host, PUBLIC_IP)
        self.assertNotEqual(request.url.host, ATTACK_IP)
        # HTTP 语义靠 Host 头保持（否则源站按域名分流会拿错内容）
        self.assertEqual(request.headers["Host"], "rebind.test")
        self.assertEqual(resolver.calls, ["rebind.test"] * len(resolver.calls))

    def test_each_redirect_hop_is_revalidated_and_repinned(self):
        """逐跳校验保留：第二跳解析到受限地址时直接拒绝，不会再发请求。"""
        responses = [
            (302, {"location": "http://rebind2.test/list.m3u"}, b""),
        ]

        class _RedirectingClient(_FakeAsyncClient):
            async def send(self, request, stream=False):
                self.requests.append(request)
                status, headers, body = responses[min(len(self.requests) - 1, len(responses) - 1)]
                return httpx.Response(status, headers=headers, content=body, request=request)

        def resolver(name):
            return (PUBLIC_IP,) if name == "rebind.test" else (ATTACK_IP,)

        with mock.patch.object(sync, "resolve_addresses", resolver), \
             mock.patch.object(sync.httpx, "AsyncClient", _RedirectingClient):
            with self.assertRaises(ValueError) as ctx:
                asyncio.run(sync.probe_manual_source("http://rebind.test/list.m3u"))
        self.assertIn("受限地址", str(ctx.exception))
        self.assertEqual(len(_FakeAsyncClient.instances[-1].requests), 1, "第二跳不该再发请求")

    def test_unresolvable_host_is_rejected_instead_of_falling_back(self):
        """解析失败必须直接拒绝：退回"让 httpx 自己解析"正是重绑定的入口。"""
        with mock.patch.object(sync, "resolve_addresses", lambda host: ()), \
             mock.patch.object(sync.httpx, "AsyncClient", _FakeAsyncClient):
            with self.assertRaises(ValueError) as ctx:
                asyncio.run(sync.probe_manual_source("http://nxdomain.test/list.m3u"))
        self.assertIn("无法解析", str(ctx.exception))


class OpenSourceRebindingTests(unittest.TestCase):
    """同步路径 _open_source：同样钉 IP（白名单 + 受限地址判定保持不变）。"""

    def setUp(self):
        _FakeAsyncClient.instances.clear()

    def _manager(self, client):
        manager = sync.SyncManager.__new__(sync.SyncManager)   # 不跑 __init__，避免真建连接池
        manager.client = client
        return manager

    def test_open_source_pins_validated_ip(self):
        client = _FakeAsyncClient()
        resolver = _flipping_resolver("whitelisted.test")
        with mock.patch.object(sync, "resolve_addresses", resolver), \
             mock.patch.object(sync, "ALLOWED_SOURCE_HOSTS", {"whitelisted.test"}):
            response = asyncio.run(self._manager(client)._open_source("http://whitelisted.test/live.m3u", {}))
        self.assertEqual(response.status_code, 200)
        request = client.requests[-1]
        self.assertEqual(request.url.host, PUBLIC_IP)
        self.assertEqual(request.headers["Host"], "whitelisted.test")

    def test_open_source_still_blocks_restricted_resolution(self):
        client = _FakeAsyncClient()
        with mock.patch.object(sync, "resolve_addresses", lambda host: (ATTACK_IP,)), \
             mock.patch.object(sync, "ALLOWED_SOURCE_HOSTS", {"whitelisted.test"}):
            with self.assertRaises(ValueError) as ctx:
                asyncio.run(self._manager(client)._open_source("http://whitelisted.test/live.m3u", {}))
        self.assertIn("restricted address", str(ctx.exception))
        self.assertEqual(client.requests, [], "被拦截的目标不得发出任何请求")

    def test_private_source_is_allowed_and_pinned_to_its_own_ip(self):
        """本地源（内网）照常放行：只挡受限地址，不挡内网网段。"""
        client = _FakeAsyncClient()
        with mock.patch.object(sync, "resolve_addresses", lambda host: ("192.0.2.10",)), \
             mock.patch.object(sync, "ALLOWED_SOURCE_HOSTS", {"local.test"}):
            response = asyncio.run(self._manager(client)._open_source("http://local.test/sub", {}))
        self.assertEqual(response.status_code, 200)
        request = client.requests[-1]
        self.assertEqual(request.url.host, "192.0.2.10")
        self.assertEqual(request.headers["Host"], "local.test")


if __name__ == "__main__":
    unittest.main()
