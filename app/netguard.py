"""网络目标守卫：受限地址判定 + 重定向状态码。

**为什么单独成模块**：抓取路径（sync.py）与测速路径（speed.py）原先各有一份"受限地址"
判据，而且两处**有意存在一处差异** ——

  * 抓取路径必须放行内网：本地源就是内网地址，一刀切禁掉等于砍掉正当用法；
  * 测速路径把内网也算受限：频道地址来自第三方播放列表，放行内网就等于把 NAS 变成内网扫描器。

那份差异以前是两段人手复制的 `if` 条件（一处写 5 个 is_xxx、另一处写 6 个），
后续任何一次改动都可能在一边改了、另一边忘了。现在它是一个**显式参数**，
差异一眼可见，也不可能再漂移。
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

# 这些写法 ipaddress 解析不了、会被当普通域名，但 getaddrinfo 会统统解析到环回，
# 所以除了「解析后判地址」之外还要先按字面量挡一道。
LOOPBACK_HOSTS = frozenset({"localhost", "localhost.", "127.0.0.1", "::1", "0.0.0.0", "[::1]"})

# 多 IP 主机逐个尝试的上限（DNS 轮询里有死地址时，不至于把可用线路判成失效）。
# v42 从 speed.py 搬到这里：钉 IP 现在是抓取路径（sync）与测速路径（speed）共用的机制，
# 上限也该只有一份（原先只在测速路径上，抓取路径钉住首个地址就没有这个兜底）。
MAX_PIN_ATTEMPTS = 3

# 手动跟随重定向时，"还要再跟一跳"的状态码（只认这几种，其余按正常响应处理）
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class SourceUnavailableError(ValueError):
    """**目标侧**取不到内容（DNS 解析不出、内容超限 / 为空、重定向打转、非 200）。

    为什么是 `ValueError` 的子类而不是独立分支：抓取流水线里多处用 `except ValueError`
    兜住「这条源抓不动」，把它们统统换成新类型会改动一大片既有分支（discover 的候选检测、
    sync 的逐源错误记账）。做成子类，既有 `except ValueError` 语义完全不变，
    只有**需要区分口径**的地方（`POST /api/sources` 的 HTTP 状态码）才显式先 catch 它。

    为什么需要区分（v47）：原先"域名解析不出来 / 内容超过上限 / 源返回了空内容"都从
    `probe_manual_source` 抛 `ValueError`，被 `add_source` 归进「地址不合法」→ HTTP 400。
    但这些都是**上游侧**失败，请求本身没写错：400 会误导调用方以为该改请求参数，
    正确的语义是 502（网关从上游取不到有效响应）。真正的输入校验错误
    （协议不是 http(s)、没有主机名、解析到环回/受限地址）仍旧抛普通 `ValueError` → 400。
    """


def is_restricted_ip(
    ip: "ipaddress.IPv4Address | ipaddress.IPv6Address",
    *,
    include_private: bool = False,
) -> bool:
    """环回 / 链路本地 / 组播 / 未指定 / 保留；`include_private=True` 时连内网一起算。

    IPv4-mapped（`::ffff:127.0.0.1`）按 IPv4 判定 —— 实测其 `is_loopback` 本来就是 True，
    这里显式归一只是为了让语义不依赖 CPython 的实现细节。
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if include_private and ip.is_private:
        return True
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    )


async def next_redirect(response, current: str) -> str | None:
    """重定向记账：是重定向就返回**下一跳的绝对地址**，否则返回 None（调用方继续用这个响应）。

    这段「取 Location → 关掉响应 → 校验 Location 存在 → 拼成绝对地址」在抓取路径
    （`sync._open_source`）与测速路径（`speed._open`）里曾经逐字重复；两处都必须**手动**
    跟随重定向（用 httpx 的 `follow_redirects` 会让每一跳绕过各自的地址守卫 —— 一个公网
    地址 302 到内网就全绕过了），所以把这段记账抽出来共用，避免哪天只改了一边。

    **刻意不把整个循环合并**：两侧的守卫与连接策略本质不同 ——
    抓取路径逐跳查主机白名单、测速路径逐跳校验并把连接钉在已校验的 IP 上（还带多 IP 重试）。
    那样合并只能靠回调式"通用化"，反而更难读、更容易改错。这里只去重复语句，保留两个显式循环。
    """
    if response.status_code not in REDIRECT_STATUSES:
        return None
    location = response.headers.get("location", "")
    status_code = response.status_code
    await response.aclose()
    if not location:
        raise RuntimeError(f"HTTP {status_code} 响应缺少 Location 头")
    return urljoin(current, location)


async def read_capped_async(response, limit: int, message: str) -> bytes:
    """把响应体读成 bytes，超过 `limit` 就抛 `SourceUnavailableError(message)`。

    抓取路径上有三处逐字重复的版本（手动加源的探测、TVBox 配置、普通源内容），
    只有错误文案不同 —— 文案由调用方传入，保持用户可见信息逐字不变（它们分别会被
    `add_source` 转成 HTTP 502、被记进源状态）。

    超限抛的是 `SourceUnavailableError`（`ValueError` 子类，见其 docstring）：
    这是上游给了超预期响应，不是调用方参数写错。

    必须**边读边判**：先 `await response.aread()` 再判长度等于先把整个响应体收进内存，
    16MiB 的上限就形同虚设（源站可以给一个 10GB 的响应把它撑爆）。
    """
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limit:
            raise SourceUnavailableError(message)
        chunks.append(chunk)
    return b"".join(chunks)


