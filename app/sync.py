from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import time
import logging
import uuid
from collections.abc import Iterable
from contextlib import aclosing
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from .config import settings
from .netguard import (
    LOOPBACK_HOSTS,
    REDIRECT_STATUSES,
    SourceUnavailableError,
    is_restricted_ip,
    next_redirect,
    read_capped_async,
    resolve_addresses,
    restricted_in,
    send_pinned,
)
from .db import (db, get_meta, purge_invalid_discovered, set_meta,
                   set_source_parent, upsert_source, utc_now)
from .parsers import (
    ParsedEntry,
    genre_header,
    looks_like_huawei_capture,
    normalize_group,
    parse_channel_list,
    parse_huawei_capture,
    parse_iptv_org_json,
    parse_m3u,
    parse_tvbox_lives,
    region_of,
)
from .revision import bump_revision

ALLOWED_SOURCE_HOSTS_BUILTIN = frozenset({
    "live.hacks.tools", "live.zbds.top", "iptv-org.github.io", "gh-proxy.com", "116.ltd", "192.168.31.110",
    # TVBox 配置站点本身；配置 lives 里指向的直播列表主机也必须在这里，否则不会被抓取
    "9280.kstore.vip", "down.nigx.cn", "193.123.86.190",
})
# 生效白名单 = 内置（代码里写死，运行时不可撤销）+ 手动放行（allowed_hosts 表）。
# 用同一个可变集合，是为了让 _open_source 与 speed._trusted_hosts 直接看到最新内容。
ALLOWED_SOURCE_HOSTS = set(ALLOWED_SOURCE_HOSTS_BUILTIN)
MAX_SOURCE_REDIRECTS = 5
SOURCE_USER_AGENT = "IPTV-Hub/1.0 (metadata sync)"


def load_allowed_hosts() -> int:
    """启动时把库里「手动放行」的主机载入内存白名单。

    ALLOWED_SOURCE_HOSTS 是内置白名单（代码里写死的那批）；手动添加的源把主机
    写进 allowed_hosts 表，这里在启动时合并进来，加源就不需要改配置重启。
    """
    hosts = db.allowed_hosts()
    ALLOWED_SOURCE_HOSTS.update(h.lower() for h in hosts)
    return len(hosts)


def approve_hosts(hosts: Iterable[str], note: str = "") -> list[str]:
    """放行一批主机：写库并同步更新内存白名单，让同步/测速立刻认它。"""
    added = db.add_allowed_hosts(hosts, note)
    ALLOWED_SOURCE_HOSTS.update(h.lower() for h in added)
    return added


def revoke_host(host: str) -> bool:
    """撤销一个主机的放行：删库并同步移出内存白名单。

    只影响手动放行的那部分；内置白名单（代码里写死的）不受影响。
    """
    target = (host or "").strip().lower()
    if not target:
        return False
    removed = db.remove_allowed_host(target)
    if removed:
        ALLOWED_SOURCE_HOSTS.discard(target)
    return removed


def restricted_resolution(host: str) -> str:
    """解析主机名，返回第一个「受限地址」（没有则空串）。

    只检查主机名在白名单里是不够的：`127.1`、`localhost.`、`0x7f.0.0.1` 这些写法
    `ipaddress.ip_address()` 解析不了、会被当成普通域名放行，而 getaddrinfo 会把它们
    统统解析到 127.0.0.1（本机实测）。所以域名必须解析后逐个地址再判一遍。
    只挡环回/链路本地/组播/未指定/保留，**不挡内网网段** —— 本地源是正当用法。

    v42：解析与判定的实现与 netguard 合并（`resolve_addresses` + `restricted_in`），
    这里只保留这个历史名字给既有调用方；抓取路径直接用 netguard 那两个函数，
    因为守卫必须和"钉住的地址列表"共用同一次解析（P0-2）。
    """
    return restricted_in(resolve_addresses(host))


