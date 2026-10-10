"""v42 审计 P1-4 / P1-5 + v43 收尾：发现模块的候选过滤、逐跳守卫与「钉住已校验 IP」。

`_fetch_page` 原先用 `follow_redirects=True` 且不做任何地址校验 —— 索引站一个 302 到
`http://127.0.0.1/…` / 内网地址，响应体就会被当成页面文本采进 `discovered` 表
（审计已复现）。现在手动逐跳，**每一跳**都要求目标是公网地址（include_private=True）；
v43 又把连接**钉在已校验的 IP** 上（照 sync._open_source / probe_manual_source 的范式），
消除"校验时解析一次、建连时再解析一次"的 DNS 重绑定窗口。

这里同样不碰网络：httpx 的 AsyncClient 被替换成只记录请求的替身（send_pinned 走
`build_request` + `send(stream=True)`），DNS 解析被 monkeypatch 成固定结果，
于是可以断言"请求实际打在被校验的 IP 上、Host 头仍是原域名"。
"""

from __future__ import annotations

import asyncio
import socket
import unittest
from unittest import mock

import httpx

from app import discover

# example.com 的旧 A 记录：不能用 203.0.113.x 这类文档网段 ——
# 它们 is_private=True，会把"放行公网"的断言搅浑。
PUBLIC_IP = "93.184.216.34"
SECOND_IP = "93.184.216.35"
ATTACK_IP = "127.0.0.1"


def _public_resolver(host: str) -> tuple[str, ...]:
    """默认 DNS 替身：任何域名都解析到同一个公网 IP（用例不碰真解析）。"""
    return (PUBLIC_IP,)


def _attack_getaddrinfo(host, *args, **kwargs):
    """模拟 httpx 建连时自己那次解析：无论问什么都答 127.0.0.1。

    修复生效时它**根本不会被调用**（连接目标是 IP 字面量，无需解析）；
    一旦被调用就说明又回到了"让 httpx 再解析一次"的老路。
    """
    _attack_getaddrinfo.calls.append(host)        # type: ignore[attr-defined]
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ATTACK_IP, 0))]


_attack_getaddrinfo.calls = []                    # type: ignore[attr-defined]


class _FakeResponse:
    """`send_pinned` 拿到的流式响应（`client.send(..., stream=True)` 的返回值形状）。"""

    def __init__(self, status: int, headers: dict | None = None, body: bytes = b""):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self.closed = False

    async def aiter_bytes(self):
        yield self._body

    async def aclose(self):
        self.closed = True


class _ScriptedClient:
    """按顺序吐响应的假 client：记录**真正发出的请求**（含被钉住的地址），最后一帧一直重复。"""

    def __init__(self, frames: list[tuple[int, dict, bytes]]):
        self.frames = frames
        self.requests: list[httpx.Request] = []
        self.responses: list[_FakeResponse] = []

    def build_request(self, method: str, url: str, headers=None):
        return httpx.Request(method, url, headers=headers)

    async def send(self, request, stream=False):
        self.requests.append(request)
        status, headers, body = self.frames[min(len(self.requests) - 1, len(self.frames) - 1)]
        response = _FakeResponse(status, headers, body)
        self.responses.append(response)
        return response

    @property
    def urls(self) -> list[str]:
        """实际连接的目标：钉 IP 后是 IP 字面量，不再是域名。"""
        return [str(r.url) for r in self.requests]

    @property
    def hosts(self) -> list[str]:
        """HTTP 语义上的主机名（Host 头）：钉 IP 后仍必须是原域名。"""
        return [r.headers["Host"] for r in self.requests]


class PlaylistShapeTests(unittest.TestCase):
    def test_userinfo_candidate_is_rejected(self):
        """审计 P1-5 原样调用：`http://u:p@h/x.m3u` 不再是候选。"""
        self.assertFalse(discover._looks_like_playlist("http://u:p@h/x.m3u"))
        self.assertFalse(discover._looks_like_playlist("http://alice:s3cr3t@host.example/sub.m3u"))
        self.assertFalse(discover._looks_like_playlist("https://sometoken@host.example/live.m3u"))

    def test_normal_candidates_still_accepted(self):
        self.assertTrue(discover._looks_like_playlist("https://host.example/iptv/live.m3u"))
        self.assertTrue(discover._looks_like_playlist("http://host.example/sub"))
        self.assertTrue(discover._looks_like_playlist("https://host.example/tvbox.json"))

    def test_other_noise_still_rejected(self):
        self.assertFalse(discover._looks_like_playlist("http://host.example/HBO/index.m3u8"))
        self.assertFalse(discover._looks_like_playlist("http://host.example/get.php?username=a&password=b&type=m3u_plus"))
        self.assertFalse(discover._looks_like_playlist("http://host.example/iptv%E7%9C%8B.m3u"))