def resolve_addresses(host: str) -> tuple[str, ...]:
    """解析主机名的全部地址（同步阻塞，调用方需丢进线程池）。解析失败返回空元组。

    保留 getaddrinfo 返回的顺序并去重：多 A/AAAA 记录的主机要按 OS 给的优先级逐个尝试
    （否则可能只试到打不通的那一个地址，把可用线路误判成失效）。

    **守卫与"钉住的连接目标"必须用同一次解析的结果**：分开解析两次，两次之间 DNS 就能
    翻转（P0-2 DNS 重绑定），所以这个函数把地址列表**返回给调用方**，由它同时用于
    「判受限」与「钉 IP」——见 `restricted_in` 与 `send_pinned`。
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return ()
    ordered: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in ordered:
            ordered.append(addr)
    return tuple(ordered)


def restricted_in(
    addresses: Iterable[str],
    *,
    include_private: bool = False,
) -> str:
    """在一批**已解析**的地址里返回第一个受限地址（都干净则空串）。

    与 `restricted_address` 的分工：这个函数不解析，专门配合 `resolve_addresses` 使用 ——
    校验的必须是"待会儿要钉住的那个地址列表"，不能另解析一次。
    """
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if is_restricted_ip(ip, include_private=include_private):
            return str(ip)
    return ""


def restricted_address(host: str, *, include_private: bool = False) -> str:
    """解析主机名并返回第一个受限地址（解析失败或无受限地址则空串）。**同步阻塞**，供线程里的代码用。

    与 `sync.restricted_resolution` 的区别：这个带 `include_private` 开关且不做缓存，
    给「目标是运维配置的公开地址、跳到内网只可能是被利用」的场景（EPG 回源）用。
    """
    return restricted_in(resolve_addresses(host), include_private=include_private)


def pin_target(parts, host: str, ip: str | None) -> tuple[str, str]:
    """把 URL 的 host 换成**已校验过的 IP**，返回 (pinned_url, Host 头)。

    ip 为 None（信任主机 / 本来就没有解析结果）时原样返回。

    **为什么要钉 IP**（v42，审计 P0-2 DNS 重绑定）：调用方只"解析一次做校验"，
    而 httpx 建连时会**自己再解析一次** —— 两次之间的 DNS 变化就能让校验时是公网 IP
    的域名在实际连接时指向 127.0.0.1 / 内网（审计已在容器内复现：解析第 3 次返回
    127.0.0.1 时成功取回内网内容）。这里直接连校验过的那个地址，解析次数收敛成一次。
    HTTP 语义用 Host 头保持，TLS 证书校验用 sni_hostname 保持（见 send_pinned）。

    原先这段逻辑长在 `speed._pin_target`（测速路径专用）；搬到本模块是为了让抓取路径
    （`sync._open_source`、`sync.probe_manual_source`）复用同一份实现，避免两边各写一遍
    后走样 —— 本模块只依赖 urlsplit/urlunsplit，不引入额外耦合。
    """
    port = parts.port
    default_port = 443 if parts.scheme == "https" else 80
    literal_host = f"[{host}]" if ":" in host else host     # IPv6 字面量必须带方括号
    host_header = literal_host if port in (None, default_port) else f"{literal_host}:{port}"
    if not ip or ip == host:
        return urlunsplit(parts), host_header
    literal = f"[{ip}]" if ":" in ip else ip
    netloc = f"{literal}:{port}" if port else literal
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, "")), host_header


async def send_pinned(
    client: httpx.AsyncClient,
    url: str,
    host: str,
    addresses: Iterable[str] = (),
    headers: dict[str, str] | None = None,
    *,
    attempts: int = MAX_PIN_ATTEMPTS,
) -> httpx.Response:
    """GET `url`，但连接**钉在 addresses 里已校验过的 IP** 上（多 IP 逐个重试到上限）。

    三条路径共用（测速 `speed._open`、抓取 `sync._open_source`、手动加源探测
    `sync.probe_manual_source`）：它们此前的差别只是"要不要先过白名单 / 怎么判受限"，
    而"连已校验 IP + 保留 Host 头 + https 设 sni_hostname + 多 IP 重试"这段必须逐字一致
    —— 这是 P0-2 的修复本体，任何一处漏掉都等于留一个 DNS 重绑定口子。

    * `addresses` 为空（信任主机、或调用方不做解析）时按原样请求，交给 httpx 自己解析；
    * 只要钉了 IP（pinned != url）就显式补 Host 头；https 还要把 SNI 与证书校验的主机名
      指回原域名，否则会因「证书不属于该 IP」直接失败（实测 https 200 且证书校验通过）；
    * 连接类异常换下一个已校验 IP 再试，全部失败才把最后一个异常抛出去。
    """
    parts = urlsplit(url)
    response: httpx.Response | None = None
    last_error: Exception | None = None
    for ip in (tuple(addresses)[:attempts] or (None,)):
        pinned, host_header = pin_target(parts, host, ip)
        request = client.build_request("GET", pinned, headers=headers)
        if pinned != url:
            request.headers["Host"] = host_header
            if parts.scheme == "https":
                # TLS SNI 与证书校验都按原始域名走，否则会因「证书不属于该 IP」失败
                request.extensions = {**request.extensions, "sni_hostname": host}
        try:
            response = await client.send(request, stream=True)
            break
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
            last_error = exc          # 换下一个已校验的 IP 再试
            continue
    if response is None:
        if last_error is not None:
            raise last_error
        raise RuntimeError("no address to connect")
    return response