def check_manual_source_url(url: str, *, resolve: bool = True) -> str:
    """校验手动添加的数据源地址，返回规范化主机名；不合法就抛 ValueError。

    只挡真正危险的地址（环回/链路本地/组播/未指定），**不挡内网网段** ——
    本项目本来就要支持内网的本地源，一刀切禁内网会把正当用法也砍掉。

    `resolve=True`（默认）时**会**做一次 DNS 解析并拒绝解析到受限地址的域名。
    注意这里解析出来的地址**只用于判定**、不返回给调用方：真正要发请求的路径必须走
    `_validate_and_resolve()` —— 它把「判受限」与「要钉住的 IP」放在**同一次解析**里，
    否则两次解析之间 DNS 翻转就是 P0-2 那个重绑定窗口。
    原先的 docstring 写的是"域名不在这里解析……抓取时由白名单机制兜住"，与实现不符
    （实现一直在解析，`resolve` 形参就是为它准备的），已按实际行为改写。
    """
    parts = urlsplit((url or "").strip())
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError("只支持 http / https 地址")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("地址里没有主机名")
    if host in LOOPBACK_HOSTS:
        raise ValueError(f"拒绝环回地址：{host}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if is_restricted_ip(ip):
            raise ValueError(f"拒绝环回 / 链路本地 / 组播 / 未指定地址：{host}")
        return host
    if not resolve:
        return host
    resolved = restricted_resolution(host)
    if resolved:
        raise ValueError(f"域名 {host} 解析到受限地址 {resolved}，已拒绝")
    return host


async def _validate_and_resolve(url: str) -> tuple[str, tuple[str, ...]]:
    """校验地址并返回 (规范化主机名, **本次要钉住的地址列表**)。

    为什么把「校验」与「解析结果」合在一次调用里（v42，审计 P0-2 DNS 重绑定）：
    守卫与"真正连接的目标"必须是**同一次 DNS 解析**的产物。分开解析两次，
    两次之间就存在翻转窗口 —— 校验时是公网 IP、httpx 建连时再解析一次变成
    127.0.0.1，守卫形同虚设（审计已在容器内复现：第 3 次解析返回 127.0.0.1 时
    成功取回内网内容）。调用方拿到这里的地址后交给 `netguard.send_pinned` 直连，
    httpx 不会再自己解析一次。

    判据与 `check_manual_source_url` 一致：只挡环回/链路本地/组播/未指定/保留，
    **不挡内网网段**（本地源 192.168.x 是正当用法）。
    """
    host = check_manual_source_url(url, resolve=False)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        # 字面量 IP：钉不钉都是它自己（pin_target 里 ip == host 会原样返回）
        return host, (host,)
    addresses = await asyncio.to_thread(resolve_addresses, host)
    restricted = restricted_in(addresses)
    if restricted:
        raise ValueError(f"域名 {host} 解析到受限地址 {restricted}，已拒绝")
    if not addresses:
        # 解析不出来就不能"退回让 httpx 自己解析"——那正好是重绑定的入口。
        # v47：改抛 SourceUnavailableError（ValueError 子类）→ 上游侧失败，
        # POST /api/sources 会据此回 502 而不是 400（见 netguard 里的说明）。
        raise SourceUnavailableError(f"域名无法解析：{host}")
    return host, addresses


# v47 删除：`check_manual_source_url_async`。
# 它是 `check_manual_source_url` 的异步包装，v42 把解析并进 `_validate_and_resolve`
# （守卫与钉 IP 必须共用同一次解析，见 P0-2）之后就没有任何调用方了 ——
# `probe_manual_source` 直接调 `_validate_and_resolve`。删掉它是为了让"哪条路径做了
# 隔离/钉 IP"只有一个答案，避免以后有人误以为还有第二条异步校验入口。
# 异步侧的唯一入口是 `_validate_and_resolve`。


def detect_format(content: bytes) -> str:
    """按内容判断源格式：m3u / json / tvbox / list / huawei。"""
    head = content.lstrip()[:8192]
    if head[:7].upper() == b"#EXTM3U" or b"#EXTINF" in head:
        return "m3u"
    if head[:1] in (b"{", b"["):
        try:
            data = json.loads(content)
        except Exception:
            return "m3u"
        if isinstance(data, dict) and isinstance(data.get("lives"), list):
            return "tvbox"
        return "json"
    if b"#EXTINF" in content[:65536]:
        return "m3u"
    # v61：华为 HWCTC / 抓包 JSP 是属性式记录（ChannelName/ChannelURL）。不认它就会落到
    # 下面按「名称,地址」逐条清单的判定里，最终被当成 m3u 解析出 0 条 ——
    # 手动加源时表现为「解析不出任何有效条目」，而内联粘贴时更糟：
    # 会被逐行的「找第一个 http 地址」兜底分支接住，名字变成 XML 残渣、地址留着 &amp;。
    if looks_like_huawei_capture(content):
        return "huawei"
    # 「名称,http(s)://地址」逐条清单（运营商内网源常见）。只看第一条有效行即可判断。
    for line in content.decode("utf-8", errors="replace")[:4096].splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # v60：`分组名,#genre#` 是分组头，不是「名称,地址」行。以前它会直接触发下面的
        # break → 整份清单被判成 m3u → parse_m3u 返回 0 条 → 手动加源报 400
        # 「解析不出任何有效条目」（实测 live.zbds.top/tv/iptv4.txt）。
        if genre_header(line):
            continue
        for sep in (",", "\uff0c", "\t"):
            idx = line.find(sep)
            if idx > 0 and line[idx + 1:].strip().lower().startswith(("http://", "https://")):
                return "list"
        break
    return "m3u"


async def probe_manual_source(url: str, fmt: str = "") -> dict[str, Any]:
    """添加数据源前的有效性检测：真抓一次并解析，但不写任何数据。

    只有把内容真的抓下来、解析出条目，才算「源有效」；顺便记录这次抓取经过的
    所有主机（含重定向跳转），调用方据此放行 —— 放行范围恰好是「实际访问成功过的
    那些主机」，不多放一个。

    任何失败都抛异常，由调用方转成 HTTP 错误信息，不会留下半个源。
    """
    url = (url or "").strip()
    # v42：原先这里先做一次 check_manual_source_url_async(url) 预检，进循环后**每跳又会
    # 再解析一次** —— 同一跳解析两次不但白花一次 DNS，还正好制造出"校验用哪次解析结果"
    # 的歧义（P0-2）。现在首跳校验由循环内的 `_validate_and_resolve` 负责：它给出的地址
    # 同时用于判受限与钉连接目标，一次解析把两件事一起办完。
    hosts: set[str] = set()
    content = b""
    headers = {"Accept": "text/plain, application/x-mpegURL, application/json", "User-Agent": SOURCE_USER_AGENT}
    timeout = httpx.Timeout(settings.request_timeout, connect=12)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers=headers) as client:
        # v57：命中 SOURCE_PROXY_HOSTS 的跳改走代理 client（代理侧解析域名，因此不钉 IP）
        proxy_clients: dict[str, httpx.AsyncClient] = {}
        current = url
        try:
            for _ in range(MAX_SOURCE_REDIRECTS + 1):
                hop_proxy = settings.proxy_for(current)
                if hop_proxy:
                    hop_client = proxy_clients.get(hop_proxy)
                    if hop_client is None:
                        hop_client = httpx.AsyncClient(
                            timeout=timeout, follow_redirects=False, headers=headers, proxy=hop_proxy,
                        )
                        proxy_clients[hop_proxy] = hop_client
                    host = (urlsplit(current).hostname or "").lower()
                    hosts.add(host)
                    response = await send_pinned(hop_client, current, host, ())
                else:
                    # 每一跳都重新校验：重定向可能把我们带到不该去的地方
                    # v42（P0-2）：这次校验同时给出**要钉住的 IP**，请求直接连它 ——
                    # 原先这里是"校验一次域名"然后 `client.send(current)` 让 httpx 再解析一次，
                    # DNS 重绑定（校验时公网、连接时 127.0.0.1）即可绕过守卫。
                    host, addresses = await _validate_and_resolve(current)
                    hosts.add(host)
                    response = await send_pinned(client, current, host, addresses)
                try:
                    if response.status_code in REDIRECT_STATUSES:
                        location = response.headers.get("location", "")
                        status_code = response.status_code
                        if not location:
                            raise RuntimeError(f"HTTP {status_code} 响应缺少 Location 头")
                        current = urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        raise RuntimeError(f"源返回 HTTP {response.status_code}")
                    content = await read_capped_async(
                        response, settings.max_source_bytes,
                        f"内容超过上限 {settings.max_source_bytes} 字节",
                    )
                finally:
                    await response.aclose()
                break
            else:
                raise SourceUnavailableError(f"重定向超过 {MAX_SOURCE_REDIRECTS} 跳")
        finally:
            for hop_client in proxy_clients.values():
                await hop_client.aclose()

    if not content.strip():
        raise SourceUnavailableError("源返回了空内容")
    detected = (fmt or "").strip().lower() or detect_format(content)
    # 'list' 必须在这里被认下来：detect_format 能识别「名称,http://地址」清单并返回它，
    # 手动加源路径若只放行 m3u/json/tvbox，用户会看到内部格式名（「不支持的格式：list」）
    # 而这类清单**永远加不进来**。
    if detected not in {"m3u", "json", "tvbox", "list", "huawei"}:
        raise ValueError(f"不支持的格式：{detected}")
    if detected == "tvbox":
        lives = parse_tvbox_lives(content)
        if not lives:
            raise ValueError("是 TVBox 配置，但 lives 列表为空")
        return {"url": url, "format": "tvbox", "entries": 0, "sub_sources": len(lives), "hosts": sorted(hosts)}
    # 解析必须离开事件循环：这是纯 CPU 的同步函数，实测 5 万行要 40 秒（1.1MB），
    # 而 MAX_SOURCE_BYTES 允许 16MiB。直接 await 在协程里跑，整个进程（含 /healthz、
    # /api/* 与已排队的同步/测速）会一起停摆 —— 实测 53KB 的清单就能把 /healthz 卡 7.6 秒。
    # 同步路径上的 _parse 已经在 to_thread 里，这里手动加源这条路径漏了。
    if detected == "json":
        entries = await asyncio.to_thread(parse_iptv_org_json, content)
    elif detected == "list":
        entries = await asyncio.to_thread(parse_channel_list, content)
    elif detected == "huawei":
        # v61：属性式抓包文件。走这里的是「按 URL 添加」的路径；内联粘贴那条路
        # 走 parse_channel_list，它内部也会认抓包内容（见 parsers.looks_like_huawei_capture）。
        entries = await asyncio.to_thread(parse_huawei_capture, content)
    else:
        entries = await asyncio.to_thread(parse_m3u, content)
    if not entries:
        raise ValueError("解析不出任何有效条目（地址可能不是直播列表）")
    return {"url": url, "format": detected, "entries": len(entries), "hosts": sorted(hosts)}

# 待执行同步的队列上限。POST /api/sync 不鉴权，若不设上限，反复请求会把 _pending
# 撑成无界列表，而且每一项都会真实执行一次全量同步（17 个源），导致内存持续增长 +
# CPU/网络长期占用。队列满时直接拒绝，返回 409。
MAX_PENDING_SYNCS = 3

logger = logging.getLogger("iptv-hub.sync")



INLINE_DIRNAME = "inline"


def inline_source_path(slug: str) -> Path:
    """本地粘贴导入源的内容文件路径（放在 data/ 下，跟着备份一起走）。"""
    safe = slug.replace("::", "_").replace("/", "_").replace("..", "_")
    return settings.data_dir / INLINE_DIRNAME / f"{safe}.m3u"


def save_inline_source(name: str, content: bytes) -> tuple[str, str]:
    """保存一份「粘贴导入」的清单，返回 (slug, url)。

    slug 由**名称**决定：用同一个名字再粘一次 = 更新同一个源，而不是又建一个。
    内容写进 data/inline/ 的文件，所以它和联网源一样有「来源」，只是抓取那一步变成读文件。
    """
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]
    slug = f"inline::{digest}"
    path = inline_source_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return slug, f"inline://{digest}"



def remove_inline_source(slug: str) -> bool:
    """删掉本地粘贴导入源的内容文件；返回是否真的删了（源不存在时返回 False）。"""
    path = inline_source_path(slug)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _orphan_marker(orphans: list[str]) -> str:
    """「本轮疑似被上游漏掉」的备注：内含孤儿集合的指纹，便于下一轮精确比对。

    带指纹而不是只带数量：数量相同但集合变了（A 恢复了、B 又掉了）不能算同一批。
    方括号里再带上完整 slug 名单：确认轮很可能走「配置未变」路径（304/同哈希），
    那条路径拿不到上游集合，只能靠这里存下的名单认出该删谁。
    """
    ordered = sorted(orphans)
    digest = hashlib.sha1("\n".join(ordered).encode("utf-8")).hexdigest()[:8]
    preview = "、".join(ordered[:3]) + ("…" if len(ordered) > 3 else "")
    # 标记内部不能含「；」：那是 notes 的分段符，含了会在「保留遗留标记」时被截成两段。
    return (
        f"上游配置未列出 {len(ordered)} 个子源（{preview}），疑似内容残缺，"
        f"下一轮仍未列出才清理 [#{digest}|{','.join(ordered)}]"
    )


_ORPHAN_MARK_RE = re.compile(r"\[#([0-9a-f]{8})\|([^\]]+)\]")


