"""订阅地址「发现」模块 —— 只发现、不自动采用。

设计边界（很重要，别扩）：

* **只抓预置的这一批页面**（`SITES`，就是用户列出的那些索引站/项目主页），不递归、不跟随到别的站；
* **只把候选地址记进库**（`discovered` 表），绝不在这里去访问候选地址 —— 因此这一步不存在 SSRF 面；
* 候选要等人在控制台点「采用」，才走 `main.add_source` 那条既有流程：真抓一次校验 → 把实际访问
  成功的主机写进放行白名单 → 入库 → 排队同步。**白名单与安全模型完全不变**；
* 每轮抓取限速（站与站之间 `_DELAY_SECONDS`）、限大小、带正常 UA —— 这些站不是 API，别把人家打疼。

为什么不自动入库：索引站上列出的地址大量是死链、广告位、甚至与你已有源重复的镜像；
每天测速预算只有约 1500 条，垃圾进来会把预算吃光，反而让真正可用的线路轮不到复检。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections.abc import Iterable
from urllib.parse import urljoin, urlsplit

import httpx

from .config import settings
from .db import db, rejected_discovered_urls, upsert_discovered, utc_now
from .masking import mask_source_url
from .netguard import (
    LOOPBACK_HOSTS,
    REDIRECT_STATUSES,
    resolve_addresses,
    restricted_in,
    send_pinned,
)

logger = logging.getLogger("iptv-hub.discover")

# 「检测判死」的墓碑保质期（天）：这段时间内发现轮次不再把该地址写回候选列表。
# 不设保质期就等于永不重试 —— 源站后来修好了也发现不了；设成 0 就等于天天白抓同一批垃圾。
REJECT_RETENTION_DAYS = 30

# 待发现的页面（用户列出的站点，去掉连不上的与重复的；后续要加站点只改这里）
SITES: tuple[tuple[str, str], ...] = (
    ("iptvindex", "https://www.iptvindex.com/"),
    ("12yue", "https://0.12yue.de5.net/"),
    ("yibababa", "https://www.yibababa.com/vod/"),
    ("ibert", "https://m3u.ibert.me/"),
    ("yang-1989", "https://yang-1989.eu.org/"),
    ("iptvs.hacks.tools", "https://iptvs.hacks.tools/"),
    ("iptv-search", "https://iptv-search.com/zh-cn/"),
    ("streamtest", "https://streamtest.in/logs"),
    ("lysvc", "https://lysvc.cc/"),
)

# 站与站之间的间隔：这些站不是给程序用的，慢一点，别被当爬虫封掉
_DELAY_SECONDS = 3.0
_PAGE_MAX_BYTES = 4 * 1024 * 1024
_EXTRACT_LIMIT = 200          # 单页最多提取多少个候选
# 单个页面最多跟几跳重定向（v42，审计 P1-4）。取值与抓取路径（MAX_SOURCE_REDIRECTS=6 次
# 尝试 = 5 跳）同风格但要更紧：索引站正常情况最多一次"补斜杠/http→https"跳转，
# 3 跳足够，多出来的只可能是被利用或站点配置出错。
_MAX_REDIRECTS = 3

# 只收「像播放列表」的地址：扩展名或路径关键字命中其一即可。
# 宁缺毋滥 —— 索引站页面上还有大量频道页、广告、跳转链接，全收进来只会淹没真正的候选。
# 强信号：**多频道列表**的扩展名（`.m3u8` 不算 —— 那是单路流地址，不是"源"）
_LIST_EXT_RE = re.compile(r"\.(m3u|json|txt|xml)(\?|#|$)", re.I)
# 单路流：`.../HBO_2/index.m3u8`、`.../playlist.m3u8` 这类是"一个频道"，收进来只会淹没真正的源
_SINGLE_STREAM_RE = re.compile(r"\.m3u8(\?|#|$)", re.I)
_PLAYLIST_HINT_RE = re.compile(r"(m3u|iptv|playlist|live|tvbox|sub|subscribe)", re.I)
# 文章/博客页的 slug 基本都带百分号编码（中文标题）；页面里泄露的账号密码也不要收
_ARTICLE_RE = re.compile(r"%[0-9a-f]{2}", re.I)
_CREDENTIAL_RE = re.compile(r"(username|password|passwd|token|pass)=", re.I)
# 图片/媒体/前端资源：`assets.hacks.tools/iptv_logo.jpg`、`.flv` 单流这类不是源
_MEDIA_EXT_RE = re.compile(r"\.(jpe?g|png|gif|webp|svg|ico|css|js|flv|ts|mp4|mp3|aac|webm|html?)(\?|#|$)", re.I)
_URL_RE = re.compile(r"""https?://[^\s"'<>()\\，。；]+""", re.I)