class CandidateRefTests(unittest.TestCase):
    """打码后的回显必须还能被前端操作：ref 是唯一能换回真实地址的东西。"""

    def test_ref_matches_original_url(self):
        url = "http://host.example/sub?token=abcdef"
        ref = discover.candidate_ref(url)
        self.assertNotIn("token", ref)
        self.assertEqual(discover.resolve_candidate_ref([url, "http://other/x.m3u"], ref), url)

    def test_unknown_ref_resolves_to_empty(self):
        self.assertEqual(discover.resolve_candidate_ref(["http://a/x.m3u"], "deadbeefcafe"), "")
        self.assertEqual(discover.resolve_candidate_ref(["http://a/x.m3u"], ""), "")


class FetchPageRedirectTests(unittest.TestCase):
    def test_redirect_to_loopback_is_rejected(self):
        """审计 P1-4 原样场景：302 → http://127.0.0.1/ 必须被拒，且不再发第二个请求。"""
        client = _ScriptedClient([(302, {"location": "http://127.0.0.1/secret"}, b"")])
        with mock.patch.object(discover, "resolve_addresses", _public_resolver):
            text, error = asyncio.run(discover._fetch_page(client, "https://index.example/"))
        self.assertEqual(text, "")
        self.assertIn("拒绝的目标主机", error)
        self.assertIn("127.0.0.1", error)
        self.assertEqual(len(client.urls), 1, "被拒的跳转不得再发出请求")

    def test_redirect_to_private_host_is_rejected_with_private_guard(self):
        """内网域名：restricted_in 必须带 include_private=True，且判的就是本次要钉的地址。"""
        calls: list[tuple[tuple[str, ...], bool]] = []
        real_restricted_in = discover.restricted_in

        def spy(addresses, *, include_private=False):
            calls.append((tuple(addresses), include_private))
            return real_restricted_in(addresses, include_private=include_private)

        resolved: list[str] = []

        def resolver(host):
            resolved.append(host)
            return ("10.0.0.5",) if host == "internal.example" else (PUBLIC_IP,)

        client = _ScriptedClient([(302, {"location": "http://internal.example/x"}, b"")])
        with mock.patch.object(discover, "resolve_addresses", resolver), \
             mock.patch.object(discover, "restricted_in", spy):
            text, error = asyncio.run(discover._fetch_page(client, "https://index.example/"))
        self.assertEqual(text, "")
        self.assertIn("受限地址", error)
        self.assertIn("10.0.0.5", error)
        # 判受限用的正是 resolve_addresses 给出的那份列表（同一次解析，才能钉住同一个 IP）
        self.assertIn((("10.0.0.5",), True), calls)
        self.assertEqual(resolved, ["index.example", "internal.example"], "每一跳都要重新解析")
        self.assertEqual(len(client.urls), 1, "被拒的跳转不得再发出请求")

    def test_normal_redirect_chain_still_works(self):
        """正常站点的"补斜杠"跳转不受影响：第二跳 200 后照样取回页面，且两跳都钉在已校验 IP 上。"""
        client = _ScriptedClient([
            (301, {"location": "/index.html"}, b""),
            (200, {}, "<html><a href=\"/live.m3u\">x</a></html>".encode()),
        ])
        with mock.patch.object(discover, "resolve_addresses", _public_resolver):
            text, error = asyncio.run(discover._fetch_page(client, "https://index.example"))
        self.assertEqual(error, "")
        self.assertIn("live.m3u", text)
        # 连接目标是 IP 字面量（不是域名），HTTP 语义靠 Host 头保持
        self.assertEqual([r.url.host for r in client.requests], [PUBLIC_IP, PUBLIC_IP])
        self.assertEqual(client.hosts, ["index.example", "index.example"])
        self.assertEqual(client.requests[1].url.path, "/index.html")
        # https 的 SNI 与证书校验仍按原域名，否则会因"证书不属于该 IP"直接失败
        self.assertEqual([r.extensions.get("sni_hostname") for r in client.requests],
                         ["index.example", "index.example"])
        self.assertTrue(all(r.closed for r in client.responses), "流式响应每条出口都必须关闭")

    def test_each_hop_is_revalidated_and_repinned(self):
        """逐跳校验/逐跳钉 IP 保留：第二跳换了域名就要用第二跳自己的地址。"""
        resolved: list[str] = []

        def resolver(host):
            resolved.append(host)
            return (PUBLIC_IP,) if host == "first.example" else (SECOND_IP,)

        client = _ScriptedClient([
            (302, {"location": "http://second.example/live.m3u"}, b""),
            (200, {}, b"<html>x</html>"),
        ])
        with mock.patch.object(discover, "resolve_addresses", resolver):
            text, error = asyncio.run(discover._fetch_page(client, "https://first.example/"))
        self.assertEqual(error, "")
        self.assertEqual(resolved, ["first.example", "second.example"])
        self.assertEqual([r.url.host for r in client.requests], [PUBLIC_IP, SECOND_IP])
        self.assertEqual(client.hosts, ["first.example", "second.example"])

    def test_too_many_redirects_is_capped(self):
        client = _ScriptedClient([(302, {"location": "/loop"}, b"")])
        with mock.patch.object(discover, "resolve_addresses", _public_resolver):
            text, error = asyncio.run(discover._fetch_page(client, "https://index.example/"))
        self.assertEqual(text, "")
        self.assertIn("跳转次数超过", error)
        self.assertEqual(len(client.urls), discover._MAX_REDIRECTS + 1)

    def test_redirect_without_location_is_reported(self):
        client = _ScriptedClient([(302, {}, b"")])
        with mock.patch.object(discover, "resolve_addresses", _public_resolver):
            text, error = asyncio.run(discover._fetch_page(client, "https://index.example/"))
        self.assertEqual(text, "")
        self.assertIn("缺少 Location", error)

    def test_oversized_page_is_still_capped(self):
        """4MiB 上限保持不变（P1-4 只改重定向，P0-2 只改连接策略，不动大小/超时口径）。"""
        body = b"x" * (discover._PAGE_MAX_BYTES + 1)
        client = _ScriptedClient([(200, {}, body)])
        with mock.patch.object(discover, "resolve_addresses", _public_resolver):
            text, error = asyncio.run(discover._fetch_page(client, "https://index.example/"))
        self.assertEqual(text, "")
        self.assertIn("超过", error)
        self.assertTrue(all(r.closed for r in client.responses))

    def test_oversized_page_aborts_reading_early(self):
        """超限必须**边读边判**：send_pinned 给的是流式响应，不能整个收进内存再判长度。"""
        consumed = {"chunks": 0}

        class _EndlessResponse(_FakeResponse):
            async def aiter_bytes(self):
                while True:                      # 源站给一个"无限大"的响应
                    consumed["chunks"] += 1
                    yield b"x" * (1024 * 1024)

        class _EndlessClient(_ScriptedClient):
            async def send(self, request, stream=False):
                self.requests.append(request)
                return _EndlessResponse(200, {})

        client = _EndlessClient([])
        with mock.patch.object(discover, "resolve_addresses", _public_resolver):
            text, error = asyncio.run(discover._fetch_page(client, "https://index.example/"))
        self.assertEqual(text, "")
        self.assertIn("超过", error)
        self.assertLessEqual(consumed["chunks"], discover._PAGE_MAX_BYTES // (1024 * 1024) + 1,
                             "超限后必须立刻停止读取，不得把响应体整个读完")