def _reconcile_tvbox_children(parent_slug: str, keep_urls: list[str]) -> tuple[int, str]:
    """对账「属于该父配置、但这次 lives 里没再列出」的子源；返回 (删除数, 给 UI 的备注)。

    **两轮确认，不立即删**：上游给出「合法但残缺」的 lives（边缘缓存半截、分批灰度、
    配置正好被截断）时，没列到的子源会被当成孤儿删掉 —— ON DELETE CASCADE 连带删光
    它的条目、同事务的孤儿清理还会删掉测速历史，订阅立刻少内容且要等上游恢复才重建。
    「少了一半」这类比例保护在子源只有两三个时必然退化（int(2*0.2)=0，实测 4 个子源丢 3 个
    照样删），所以用时间维度确认：第一次只把标记写进 `sources.orphan_note`，下一次
    **同一批**孤儿仍未列出才真删。真人删掉一条 lives 只是延后一轮清理，不会永久残留。

    标记存**独立列**而不是 `error_message`：后者会被父源任何一次异常整条覆盖
    （`_record_source_error`），标记一丢，之后每一轮都会被当成"第一次见到"而永不清理；
    而且 `error_message` 有 500 字截断，孤儿一多标记就会被截成无法解析。
    独立列没有这两个问题，也不再需要「配置未变轮」那套特例。

    **同步阻塞操作，调用方必须用 asyncio.to_thread** —— 它内部是 BEGIN IMMEDIATE，
    若写入锁被别的写者占着（例如测速落库），最长会等 busy_timeout(5s)；
    直接放在协程里等于把整个事件循环卡住 5 秒（本该避免的那类问题）。
    """
    if not keep_urls:
        return 0, ""
    placeholders = ",".join("?" for _ in keep_urls)
    with db.write() as conn:
        orphans = [
            row["slug"]
            for row in conn.execute(
                f"SELECT slug FROM sources WHERE parent_slug=? AND url NOT IN ({placeholders})",
                (parent_slug, *keep_urls),
            ).fetchall()
        ]
        row = conn.execute("SELECT orphan_note FROM sources WHERE slug=?", (parent_slug,)).fetchone()
        previous = (row["orphan_note"] if row else "") or ""
        if not orphans:
            if previous:
                conn.execute("UPDATE sources SET orphan_note='' WHERE slug=?", (parent_slug,))
            return 0, ""
        marker = _orphan_marker(orphans)
        if marker != previous:
            logger.warning(
                "TVBox 疑似上游内容残缺：%s 本轮未列出 %s 个子源（%s），本轮只记录不清理",
                parent_slug, len(orphans), "、".join(sorted(orphans)[:5]),
            )
            conn.execute("UPDATE sources SET orphan_note=? WHERE slug=?", (marker, parent_slug))
            return 0, "上游配置未列出 %s 个子源，疑似内容残缺；下一轮仍未列出才清理" % len(orphans)
        for slug in orphans:
            conn.execute("DELETE FROM sources WHERE slug=?", (slug,))
        conn.execute("UPDATE sources SET orphan_note='' WHERE slug=?", (parent_slug,))
        conn.execute(
            "DELETE FROM stream_health WHERE fingerprint NOT IN "
            "(SELECT DISTINCT stream_fingerprint FROM source_entries)"
        )
    logger.info("TVBox 孤儿子源已清理（连续两轮未列出）：%s", "、".join(sorted(orphans)))
    return len(orphans), ""


def select_sync_sources(source_slug: str = "") -> tuple[list[dict], str]:
    """挑出本轮要同步的源（v51）。返回 `(rows, skipped_disabled_slug)`。

    两层过滤：

      * `enabled=1` —— 已停用的源不抓取。**单源同步入口也必须认这一条**：否则用户在
        「数据源」页面手动点一次「同步此源」，就会把停用源的条目全部重建回 `active=1`，
        停用状态等于白设（它的线路又会回到订阅与测速队列里）。
      * TVBox **子源**（slug 以 `tvbox::` 开头且 format<>'tvbox'）—— 它们由父配置同步时
        逐个带过去，这里再当普通源抓一遍就是每轮抓两次（详见 _run_inner 的注释）。

    单源请求一个已停用源时，过滤后列表为空；调用方需要区分「slug 不存在」与「存在但
    已停用」，所以这里顺手把后者查出来返回，由调用方打一行「跳过已停用源」的日志。
    """
    rows = db.rows(
        "SELECT * FROM sources WHERE enabled=1 AND (? = '' OR slug = ?) "
        "AND NOT (format <> 'tvbox' AND slug LIKE 'tvbox::%') ORDER BY priority",
        (source_slug, source_slug),
    )
    skipped = ""
    if source_slug and not rows:
        row = db.row("SELECT enabled FROM sources WHERE slug=?", (source_slug,))
        if row and not int(row["enabled"] or 0):
            skipped = source_slug
            logger.info("跳过已停用源 %s（enabled=0）", skipped)
    elif not source_slug:
        # 全量同步：SQL 里已经把它们滤掉，但日志要能自证「这轮为什么没抓它」——
        # 否则用户看到某个源长期 status 不更新，只能靠翻库才知道是自己停用过。
        disabled = db.rows("SELECT slug FROM sources WHERE enabled=0 ORDER BY priority")
        if disabled:
            logger.info(
                "跳过已停用源 %s 个：%s",
                len(disabled),
                "、".join(row["slug"] for row in disabled[:5]),
            )
    return rows, skipped


class SyncManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._queue_guard = asyncio.Lock()
        self.current_task: asyncio.Task | None = None
        self.current_run_id: str | None = None
        self.current_source_slug: str = ""
        self._pending: list[tuple[str, str, str]] = []
        self._closing = False
        # 「全量同步被去重丢弃」的补偿标志（v42，审计 P1-3）：全量轮一旦开始就已经读完
        # 源列表（见 _run_inner 开头的 SELECT），此后新入库的源不在这一轮里；而 queue()
        # 的等价任务去重会把这个"补一轮"的请求也丢掉 —— 新源就停在 status='never'，
        # 叠加单源队列上限 3 的拒绝，最长要等次日 03:00 才被同步。
        # 这里记下"期间有人请求过全量"（值 = 那次请求的 trigger，空串表示没有待补偿的请求，
        # 留着 trigger 是为了在 sync_runs 里一眼看出补排的那轮是谁触发的），
        # 等当前这一轮彻底结束（队列也空了）再自动补排一轮。
        self._full_dirty = ""
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout, connect=12),
            follow_redirects=False,
            headers={"User-Agent": SOURCE_USER_AGENT},
        )
        # v57：按需创建的「走代理」client，键是代理地址。httpx 0.28 **没有** per-request proxy
        # （AsyncClient.get/send 都没有该参数），只能一个代理一个 client；close() 里统一关闭。
        self._proxy_clients: dict[str, httpx.AsyncClient] = {}

    async def close(self) -> None:
        self._closing = True
        async with self._queue_guard:
            self._pending.clear()
            self._full_dirty = ""
        if self.current_task and not self.current_task.done():
            self.current_task.cancel()
            try:
                await self.current_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        await self.client.aclose()
        # v57：代理 client 也不能漏关，否则每次重启都会漏一条连接池
        for proxy_client in self._proxy_clients.values():
            await proxy_client.aclose()
        self._proxy_clients.clear()

    async def queue(self, trigger: str = "manual", source_slug: str = "") -> tuple[str, bool]:
        """把一次同步加入执行队列；同一时间只跑一个任务，其余按提交顺序依次执行。

        队列长度受 MAX_PENDING_SYNCS 限制，防止未鉴权的 POST /api/sync 被刷出无界积压。
        """
        async with self._queue_guard:
            if self._closing:
                return "", False
            # 去重：等价任务没必要背靠背再排一轮。启动阶段尤其容易叠加 —— 调度补跑、
            # 库空时的 initial、区域策略变更的 region-policy 三个触发源各自独立 queue，
            # 实测一次重启能串上 2~3 轮全量同步（每轮都真抓全部子源并触发整轮测速）。
            # 全量任务（source_slug 为空）：正在跑或已排队有全量就拒绝。
            # 单源任务：只在同 slug 已排队时拒绝 —— 正在跑的全量可能已经越过这个源
            # （比如刚 add 的新源），不能拿它当「已覆盖」。
            if not source_slug:
                for pid, _, pslug in self._pending:
                    if not pslug:
                        logger.info("全量同步已在队列中（run=%s），忽略重复请求 trigger=%s", pid, trigger)
                        return pid, False
                if self.current_run_id and not self.current_source_slug:
                    logger.info(
                        "全量同步正在执行（run=%s），忽略重复请求 trigger=%s",
                        self.current_run_id, trigger,
                    )
                    # 正在跑的这一轮**早已读完源列表**，刚入库的源不在里面：
                    # 记下补偿标志，等它跑完再自动补一轮（见 _full_dirty 的说明）。
                    self._full_dirty = trigger
                    return self.current_run_id, False
            else:
                for pid, _, pslug in self._pending:
                    if pslug == source_slug:
                        logger.info("源 %s 的同步已在队列中（run=%s），忽略重复请求", source_slug, pid)
                        return pid, False
            if len(self._pending) >= MAX_PENDING_SYNCS:
                logger.warning(
                    "同步队列已满(%d)，拒绝 trigger=%s source=%s",
                    len(self._pending), trigger, source_slug or "all",
                )
                # 全量请求被"队列满"挡掉时同样记补偿标志：批量入库场景正是这样 ——
                # 前 3 条新源占满单源队列，收尾的全量请求被拒，不带这个标志的话
                # 这些源要等到次日定时同步才被扫到（v42，审计 P1-3）。
                # 单源请求被拒则不置位：那只是"这一条这次没排上"，收尾的全量请求会兜住。
                if not source_slug:
                    self._full_dirty = trigger
                return "", False
            run_id = uuid.uuid4().hex
            self._pending.append((run_id, trigger, source_slug))
            if not self.current_task or self.current_task.done():
                self._start_next_locked()
            return run_id, True

    def _start_next_locked(self) -> None:
        if self._closing:
            self._pending.clear()
            return
        while self._pending:
            run_id, trigger, source_slug = self._pending.pop(0)
            self.current_run_id = run_id
            self.current_source_slug = source_slug
            self.current_task = self._run_and_next(run_id, trigger, source_slug)
            return

    def _run_and_next(self, run_id: str, trigger: str, source_slug: str) -> "asyncio.Task":
        async def _runner() -> None:
            try:
                await self.run(run_id, trigger, source_slug)
            finally:
                async with self._queue_guard:
                    if self.current_run_id == run_id:
                        self.current_run_id = None
                        self.current_source_slug = ""
                    self._start_next_locked()
                    # _full_dirty 的兑现点（v42，审计 P1-3）：等**队列真的空了**再补排 ——
                    # 若还有待跑的轮次就直接插队，会把 MAX_PENDING_SYNCS 的上限绕过去。
                    # 标志在这里消费掉，避免无限补排。
                    if self._full_dirty and not self._pending and not self._closing:
                        dirty_trigger = self._full_dirty
                        self._full_dirty = ""
                        logger.info("补排一轮全量同步（trigger=%s）：期间有新源入库，上一轮全量已越过源列表", dirty_trigger)
                        self._pending.append((uuid.uuid4().hex, f"{dirty_trigger}+dirty", ""))
                        self._start_next_locked()

        return asyncio.create_task(_runner())

    def _mark_run(self, run_id: str, status: str) -> None:
        """把中断/异常的同步记录落定，避免出现永远「同步中」的僵尸状态。"""
        try:
            with db.write() as conn:
                conn.execute(
                    "UPDATE sync_runs SET status=?, finished_at=? WHERE id=? AND status='running'",
                    (status, utc_now(), run_id),
                )
        except Exception:
            pass

    async def run(self, run_id: str, trigger: str, source_slug: str = "") -> None:
        # 注意：锁由 _run_inner 持有，这里只负责状态落定（asyncio.Lock 不可重入）
        try:
            await self._run_inner(run_id, trigger, source_slug)
        except asyncio.CancelledError:
            # _mark_run 是同步 sqlite 写（BEGIN IMMEDIATE，busy_timeout 5s），
            # 不能在事件循环上直接调 —— 测速落库占着写锁时会把循环卡住
            await asyncio.to_thread(self._mark_run, run_id, "cancelled")
            raise
        except Exception:
            logger.exception("同步任务异常 run=%s", run_id)
            await asyncio.to_thread(self._mark_run, run_id, "error")

    async def _run_inner(self, run_id: str, trigger: str, source_slug: str = "") -> None:
        async with self._lock:
            started = utc_now()
            await asyncio.to_thread(_insert_sync_run, run_id, trigger, source_slug, started)
            # 排除 TVBox **子源**：它们是父配置 lives 里发现的直播列表，父源同步时会
            # 逐个同步一遍；如果这里再当普通源抓一次，每轮就抓两遍（实测 23 个源里有 2 个
            # 子源被重复抓取，items_seen 也重复计数）。父源(format='tvbox')本身要保留。
            # v51 追加一层：`enabled=0` 的源不抓（含单源同步入口）—— 见 select_sync_sources。
            # 第二个返回值（被跳过的停用源 slug）只用于测试断言，日志由该函数自己打。
            sources, _ = await asyncio.to_thread(select_sync_sources, source_slug)
            totals = {"ok": 0, "not_modified": 0, "failed": 0, "seen": 0, "added": 0, "removed": 0}

            # region-policy 触发的同步是「区域策略变了，必须重建存量条目」，
            # 不能被条件请求/内容哈希短路（详见 _sync_source 的说明）
            force = trigger == "region-policy"

            async def _sync_one(source: dict) -> dict[str, Any]:
                try:
                    return await self._sync_source(source, force=force)
                except Exception as exc:
                    # httpx 的超时类异常 str() 是空串，直接存会得到一条「空白错误」，
                    # 界面上看不出任何信息；此时至少记下异常类型名。
                    message = (str(exc)[:500] or type(exc).__name__)[:500]
                    try:
                        await asyncio.to_thread(self._record_source_error, source["slug"], message)
                    except Exception:
                        # 记录失败本身失败（sqlite busy/IO 异常时最可能）不能让它穿出
                        # _guarded：gather 不带 return_exceptions，一个穿出会让其余源的
                        # 同步协程游离 —— run 已标记 error，它们却还在后台写库。
                        logger.exception("记录源失败信息时出错 %s", source["slug"])
                    return {"source": source["slug"], "status": "error", "message": message}

            # 并发抓取（共享同一 AsyncClient 连接池），最坏耗时从「串行和」降为「最慢单个」。
            # 但要限并发：每个源的响应体整段留在内存（上限 max_source_bytes，默认 16MiB），
            # 不限的话 N 个源同时出网的峰值内存就是 N×16MiB，解析任务也会一起挤占线程池。
            sync_sem = asyncio.Semaphore(settings.sync_concurrency)

            async def _guarded(source: dict) -> dict[str, Any]:
                async with sync_sem:
                    return await _sync_one(source)

            details = list(await asyncio.gather(*(_guarded(source) for source in sources)))
            for detail in details:
                state = detail["status"]
                if state == "not_modified":
                    totals["not_modified"] += 1
                elif state == "ok":
                    totals["ok"] += 1
                    totals["seen"] += detail.get("items", 0)
                    totals["added"] += detail.get("added", 0)
                    totals["removed"] += detail.get("removed", 0)
                else:
                    totals["failed"] += 1
            if totals["failed"] == 0:
                status = "succeeded"
            elif totals["ok"] or totals["not_modified"]:
                status = "partial"
            else:
                status = "failed"
            await asyncio.to_thread(_finish_sync_run, run_id, status, totals, details)
            if status in {"succeeded", "partial"}:
                bump_revision()
                if settings.auto_speed_after_sync:
                    await self._auto_maintenance(run_id)
                await _weekly_maintenance()
                await _quarterly_purge()
                if settings.static_export:
                    try:
                        from .static_export import write_static_exports
                        counts = await asyncio.to_thread(write_static_exports)
                        logger.info("静态订阅已更新 data/live.m3u、data/live.txt：%s", counts)
                    except Exception:
                        logger.warning("静态订阅生成失败", exc_info=True)

    async def _auto_maintenance(self, run_id: str) -> None:
        """同步后的自动维护：复活冷却期满的线路 -> 抽样测速 -> 标记失效 -> 达标剔除。

        只在**全量同步**后跑整轮测速：原来每轮同步（含单源）都跑，批量入库 N 个源就会串起
        N 轮全量测速 —— 网络被打满、正常线路被连续判失效（实测出现过一轮 470/470 全失效），
        页面上表现为「一直在测、怎么都测不完」。单源同步的新条目由下次全量同步的维护覆盖。
        """
        from .speed import auto_budget, revive_expired, speed_tester

        if self.current_source_slug:
            logger.info(
                "自动维护：本次是单源同步（%s），跳过整轮测速（新条目由下次全量同步的维护覆盖）",
                self.current_source_slug,
            )
            return
        now_mono = time.monotonic()
        last_mono = getattr(self, "_last_auto_maintenance", 0.0)
        gap = now_mono - last_mono if last_mono else None
        if gap is not None and gap < settings.auto_maintenance_min_interval:
            logger.info(
                "自动维护：距上次仅 %.1f 分钟（最小间隔 %s 分钟），跳过整轮测速",
                gap / 60, settings.auto_maintenance_min_interval // 60,
            )
            return
        self._last_auto_maintenance = now_mono

        revived = await asyncio.to_thread(revive_expired)
        if revived:
            logger.info("自动维护：复活冷却期满线路 %s 条", revived)

        # 预算按存量自适应：固定 1500 条/天跟不上复检周期需求（约 17000 条/天），
        # 会让失效线路把预算吃光、可用线路长期不复检。详见 speed.auto_budget。
        budget = await asyncio.to_thread(auto_budget)
        logger.info(
            "自动维护：本轮预算 %s 条（自适应=%s，下限 %s，上限 %s）",
            budget,
            settings.auto_speed_adaptive,
            settings.auto_speed_limit,
            settings.auto_speed_max,
        )
        try:
            started = await speed_tester.start(
                scope="all",
                channel_key="",
                limit=budget,
                refresh=False,
                region=settings.auto_speed_region if settings.auto_speed_region in {"domestic", "international"} else "",
                cap=settings.auto_speed_max,
            )
        except Exception:
            logger.exception("自动维护：启动测速失败（本轮跳过）")
            return
        if not started:
            # 已有测速任务在跑。本轮不排队重试：新入队条目会在下一次自动维护（或手动测速）
            # 时被覆盖到。这里必须留日志 —— 否则「刚加的源为什么没被测速」完全查不出来。
            logger.warning(
                "自动维护：已有测速任务在运行，本轮跳过（预算 %s 条未执行）；新增/待复检条目将在下次自动维护时处理",
                budget,
            )
            return
        if not speed_tester._task:
            logger.warning("自动维护：测速任务未创建，本轮跳过")
            return
        try:
            await speed_tester._task
        except Exception:
            logger.exception("自动维护：测速任务异常结束")
            return
        progress = speed_tester.progress
        # 整轮全灭的异常告警：正常网络下不可能跑几十上百条一条都不通。实测出现过
        # 「470/470 全失效」——那多半是网络/上游抖动而不是线路真死了。这里只告警、不改判定
        # （不做自动复活/回滚，避免掩盖真实失效），便于事后定位。
        if int(progress.get("done") or 0) >= 30 and int(progress.get("ok") or 0) == 0:
            logger.warning(
                "自动维护：本轮 %s 条全部判定失效（0 条可用），疑似网络/上游异常而非线路真失效",
                progress.get("done"),
            )
        logger.info(
            "自动维护完成 run=%s 测速 %s/%s 可用 %s 失效 %s 跳过 %s 剔除 %s",
            run_id,
            progress.get("done"),
            progress.get("total"),
            progress.get("ok"),
            progress.get("failed"),
            progress.get("blocked"),
            progress.get("pruned"),
        )
        await asyncio.to_thread(
            _save_auto_speed,
            run_id,
            {
                "revived": revived,
                "total": progress.get("total", 0),
                "done": progress.get("done", 0),
                "ok": progress.get("ok", 0),
                "failed": progress.get("failed", 0),
                "blocked": progress.get("blocked", 0),
                "pruned": progress.get("pruned", 0),
                "finished_at": progress.get("finished_at"),
            },
        )
        bump_revision()
        if settings.static_export:
            try:
                from .static_export import write_static_exports
                counts = await asyncio.to_thread(write_static_exports)
                logger.info("测速完成，静态订阅已刷新：%s", counts)
            except Exception:
                logger.warning("静态订阅生成失败", exc_info=True)



    def _client_for(self, url: str) -> tuple[httpx.AsyncClient, bool]:
        """返回 (client, 是否走代理)（v57）。

        走代理时**不能钉 IP**：CONNECT 由代理侧解析域名，`netguard.send_pinned` 的
        「连已校验 IP」在这条路上没有意义（调用方传 `addresses=()` 即原样请求）。
        代价是这一路失去 DNS 重绑定防护 —— 所以名单必须是**运维显式配置**的主机
        （`SOURCE_PROXY_HOSTS`），且仍要过 `ALLOWED_SOURCE_HOSTS` 白名单。
        """
        proxy = settings.proxy_for(url)
        if not proxy:
            return self.client, False
        client = self._proxy_clients.get(proxy)
        if client is None:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(settings.request_timeout, connect=12),
                follow_redirects=False,
                headers={"User-Agent": SOURCE_USER_AGENT},
                proxy=proxy,
            )
            self._proxy_clients[proxy] = client
        return client, True

    async def _open_source(self, url: str, headers: dict[str, str]) -> httpx.Response:
        """打开数据源并手动跟随重定向：每一跳的主机都必须在 ALLOWED_SOURCE_HOSTS 内。

        原实现用 follow_redirects=False，导致 301/302 只会被当成 `HTTP 302` 直接判失败；
        而 `response.url` 永远等于请求地址，白名单校验形同虚设。
        改成手动跟随后，白名单真正生效，源站正常的跳转（含 http→https、末尾补斜杠）
        也不再让整次同步失败。
        """
        current = url
        for _ in range(MAX_SOURCE_REDIRECTS + 1):
            parts = urlsplit(current)
            host = (parts.hostname or "").lower()
            if host not in ALLOWED_SOURCE_HOSTS:
                raise ValueError(f"source host not approved: {host or 'missing'}")
            # 白名单只认主机名；若该域名（或它的 DNS）被劫持到环回/链路本地，这里再挡一层。
            # 只挡受限地址，内网源（192.168.x）照常放行。
            # getaddrinfo 是同步阻塞调用（实测每次 6~64ms），必须丢线程池：这段代码跑在
            # 事件循环里，而同进程还要服务 /api/*，同步解析会把 Web 界面一起卡住。
            # v57：命中 SOURCE_PROXY_HOSTS 的源走代理 —— 解析交给代理侧，这里传 addresses=()
            # 让 pin_target 原样返回 URL，httpx 用 CONNECT 建立隧道；Host/SNI 由 httpx 保持。
            client, proxied = self._client_for(current)
            if proxied:
                response = await send_pinned(client, current, host, (), headers)
            else:
                addresses = await asyncio.to_thread(resolve_addresses, host)
                restricted = restricted_in(addresses)
                if restricted:
                    raise ValueError(f"source host {host} resolves to restricted address {restricted}")
                if not addresses:
                    raise ValueError(f"source host {host} 无法解析")
                # v42（P0-2 DNS 重绑定）：上面那次解析既用于守卫、也用于**钉住连接目标** ——
                # 原先白名单 + restricted_resolution 只是"校验一次"，httpx 建连时会自己再解析
                # 一次，两次之间的 DNS 变化就能让校验时的公网 IP 变成连接时的 127.0.0.1。
                # Host 头与 https 的 sni_hostname 由 netguard.send_pinned 保持原域名语义。
                response = await send_pinned(self.client, current, host, addresses, headers)
            redirected_to = await next_redirect(response, current)
            if redirected_to is None:
                return response
            current = redirected_to
        raise ValueError(f"source redirects exceed {MAX_SOURCE_REDIRECTS} hops")

    async def _sync_tvbox(self, source: dict, force: bool = False) -> dict[str, Any]:
        """TVBox 配置源：抓配置 → 取出 lives 直播列表 → 每个子地址按普通源同步。

        安全边界与单源**完全一致**：每个子地址的主机都必须在 ALLOWED_SOURCE_HOSTS 里。
        不在白名单的子地址一律不抓取，只把主机名写进该源的 error_message，
        由「数据源」页面显示「需放行主机：xxx」——这是**只读提示**，页面上没有放行按钮，
        放行只能由有服务器权限的人改配置，所以安全模型不变。

        配置里新增的直播列表会自动成为一个新的子源（slug 由 URL 哈希得出），
        复用完整的同步/测速/剔除/导出流程，也能在数据源页面单独看到状态。
        """
        headers = {"Accept": "application/json"}
        # **不做条件请求、也不因「内容没变」短路**：配置 304 只说明子源**集合**没变，
        # 而子源内容每轮都要抓（这是它们唯一的同步路径）；跳过解析还会带来一整类隐蔽问题 ——
        # 管理员删掉的子源不会重建、「需放行主机」提示与孤儿标记会丢。配置本身只有几百 KB，
        # 每轮抓一次的代价远小于这些坑（实测：短暂短路过一轮就冻住过全部子源）。
        checked = utc_now()
        http_status = 200
        digest = source["content_sha256"] or ""
        etag = source["etag"] or ""
        last_modified = source["last_modified"] or ""
        async with aclosing(await self._open_source(source["url"], headers)) as response:
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}")
            content = await read_capped_async(
                response, settings.max_source_bytes, "TVBox 配置超过大小上限"
            )
            etag = response.headers.get("etag", "")
            last_modified = response.headers.get("last-modified", "")
        digest = hashlib.sha256(content).hexdigest()
        # 配置最大 16MiB，纯 CPU 的 JSON 解析放线程里跑（与 _sync_source 的 self._parse 同款），
        # 直接在事件循环上解析会把 Web 界面一起卡住。
        lives = await asyncio.to_thread(parse_tvbox_lives, content)
        if not lives:
            raise ValueError("TVBox 配置的 lives 里没有可用的直播列表")

        allowed: list[tuple[str, str]] = []
        pending: list[str] = []
        for name, url in lives:
            host = (urlsplit(url).hostname or "").lower()
            if host in ALLOWED_SOURCE_HOSTS:
                allowed.append((name, url))
            elif host not in pending:
                pending.append(host)

        total_items = total_added = total_removed = 0
        succeeded = 0
        failed = 0
        skipped = 0
        for name, url in allowed:
            existing = await asyncio.to_thread(db.row, "SELECT * FROM sources WHERE url=?", (url,))
            if existing is None:
                sub_slug = "tvbox::" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
                # format='auto'：lives 指向的可能是 m3u / json / 纯文本清单，写死 m3u 会让
                # 清单类子源每次同步都报「解析不出条目」而永久失败（实测 0 条 vs 2 条）。
                # parent_slug 记下所属父配置，便于对账清理配置里已删除的孤儿子源。
                # 用 upsert_source 返回的**实际生效 slug**，而不是本地推算的 sub_slug：
                # 该地址若在查询后被别的写者抢先占用，upsert 会复用到既有行，
                # 再按 sub_slug 查就是 None → _sync_source(None) 抛 TypeError，
                # 被子源异常处理吞成「子源同步失败」而永远不成功。
                effective = await asyncio.to_thread(
                    upsert_source, sub_slug, f"{source['name']} · {name}", url, "auto",
                    source["priority"], source["slug"],
                )
                existing = await asyncio.to_thread(db.row, "SELECT * FROM sources WHERE slug=?", (effective,))
            elif existing["slug"].startswith("tvbox::") and existing["format"] != "tvbox":
                # 已经存在的子源（含从旧版本迁移过来的）：补记父源，否则它永远进不了
                # 下面的孤儿对账。**只认自家子源**（slug 前缀 + format），
                # 用户手动添加的源绝不能被标记成子源，否则会被对账逻辑删掉。
                if (existing["parent_slug"] or "") != source["slug"]:
                    await asyncio.to_thread(set_source_parent, existing["slug"], source["slug"])
            else:
                # 这个地址已经属于普通/手动源：它同时也在顶层源集合里，父源再同步一遍
                # 就是每轮抓两次、items_seen/added 重复计数（正是本函数要避免的重复）。
                # 交给顶层那一轮同步即可，这里只记一条日志。
                skipped += 1
                logger.info(
                    "TVBox 配置里的地址已属于既有源 %s，跳过（由顶层同步负责）：%s",
                    existing["slug"], url,
                )
                continue
            if not int(existing.get("enabled", 1) or 0):
                # v51：父配置里列出的子源若已被停用，同样不抓。少了这一条，停用的 TVBox
                # 子源会随父源每次同步被重新抓取并重建条目 —— 停用状态形同虚设。
                skipped += 1
                logger.info("跳过已停用源 %s（enabled=0，来自 TVBox 配置 %s）", existing["slug"], source["slug"])
                continue
            try:
                # force 必须透传：KEEP_REGION 切换后 region-policy 会强制同步，靠这条
                # 路径重新激活「上一轮策略置 0」的条目；子源只经父源进来，父源吞掉 force
                # 就仍走 304/同哈希短路 → 子源内容未变即返回 not_modified，条目永久不可见。
                detail = await self._sync_source(existing, force=force)
            except Exception as exc:
                logger.warning("TVBox 子源同步失败 %s：%s", url, str(exc)[:150])
                failed += 1
                continue
            if detail.get("status") in {"ok", "not_modified"}:
                succeeded += 1
            total_items += detail.get("items", 0) or 0
            total_added += detail.get("added", 0) or 0
            total_removed += detail.get("removed", 0) or 0

        notes: list[str] = []
        # 孤儿子源对账：父配置里这次**没再列出**的子源（且是我们为这个父源建的）才清理。
        # 否则源站删掉一条 lives 后，旧子源仍留在 sources 表被永久同步，它的条目继续进订阅。
        # 保险一：allowed 为空（配置里所有子主机都没放行、或上游返回了残缺内容）时
        # **不能**执行对账，否则会把全部子源当成孤儿删掉、连带其条目一起离开订阅。
        # 保险二：见 _reconcile_tvbox_children —— 连续两轮未列出才真删。
        if allowed:
            # keep_urls 必须含**本次 lives 列出的全部 URL**（含主机未放行的 pending 部分）：
            # 「放行与否」只决定抓不抓，不决定上游是否还列着它。只传 allowed 的话，
            # 撤销某个主机的放行后，其子源会因「连续两轮未列出」被误删（连带条目离开订阅）。
            _, orphan_note = await asyncio.to_thread(
                _reconcile_tvbox_children, source["slug"], [url for _, url in lives]
            )
            if orphan_note:
                notes.append(orphan_note)
        if pending:
            notes.append("需放行主机：" + "、".join(pending) + "（未放行已跳过，放行需加入 ALLOWED_SOURCE_HOSTS）")
        if failed:
            notes.append(f"{failed} 个子源同步失败")
        # skipped 也算「这个父配置没有失败」：那些地址由顶层同步负责，
        # 全是既有源时 succeeded 会是 0，不能因此把父源标成 error。
        status = "error" if (not allowed or (succeeded == 0 and skipped == 0)) else "ok"
        await asyncio.to_thread(
            _save_tvbox_source, source["slug"], status, checked, etag, last_modified,
            digest, total_items, "；".join(notes), http_status,
        )
        return {
            "source": source["slug"], "status": status, "items": total_items,
            "added": total_added, "removed": total_removed,
            "sub_sources": len(allowed), "pending_hosts": pending,
        }

    async def _sync_inline(self, source: dict) -> dict[str, Any]:
        """本地粘贴导入的源：内容在 data/inline/ 下的文件里，不发起任何网络请求。

        它复用与联网源**完全相同**的落库 / 测速 / 剔除 / 导出流程，
        只是把「抓取」这一步换成「读文件」，所以排序、失效剔除、区域策略全都一致。
        """
        path = inline_source_path(source["slug"])
        if not path.exists():
            raise ValueError(f"本地清单文件缺失（{path.name}）；重新粘贴导入即可恢复")
        content = await asyncio.to_thread(path.read_bytes)
        entries = await asyncio.to_thread(self._parse, "list", content)
        if not entries:
            raise ValueError("清单里没有解析出任何有效条目")
        digest = hashlib.sha256(content).hexdigest()
        checked = utc_now()
        total_count = len(entries)
        # 粘贴清单不带 group-title / country，region_of 只能靠频道名猜，纯 ASCII 名
        # （CCTV‑1、CGTN 等）会被判成 international 并被 KEEP_REGION 丢掉。
        # 用户主动粘贴 = 明确表达「我要这些频道」，所以按当前保留区域归档，不做启发式过滤。
        added, removed = await asyncio.to_thread(
            self._activate_entries, source["slug"], entries, checked, digest, "", "", total_count,
            settings.keep_region if settings.keep_region in {"domestic", "international"} else "",
        )
        return {"source": source["slug"], "status": "ok", "items": total_count, "added": added, "removed": removed}

    async def _sync_source(self, source: dict, force: bool = False) -> dict[str, Any]:
        """同步单个源。

        force=True 用于「必须重建存量条目」的场景（当前是 KEEP_REGION 变更后的强制同步）：
        此时**不发送条件请求头、也不走内容哈希短路**，保证一定会执行到 _activate_entries。
        否则源返回 304（或内容哈希未变）就直接 return，而区域策略已经把条目置成
        inactive —— 条目再也回不来，整个源会从订阅里消失且不会自愈（实测复现过）。
        """
        if source["format"] == "tvbox":
            return await self._sync_tvbox(source, force=force)
        if source["format"] == "inline":
            return await self._sync_inline(source)
        accept = {
            "json": "application/json",
            "auto": "application/x-mpegURL, application/json, text/plain, */*",
        }.get(source["format"], "text/plain, application/x-mpegURL")
        headers = {"Accept": accept}
        # 条件请求头的作用是省流量；但 force 场景下我们**需要**完整内容来重建条目
        if not force:
            if source["etag"]:
                headers["If-None-Match"] = source["etag"]
            if source["last_modified"]:
                headers["If-Modified-Since"] = source["last_modified"]
        checked = utc_now()
        # httpx.Response 本身不支持异步上下文管理器，用 aclosing 保证流被关闭
        async with aclosing(await self._open_source(source["url"], headers)) as response:
            if response.status_code == 304:
                await asyncio.to_thread(_mark_source_not_modified, source["slug"], checked, 304)
                return {"source": source["slug"], "status": "not_modified", "items": source["item_count"]}
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}")
            content = await read_capped_async(
                response, settings.max_source_bytes,
                f"source content exceeds {settings.max_source_bytes} bytes",
            )
            etag = response.headers.get("etag", "")
            last_modified = response.headers.get("last-modified", "")
        digest = hashlib.sha256(content).hexdigest()
        if not force and digest == source["content_sha256"] and source["item_count"] > 0:
            await asyncio.to_thread(
                _mark_source_not_modified, source["slug"], checked, 200, etag, last_modified
            )
            return {"source": source["slug"], "status": "not_modified", "items": source["item_count"], "reason": "same_hash"}
        # 解析（最多两万条）与落库都是同步阻塞的重活，放线程里跑，避免卡住事件循环
        entries = await asyncio.to_thread(self._parse, source["format"], content)
        if not entries:
            raise ValueError("parsed source contains no valid entries")
        if source["item_count"] >= 100 and len(entries) < max(1, int(source["item_count"] * 0.2)):
            raise ValueError(f"source shrank abnormally: {source['item_count']} -> {len(entries)}")
        # 区域保留策略：先基于全量做萎缩检查，再按 KEEP_REGION 过滤后再激活
        total_count = len(entries)
        if settings.keep_region in {"domestic", "international"}:
            entries = [
                e for e in entries
                if region_of(source["slug"], e.country, e.group_title, e.name) == settings.keep_region
            ]
        added, removed = await asyncio.to_thread(
            self._activate_entries, source["slug"], entries, checked, digest, etag, last_modified, total_count
        )
        return {"source": source["slug"], "status": "ok", "items": total_count, "added": added, "removed": removed}

    @staticmethod
    def _parse(source_format: str, content: bytes) -> list[ParsedEntry]:
        if source_format == "m3u":
            return parse_m3u(content)
        if source_format == "json":
            return parse_iptv_org_json(content)
        if source_format == "list":
            return parse_channel_list(content)
        if source_format == "huawei":
            return parse_huawei_capture(content)
        if source_format == "auto":
            # 未知内容自我识别：TVBox 的 lives 既可能是 m3u，也可能是 m3u8 目录、
            # json、甚至「名称,URL」纯文本清单，登记时不写死格式才不会被卡成永久失败。
            return SyncManager._parse(detect_format(content), content)
        if source_format == "tvbox":
            # detect_format 对「带 lives 的 JSON」会返回 tvbox，而它作为**子源内容**
            # 是嵌套配置（本项目不展开嵌套）。给一句能看懂的报错，
            # 而不是让用户看到内部格式名「unknown source format: tvbox」。
            raise ValueError("内容是 TVBox 配置（含 lives），不能作为子源；请直接添加配置地址")
        raise ValueError(f"unknown source format: {source_format}")

    @staticmethod
    def _activate_entries(
        source_slug: str,
        entries: list[ParsedEntry],
        now: str,
        digest: str,
        etag: str,
        last_modified: str,
        total_count: int | None = None,
        region_hint: str = "",
    ) -> tuple[int, int]:
        """激活条目。total_count 为解析出的全量条目数（用于 sources.item_count 记录）。

        注意：同步流程先执行 `UPDATE ... SET active=0 WHERE source_slug=?` 全量置零，
        再对传入（可能已按 KEEP_REGION 过滤）的条目重新激活——未传入的国外条目保持 inactive。

        region_hint 非空时直接作为区域，不再调 region_of 猜：用于**用户手动粘贴**的清单 ——
        那种清单没有 group-title / country，而 region_of 对纯 ASCII 频道名（CCTV‑1、CGTN）
        会判成 international，会把用户明确想看的央视线路默默丢掉。
        """
        def region_for(entry: ParsedEntry) -> str:
            return region_hint or region_of(source_slug, entry.country, entry.group_title, entry.name)
        new_keys = {entry.entry_key for entry in entries}
        with db.write() as conn:
            # old_keys 取该源**全部**键（不分 active）：否则「昨日被剔除、今日被重新激活」
            # 的线路每天都会被计入 items_added，同步历史里的「新增」持续虚增、失去信号意义
            old_keys = {row[0] for row in conn.execute("SELECT entry_key FROM source_entries WHERE source_slug=?", (source_slug,))}
            conn.execute("UPDATE source_entries SET active=0 WHERE source_slug=?", (source_slug,))
            conn.executemany(
                """
                INSERT INTO source_entries(
                    source_slug, entry_key, stream_fingerprint, channel_key, name, normalized_name,
                    tvg_id, logo, group_title, country, language, quality, url, user_agent,
                    referrer, labels, region, active, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(source_slug, entry_key) DO UPDATE SET
                    stream_fingerprint=excluded.stream_fingerprint,
                    channel_key=excluded.channel_key,
                    name=excluded.name,
                    normalized_name=excluded.normalized_name,
                    tvg_id=excluded.tvg_id,
                    logo=excluded.logo,
                    group_title=excluded.group_title,
                    country=excluded.country,
                    language=excluded.language,
                    quality=excluded.quality,
                    url=excluded.url,
                    user_agent=excluded.user_agent,
                    referrer=excluded.referrer,
                    labels=excluded.labels,
                    region=excluded.region,
                    active=1,
                    last_seen_at=excluded.last_seen_at
                """,
                [
                    (
                        source_slug,
                        entry.entry_key,
                        entry.stream_fingerprint,
                        entry.channel_key,
                        entry.name,
                        entry.normalized_name,
                        entry.tvg_id,
                        entry.logo,
                        normalize_group(entry.name, entry.group_title, region_for(entry)),
                        entry.country,
                        entry.language,
                        entry.quality,
                        entry.url,
                        entry.user_agent,
                        entry.referrer,
                        entry.labels,
                        region_for(entry),
                        now,
                        now,
                    )
                    for entry in entries
                ],
            )
            # 尊重剔除态：上面 upsert 对「仍在源里出现」的条目一律 active=1，会把测速
            # 剔除（fail_count 超阈值）的死线路每天重新激活 —— last_fail_at 永远新鲜、
            # 永远够不到 RETEST_AFTER_DAYS 的复活窗口，revive_count 永不累积，
            # 「剔除 -> 14 天复活 -> 季度清理」整条状态机对这类线路（死线路的绝大多数）
            # 形同虚设，且每天白白消耗测速预算。同步后立刻把超阈值的压回 inactive，
            # 生命周期才真正闭环。
            conn.execute(
                """
                UPDATE source_entries SET active=0
                WHERE source_slug=? AND active=1 AND stream_fingerprint IN (
                    SELECT fingerprint FROM stream_health
                    WHERE status IN ('error','unsupported') AND fail_count > ?
                )
                """,
                (source_slug, settings.prune_max_fail),
            )
            conn.execute(
                """
                UPDATE sources SET etag=?, last_modified=?, content_sha256=?, status='ok',
                    last_http_status=200, last_checked_at=?, last_success_at=?, last_changed_at=?,
                    item_count=?, error_message='' WHERE slug=?
                """,
                (etag, last_modified, digest, now, now, now, total_count if total_count is not None else len(entries), source_slug),
            )
        return len(new_keys - old_keys), len(old_keys - new_keys)

    @staticmethod
    def _record_source_error(source_slug: str, message: str) -> None:
        with db.write() as conn:
            conn.execute(
                "UPDATE sources SET status='error', last_checked_at=?, error_message=? WHERE slug=?",
                (utc_now(), message, source_slug),
            )