def _looks_like_playlist(url: str) -> bool:
    """是不是「值得记下来的候选源」——只看形状，不实际访问。

    实测（首轮真实发现 265 条）三类噪音必须挡掉，否则人工确认会被淹没：
      * **单路流**（`.m3u8`）：184 条里绝大多数是 `.../HBO_2/index.m3u8` 这种单个频道地址；
      * **文章页**：`lysvc.cc/iptv看不了…` 这类博客链接只是因为路径里含 "iptv" 被命中；
      * **别人泄露的账号密码**：`get.php?username=…&password=…&type=m3u_plus` 确实是源，
        但那是页面上泄露的他人凭据，不该由我们收进来。

    v42（审计 P1-5）：查询串里的 `username=`/`password=` 只挡住了"参数式"凭据，
    `http://alice:s3cr3t@host/x.m3u` 这种 **userinfo 式**凭据照样会被收成候选 ——
    然后落库、被 `/api/discovered` 原样回显、还会被 `logger.info("候选检测 %s")`
    打进 docker 日志。userinfo 一律不收（正常订阅地址不该带它）。
    """
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return False
    if parts.username or parts.password:
        return False
    target = parts.path + ("?" + parts.query if parts.query else "")
    if _CREDENTIAL_RE.search(target) or _ARTICLE_RE.search(parts.path):
        return False
    if _MEDIA_EXT_RE.search(target):
        return False
    if _LIST_EXT_RE.search(target):
        return True
    if _SINGLE_STREAM_RE.search(target):
        return False
    # 没有扩展名：只接受「端点形状」的短路径（`/api/xxx`、`/sub`、`/live` 之类），
    # 长路径基本都是文章页
    last = parts.path.rstrip("/").split("/")[-1]
    return bool(last) and len(last) <= 24 and bool(_PLAYLIST_HINT_RE.search(parts.path))


def _extract(text: str, page_url: str) -> list[str]:
    """从页面文本里提取候选地址（含相对路径，按页面地址补全）。不访问、不递归。"""
    found: list[str] = []
    seen: set[str] = set()
    # 先扫绝对地址，再单独处理 href/src 里的相对地址
    candidates = list(_URL_RE.findall(text))
    for m in re.finditer(r"""(?:href|src)\s*=\s*["']([^"']+)["']""", text, re.I):
        raw = m.group(1).strip()
        if raw.startswith(("http://", "https://")):
            candidates.append(raw)
        elif raw.startswith("/") or raw.startswith("./"):
            candidates.append(urljoin(page_url, raw))
    for raw in candidates:
        url = raw.rstrip(".,;)】」'\"")
        if url in seen or not _looks_like_playlist(url):
            continue
        seen.add(url)
        found.append(url)
        if len(found) >= _EXTRACT_LIMIT:
            break
    return found