class FetchPagePinningTests(unittest.TestCase):
    """v43：发现模块的连接钉在**校验用的那一次解析**给出的 IP 上（P0-2 收尾）。"""

    def setUp(self):
        _attack_getaddrinfo.calls = []

    def test_request_goes_to_validated_ip_even_if_dns_flips(self):
        """审计 P0-2 原样场景：校验后 DNS 翻转到 127.0.0.1，请求仍打在已校验的公网 IP 上。"""
        calls: list[str] = []

        def flipping(host):
            calls.append(host)
            return (PUBLIC_IP,) if len(calls) == 1 else (ATTACK_IP,)

        client = _ScriptedClient([(200, {}, b"<html>x</html>")])
        with mock.patch.object(discover, "resolve_addresses", flipping), \
             mock.patch.object(socket, "getaddrinfo", _attack_getaddrinfo):
            text, error = asyncio.run(discover._fetch_page(client, "https://rebind.example/"))
        self.assertEqual(error, "")
        self.assertEqual(len(client.requests), 1, "只该发一次请求")
        # 关键断言：连接目标是**校验过的公网 IP**，不是被翻转后的 127.0.0.1
        self.assertEqual(client.requests[0].url.host, PUBLIC_IP)
        self.assertNotEqual(client.requests[0].url.host, ATTACK_IP)
        # HTTP 语义靠 Host 头保持（否则源站按域名分流会拿错内容）
        self.assertEqual(client.hosts, ["rebind.example"])
        # 守卫与钉 IP 共用同一次解析：解析只发生一次，httpx 不该再解析
        self.assertEqual(calls, ["rebind.example"])
        self.assertEqual(_attack_getaddrinfo.calls, [], "httpx 不得再调用 getaddrinfo 解析域名")

    def test_unresolvable_host_is_rejected_instead_of_falling_back(self):
        """解析不出来必须直接拒绝：退回"让 httpx 自己解析"正是重绑定的入口。"""
        client = _ScriptedClient([(200, {}, b"<html>x</html>")])
        with mock.patch.object(discover, "resolve_addresses", lambda host: ()):
            text, error = asyncio.run(discover._fetch_page(client, "https://nxdomain.example/"))
        self.assertEqual(text, "")
        self.assertIn("无法解析", error)
        self.assertEqual(client.requests, [], "没解析出地址就不得发出任何请求")


if __name__ == "__main__":
    unittest.main()