def _save_tvbox_source(slug: str, status: str, checked: str, etag: str, last_modified: str,
                       digest: str, item_count: int, note: str, http_status: int = 200) -> None:
    with db.write() as conn:
        conn.execute(
            """
            UPDATE sources SET status=?, last_http_status=?, last_checked_at=?,
                last_success_at=CASE WHEN ?='ok' THEN ? ELSE last_success_at END,
                etag=?, last_modified=?, content_sha256=?, item_count=?, error_message=? WHERE slug=?
            """,
            (status, http_status, checked, status, checked, etag, last_modified, digest, item_count, note[:500], slug),
        )


def _insert_sync_run(run_id: str, trigger: str, source_slug: str, started: str) -> None:
    with db.write() as conn:
        conn.execute(
            "INSERT INTO sync_runs(id, trigger, source_filter, status, started_at) VALUES (?, ?, ?, 'running', ?)",
            (run_id, trigger, source_slug, started),
        )


def _finish_sync_run(run_id: str, status: str, totals: dict, details: list) -> None:
    with db.write() as conn:
        conn.execute(
            """
            UPDATE sync_runs SET status=?, finished_at=?, sources_ok=?, sources_not_modified=?,
                sources_failed=?, items_seen=?, items_added=?, items_removed=?, details_json=?
            WHERE id=?
            """,
            (
                status,
                utc_now(),
                totals["ok"],
                totals["not_modified"],
                totals["failed"],
                totals["seen"],
                totals["added"],
                totals["removed"],
                json.dumps(details, ensure_ascii=False),
                run_id,
            ),
        )