async def _fetch_page(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """抓一个页面；返回 (页面文本, 错误说明)。抓不到就返回空文本 + 原因。

    v42（审计 P1-4）：原先 client 用 `follow_redirects=True`，等于**每一跳都不过守卫** ——
    索引站（或它的 DNS/上游）一个 302 到 `http://127.0.0.1/…` / 内网地址，响应体就会被
    当成页面文本采进 `discovered` 表（审计已复现）。现在手动逐跳，并且**每一跳**都要求
    目标是公网地址：SITES 是运维写死的固定公网站点，跳到内网/环回只可能是被利用，
    所以这里沿用手工加源路径的口径 `include_private=True`（连内网一起拦）；
    主机名本身先按字面量挡一道（`localhost` 这类 ipaddress 解析不了的写法）。
    其余行为刻意保持不变：4MiB 上限、30s 超时（在 AsyncClient 上）、站点间隔照旧。

    v43（审计 P0-2 在发现模块的最后一处收尾）：逐跳校验原先只是"解析一次做校验"，
    紧接着 `client.stream("GET", current)` 又让 httpx **自己再解析一次** —— 两次解析之间
    DNS 翻转（校验时公网 IP、建连时 127.0.0.1）就能绕过守卫，与 sync.py 修掉的是同一个
    窗口。现在照 `sync._open_source` / `sync.probe_manual_source` 的现成范式：`resolve_addresses`
    的**同一次解析结果**同时用于「判受限」（`restricted_in`）与「钉住连接目标」
    （`netguard.send_pinned`，连已校验 IP + 保留 Host 头 + https 设 sni_hostname），
    httpx 不再解析域名。解析不出来的主机直接拒绝 —— "退回让 httpx 自己解析"正是重绑定的入口。
    """
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        host = (urlsplit(current).hostname or "").lower()
        if not host or host in LOOPBACK_HOSTS:
            return "", f"拒绝的目标主机：{host or 'missing'}"
        # getaddrinfo 是同步阻塞调用，必须丢线程池：发现轮是在事件循环里跑的，
        # 直接同步解析会把同进程的 /api/* 与 /healthz 一起卡住（sync.py 有过同类事故）。
        # 这次解析的地址列表同时用于判受限与钉 IP（P0-2）：守卫与连接目标必须同源。
        addresses = await asyncio.to_thread(resolve_addresses, host)
        blocked = restricted_in(addresses, include_private=True)
        if blocked:
            return "", f"目标解析到受限地址 {host} -> {blocked}，已拒绝"
        if not addresses:
            return "", f"目标无法解析：{host}"
        try:
            response = await send_pinned(client, current, host, addresses)
            try:
                if response.status_code in REDIRECT_STATUSES:
                    location = response.headers.get("location", "")
                    if not location:
                        return "", f"HTTP {response.status_code} 响应缺少 Location 头"
                    # 下一跳回到循环顶部重新校验（相对地址按当前地址补全）
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    return "", f"HTTP {response.status_code}"
                # 必须**边读边判**：send_pinned 给的是流式响应（client.send(..., stream=True)），
                # 先整体 aread() 再判长度等于把整个响应体收进内存，4MiB 上限就形同虚设
                # （索引站可以给一个任意大的响应把它撑爆）。超限直接报错、不截断，
                # 与 netguard.read_capped_async 同一个口径。
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > _PAGE_MAX_BYTES:
                        return "", f"页面超过 {_PAGE_MAX_BYTES} 字节"
                    chunks.append(chunk)
                return b"".join(chunks).decode("utf-8", "replace"), ""
            finally:
                # send_pinned 返回的是流式响应，不是 client.stream 的上下文管理器：
                # 每条出口（正常、超限、非 200、异常）都必须显式关闭，否则连接会一直挂着。
                await response.aclose()
        except Exception as exc:  # noqa: BLE001 - 单个站点失败不能影响整体
            return "", f"{type(exc).__name__}: {str(exc)[:80]}"
    return "", f"跳转次数超过 {_MAX_REDIRECTS}"


async def run_discovery() -> dict:
    """跑一轮发现：逐站抓取 → 提取候选 → 写进 `discovered` 表。返回统计。

    幂等：同一地址再次发现只累加 hits 并更新 last_seen_at；已经采用/忽略过的不会回到 new。
    """
    known_urls = {row["url"] for row in db.rows("SELECT url FROM sources")}
    # 已经处理过的候选（采用/忽略）也算重复：不再当候选，也不重新标成 new
    # 候选表里已经有的一律算重复（无论是待处理还是已处理），一次查完，别在循环里逐条查库
    existing_urls = {row["url"] for row in db.rows("SELECT url FROM discovered")}
    handled_urls = {row["url"] for row in db.rows(
        "SELECT url FROM discovered WHERE state IN ('accepted','ignored')"
    )}
    # 检测判死的地址：保质期内不再当候选（否则同一批垃圾每天重新冒出来、重新检测、重新删）
    rejected_urls = rejected_discovered_urls(REJECT_RETENTION_DAYS)
    stats = {"sites": 0, "failed": 0, "found": 0, "new": 0, "skipped": 0,
             "duplicate": 0, "errors": []}
    timeout = httpx.Timeout(30, connect=12)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; IPTV-Hub-Discovery/1.0)",
        "Accept": "text/html,application/xhtml+xml",
    }
    # follow_redirects=False 是 P1-4 修复的一部分：重定向必须手动逐跳并每跳校验地址
    # （详见 _fetch_page）。用 httpx 的自动跟随会让每一跳都绕过地址守卫。
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers=headers) as client:
        for name, page in SITES:
            text, error = await _fetch_page(client, page)
            stats["sites"] += 1
            if error:
                stats["failed"] += 1
                stats["errors"].append(f"{name}: {error}")
                logger.warning("发现站点抓取失败 %s（%s）：%s", name, page, error)
            else:
                for url in _extract(text, page):
                    if url in known_urls:
                        continue          # 已经在数据源里，没必要当候选
                    if url in rejected_urls:
                        stats["skipped"] += 1   # 已经检测判死过（保质期内），不再重复劳动
                        continue
                    if url in handled_urls or url in existing_urls:
                        # 库里已经有这条候选了（上次抓过、还没处理）：累加 hits 就够了，
                        # 不再重复计入「发现」——「每天对比数据库、重复的跳过」就是这个口径。
                        upsert_discovered(url, page)
                        stats["duplicate"] += 1
                        continue
                    stats["found"] += 1
                    if upsert_discovered(url, page):
                        stats["new"] += 1
            await asyncio.sleep(_DELAY_SECONDS)
    logger.info(
        "发现完成：%s 个站点（失败 %s），候选 %s 条（新增 %s，重复 %s，跳过已判死 %s）",
        stats["sites"], stats["failed"], stats["found"], stats["new"],
        stats["duplicate"], stats["skipped"],
    )
    return stats


