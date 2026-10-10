"""线路测速：对小样本字节做限速抽样，评估可用性与吞吐。

设计约束（避免对第三方源站造成压力）：
- 只读取有限字节（默认 1MB）并在超时后立刻断开，绝不下完整流；
- HLS 会跨最多 HLS_SEGMENTS 个连续分片累计读取，避免被单个分片的边缘缓存放大；
- 并发受限（默认 8），单次任务条目有上限；
- 仅支持 http/https，rtmp/rtsp/udp 等标记为 unsupported 且不发起连接；
- 结果落库，供排序与导出使用，任务可随时取消。
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import threading
import time
from contextlib import aclosing
from urllib.parse import urljoin, urlsplit, urlparse

import httpx

from .config import settings
from .netguard import is_restricted_ip, next_redirect, send_pinned
from .db import db, utc_now
from .revision import bump_revision

logger = logging.getLogger("iptv-hub.speed")

# 测速跟随跳转时最多几跳（每一跳都要重过地址守卫）
MAX_SPEED_REDIRECTS = 3
# 多 IP 主机逐个尝试的上限（DNS 轮询里有死地址时，不至于把可用线路判成失效）
# v42：常量本身搬到了 netguard.MAX_PIN_ATTEMPTS —— 钉 IP 现在是测速与抓取两条路径
# 共用的机制（P0-2），上限只能有一份，否则两边会各留一个数。

SUPPORTED_SCHEMES = {"http", "https"}
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
PLAYLIST_SAMPLE = 262144
# HLS 连续取多少个分片做累计测量（跨分片才能反映持续吞吐）
HLS_SEGMENTS = 3
# 测量方法版本。改动测速算法时**必须**递增：所有 method 不等于当前值的旧记录会被
# _pending_sql 视为「待重测」，于是算法升级后数据自动重算，不需要人工清库或写迁移脚本。
METHOD_VERSION = "3-steady-state"

# `_save_batch` 允许写入 stream_health.status 的取值。表定义只有 `NOT NULL`、没有 CHECK，
# 任何拼错/为空的 status 都会静默落库，而下游全部按 `status IN (...)` 精确匹配 ——
# 落在集合外的值既不算 ok 也不算 error：不进 `dead/flagged/pruned` 统计、不被剔除、
# 也不会被 `restore_purged_streams` 认领，属于"隐形的第三条状态"。线上库里实测有 51 行
# `status=''`（旧版本代码留下的：method 也为空，会被 _pending_sql 当作待重测自愈）。
# 这里在写入口兜底，保证不再产生新的集合外取值。
_SAVEABLE_STATUSES = frozenset({"ok", "error", "unsupported", "blocked"})

# `fail_count`（连续失败次数）的上限。它只被三处消费，且全部是阈值比较：
#   * 剔除判据   `fail_count > PRUNE_MAX_FAIL`（默认 3）
#   * 标记统计   `fail_count BETWEEN 1 AND PRUNE_MAX_FAIL`
#   * 复活判据   `fail_count > PRUNE_MAX_FAIL`（见 revive_expired）
# 所以上限只要**明显高于 PRUNE_MAX_FAIL** 就不改变任何既有语义，纯粹防止
# 「剔除流水线被关掉（AUTO_PRUNE_AFTER_SPEED=false）时计数无限增长」——
# 那种情况下计数器会一直 +1，永远不回落，日志/界面上的数字失去参考价值。
FAIL_COUNT_MAX = 1000

# `unsupported` 的复检窗口（天）。与 blocked 的 RETEST_AFTER_DAYS 分开取值，因为这一档
# 混着两类寿命完全不同的记录：**永远测不了**的「非 HTTP 协议」线路（rtmp/rtsp/udp）与
# **可能回来**的「域名解析不出来」。取 30 天是折中：非 HTTP 协议那部分白测被压到最低，
# 死域名也仍有半年内多次复核的机会。blocked 用 RETEST_AFTER_DAYS（默认 14 天）。
UNSUPPORTED_RETEST_DAYS = 30

# 测速结果分批落库的批大小。一轮几千条要跑十几分钟，原先只在全部跑完时写一次，
# 造成两个实际问题：进度/状态在整轮里都是旧的；中途重启（或崩溃）会把整轮测量丢光。
# 分批后最长丢失窗口只有这么多条，同时仍保留批量事务的开销优势。
FLUSH_EVERY = 200


class TargetNotMeasurable(Exception):
    """基类：**这一轮根本没测成**，而不是「线路坏」。

    单独定义这个体系，是为了让调用方把它和「线路真的不通」区分开：
    这类结果不做失败计数（fail_count 不递增、last_fail_at 不写），只让该条目退出待测队列，
    按长复检周期再试，避免误伤本地源、也避免污染剔除计数。

    v48 拆分（此前只有一个 `BlockedTarget`，四类互不相干的原因共用它）：
      * `BlockedTarget`    —— 命中内网/保留地址段且不在信任主机列表 → 真的安全拦截；
      * `UnmeasurableTarget` —— 协议不支持 / 缺主机名 / **域名解析不出来** / 地址无法识别。
    为什么必须拆：生产库里 `status='blocked'` 的 549 行中有 482 行（87.8%）其实是
    「域名解析不出来」，文案却统一写成「SSRF 防护已跳过」—— 既把"测不了"误报成安全事件，
    又让 blocked 这个状态失去了「被守卫拦下」的准确含义。
    """

    #: 落库时写入 stream_health.status 的取值，子类覆盖
    status = "unsupported"
    #: 落库文案前缀；空串表示直接用异常自身的说明
    reason_prefix = ""


class BlockedTarget(TargetNotMeasurable):
    """目标落在内网/环回/链路本地/保留地址段，且不在信任主机列表里 —— 真正的安全拦截。"""

    status = "blocked"
    reason_prefix = "内网/保留地址被策略拦截"


class UnmeasurableTarget(TargetNotMeasurable):
    """**连不上/测不了**：协议不支持、缺主机名、域名解析不出来、地址无法识别。

    这些都不是安全拦截，也不构成「线路失效」的证据 —— 域名今天解析不出来，明天可能就好了。
    """

    status = "unsupported"
    reason_prefix = ""


# 带 TTL 的解析缓存。**不能用 lru_cache**：pinning 之后连接永远打到缓存里的 IP，
# 而 CDN 会轮换地址 —— 永不过期的缓存会让线路「持续假失效，直到进程重启」。
_RESOLVE_TTL = 300.0
_RESOLVE_NEGATIVE_TTL = 30.0          # 解析失败缓存短一点，DNS 抖动不至于封禁 5 分钟
_RESOLVE_CACHE: dict[str, tuple[float, tuple[str, ...]]] = {}
_RESOLVE_LOCK = threading.Lock()


def _resolve(host: str) -> tuple[str, ...]:
    """解析主机名到 IP 列表（同步阻塞，调用方需丢进线程）。解析失败返回空元组。

    **保留 getaddrinfo 返回的顺序**（原来用 set 收集，顺序随 PYTHONHASHSEED 变，
    多 A/AAAA 主机可能只试到打不通的那个地址族，把可用线路误判成失效）。
    """
    now = time.monotonic()
    with _RESOLVE_LOCK:
        hit = _RESOLVE_CACHE.get(host)
        if hit is not None:
            ttl = _RESOLVE_TTL if hit[1] else _RESOLVE_NEGATIVE_TTL
            if now - hit[0] < ttl:
                return hit[1]
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        addresses: tuple[str, ...] = ()
    else:
        ordered: list[str] = []
        for info in infos:
            addr = info[4][0]
            if addr not in ordered:        # 去重但保留 OS 给的优先级
                ordered.append(addr)
        addresses = tuple(ordered)
    with _RESOLVE_LOCK:
        if len(_RESOLVE_CACHE) > 4096:
            _RESOLVE_CACHE.clear()
        _RESOLVE_CACHE[host] = (now, addresses)
    return addresses


def _trusted_hosts() -> set[str]:
    """测速允许访问的内网主机：数据源白名单 + SPEED_ALLOW_PRIVATE_HOSTS 额外项。

    数据源白名单里的主机正是用户明确信任的内容来源（例如本地 IPTV 网关），
    它们本来就跑在内网，必须放行，否则用户自己的本地源会全部测不了。
    """
    from .sync import ALLOWED_SOURCE_HOSTS  # 延迟导入，避免模块级循环依赖

    extra = {h.strip().lower() for h in settings.speed_allow_private_hosts.split(",") if h.strip()}
    return {h.lower() for h in ALLOWED_SOURCE_HOSTS} | extra


def requeue_newly_trusted() -> int:
    """把「URL 主机已在信任名单里、但状态还是 blocked」的行强制重新排队，返回处理行数。

    v48：blocked / unsupported 的复检窗口被拉长到 RETEST_AFTER_DAYS（默认 14 天），
    这样内网/死域名的线路不会天天白测。代价是：用户把某台内网主机补进
    `SPEED_ALLOW_PRIVATE_HOSTS`（或它进了数据源白名单）之后，那些行最多要等 14 天
    才会被重新测到。改 .env 必然走 `docker compose up -d --force-recreate`，
    所以把「名单生效」和「重新排队」绑在启动上就够 —— 本函数在 lifespan 里调用一次。

    幂等：只把 `checked_at` 清空（`'' < 任何时间戳`，必然落进长复检档），
    不动 status/fail_count；测完自然会写回真实状态。没有 blocked 行时一次 SELECT 就返回，
    不产生任何写入。
    """
    rows = db.rows("SELECT fingerprint, url FROM stream_health WHERE status='blocked'")
    if not rows:
        return 0
    trusted = _trusted_hosts()
    targets = [(row["fingerprint"],) for row in rows
               if (urlsplit(row["url"]).hostname or "").lower() in trusted]
    if not targets:
        return 0
    with db.write() as conn:
        conn.executemany(
            "UPDATE stream_health SET checked_at='' WHERE fingerprint=?", targets
        )
    logger.info("放行名单命中的 blocked 线路已重新排队 %s 条", len(targets))
    return len(targets)


async def _assert_target_ok(url: str) -> None:
    """发起请求前的目标地址守卫（SSRF 防护）。

    频道地址来自第三方播放列表。若不校验，列表里塞一个 http://127.0.0.1/… 或
    http://<内网IP>:<管理端口>/… 就能让 NAS 去请求自己的内网服务（盲 SSRF）。
    这里解析主机并拦截所有非公网地址；信任主机直接放行。
    """
    parts = urlsplit(url)
    if parts.scheme not in SUPPORTED_SCHEMES:
        # 协议不支持 → 测不了，不是安全拦截（v48 归类修正）
        raise UnmeasurableTarget(f"非 HTTP 协议：{parts.scheme or 'missing'}")
    host = (parts.hostname or "").lower()
    if not host:
        raise UnmeasurableTarget("地址缺少主机名")
    if host in _trusted_hosts():
        return
    addresses = await asyncio.to_thread(_resolve, host)
    if not addresses:
        # v48：域名解析不出来归「测不了」而不是「被拦截」—— 生产库里这类占 blocked 的 87.8%，
        # 旧文案「SSRF 防护已跳过」把它们误报成了安全事件。
        raise UnmeasurableTarget(f"域名无法解析：{host}")
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            raise UnmeasurableTarget(f"无法识别的地址 {host} -> {raw}") from None
        # 测速路径**连内网一起拦**：频道地址来自第三方列表，放行内网等于把 NAS 变成内网扫描器。
        # 抓取路径用同一个函数的默认值（不拦内网，因为本地源就是内网地址）——
        # 这处差异以前是两份人手复制的 if，现在是显式参数，见 netguard.py。
        if is_restricted_ip(ip, include_private=True):
            raise BlockedTarget(f"{host} -> {raw}")


async def _open(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> httpx.Response:
    """打开目标并**手动**跟随跳转，每一跳都先过地址守卫，并把连接**钉在已校验的 IP 上**。

    不能用 httpx 的 follow_redirects：那样一个公网地址 302 到内网即可绕过全部校验
    （同步路径当初就是这个坑，见 _open_source 的注释）。

    也不能只「校验一次域名」：`_assert_target_ok` 解析一次，而 httpx 建连时会**自己
    再解析一次** —— 两次之间的 DNS 变化（DNS 重绑定）就能让校验时是公网 IP 的域名在
    实际连接时指向 127.0.0.1 / 内网，守卫形同虚设。所以这里解析一次、校验一次，
    然后**直接连那个 IP**；HTTP 语义用 Host 头保持，TLS 证书校验用 sni_hostname 保持
    （实测 https 200 且证书校验通过）。

    多 IP 主机（DNS 轮询里有一台是死的）会按顺序重试最多 MAX_PIN_ATTEMPTS 个地址，
    避免把本来可用的线路误判成失效。
    """
    current = url
    for _ in range(MAX_SPEED_REDIRECTS + 1):
        await _assert_target_ok(current)
        parts = urlsplit(current)
        host = (parts.hostname or "").lower()
        addresses: tuple[str, ...] = ()
        if host not in _trusted_hosts():
            # _resolve 缓存未命中时是阻塞式 getaddrinfo（几十毫秒到数秒），
            # 必须放线程里跑 —— 这里与 _assert_target_ok 同款处理；直接同步调会把
            # 事件循环（含 /healthz、/api/*）一起卡住，sync.py 当初就出过同类事故。
            addresses = await asyncio.to_thread(_resolve, host)
        # 「连已校验 IP + Host 头 + https 的 sni_hostname + 多 IP 重试」这段与抓取路径
        # 共用 netguard.send_pinned（v42）：它正是 P0-2 的修复本体，两边各写一遍迟早走样。
        response = await send_pinned(client, current, host, addresses, headers)
        # 重定向记账与抓取路径共用（见 netguard.next_redirect）；这里的差异是连接前
        # 那一跳守卫 + 钉 IP 重试，那部分刻意不合并。
        redirected_to = await next_redirect(response, current)
        if redirected_to is None:
            return response
        current = redirected_to
    raise RuntimeError(f"跳转次数超过 {MAX_SPEED_REDIRECTS}")


# 用「跳过预热段」的稳态速率所需的最少有效样本。流若刚过预热就结束，
# 剩余窗口只有一两 KB，算出来的速率纯属噪声（实测把本地源算成 0.1~0.8 KB/s），
# 此时退回整段平均更合理。
MIN_STEADY_BYTES = 32768
MIN_STEADY_SECONDS = 0.1

# 窗口到期时若还没读满预热段、且平均速率低于此值，认为这个窗口不具代表性
# （多半是慢启动或中途停顿），延长到 speed_stall_timeout 再测一次。
# 正常线路 5 秒内早该读满 512KB 预热段（约 100 KB/s 出头就能越过），所以
# 这个门槛不会拖慢真正快的线路；只有「确实慢」的线路会多花几秒。
SLOW_WINDOW_KBPS = 256.0


def _headers(ua: str, referer: str) -> dict[str, str]:
    headers = {"User-Agent": ua or DEFAULT_UA, "Accept": "*/*", "Connection": "close"}
    if referer:
        headers["Referer"] = referer
    return headers


def _is_playlist(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(".m3u8") or path.endswith(".m3u")


def _client() -> httpx.AsyncClient:
    timeout = httpx.Timeout(
        connect=min(5.0, settings.speed_timeout),
        # 读超时必须**大于**两段式窗口的延长时间，否则停顿还没到延长点就先抛 ReadTimeout，
        # 延长窗口等于白加（实测 speed_timeout=5 时 5s 就读超时，12s 的窗口永远走不到）。
        read=max(min(10.0, settings.speed_timeout), settings.speed_stall_timeout + 2.0),
        write=5.0,
        pool=5.0,
    )
    return httpx.AsyncClient(
        timeout=timeout,
        # 必须关掉自动跳转：改由 _open() 手动逐跳跟随并做地址守卫，
        # 否则公网地址一次 302 到内网就能绕过 SSRF 防护
        follow_redirects=False,
        verify=False,
        limits=httpx.Limits(max_connections=settings.speed_concurrency + 4, max_keepalive_connections=4),
    )


async def _read_sample(client: httpx.AsyncClient, urls: list[str], headers: dict[str, str], sample_bytes: int):
    """返回 (http_status, latency_ms, speed_kbps, bytes_read, error)。

    urls 是「按顺序连续读取」的候选地址：HLS 会传入连续的多个分片。

    速率取**跳过预热段之后的持续吞吐**，而不是整段平均：
    国内 HLS 源普遍在 CDN 边缘预缓冲几百 KB，开头瞬间吐出很高的速率再断崖回落
    （实测同一线路开头 512KB 是 2707KB/s，之后只有 450KB/s）。只读一小段或算整段平均，
    都会把「开局快、随后卡顿」的线路排到真正流畅的线路前面。
    """
    start = time.perf_counter()
    latency: int | None = None
    total = 0
    status = 0
    first_head = b""
    http_error = ""
    read_error = ""
    warmup_at: float | None = None  # 累计读满 warmup 字节时的相对时刻
    warmup_bytes = settings.speed_warmup_bytes
    # 两段式截止时间。正常线路 1MB/5s 足够；但如果到期时只读到很小一段，说明它是
    # 「慢启动 / 中途停顿」而不是快到读不完 —— 实测这类线路再给到 15 秒往往能读到
    # 几百 KB（确实可播），而 5 秒窗口只看得到 16KB、算出来只有 2~3 KB/s。
    # 不多给一次机会的话，判 ok 会得到一个假得离谱的低速，把好线路排到同频道最后。
    deadline = settings.speed_timeout
    extended = False

    def out_of_time() -> bool:
        return (time.perf_counter() - start) >= deadline

    def try_extend() -> bool:
        """到期时再给一次延长机会（慢启动/中途停顿的线路），返回是否真的延长了。"""
        nonlocal extended, deadline
        if extended:
            return False
        # 延长条件不能只看 min_sample：停顿后到达的第一个块往往刚好把
        # total 顶过 32768，于是「不再延长」并在 6 秒的窗口上算出一个
        # 假低速（实测 16KB+停 6 秒+随后 1.5MB/s 的线路被记成 8 KB/s，
        # 排到同频道最后）。真正说明「这个窗口不具代表性」的是：
        # 还没越过预热点，而且这段的平均速率低得不像「快到读不完」。
        if total < settings.speed_min_sample_bytes or (
            total < warmup_bytes
            and total / 1024 / max(time.perf_counter() - start, 0.001)
            < SLOW_WINDOW_KBPS
        ):
            extended = True
            deadline = max(deadline, settings.speed_stall_timeout)
            return True
        return False

    for url in urls:
        # 分片边界也要先尝试延长：第 1 个分片恰在 deadline 附近下完且样本不足时，
        # 原实现直接 break，慢启动线路拿不到延长窗口（延长只在 chunk 循环内评估）
        if total >= sample_bytes or (out_of_time() and not try_extend()):
            break
        try:
            response = await _open(client, url, headers)
            # httpx.Response 不支持异步上下文管理器，用 aclosing 保证流被关闭
            async with aclosing(response):
                status = response.status_code
                if status >= 400:
                    http_error = f"HTTP {status}"
                    break
                # 不指定分块大小：httpx 会按网络实际到达的片段 yield。
                # 指定 16384 会攒满 16KB 才吐出来 —— 于是 (a) latency 记的是「收满 16KB 的
                # 耗时」而不是首字节时间（实测真实 TTFB 50ms 被记成 4630ms，而 latency 是
                # 导出的排序键），(b) total 只在 16KB 边界更新，样本门槛判定变得很粗。
                async for chunk in response.aiter_bytes():
                    if not first_head:
                        first_head = chunk[:64]
                    if latency is None:
                        latency = int((time.perf_counter() - start) * 1000)
                    total += len(chunk)
                    if warmup_at is None and total >= warmup_bytes:
                        warmup_at = time.perf_counter() - start
                    if out_of_time():
                        if try_extend():
                            continue          # 再给一次机会，而不是就地判一个假低速
                        break
                    if total >= sample_bytes:
                        break
        except TargetNotMeasurable:
            # 已读到数据就把这一段算完（后面的分片被拦/测不了不影响已测结果）；
            # 一点都没读到则向上抛出，由 probe() 判定为「跳过」而不是「失效」
            if total > 0:
                break
            raise
        except Exception as exc:  # noqa: BLE001 - 测速只关心可用性
            read_error = type(exc).__name__[:60]
            break
    if total <= 0:
        return status, latency, None, 0, http_error or read_error or "未读到数据"
    # 读到了字节，但 HTTP 状态是错误：不能算 ok —— 否则「第一段正常、第二段 404」
    # 会拿着半截样本算出一个虚高速度（实测能到 27MB/s）并长期占着 ok。
    if http_error:
        return status, latency, None, total, http_error
    # 内容校验：JSON/HTML 错误响应不是可播的流（返回 JSON 的假 200 是「打不开」的常见根因）
    stripped = first_head.lstrip()
    if stripped.startswith((b"{", b"<!DOCTYPE", b"<!doctype", b"<html", b"<XML", b"<?xml")):
        return status, latency, None, total, "非流媒体响应(JSON/HTML)"
    # 样本量门槛：**延长窗口之后**仍然只有一个分块 / 几百字节，才说明这条流真的不通畅。
    # 必须放在延长之后判断，否则会误杀「慢启动但可播」的线路（实测误杀面约 7000 条）。
    if total < settings.speed_min_sample_bytes:
        return status, latency, None, total, f"样本不足({total}B/{deadline:.0f}s)"
    elapsed = max(time.perf_counter() - start, 0.001)
    if latency is None:
        latency = int(elapsed * 1000)
    measured_bytes, measured_elapsed = total, elapsed
    if (
        warmup_at is not None
        and (total - warmup_bytes) >= MIN_STEADY_BYTES
        and (elapsed - warmup_at) >= MIN_STEADY_SECONDS
    ):
        measured_bytes = total - warmup_bytes
        measured_elapsed = max(elapsed - warmup_at, 0.001)
    speed = round((measured_bytes / 1024) / measured_elapsed, 1)
    return status, latency, speed, total, ""
async def _playlist_segments(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], depth: int = 0
) -> list[str]:
    """取 HLS 播放列表里按播放顺序的连续分片地址（最多 HLS_SEGMENTS 个）。

    跨分片测量才能反映持续吞吐：单看第一个分片会受该分片自身的边缘缓存影响。

    **master playlist 必须下钻一层**：带 `#EXT-X-STREAM-INF` 的列表里，那些非注释行是
    「变体播放列表」的地址（几百字节文本），不是媒体分片。原实现把它们当分片读，
    样本只有约 200B，会被样本门槛判成「样本不足 → 失效」—— 于是**本来可播的线路从订阅里
    消失**（库里 `.../index.m3u8` 这种形态 1900+ 条，播放器打开同一地址完全正常）。
    """
    buf = b""
    try:
        response = await _open(client, url, headers)
        async with aclosing(response):
            if response.status_code >= 400:
                return []
            async for chunk in response.aiter_bytes(16384):
                buf += chunk
                if len(buf) >= PLAYLIST_SAMPLE:
                    break
    except TargetNotMeasurable:
        raise
    except Exception:  # noqa: BLE001
        return []
    head = buf[:256].decode("utf-8", "ignore").lstrip()
    # 必须是真正的 m3u8：JSON/HTML 错误响应直接判失败（避免假 200 混入可用线路）
    if not (head.startswith("#EXTM3U") or head.startswith("#EXT-X")):
        return []
    text = buf.decode("utf-8", "ignore")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        return []
    if "#EXT-X-STREAM-INF" in text:
        # master playlist：第一个非注释行是变体播放列表；下钻最多两层，
        # 一层不够（线上存在 master -> master -> 媒体列表的两层嵌套），
        # 无限制又会被异常嵌套拖死
        if depth >= 2:
            return []
        return await _playlist_segments(client, urljoin(url, lines[0]), headers, depth + 1)
    segments: list[str] = []
    for line in lines:
        segments.append(urljoin(url, line))
        if len(segments) >= HLS_SEGMENTS:
            break
    return segments


async def probe(client: httpx.AsyncClient, url: str, ua: str = "", referer: str = "") -> dict:
    """对单条线路抽样测速，返回落库所需的字典。

    三种「没测成」的结果（v48 起区分开，都会带 `not_measured: True`）：

      * `status="blocked"`      —— 目标命中内网/保留段且不在信任列表：**真的安全拦截**；
      * `status="unsupported"`  —— 协议不支持 / 缺主机名 / 域名解析不出来 / 地址无法识别，
        即「我们连不上，不是线路坏」。
      * 两者都只让该条目退出当轮队列，`not_measured` 保证 fail_count 不被推进。

    与之相对，任务取消返回 status="skipped"，那种结果**不落库**——取消是任务的属性，
    不是线路的属性，落库会污染数据。
    """
    headers = _headers(ua, referer)
    scheme = urlparse(url).scheme.lower()
    if scheme not in SUPPORTED_SCHEMES:
        # v48：同样标注 not_measured —— 非 HTTP 协议永远测不了，没有测量证据，
        # 因此不该按「失败」记账（但 status 仍是 unsupported，口径不变）。
        return {"status": "unsupported", "latency_ms": None, "speed_kbps": None,
                "http_status": None, "bytes_read": 0,
                "error": f"非 HTTP 协议：{scheme or 'missing'}", "not_measured": True}

    def not_measured(exc: TargetNotMeasurable) -> dict:
        """落库用的「没测成」结果：status 由异常类型决定，并且**必须**带 not_measured。

        `not_measured` 是 v48 新增的落库标记：_save_batch 据此把它按「成功一侧」处理
        （fail_count 清零、last_fail_at 清空），与 blocked 完全同待遇 —— 我们没有任何
        测量证据，就不能把它算成一次失败，否则这些线路会被推进 20h 的失效复测档，
        白白吃掉测速预算（线上曾有 482 条域名已死的线路持续复测）。
        """
        text = f"{exc.reason_prefix}：{exc}" if exc.reason_prefix else str(exc)
        return {"status": exc.status, "latency_ms": None, "speed_kbps": None,
                "http_status": None, "bytes_read": 0, "error": text[:120],
                "not_measured": True}

    targets = [url]
    try:
        if _is_playlist(url):
            segments = await _playlist_segments(client, url, headers)
            if segments:
                targets = segments
            else:
                # 解析不到分片时**不能**退化为测播放列表文本：m3u8 文本不在内容校验
                # 的黑名单里，小列表会被「样本不足」误杀可播线路（嵌套 master），
                # 长列表会按纯文本下载速度算出一个虚高值排到线路 1 —— 两个方向都错。
                return {"status": "error", "latency_ms": None, "speed_kbps": None,
                        "http_status": None, "bytes_read": 0, "error": "播放列表解析不到分片"}
        status, latency, speed, read, error = await _read_sample(client, targets, headers, settings.speed_sample_bytes)
    except TargetNotMeasurable as exc:
        logger.debug("测速跳过（未测量）: %s (%s: %s)", url, type(exc).__name__, exc)
        return not_measured(exc)

    if error and speed is None:
        return {"status": "error", "latency_ms": latency, "speed_kbps": None, "http_status": status or None, "bytes_read": read, "error": error[:120]}
    return {"status": "ok", "latency_ms": latency, "speed_kbps": speed, "http_status": status or None, "bytes_read": read, "error": ""}


def _pending_sql(scope: str, channel_key: str, refresh: bool, region: str = "") -> tuple[str, list[object]]:
    # status='purged' 是季度清理留下的墓碑，永远不再测 —— 连 refresh（手动全量重测）
    # 也跳过，否则一次全量重测就会把它改回 error、重新进入「剔除→复活」循环。
    # v51：`s.enabled=1` —— 已停用的数据源一律不测（含 refresh=true 的全量重测与收藏夹）。
    # 停用是「这个源现在不可用」的持久状态，继续测它既白吃测速预算，又会把它的线路状态
    # 反复改写。这里是**唯一**的待测队列入口（调用方：auto_budget 与 speed_tester.start），
    # 所以一处过滤覆盖全部 scope；没有选择「把停用源的条目置 active=0」是因为那样必须
    # 重新同步才能恢复（启用后最长要等一轮同步线路才回来），而且会把「源级停用」与
    # 「条目已从源站消失」两种语义混在 active 一列上。
    clauses = ["e.active=1", "s.enabled=1", "COALESCE(h.status, '') <> 'purged'"]
    params: list[object] = []
    if scope == "favorites":
        clauses.append("f.channel_key IS NOT NULL")
    elif scope == "channel":
        clauses.append("e.channel_key=?")
        params.append(channel_key)
    if region in {"domestic", "international"}:
        clauses.append("e.region=?")
        params.append(region)
    cooled = _hours_ago(settings.fail_retest_hours)      # 失效线路：每 20h 复测并累加标记
    stale = _hours_ago(settings.retest_ok_hours)         # 正常线路：超过 72h 没测就重测
    # v48：blocked / unsupported 走**长复检档**，不再和正常线路挤 72h。两个档不同长：
    #   * blocked（内网/保留地址被策略拦截）= RETEST_AFTER_DAYS（默认 14 天）：
    #     内网地址不会自己变成公网，短期内重测必然得到同一个结论。唯一会改变结论的是
    #     用户改 SPEED_ALLOW_PRIVATE_HOSTS，而那必然走 --force-recreate 重启 ——
    #     启动时的 requeue_newly_trusted() 会把名单内主机的 blocked 行强制重新排队，
    #     所以拉长窗口不会造成"刚放行却永远不测"。
    #   * unsupported（域名解析不出来 / 非 HTTP 协议）= UNSUPPORTED_RETEST_DAYS（30 天）：
    #     这一档里混着**永远不会变**的「非 HTTP 协议」线路（rtmp/rtsp/udp，重测一万次
    #     也是同样结论），窗口取更长能把这部分白测压到最低；死域名半年内仍会被复核多次。
    #     线上实测这两类合计 832 行，原先挤在 72h 档等于每天白测约 280 条。
    blocked_window = _hours_ago(settings.retest_after_days * 24)
    unsupported_window = _hours_ago(UNSUPPORTED_RETEST_DAYS * 24)
    if not refresh:
        # 待测六类：从没测过 / 测量方法与当前版本不符（算法升级后的自愈）/ 失效且冷却期已到 /
        #           blocked 且过 14 天档 / unsupported 且过 30 天档 / 其余超过 72h 复检周期。
        # 前两类不看 status（自愈通道）；顺序与 auto_budget 的分档划分保持一致。
        clauses.append(
            "(h.fingerprint IS NULL OR COALESCE(h.method, '') <> ?"
            " OR (h.fail_count > 0 AND h.last_fail_at <> '' AND h.last_fail_at <= ?)"
            " OR (h.status = 'blocked' AND h.checked_at < ?)"
            " OR (h.status = 'unsupported' AND h.checked_at < ?)"
            " OR (COALESCE(h.status, '') NOT IN ('blocked', 'unsupported') AND h.checked_at < ?))"
        )
        params.extend([METHOD_VERSION, cooled, blocked_window, unsupported_window, stale])
    where = " AND ".join(clauses)
    sql = f"""
        SELECT e.stream_fingerprint AS fingerprint,
               MIN(e.url) AS url,
               MIN(e.user_agent) AS user_agent,
               MIN(e.referrer) AS referrer
        FROM source_entries e
        JOIN sources s ON s.slug=e.source_slug
        LEFT JOIN favorites f ON f.channel_key=e.channel_key
        LEFT JOIN stream_health h ON h.fingerprint=e.stream_fingerprint
        WHERE {where}
        GROUP BY e.stream_fingerprint
        ORDER BY (h.fingerprint IS NULL) DESC,
                 (h.status='ok' AND h.checked_at < ?) DESC,
                 (h.fail_count > 0 AND h.last_fail_at <> '' AND h.last_fail_at <= ?) DESC,
                 COALESCE(h.checked_at, '') ASC,
                 s.priority
    """
    # 第二个排序键（可用但已过复检期）必须排在「失效复测」之前：
    # 失效线路数量是可用线路的数倍，且 FAIL_RETEST_HOURS(20h) 小于每日同步间隔，
    # 于是它们每天全部到期、又整体排在可用线路前面，把预算吃光 ——
    # 结果是可用线路永远轮不到复检，死线路会一直以 ok 状态占着线路 1。
    return sql, [*params, stale, cooled]


def cleanup_long_dead_streams(*, dry_run: bool | None = None) -> dict[str, int]:
    """v48「长期失效」清理：把**连续失效满 N 天**的线路条目置为 active=0（可恢复）。

    为什么需要它：`AUTO_PRUNE_AFTER_SPEED` 关掉之后（用户明确要求保持关闭），
    「标记 → 剔除」流水线冻结，判死但仍 active 的条目会一直堆积 —— 线上实测
    error 类仍有 9355 条 active 条目（distinct 指纹 7937），占活跃条目的三成。
    本函数提供一条**温和、可逆、按天级节奏**的清理路径，替代"每轮激进剔除"。

    ## 判据（四条全满足才停用）
    1. 条目 `active=1`，且其 `source_entries.region` 不受 KEEP_REGION 影响
       （区域策略已经把另一区域置 0，无需重复处理）；
    2. 健康行存在且 `status IN ('error', 'unsupported')` —— **当前仍然判死**。
       `blocked` 不参与：它表示「我们没测成」而不是「线路坏」，停用它没有依据；
    3. `dead_since <> ''` 且 `dead_since <= now - N 天`。`dead_since` 是 v48 新加的
       「连续失效起点」列，由 `_save_batch` 维护（首次判定失败时写入、任何一次成功或
       没测成时清空）。**不用 fail_count 当尺子**：线上实测它被钉死在
       PRUNE_MAX_FAIL=3（2669 行停在 3），乘复检间隔最多推出 60 小时，永远够不到 N 天；
       也不用 `last_fail_at`/`checked_at`，它们每次复测都被刷新，记的是"最近一次"而非"起点"；
    4. `status <> 'purged'`（已被季度清理停用的不再处理，保证幂等）。

    ## 保底：每个频道至少留 1 条
    按 `channel_key` 分组，先算「停用这些候选之后该频道还剩多少条 active 条目」：
      * 还剩 ≥1 条 → 候选全部停用；
      * 一条不剩 → **只停用候选里最差的那几条，留下 1 条**，避免整个频道从订阅里消失。
    留下的那条按「可恢复性」挑：优先留**非判死**的（正常能用），其次留速度最快的，
    最后按 source priority / url 兜底，与导出侧 `_drop_dead_lines` 的保底思路一致。

    ## dry-run
    `dry_run=None` 时跟随配置：`AUTO_CLEANUP_DEAD_DAYS=0`（默认）等价于 dry-run ——
    只统计不落库，调用方把数字写进日志。显式传 `dry_run=True` 可强制只统计。

    返回统计字典（无论 dry-run 与否都返回同一组数字，便于对照）。
    """
    days = settings.auto_cleanup_dead_days
    result = {"enabled": int(bool(days)), "days": int(days), "candidates": 0,
              "deactivated": 0, "kept_alive_channels": 0, "channels": 0}
    if days <= 0:
        # 关闭：行为与加这个特性之前完全一致（连查询都不做，避免每周多一次全表扫描）
        return result
    if dry_run is None:
        dry_run = False

    cutoff = _hours_ago(days * 24)
    # 候选：当前判死、且连续失效起点早于 cutoff 的活跃条目。
    # 用 idx_health_dead_since 做范围扫描，再按主键回表取 status，代价很小。
    rows = db.rows(
        """
        SELECT e.channel_key, e.source_slug, e.entry_key, e.stream_fingerprint,
               e.region, e.quality, e.url,
               h.speed_kbps, h.status AS health_status, s.priority
        FROM source_entries e
        JOIN sources s ON s.slug=e.source_slug
        JOIN stream_health h ON h.fingerprint=e.stream_fingerprint
        WHERE e.active=1
          AND h.status IN ('error', 'unsupported')
          AND h.dead_since <> '' AND h.dead_since <= ?
        """,
        (cutoff,),
    )
    result["candidates"] = len(rows)
    if not rows:
        return result

    channels = {row["channel_key"] for row in rows}
    result["channels"] = len(channels)

    # 每个频道当前有多少条**不判死**的活跃条目 —— 决定要不要保底
    survivors: dict[str, int] = {}
    if channels:
        placeholders = ", ".join("(?)" for _ in channels)
        for row in db.rows(
            f"""
            WITH ch(channel_key) AS (VALUES {placeholders})
            SELECT e.channel_key AS channel_key, COUNT(*) AS n
            FROM source_entries e
            JOIN ch ON ch.channel_key = e.channel_key
            LEFT JOIN stream_health h ON h.fingerprint = e.stream_fingerprint
            WHERE e.active=1
              AND NOT (h.status IN ('error', 'unsupported')
                       AND h.dead_since <> '' AND h.dead_since <= ?)
            GROUP BY e.channel_key
            """,
            [*channels, cutoff],
        ):
            survivors[row["channel_key"]] = int(row["n"])

    def keep_rank(row: dict):
        """保底时"留哪一条"的排序键（越小越该留）。"""
        alive = 0 if row["health_status"] == "ok" else 1
        return (alive, -(row["speed_kbps"] or 0.0), row["priority"], row["source_slug"],
                row["entry_key"])

    doomed: dict[str, list[tuple[str, str]]] = {}     # slug -> [(entry_key, fingerprint)]
    for row in rows:
        doomed.setdefault(row["source_slug"], []).append((row["entry_key"], row["stream_fingerprint"]))

    by_channel: dict[str, list[dict]] = {}
    for row in rows:
        by_channel.setdefault(row["channel_key"], []).append(row)

    keep: list[dict] = []
    for channel_key, group in by_channel.items():
        if survivors.get(channel_key, 0) <= 0:
            # 该频道除了这些候选之外没有别的可用条目 → 保底留一条
            group_sorted = sorted(group, key=keep_rank)
            keep.append(group_sorted[0])
            result["kept_alive_channels"] += 1
    kept = {(row["source_slug"], row["entry_key"]) for row in keep}

    pairs = [(slug, entry_key)
             for slug, items in doomed.items()
             for entry_key, _fp in items
             if (slug, entry_key) not in kept]
    result["deactivated"] = len(pairs)
    if dry_run or not pairs:
        return result
    with db.write() as conn:
        # 只置 active=0：不删条目、不动健康记录、不改 region —— 与 KEEP_REGION 的区域剔除
        # 走同一套「停用可恢复」语义，`/api/prune/restore`（按 fingerprint）能把它放回来。
        conn.executemany(
            "UPDATE source_entries SET active=0 WHERE source_slug=? AND entry_key=? AND active=1",
            pairs,
        )
    return result


def _hours_ago(hours: float) -> str:
    """N 小时前的 UTC ISO 时间戳。

    三个复检窗口共用这一个：正常线路超过 `retest_ok_hours` 没测就重测、已标记失效的每
    `fail_retest_hours` 复测一次、被剔除的满 `retest_after_days` 天复活。原先三份实现
    除了时间单位（时/天）以外逐字相同，各自还在函数内重新 import 一次 datetime。
    """
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def revive_expired() -> int:
    """复活冷却期满的失效线路：恢复可见并把标记归零，重新走「标记 -> 剔除」流程。

    只复活「因失效被剔除」的线路；被 KEEP_REGION 区域策略剔除的线路必须保持不变，
    否则区域策略会被复活流程逐步推翻（切区域后旧的失败记录会把国外线路放回来）。

    每次复活都会给对应健康行累加 `revive_count` —— 这是季度清理的依据：
    「复活过、复活后仍然失效」说明等回来也没用，不该无限循环地白测下去。
    已被季度清理停用（status='purged'）的线路不会被复活。
    """
    # 必须带上 fail_count > PRUNE_MAX_FAIL：prune_fingerprints 正是用这个条件剔除的，
    # 而复活会把 fail_count 清零，所以它到现在还 > 阈值就说明「当初确实是被测速剔除的」。
    # 少了这一条，同步置成 active=0 的「源里已消失」条目也会被复活（实测 2687 条），
    # 它们会重新进入统计/导出，同时白白把 revive_count +1 —— 直接污染季度清理的判定。
    #
    # v59（「停用即静默」的收尾）：**已被主动停用**的源不参与复活。少了这一条，停用源里
    # 因失效被剔除（active=0）的条目会在每轮全量同步后的自动维护里被重新激活：该源已经
    # 不再测速，复活只是把一批状态仍是 error 的条目塞回目录/统计（不进订阅，但会让
    # /api/channels、/api/dead-channels、/api/groups 与 stats 的数字反复变化），而且正好
    # 抵消用户按 README 指引手工做的「把死线置 active=0」。生产实测（2026-10-08）：
    # 被停用的那个源有 89 个指纹满足复活条件，按真实配置会在 10-19 ~ 10-21 起被复活。
    enabled_clause = "source_slug IN (SELECT slug FROM sources WHERE enabled=1)"
    where = [
        "active=0",
        "stream_fingerprint IN (SELECT fingerprint FROM stream_health "
        "WHERE last_fail_at <> '' AND last_fail_at <= ? AND status <> 'purged' AND fail_count > ?)",
        enabled_clause,
    ]
    # 被剔除的线路：失效满 retest_after_days 天（换算成小时）后重新纳入检测
    params: list[object] = [_hours_ago(settings.retest_after_days * 24), settings.prune_max_fail]
    if settings.keep_region in {"domestic", "international"}:
        where.append("region=?")
        params.append(settings.keep_region)
    with db.write() as conn:
        fingerprints = [
            row[0]
            for row in conn.execute(
                f"SELECT DISTINCT stream_fingerprint FROM source_entries WHERE {' AND '.join(where)}",
                params,
            )
        ]
        if not fingerprints:
            return 0
        pairs = [(fp,) for fp in fingerprints]
        if settings.keep_region in {"domestic", "international"}:
            # 激活 UPDATE 必须带与 SELECT 一致的区域条件：指纹是一对多的（同一 URL 可被
            # 国内源与国外源同时收录），纯按指纹激活会把同指纹下被区域策略置 0 的
            # 另一区域条目一并复活 —— 国外线路会短暂混进「仅国内」订阅。
            # 写法与 restore_purged_streams 保持一致。
            # v59：与 SELECT 一样带上 enabled 条件 —— 同一指纹可能同时挂在启用源与停用源上，
            # 只按指纹激活会把停用源的条目也一起放回来。
            cursor = conn.executemany(
                "UPDATE source_entries SET active=1 WHERE active=0 AND stream_fingerprint=? "
                f"AND {enabled_clause} AND region=?",
                [(fp, settings.keep_region) for fp in fingerprints],
            )
        else:
            cursor = conn.executemany(
                "UPDATE source_entries SET active=1 WHERE active=0 AND stream_fingerprint=? "
                f"AND {enabled_clause}",
                pairs,
            )
        revived = cursor.rowcount if cursor else 0
        # 只清「这次真正复活」的那批指纹。原实现按 last_fail_at <= cutoff 全表清，
        # 会把另一区域（未被复活）的失败记录也一并抹掉。
        conn.executemany(
            "UPDATE stream_health SET fail_count=0, last_fail_at='', revive_count=revive_count+1 "
            "WHERE fingerprint=?",
            pairs,
        )
    return revived


PURGE_REASON = "复活后仍然失效，已按季度规则永久停用"


def purge_revived_streams(apply: bool = False) -> dict[str, int | bool]:
    """季度清理：永久停用「复活过、复活后仍然失效」的线路。

    判定（三个条件同时满足）：

      * `revive_count >= PURGE_MIN_REVIVE`（被复活过；复活时计数 +1）
      * `fail_count >= 1`（复活之后又失败过 —— 复活会把 fail_count 清零，所以它天然表示「复活后」）
      * 当前 `status` 属于 error / unsupported（确实还是失效）

    实现是把 `stream_health.status` 置为 `'purged'`，而不是物理删除：

      * `_pending_sql` 从此不再选它（连手动全量重测也跳过），不再白耗测速预算；
      * 导出本来只认 ok 与未测，所以它自然不会进订阅；
      * **健康行本身就是墓碑** —— 下次同步即使又抓到同一个 URL，它也不会复活；
      * 想反悔只要把 status 改回去（`restore_purged_streams()`），操作可逆。

    `apply=False` 时只统计不落库，便于先看会删掉什么。
    """
    with db.write() as conn:
        rows = conn.execute(
            """
            SELECT fingerprint FROM stream_health
            WHERE revive_count >= ? AND fail_count >= 1 AND status IN ('error', 'unsupported')
            """,
            (settings.purge_min_revive,),
        ).fetchall()
        fingerprints = [(row[0],) for row in rows]
        result: dict[str, int | bool] = {"candidates": len(fingerprints), "dry_run": not apply}
        if not apply or not fingerprints:
            result["purged"] = 0
            result["deactivated_entries"] = 0
            return result
        conn.executemany(
            """
            UPDATE stream_health SET status='purged', fail_count=0, last_fail_at='',
                   speed_kbps=NULL, latency_ms=NULL, http_status=NULL, error=?, method=?
            WHERE fingerprint=?
            """,
            [(PURGE_REASON, METHOD_VERSION, fp) for (fp,) in fingerprints],
        )
        cursor = conn.executemany(
            "UPDATE source_entries SET active=0 WHERE active=1 AND stream_fingerprint=?", fingerprints
        )
        result["purged"] = len(fingerprints)
        result["deactivated_entries"] = cursor.rowcount if cursor else 0
        return result


def purged_count() -> int:
    """已被季度清理永久停用的线路数。"""
    return int((db.row("SELECT COUNT(*) AS n FROM stream_health WHERE status='purged'") or {"n": 0})["n"])


def restore_purged_streams() -> int:
    """撤销季度清理：把 purged 的线路放回正常失效态，并**重新激活它们的条目**。

    只改 status 是不够的：purge 时把条目置成了 active=0，而 `_pending_sql` 要求
    `e.active=1`，所以「恢复」之后它们仍然不会被测、也不会进订阅 —— 接口与文档承诺的
    「放回复测周期」实际做不到（实测 round-trip 复现过）。这里把条目一并恢复，
    并遵循 KEEP_REGION（与 /api/prune/restore 的区域守卫一致，不把另一区域拉回来）。
    """
    with db.write() as conn:
        fingerprints = [
            (row[0],)
            for row in conn.execute("SELECT fingerprint FROM stream_health WHERE status='purged'")
        ]
        if not fingerprints:
            return 0
        # checked_at 也清掉：否则它带着旧的复检时间戳，最长要再等 RETEST_OK_HOURS(72h)
        # 才会被重新测到 —— 调用方点「恢复」的预期是马上回到周期里。
        conn.executemany(
            "UPDATE stream_health SET status='error', error='', checked_at='' WHERE fingerprint=?",
            fingerprints,
        )
        if settings.keep_region in {"domestic", "international"}:
            conn.executemany(
                "UPDATE source_entries SET active=1 WHERE active=0 AND region=? AND stream_fingerprint=?",
                [(settings.keep_region, fp) for (fp,) in fingerprints],
            )
        else:
            conn.executemany(
                "UPDATE source_entries SET active=1 WHERE active=0 AND stream_fingerprint=?",
                fingerprints,
            )
    return len(fingerprints)


def _daily(rows: int, hours: int) -> int:
    """把存量按复检周期折算成「每天需要测多少条」。"""
    return -(-rows * 24 // max(1, hours))


# auto_budget() 要跨 2.6 万条做一次全表聚合（实测约 125ms），而 /api/stats 会被
# 前端频繁轮询，所以加一层短 TTL 缓存：预算本来就是个估算值，且同步/测速完成后
# 最多 60 秒就会刷新，不影响自动维护取到正确量级。
_BUDGET_CACHE: list = [0.0, None]
BUDGET_TTL = 60.0
# 待测队列长度同理：一次全表分组 + 排序约 150ms，用同样的短 TTL 缓存挡住轮询。
_PENDING_CACHE: list = [0.0, None]
PENDING_TTL = 60.0
# 两个缓存都会被 FastAPI 线程池里的同步端点并发读写（/api/stats），必须加锁；
# 用 None 当「未缓存」哨兵：计数为 0 是**有效值**，用真值判断会导致刚把欠账清到 0 时
# （正是最该省的时候）每次轮询都重跑一遍几百毫秒的全表分组查询。
_CACHE_LOCK = threading.Lock()


def pending_count() -> int:
    """真实待测队列长度（与 _pending_sql 完全同口径）。

    /api/stats 原先的 pending 只数「从没测过 + 测量方法过时」两类，
    漏掉了「连续失败待复测」和「可用待复检」——实测真实队列 5987 条时它显示 0，
    等于把欠账藏起来了。这里直接用选测速目标的那条查询数一遍。
    """
    now = time.monotonic()
    with _CACHE_LOCK:
        if _PENDING_CACHE[1] is not None and now - _PENDING_CACHE[0] < PENDING_TTL:
            return _PENDING_CACHE[1]
    sql, params = _pending_sql("all", "", False, "")
    row = db.row(f"SELECT COUNT(*) AS n FROM ({sql})", params)
    count = int(row["n"]) if row else 0
    with _CACHE_LOCK:
        _PENDING_CACHE[0] = now
        _PENDING_CACHE[1] = count
    return count


def auto_budget() -> int:
    """自动维护每轮的测速预算。

    固定预算必须 >= 存量按设计复检周期折算出的日需求量，否则队列永远排不完：

      * 待测口径必须与 `_pending_sql` 的分档完全一致，即按**复检节奏**分两档，
        而不是按 status 分：
          - `fail_count > 0 AND last_fail_at <> ''` 的（连续失败中）按 FAIL_RETEST_HOURS 复测；
          - 其余全部（含 ok，以及 fail_count 恒为 0 的 blocked）按 RETEST_OK_HOURS 复检。
        早先这里用 `status <> 'ok'` 当失效侧，把 blocked 也算成 20h 复测 ——
        而 blocked 的 fail_count 恒为 0、实际 72h 才重新入队，等于把它们的
        日需求从 24/72 抬到 24/20，虚增预算（41 条约虚增 35 条/天）。
      * FAIL_RETEST_HOURS 默认 20 小时 < 每日同步间隔，所以连续失败的线路每天全部到期；
      * 这类线路数量通常是可用线路的数倍，一旦预算小于它们的日需求，
        预算就被它们整轮吃光 —— 可用线路长期不复检（死线路一直挂着 ok 状态），
        而且每条失效线路的 fail_count 涨得太慢，永远达不到 PRUNE_MAX_FAIL 剔除阈值
        （实测卡在 1~2，几乎没有任何线路走到剔除），「标记 -> 剔除 -> 复活」整条
        流水线实际处于冻结状态。

    这里按真实存量算预算，上限 AUTO_SPEED_MAX；下限仍是 AUTO_SPEED_LIMIT。
    开销实测约 12~20 条/秒，16000 条约 15~20 分钟，每天一次可接受。
    """
    if not settings.auto_speed_adaptive:
        return max(1, settings.auto_speed_limit)
    now = time.monotonic()
    with _CACHE_LOCK:
        if _BUDGET_CACHE[1] is not None and now - _BUDGET_CACHE[0] < BUDGET_TTL:
            return _BUDGET_CACHE[1]
    # 分档谓词与 _pending_sql 的 WHERE 逐字对应（v48 起是五档）：
    #   未测（fingerprint 为 NULL）                    -> 立即到期
    #   连续失败中（fail_count>0 且有 last_fail_at）    -> FAIL_RETEST_HOURS
    #   blocked（策略拦截）                            -> RETEST_AFTER_DAYS（14 天）
    #   unsupported（协议不支持 / 域名解析不出来）      -> UNSUPPORTED_RETEST_DAYS（30 天）
    #   其余（ok / 其他）                               -> RETEST_OK_HOURS
    # 档与档之间用「前档取反」划分，保证五档之和恒等于活跃线路数，不会有条目落在档外；
    # 「连续失败」判在最前面，与 _pending_sql 里 OR 子句的先后顺序保持一致。
    # v48 之前 blocked/unsupported 被算进 72h 档，832 行挤在那里每天白测约 280 条。
    #
    # 必须先按 stream_fingerprint 去重再统计：_pending_sql 是 GROUP BY fingerprint 的，
    # 同一指纹可能挂在多个条目（多个频道/多个源）上，直接对 JOIN 结果计数会重复计入、
    # 把预算虚增一截（实测条目 26869 vs 去重 24674，虚增约 8.9%）。
    row = db.row(
        """
        SELECT
            SUM(CASE WHEN h.fingerprint IS NULL THEN 1 ELSE 0 END) AS untested,
            SUM(CASE WHEN h.fingerprint IS NOT NULL
                      AND h.fail_count > 0 AND h.last_fail_at <> ''
                     THEN 1 ELSE 0 END) AS failing,
            SUM(CASE WHEN h.fingerprint IS NOT NULL
                      AND NOT (h.fail_count > 0 AND h.last_fail_at <> '')
                      AND h.status = 'blocked'
                     THEN 1 ELSE 0 END) AS blocked_n,
            SUM(CASE WHEN h.fingerprint IS NOT NULL
                      AND NOT (h.fail_count > 0 AND h.last_fail_at <> '')
                      AND h.status = 'unsupported'
                     THEN 1 ELSE 0 END) AS unsupported_n,
            SUM(CASE WHEN h.fingerprint IS NOT NULL
                      AND NOT (h.fail_count > 0 AND h.last_fail_at <> '')
                      AND COALESCE(h.status, '') NOT IN ('blocked', 'unsupported')
                     THEN 1 ELSE 0 END) AS steady
        FROM (
            SELECT e.stream_fingerprint AS fp
            FROM source_entries e
            WHERE e.active=1
              AND NOT EXISTS (SELECT 1 FROM stream_health h0
                              WHERE h0.fingerprint=e.stream_fingerprint AND h0.status='purged')
            GROUP BY e.stream_fingerprint
        ) d
        LEFT JOIN stream_health h ON h.fingerprint=d.fp
        """
    ) or {}
    untested = int(row.get("untested") or 0)
    failing = int(row.get("failing") or 0)
    blocked_n = int(row.get("blocked_n") or 0)
    unsupported_n = int(row.get("unsupported_n") or 0)
    steady = int(row.get("steady") or 0)
    need = (untested
            + _daily(steady, settings.retest_ok_hours)
            + _daily(failing, settings.fail_retest_hours)
            + _daily(blocked_n, settings.retest_after_days * 24)
            + _daily(unsupported_n, UNSUPPORTED_RETEST_DAYS * 24))
    budget = max(settings.auto_speed_limit, min(need, settings.auto_speed_max))
    with _CACHE_LOCK:
        _BUDGET_CACHE[0] = now
        _BUDGET_CACHE[1] = budget
    return budget


class SpeedTester:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        # start 串行化：busy 检查与 create_task 之间隔着 to_thread 查询，
        # 不加锁则并发双击能同时通过 busy 检查，起两个测速任务互相覆盖进度
        self._start_lock = asyncio.Lock()
        self._cancel = asyncio.Event()
        self.progress: dict = {
            "running": False,
            "total": 0,
            "done": 0,
            "ok": 0,
            "failed": 0,
            "blocked": 0,
            "skipped": 0,
            "pruned": 0,
            # v42（审计 P2-7）：疑似"整轮全灭"时回滚本轮失败计数的条数 —— 进度接口直接可见，
            # 免得只能靠日志判断护栏有没有生效。
            "rolled_back": 0,
            "scope": "",
            "started_at": None,
            "finished_at": None,
            "message": "",
        }

    @property
    def busy(self) -> bool:
        return bool(self._task and not self._task.done())

    def reset(self) -> None:
        self._cancel = asyncio.Event()
        self.progress.update(
            # rolled_back 也要归零：它是"本轮回滚了多少条"的计数，跨轮残留会让界面/日志
            # 把上一轮的数字当成这一轮的（v42，审计 P1-2）。
            {"running": False, "total": 0, "done": 0, "ok": 0, "failed": 0, "blocked": 0, "skipped": 0, "pruned": 0, "rolled_back": 0, "scope": "", "started_at": None, "finished_at": None, "message": ""}
        )

    async def start(self, scope: str = "favorites", channel_key: str = "", limit: int = 300, refresh: bool = False, region: str = "", cap: int | None = None) -> bool:
        async with self._start_lock:
            if self.busy:
                return False
            # cap 只给自动维护用（自适应预算按存量算，会超过 SPEED_MAX_ITEMS）；
            # 手动测速不传 cap，仍受 SPEED_MAX_ITEMS 约束，行为不变。
            limit = max(1, min(limit, cap or settings.speed_max_items))
            sql, params = _pending_sql(scope, channel_key, refresh, region)
            # 这条查询要跨 2.4 万行做分组与排序，是同步阻塞的，放线程里跑避免卡住事件循环
            rows = await asyncio.to_thread(db.rows, sql + " LIMIT ?", [*params, limit])
            self.reset()
            self.progress.update({"running": True, "total": len(rows), "scope": region or scope, "started_at": utc_now(), "message": "测速任务已启动"})
            self._task = asyncio.create_task(self._run(rows))
            return True

    async def cancel(self) -> None:
        if self.busy:
            self._cancel.set()
            self.progress["message"] = "正在停止"

    async def _run(self, rows: list[dict]) -> None:
        """测速任务入口：任何异常都必须收尾。

        否则 progress['running'] 会永远停在 True —— busy 已经是 False（任务结束了），
        但前端 pollSpeedTest 读的是 running，会一直轮询显示「测速中」且按钮无法恢复。
        """
        try:
            await self._run_inner(rows)
        except asyncio.CancelledError:
            self.progress["message"] = "测速已取消"
            raise
        except Exception:
            logger.exception("测速任务异常中止")
            self.progress["message"] = "测速异常中止（详见容器日志）"
        finally:
            self.progress["running"] = False
            if not self.progress.get("finished_at"):
                self.progress["finished_at"] = utc_now()

    async def _run_inner(self, rows: list[dict]) -> None:
        sem = asyncio.Semaphore(settings.speed_concurrency)
        rows_by_fp = {row["fingerprint"]: row for row in rows}

        async with _client() as client:

            async def one(row: dict) -> tuple[str, dict]:
                if self._cancel.is_set():
                    return row["fingerprint"], {"status": "skipped", "error": "任务已取消"}
                async with sem:
                    if self._cancel.is_set():
                        return row["fingerprint"], {"status": "skipped", "error": "任务已取消"}
                    result = await probe(client, row["url"], row.get("user_agent", ""), row.get("referrer", ""))
                return row["fingerprint"], result

            tasks = [asyncio.create_task(one(row)) for row in rows]
            buffered: list[tuple[str, str, dict]] = []
            fail_counts: dict[str, int] = {}
            written = 0
            # 本轮真正落库过的指纹（v42，审计 P1-2）：疑似全灭时要按这份名单回滚失败计数，
            # 否则"护栏只推迟删除"并不成立 —— 见 _rollback_round_failures 的说明。
            round_fingerprints: list[str] = []

            async def flush() -> None:
                """把已攒下的结果落库。

                分批落库而不是等全部跑完：一轮几千条要跑十几分钟，原先只在结束时写一次，
                导致（a）中途看到的进度和状态全是旧的、（b）重启/崩溃会把整轮测量丢光
                —— 这两种情况都实际发生过。分批后最长丢失窗口只有 FLUSH_EVERY 条。
                """
                nonlocal buffered, written
                if not buffered:
                    return
                counts = await asyncio.to_thread(self._save_batch, buffered)
                fail_counts.update(counts)
                written += len(buffered)
                round_fingerprints.extend(fp for fp, _, _ in buffered)
                buffered = []
                bump_revision()

            try:
                for finished in asyncio.as_completed(tasks):
                    fingerprint, result = await finished
                    status = result.get("status")
                    if status == "skipped":
                        # 只有「任务取消」会用 skipped，且**不落库**（取消是任务的属性）
                        self.progress["skipped"] += 1
                    else:
                        buffered.append((fingerprint, rows_by_fp[fingerprint]["url"], result))
                        if status == "ok":
                            self.progress["ok"] += 1
                        elif status == "blocked":
                            # 守卫拦截：落库但不算失败（fail_count 不递增），
                            # 否则这些条目每轮都会重新入队，欠账永远清不到 0
                            self.progress["blocked"] += 1
                        else:
                            self.progress["failed"] += 1
                    self.progress["done"] += 1
                    if len(buffered) >= FLUSH_EVERY:
                        await flush()
            finally:
                # 取消 / 抛异常时也要收尾：取消还没跑完的探测，并把已攒下的结果落库。
                # 原先这两句写在正常路径末尾，取消时根本执行不到 —— 最后不足 FLUSH_EVERY
                # 条的结果会全部丢掉（调 _run_inner 并 cancel 的实测复现过）。
                for task in tasks:
                    if not task.done():
                        task.cancel()
                # 取消后要回收这些任务：不 await 就退出 _client() 上下文的话，
                # 被取消的探测会在连接池关闭后才落地（且未 retrieve 的异常会进日志噪音）。
                await asyncio.gather(*tasks, return_exceptions=True)
                await flush()
            to_prune = [fp for fp, count in fail_counts.items() if count > settings.prune_max_fail]

        removed = 0
        # 永久护栏（v42）：整轮大面积失效就不剔除。实测出现过「470/470 可用 0 失效 470」——
        # 那是网络被自己打满造成的误判，若照常剔除就会把好线路删掉（用户看到的是"频道线路消失"）。
        # 只推迟删除、不改变判定：该轮标记的失效照旧写入，等某轮 ok>0（网络正常）时再按
        # fail_count 正常剔除。阈值 done>=30 是为了不干扰只测几条的小轮次。
        # （v42 修订，审计 P1-2：上面这句"只推迟删除"是第一版口径，实测并不成立 ——
        #  本轮失败计数已经随 _save_batch 落库，网络恢复的第一轮就会越过阈值被剔除。
        #  现在改成"本轮判定作废"：连同写进去的失败计数一起回滚，见下方注释。）
        #
        # v42 比例判定（审计 P2-7）：原先只认 `round_ok == 0`（"一条可用都没有"），只要有一条
        # 通了护栏就完全不生效。实测有一轮 `ok=9/353`（2.5% 可用）没触发，281 条失败照常累加
        # fail_count，而库里当时已有 2669 条停在 `fail_count=3`（阈值 4）—— 差点被整批剔除。
        # 现在改成比例：ok < 20% 即认为"网络/上游整体异常"，推迟本轮剔除。
        # 只推迟删除、不改判定（失效标记照旧写入）。
        # v59：`done` 里含 `skipped`（取消导致的"没测成"），而取消一轮就会让 ok 比例
        # 人为变低、误触护栏。判定只用**真正有测量结论**的条数（done - skipped）。
        round_done = int(self.progress.get("done") or 0) - int(self.progress.get("skipped") or 0)
        round_ok = int(self.progress.get("ok") or 0)
        all_dead_suspect = round_done >= 30 and round_ok * 5 < round_done
        rolled_back = 0
        if to_prune and settings.auto_prune_after_speed and not all_dead_suspect:
            removed = await asyncio.to_thread(prune_fingerprints, to_prune)
            self.progress["pruned"] = removed
        elif all_dead_suspect:
            # 审计 P1-2（v42）：光跳过 prune 还不够 —— 本轮的 fail_count 已经 +1 落库了
            # （见 _save_batch 的 upsert），等网络恢复正常的第一轮，这些被误判的线路会因为
            # 累积计数直接越过阈值被剔除（active=0），"只推迟"实际等于"晚一轮删除"。
            # 所以这里把**本轮写进去的那一次失败**原样回滚，让这一轮等价于"没有判定过"。
            # v59：回滚必须连 status/checked_at/dead_since 一起（v42 只回滚了计数，
            # 导致线路立刻从订阅消失且 72h 不重测，详见 _rollback_round_failures）。
            if round_fingerprints:
                rolled_back = await asyncio.to_thread(_rollback_round_failures, round_fingerprints)
                self.progress["rolled_back"] = rolled_back
            logger.warning(
                "测速：本轮 %s 条里只有 %s 条可用（%.1f%%，低于 20%% 护栏），疑似网络/上游异常 —— "
                "护栏生效：本轮不剔除任何线路，并已回滚本轮写入的失败判定 %s 条"
                "（待网络正常的那一轮再按 fail_count 清理）",
                round_done, round_ok, (round_ok / round_done * 100) if round_done else 0.0, rolled_back,
            )

        if written:
            bump_revision()

        self.progress["running"] = False
        self.progress["finished_at"] = utc_now()
        self.progress["message"] = "测速完成" if not self._cancel.is_set() else "测速已取消"

    def _save_batch(self, items: list[tuple[str, str, dict]]) -> dict[str, int]:
        """批量写测速结果，返回 fingerprint -> 最新连续失败次数。

        全部写入与回读共用一次连接与一个事务，避免逐条开连接的开销；
        连续失败次数沿用「失败 +1 / 成功清零」的 upsert 语义。
        status='blocked'（守卫拦截，测不了）按「成功」一侧处理：fail_count 清零、
        last_fail_at 清空。这样它只退出待测队列、按正常复检周期再试，既不会被算成
        失效，也不会因为 fail_count 累积而被剔除。
        """
        if not items:
            return {}
        with db.write() as conn:
            params: list[tuple[object, ...]] = []
            for fingerprint, url, result in items:
                # 不用 `result.get("status", "error")`：那只在**键缺失**时兜底，
                # 而 `{"status": ""}` / `{"status": None}` 会原样落到 status 列。
                # 见 _SAVEABLE_STATUSES 的说明。空值静默归入 error（与键缺失同待遇），
                # 非空但不认识的值额外记一条 WARNING，便于定位是谁写坏的。
                raw_status = result.get("status")
                if raw_status in _SAVEABLE_STATUSES:
                    status = raw_status
                else:
                    status = "error"
                    if raw_status:
                        logger.warning(
                            "测速结果里的 status 取值不认识，按 error 处理：%r（%s）",
                            raw_status, url[:120],
                        )
                # 「成功一侧」= 真的测通了（ok）、被守卫拦下（blocked）、或压根没测成
                # （not_measured：域名解析不出来 / 协议不支持）。三者都不构成失败证据，
                # 一律 fail_count 清零、last_fail_at 清空，与 v48 之前的 blocked 同待遇。
                success_side = status in ("ok", "blocked") or bool(result.get("not_measured"))
                now = utc_now()
                params.append(
                    (
                        fingerprint,
                        url,
                        status,
                        result.get("latency_ms"),
                        result.get("speed_kbps"),
                        result.get("http_status"),
                        result.get("bytes_read", 0),
                        result.get("error", "")[:200],
                        success_side,
                        fingerprint,
                        FAIL_COUNT_MAX,      # ← INSERT 分支 MIN(...) 的上限
                        success_side,
                        now,                 # 非 success_side 时写 last_fail_at
                        status,
                        success_side,
                        METHOD_VERSION,
                        success_side,        # ← dead_since INSERT：CASE WHEN
                        now,                 # ← dead_since INSERT：首次失败起点
                        now,                 # ← checked_at
                        success_side,        # ← ON CONFLICT 分支 CASE WHEN
                        FAIL_COUNT_MAX,      # ← ON CONFLICT 分支 MIN(...) 的上限
                        success_side,        # ← ON CONFLICT 分支 dead_since CASE WHEN
                    )
                )
            conn.executemany(
                """
                INSERT INTO stream_health(fingerprint, url, status, latency_ms, speed_kbps, http_status, bytes_read, error, fail_count, last_fail_at, ok_total, fail_total, method, dead_since, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                        CASE WHEN ? THEN 0 ELSE MIN(COALESCE((SELECT fail_count FROM stream_health WHERE fingerprint=?), 0) + 1, ?) END,
                        CASE WHEN ? THEN '' ELSE ? END,
                        CASE WHEN ?='ok' THEN 1 ELSE 0 END,
                        CASE WHEN ? THEN 0 ELSE 1 END,
                        ?,
                        CASE WHEN ? THEN '' ELSE ? END,
                        ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    url=excluded.url,
                    status=excluded.status,
                    latency_ms=excluded.latency_ms,
                    speed_kbps=excluded.speed_kbps,
                    http_status=excluded.http_status,
                    bytes_read=excluded.bytes_read,
                    error=excluded.error,
                    fail_count=CASE WHEN ? THEN 0 ELSE MIN(stream_health.fail_count + 1, ?) END,
                    -- excluded.last_fail_at 在 success_side 时已经是 ''（见上面的 CASE），
                    -- 非 success_side 时是本次时间戳，所以这里直接取 excluded 的值即可。
                    last_fail_at=excluded.last_fail_at,
                    ok_total=stream_health.ok_total + excluded.ok_total,
                    fail_total=stream_health.fail_total + excluded.fail_total,
                    method=excluded.method,
                    -- v48 连续失效起点：测得成功/没测成 -> 清空（本轮证明它不坏，或没有证据）；
                    -- 判定失败 -> 首次失败时填本次时间，之后**保持不动**（要的就是"起点"）。
                    dead_since=CASE WHEN ? THEN ''
                                    WHEN stream_health.dead_since <> '' THEN stream_health.dead_since
                                    ELSE excluded.dead_since END,
                    checked_at=excluded.checked_at
                WHERE stream_health.status <> 'purged'
                """,
                params,
            )
            counts: dict[str, int] = {}
            for fingerprint, _, _ in items:
                row = conn.execute("SELECT fail_count FROM stream_health WHERE fingerprint=?", (fingerprint,)).fetchone()
                counts[fingerprint] = int(row["fail_count"]) if row else 0
        return counts


def prune_fingerprints(fingerprints: list[str]) -> int:
    """按流指纹剔除线路（从列表与订阅中移除，记录保留可恢复）。"""
    if not fingerprints:
        return 0
    with db.write() as conn:
        cursor = conn.executemany(
            "UPDATE source_entries SET active=0 WHERE active=1 AND stream_fingerprint=?",
            [(fp,) for fp in fingerprints],
        )
    return cursor.rowcount if cursor else 0


def _rollback_round_failures(fingerprints: list[str]) -> int:
    """把一轮"疑似整体误判"里刚写进去的失败判定整条回退，返回回退的行数（v42/v59）。

    为什么需要：`_save_batch` 在落库时就把本次失败算进了 fail_count（"失败 +1 / 成功清零"），
    而护栏只在 prune 那一步跳过 —— 于是被误判的线路**已经**背上了这一轮的失败计数。
    等网络恢复正常的第一轮，它们会直接越过 `prune_max_fail` 被剔除（active=0），
    用户看到的就是"网络抖一下，一批本来好好的线路没了"。这里把本轮那次判定撤销，
    让护栏真正等价于"这一轮没有判定过"。

    v59 修正（P1，有生产数据证据）：v42 只回退了 `fail_count`，**没有回退同一批
    `_save_batch` 一起写进去的 `status='error'` / `checked_at=now` / `dead_since=now`**。
    后果比"晚一轮删除"更糟，是"好线路当场消失、而且 72 小时不回来"：

      * 导出只放行 `h.status IS NULL OR h.status='ok'`（static_export.py），
        `status` 仍是 `'error'` → 这条线路**立刻从 /iptv、live.m3u 里消失**；
      * `_pending_sql` 的失效档要求 `fail_count > 0`、正常档要求 `checked_at < now-72h`，
        被回退成 `fail_count=0` + 全新的 `checked_at` → **72 小时不进入待测队列**。
    生产实测（2026-10-08）：active 条目中 `status='error' AND fail_count=0` 共 328 条 /
    194 个频道，`checked_at` 集中在 10-05 那两次低可用率轮次 —— 只有"只回退计数"能造成。

    现在的口径：本轮**首次失败**（`fail_count` 由 0→1，即行内旧值为 1）的整条判定作废 ——
    计数归零、`status` 恢复 `'ok'`、`error`/`dead_since`/`checked_at` 清空，让它立刻回到
    订阅与待测队列；已经失败过一轮以上（旧值 ≥2）的只减计数，保留原有判定与时间戳。
    用 `status='error'` 而不是 `IN ('error','unsupported')` 做"作废"条件：`unsupported`
    属于 `not_measured`（`_save_batch` 走成功一侧、计数本就为 0），不该被改成 ok。

    * 分批执行：一轮最多 `SPEED_MAX_ITEMS`（默认 5000）条，而 SQLite 的变量数上限在旧版本
      只有 999，一次性 IN 会直接报错（分批后每批 400 个占位符，留足余量）；
    * `last_fail_at` 不回滚：本轮确实探测失败过，留着一个**会很快重测**的时间戳无害
      （复检调度按它算下次重测时间），而"计数"才是决定剔除的东西；
    * SQLite 的 SET 表达式对**更新前**的行取值，所以 `fail_count = 1` 就是"本轮首次失败"。
    """
    if not fingerprints:
        return 0
    total = 0
    with db.write() as conn:
        for start in range(0, len(fingerprints), 400):
            chunk = fingerprints[start:start + 400]
            placeholders = ",".join("?" * len(chunk))
            cursor = conn.execute(
                f"""
                UPDATE stream_health SET
                    status     = CASE WHEN fail_count = 1 AND status = 'error' THEN 'ok' ELSE status END,
                    error      = CASE WHEN fail_count = 1 AND status = 'error' THEN '' ELSE error END,
                    dead_since = CASE WHEN fail_count = 1 AND status = 'error' THEN '' ELSE dead_since END,
                    checked_at = CASE WHEN fail_count = 1 AND status = 'error' THEN '' ELSE checked_at END,
                    fail_count = MAX(0, fail_count - 1)
                WHERE fingerprint IN ({placeholders}) AND status IN ('error','unsupported')
                """,
                chunk,
            )
            total += cursor.rowcount if cursor else 0
    return total


speed_tester = SpeedTester()