def _mark_source_not_modified(source_slug: str, checked: str, http_status: int, etag: str = "", last_modified: str = "") -> None:
    """把源标记为「内容未变化」（304 或内容哈希一致）。

    v42（审计①，时间炸弹）：304 分支原先**漏了 last_success_at**，只更新
    status/last_http_status/last_checked_at/error_message —— 而首页「源异常」告警的谓词
    用的正是 last_success_at（main.py 里 `last_success_at < ?`，"超过 N 天没有成功同步"）。
    生产库里 40/55 个源走 304 路径，最早的一条当时只剩 13.9 小时余量，之后 71.7 小时内
    会全部陆续误报 —— v40 刚修掉的"误报"会以另一种形式回来。
    304 本身就是**同步成功**（内容没变 = 源正常），和下面的"200 + 哈希一致"分支口径一致，
    两处必须都推进 last_success_at。
    """
    with db.write() as conn:
        if http_status == 304:
            conn.execute(
                "UPDATE sources SET status='not_modified', last_http_status=304, last_checked_at=?, "
                "last_success_at=?, error_message='' WHERE slug=?",
                (checked, checked, source_slug),
            )
        else:
            conn.execute(
                """
                UPDATE sources SET status='not_modified', last_http_status=200, last_checked_at=?,
                    last_success_at=?, etag=?, last_modified=?, error_message='' WHERE slug=?
                """,
                (checked, checked, etag, last_modified, source_slug),
            )