def candidate_ref(url: str) -> str:
    """候选地址的**不透明句柄**：sha1 前 12 位。

    为什么需要它（v42，审计 P1-5）：`/api/discovered` 回显的 `url` 现在一律经过
    `mask_source_url` 打码（候选里可能夹带上游凭据），打码串不能再当数据库主键
    （多个候选可能打码成同一串），所以另外给前端一个不含任何凭据的操作句柄 ——
    「检测/采用/忽略」这三个动作带着它回来，服务端再换回真实地址。
    """
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:12]


def resolve_candidate_ref(urls: Iterable[str], ref: str) -> str:
    """在候选地址集合里按句柄找回原地址（找不到返回空串）。

    句柄空间只有 48 位，但候选表规模在千条量级，碰撞概率可以忽略；即便如此也按
    "命中即返回"处理，不会因为碰撞把动作打到别的候选上（最坏是多一次误操作，
    而误操作本身还要再过一遍完整的添加源校验）。
    """
    target = (ref or "").strip().lower()
    if not target:
        return ""
    for url in urls:
        if candidate_ref(url) == target:
            return url
    return ""


def discovered_summary(limit: int = 100) -> dict:
    """给界面用的候选清单：new 优先，其次 ignored/accepted 的历史。

    v42（审计 P1-5）：返回前把每行 `url` 换成 `mask_source_url` 的结果 ——
    候选地址可能带着上游凭据（userinfo / 查询串令牌），界面、浏览器缓存、
    截图与接口日志都不该拿到原文。前端改用 `ref` 做操作句柄（见 candidate_ref）。
    """
    rows = db.rows(
        """
        SELECT url, page, first_seen_at, last_seen_at, hits, state,
               check_status, check_items, check_message, checked_at
        -- 已经在数据源里的地址一律不显示（用户要求）：这类候选点了"采用"也只会拿到
        -- 409「已经添加过了」，留在列表里就是永远入不进去的假待办。
        FROM discovered WHERE state <> 'ignored'
          AND url NOT IN (SELECT url FROM sources)
        ORDER BY (state <> 'new'), (check_status = 'ok') DESC,
                 last_seen_at DESC LIMIT ?
        """,
        (limit,),
    )
    counts = {row["state"]: row["n"] for row in db.rows(
        "SELECT state, COUNT(*) AS n FROM discovered "
        "WHERE url NOT IN (SELECT url FROM sources) GROUP BY state"
    )}
    for row in rows:
        # 先算句柄再打码：句柄必须由**原始地址**算出，否则前端拿它换不回真实地址
        row["ref"] = candidate_ref(row["url"])
        row["url"] = mask_source_url(row["url"])
    return {"items": rows, "counts": counts, "sites": [p for _, p in SITES],
            "checked_at": utc_now()}