def _save_auto_speed(run_id: str, payload: dict) -> None:
    with db.write() as conn:
        conn.execute(
            "UPDATE sync_runs SET auto_speed_json=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), run_id),
        )


def _weekly_maintenance_job() -> dict[str, int]:
    """真正的每周维护动作（同步阻塞，必须在线程里跑）。

    v48 起多一步「长期失效清理」（`speed.cleanup_long_dead_streams`）：
    它只把**连续失效满 AUTO_CLEANUP_DEAD_DAYS 天**的条目置为 active=0（可恢复、不删数据），
    用来替代已被关闭的每轮激进剔除。配置为 0（默认关闭时）时该函数直接返回，
    连查询都不做，行为与加这个特性之前完全一致。

    清理失败绝不影响同步/测速：这里单独 try，只记日志并把 `cleanup_error` 写进返回值。
    """
    stats = db.prune_history(settings.sync_runs_keep, settings.inactive_retention_days)
    db.maintain()
    from .speed import cleanup_long_dead_streams      # 延迟导入，避免模块级循环依赖

    try:
        cleanup = cleanup_long_dead_streams()
        logger.info(
            "长期失效清理（AUTO_CLEANUP_DEAD_DAYS=%s）：候选 %s 条 / 涉及频道 %s 个，"
            "实际停用 %s 条，为 %s 个「只剩判死线路」的频道各保底留 1 条",
            cleanup["days"], cleanup["candidates"], cleanup["channels"],
            cleanup["deactivated"], cleanup["kept_alive_channels"],
        )
        stats["cleanup_dead_days"] = cleanup["days"]
        stats["cleanup_candidates"] = cleanup["candidates"]
        stats["cleanup_deactivated"] = cleanup["deactivated"]
        stats["cleanup_kept_alive_channels"] = cleanup["kept_alive_channels"]
    except Exception as exc:  # noqa: BLE001 - 清理失败不能拖垮维护
        logger.warning("长期失效清理失败（不影响同步与测速）：%s", exc)
        stats["cleanup_dead_days"] = settings.auto_cleanup_dead_days
        stats["cleanup_error"] = 1
    return stats


async def run_maintenance_now() -> dict[str, int]:
    """立即执行一次维护（供 HTTP 手动触发）。"""
    return await asyncio.to_thread(_weekly_maintenance_job)


async def _weekly_maintenance() -> None:
    """每周一次数据库维护：清理历史数据 + VACUUM + WAL checkpoint。

    以 data 目录下的 marker 文件记录上次维护时间（满 7 天才执行），避免每次同步都跑。
    整段都是同步阻塞操作（VACUUM 可能耗时数秒），必须丢到线程里，
    否则会卡住事件循环 —— 这是「更新源时网页假死」的根因之一。
    """
    marker = settings.data_dir / ".maintain_marker"
    try:
        last = datetime.fromisoformat(marker.read_text(encoding="utf-8"))
    except Exception:
        last = None
    now = datetime.now(timezone.utc)
    if last is not None and (now - last).total_seconds() < 7 * 86400:
        return
    try:
        stats = await asyncio.to_thread(_weekly_maintenance_job)
        marker.write_text(now.isoformat(timespec="seconds"), encoding="utf-8")
        logger.info("每周维护完成：%s + VACUUM/checkpoint", stats)
    except Exception as exc:
        logger.warning("每周维护失败（下次同步再试）：%s", exc)


async def _quarterly_purge() -> None:
    """季度清理：永久停用「复活过、复活后仍然失效」的线路。

    与每周维护同样的节流思路，但间隔取 settings.purge_interval_days（默认 90 天），
    marker 文件独立（data/.purge_marker），只有成功执行才写 —— 失败下次同步重试。
    """
    marker = settings.data_dir / ".purge_marker"
    try:
        last = datetime.fromisoformat(marker.read_text(encoding="utf-8"))
    except Exception:
        last = None
    now = datetime.now(timezone.utc)
    if last is not None and (now - last).total_seconds() < settings.purge_interval_days * 86400:
        return
    try:
        from .speed import purge_revived_streams

        result = await asyncio.to_thread(purge_revived_streams, True)
        marker.write_text(now.isoformat(timespec="seconds"), encoding="utf-8")
        if result.get("purged"):
            bump_revision()
            logger.info(
                "季度清理完成：停用复活后仍失效的线路 %s 条（连带停用条目 %s 条）",
                result["purged"], result["deactivated_entries"],
            )
        else:
            logger.info("季度清理完成：没有「复活后仍失效」的线路，无需停用")
    except Exception as exc:
        logger.warning("季度清理失败（下次同步再试）：%s", exc)


sync_manager = SyncManager()


_DISCOVER_LAST_KEY = "discover_last_run"


async def run_discovery_job(reason: str = "scheduled") -> dict:
    """跑一轮「发现」（每天 05:00 定时 / 启动补跑 / 手动触发都走这里）。

    只抓预置的索引页、只把像播放列表的地址记进 `discovered` 表；**绝不自动采用**。
    已经入库的地址在发现阶段就被跳过（见 discover.run_discovery），已处理过的候选不会被
    重新标成新候选 —— 所以同一批地址不会每天重复来打扰。
    """
    from . import discover

    try:
        stats = await discover.run_discovery()
        # 必须是模块级函数 set_meta，不是 db.set_meta —— db 是 Database **实例**，
        # 而 set_meta/get_meta 是 db.py 里的模块级函数。这里原先写成 db.set_meta，
        # 于是每轮发现都在「抓取已完成、写时间戳」这一步抛 AttributeError，被下面的
        # except 吞成 WARNING「发现任务失败」，连带跳过后面的 purge_invalid_discovered：
        # 表现在线上日志里（发现永远记不上 discover_last_run，启动补跑也永远判定不了），
        # 而 meta 表里确实从来没有这个键。两处一起修（另一处见 scheduler_loop 的 get_meta）。
        await asyncio.to_thread(set_meta, _DISCOVER_LAST_KEY, utc_now())
        logger.info(
            "发现完成（%s）：%s 个站点，候选 %s 条（新增 %s，重复跳过 %s，已判死跳过 %s），失败站点 %s",
            reason, stats.get("sites"), stats.get("found"), stats.get("new"),
            stats.get("duplicate"), stats.get("skipped"), stats.get("failed"),
        )
        left = await asyncio.to_thread(purge_invalid_discovered)
        if left:
            logger.info("发现后清理无效候选 %s 条", left)
        return stats
    except Exception as exc:  # noqa: BLE001 - 发现失败绝不能影响同步与测速
        logger.warning("发现任务失败（%s）：%s", reason, exc)
        return {}


async def scheduler_loop() -> None:
    from zoneinfo import ZoneInfo

    try:
        zone = ZoneInfo(settings.timezone)
    except Exception:
        zone = timezone(timedelta(hours=8))

    logger.info(
        "定时任务：同步/测速每天 %02d:%02d，订阅地址发现每天 %02d:%02d",
        settings.sync_hour, settings.sync_minute, settings.discover_hour, settings.discover_minute,
    )

    # 补跑：容器在当天调度点**之后**重启（更新/崩溃恢复）时，今天的同步会被静默跳过，
    # 最长空窗近 48 小时。启动时检查「今天已到调度点、且今天还没有成功的同步」，
    # 满足就先补跑一次再进入常规睡眠循环。
    try:
        now = datetime.now(zone)
        todays_target = now.replace(hour=settings.sync_hour, minute=settings.sync_minute, second=0, microsecond=0)
        if todays_target <= now:
            last_ok = await asyncio.to_thread(
                db.row,
                # 状态值必须是 sync_runs 里真实写入的那两个（本文件写 run 的地方全部成功
                # 写 'succeeded'、有源失败写 'partial'）——**从来没有 'ok'**。
                # 原写法查 status='ok' 永远查不到 → ran_today 恒为 False → 每次容器重启都
                # 补跑一次全量同步+测速（实测两分钟内连续两次重启各触发一轮，白抓 24 个源
                # 并多跑一整轮测速）。
                "SELECT finished_at FROM sync_runs WHERE status IN ('succeeded','partial') "
                "ORDER BY finished_at DESC LIMIT 1",
            )
            # sync_runs 的时间戳是 UTC（utc_now()），与本地时区的今日调度点比较前要先换算；
            # 两者同为 ISO 格式，字典序比较即时间序比较
            target_utc = todays_target.astimezone(timezone.utc).isoformat(timespec="seconds")
            ran_today = bool(last_ok and last_ok["finished_at"] and last_ok["finished_at"] >= target_utc)
            if not ran_today:
                logger.info("已过今日调度点(%02d:%02d)且今日尚无成功同步，补跑一次", settings.sync_hour, settings.sync_minute)
                await sync_manager.queue("scheduled")
    except Exception as exc:
        logger.warning("补跑检查失败（不影响常规调度）：%s", exc)

    # 启动补跑（发现）：容器在 05:00 之后重启时，别把今天的发现轮次整个跳过。
    # 用 meta 记的上一轮时间判断「今天跑过没有」，不额外建表。
    try:
        now = datetime.now(zone)
        discover_target = now.replace(
            hour=settings.discover_hour, minute=settings.discover_minute, second=0, microsecond=0
        )
        if discover_target <= now:
            # 同 set_meta：这里是模块级函数 get_meta，写成 db.get_meta 会让「启动补跑
            # 发现」每次开机都抛 AttributeError → 被下面 except 吞成一条 WARNING，
            # 于是 05:00 之后重启的容器永远不会补跑当天的发现轮次（线上实测复现）。
            last = await asyncio.to_thread(get_meta, _DISCOVER_LAST_KEY)
            last_local = ""
            if last:
                try:
                    last_local = datetime.fromisoformat(last).astimezone(zone).date().isoformat()
                except ValueError:
                    last_local = ""
            if last_local != now.date().isoformat():
                logger.info("已过今日发现时间(%02d:%02d)，补跑一轮发现", settings.discover_hour, settings.discover_minute)
                asyncio.create_task(run_discovery_job("startup-catchup"))
    except Exception as exc:
        logger.warning("发现补跑检查失败（不影响调度）：%s", exc)

    while True:
        now = datetime.now(zone)
        sync_target = now.replace(hour=settings.sync_hour, minute=settings.sync_minute, second=0, microsecond=0)
        if sync_target <= now:
            sync_target += timedelta(days=1)
        discover_target = now.replace(
            hour=settings.discover_hour, minute=settings.discover_minute, second=0, microsecond=0
        )
        if discover_target <= now:
            discover_target += timedelta(days=1)
        # 两个调度点各睡各的：谁先到就先干谁，互不阻塞（发现放后台任务，不占用同步循环）
        target = min(sync_target, discover_target)
        await asyncio.sleep(max(1, (target - now).total_seconds()))
        if target == discover_target:
            asyncio.create_task(run_discovery_job("scheduled"))
        else:
            await sync_manager.queue("scheduled")
