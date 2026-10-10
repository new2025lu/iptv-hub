from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Callable
from urllib.parse import parse_qsl, quote, urljoin, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

import httpx

from .config import settings
from .parsers import parse_channel_list
from .db import (SOURCE_SEEDS, SQLITE_TEMP_DIR, db, delete_source,
                   purge_invalid_discovered, reject_discovered, tvbox_seeds,
                   upsert_source, utc_now)
from .db import set_source_enabled as db_set_source_enabled
from . import static_export
from .revision import bump_revision, current_revision
from .speed import (
    METHOD_VERSION,
    UNSUPPORTED_RETEST_DAYS,
    auto_budget,
    pending_count,
    purged_count,
    purge_revived_streams,
    requeue_newly_trusted,
    restore_purged_streams,
    revive_expired,
    speed_tester,
)
from . import discover
from .db import save_discovered_check, set_discovered_state
from .netguard import LOOPBACK_HOSTS, REDIRECT_STATUSES, SourceUnavailableError, restricted_address
# 展示用脱敏规则搬到了独立模块（v42，审计 P1-5）：discover 也要用它，而 main ← discover
# 是既有导入方向，反向导入会成环；顺带让脱敏规则可以在不装 FastAPI 的环境里单测。
from .masking import mask_source_url
from .sync import (
    ALLOWED_SOURCE_HOSTS_BUILTIN,
    SOURCE_USER_AGENT,
    approve_hosts,
    remove_inline_source,
    save_inline_source,
    load_allowed_hosts,
    probe_manual_source,
    revoke_host,
    run_maintenance_now,
    scheduler_loop,
    sync_manager,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
# 首页模板放在 static 目录**之外**：StaticFiles 会把整个 static 挂出去，
# 之前直开 /static/index.html 能看到带 __APP_VERSION__ 占位符的原始模板
INDEX_FILE = Path(__file__).resolve().parent / "index.html"
logger = logging.getLogger("iptv-hub.main")

# 订阅导出缓存：key -> (expires_at, payload_bytes, media_type, etag)，TTL 内直接复用
_EXPORT_CACHE: dict[tuple, tuple[float, bytes, str, str]] = {}
EXPORT_CACHE_TTL = 60.0
# 缓存键包含请求方传来的 mode/source/region/top，必须限长限容，否则随意组合会撑爆内存。
# 单份 M3U 已到 1.5MB 量级，所以除条数外还按总字节数设上限。
EXPORT_CACHE_MAX = 12
EXPORT_CACHE_MAX_BYTES = 24 * 1024 * 1024
# 单份 payload 实测约 1.8MB，8MB 预算连 5 份都装不下，缓存几乎必然被打穿。
# 24MB 可容纳 ~13 份（容器内存无硬上限、常驻仅 60MB 级，这点开销可以接受）。
_EXPORT_CACHE_LOCK = threading.Lock()
# 导出缓存的 single-flight：key -> 渲染锁。导出端点跑在 FastAPI 线程池里，是真并发；
# TTL 过期或新 key 上 N 个请求会各自全量渲染一遍（实测同一冷 key 8 并发 → 累计 10.77 秒
# CPU、每个请求 6.5~11.2 秒，而单次渲染只要 0.86 秒），多台播放器在同步/测速后同时刷新
# 就能把 NAS CPU 打满十几秒。持锁后**再查一次缓存**，等锁的人直接复用别人刚渲染好的结果。
_EXPORT_RENDER_LOCKS: dict[tuple, threading.Lock] = {}
_EXPORT_RENDER_GUARD = threading.Lock()


def _export_render_lock(key: tuple) -> threading.Lock:
    with _EXPORT_RENDER_GUARD:
        lock = _EXPORT_RENDER_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _EXPORT_RENDER_LOCKS[key] = lock
        return lock


# 同时进行的**不同 key** 导出渲染上限。导出是长纯 Python 循环（持 GIL），单次渲染在飞
# 内存约 25~30MB（1.7 万行行对象 + 2355 条分组表 + 5375 条输出行 + 1.4MB payload）。
# 实测 8 路并发×4 次导出把峰值 RSS 推到 416.8MiB，而容器没有内存上限 —— 这正是
# 「压测后内存停在 300~360MiB」的机制。限成 2 路后峰值约回到 130MiB 级，
# 代价只是「同时刷多个不同订阅」时排队（各自仍是秒级）。
_EXPORT_RENDER_SLOTS = threading.Semaphore(2)

# 导出正文里的 EPG 地址占位符。**不把真实地址放进缓存键**：那个地址是按请求 Host 推断的，
# 于是"换一个 Host 头就是一个新缓存键"，未鉴权者循环换 Host 就能反复触发全量渲染
# （实测单次约 0.86s CPU、1.8MB payload）。改成：缓存里存占位符（ETag 也随之与 Host 无关），
# 响应时按本次请求替换 —— 每个客户端仍拿到**自己能访问的**地址。
EXPORT_TVG_PLACEHOLDER = "__IPTV_HUB_TVG_URL__"

# 「发现」页采用候选时写入的数据源分组名（v40）。抽成常量是因为它有三个读法：
#   1) 数据源页据此分区展示（前端只认字符串本身，值改了就自动跟着变）；
#   2) 导出订阅时，**只由这一组源提供的频道**会以这个字符串作为 group-title 单独成组；
#   3) v43：独立出口 /discovered.m3u、/discovered.txt 固定用这一组（等价 ?group=发现源），
#      改分组名时两个出口跟着一起变，不会出现"出口指向一个已经不存在的组"。
# 手动添加与 TVBox 子源都不传分组（保持空串 = 默认区块 / 原有分组）。
DISCOVERED_SOURCE_GROUP = "发现源"

# EPG 回源状态：非阻塞单飞 + 失败退避。请求线程绝不等待回源（见 epg_proxy）。
_EPG_FETCH_LOCK = threading.Lock()
_EPG_REFRESHING = False           # 是否有后台线程正在回源
_EPG_LAST_ATTEMPT = 0.0           # 上次尝试回源的 monotonic 时刻（退避用）
_EPG_LAST_ERROR = ""              # 最近一次回源失败原因（没缓存时回给播放器）
_EPG_BACKOFF_SECONDS = 60.0       # 失败后 60 秒内不重复回源（原来每次请求都会重试）
_EPG_MAX_REDIRECTS = 3
_EPG_SNAPSHOT_MAX_BYTES = 8 * 1024 * 1024   # 超过它就退回 FileResponse 流式下发，不占内存


# 频道目录「全量有序 channel_key 列表」缓存：key -> (expires_at, keys)。
# 见 _channel_keys_all() 的说明：阶段 1 的全量聚合是这条接口 90% 以上的开销，
# 而它只依赖 (筛选条件, 排序, 数据版本)。
_CHANNEL_KEYS_CACHE: dict[tuple, tuple[float, list[str]]] = {}
_CHANNEL_KEYS_LOCK = threading.Lock()
# 阶段 1 聚合的 single-flight 计算锁（见 _channel_keys_all）：缓存 miss 时串行化计算
_CHANNEL_KEYS_COMPUTE_LOCK = threading.Lock()
CHANNEL_KEYS_CACHE_MAX = 6
CHANNEL_KEYS_TTL = 60.0

# /api/stats 与 /api/sources 的缓存 TTL（版本号变化即失效，TTL 只兜底）
STATS_TTL = 5.0


def require_admin(request: Request) -> None:
    if not settings.admin_token:
        return
    supplied = request.headers.get("authorization", "")
    expected = f"Bearer {settings.admin_token}"
    # compare_digest 对含非 ASCII 的 str 会抛 TypeError（请求头是任意字节按 latin-1 解出的），
    # 于是 `Authorization: Bearer 中文` 这种探测会得到 500 + 栈信息而不是 401。
    # 先编成 bytes 再比，长度不同也照样是恒定时间比较。
    if not secrets.compare_digest(supplied.encode("utf-8", "surrogateescape"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="需要管理员令牌")


def _mark_interrupted_runs() -> None:
    """清理历史僵尸同步记录（容器重启导致永远「同步中」的记录）。"""
    with db.write() as conn:
        conn.execute("UPDATE sync_runs SET status='interrupted', finished_at=? WHERE status='running'", (utc_now(),))


def _update_region_policy_marker() -> bool:
    """比对 KEEP_REGION 与上次记录值（data/.region_policy），不同则更新记录并返回 True。

    .env.example 承诺「改动后启动时检测到变化并自动排队一次强制同步，重建存量条目」，
    这里就是这个检测：记录文件缺失视为首次运行（不算变化，全新部署有 initial 同步兜底）；
    之后任何方向的切换（含改成留空=恢复全部区域）都会返回 True，由 lifespan 排队强制同步。
    """
    marker = settings.data_dir / ".region_policy"
    current = (settings.keep_region or "").strip().lower()
    try:
        previous = marker.read_text(encoding="utf-8").strip().lower()
    except OSError:
        previous = None
    if previous == current:
        return False
    try:
        marker.write_text(current, encoding="utf-8")
    except OSError:
        return False
    return previous is not None


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 应用自己的 logger（iptv-hub.sync / iptv-hub.speed / iptv-hub.main）此前没有任何
    # handler，INFO 级日志被静默丢弃 —— 出问题时 docker logs 里只有 uvicorn 的启停信息，
    # 同步与自动维护的过程完全不可见（排查「加源后为什么没测速」时就被这一点挡住）。
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx/httpcore 在 INFO 级会为**每一次请求**打一行日志。测速一轮几千条，
    # 会把这些噪音刷满日志文件、并把应用自己的日志淹掉（实测 10 分钟 87 行全是它），
    # 因此只放行到 WARNING —— 连接层真正的异常仍然看得到。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # 初始化都是同步阻塞的重活（建表/迁移、区域回填、分组归一化要遍历全部活跃条目），
    # 放线程里跑，避免启动阶段长时间占住事件循环
    await asyncio.to_thread(db.initialize)
    # 启动第一行就把**实际使用的库路径**与规模打出来。DATA_DIR 默认 /data，任何忘了设
    # DATA_DIR 的实验脚本都会静默操作生产库（本项目真实踩过：一次复核实验把生产库清成
    # 3 条测试数据）；日志里第一时间能看到路径，就能立刻发现打错了库。
    stats = await asyncio.to_thread(
        db.row,
        "SELECT (SELECT COUNT(*) FROM sources) AS sources, "
        "(SELECT COUNT(*) FROM source_entries WHERE active=1) AS active",
    ) or {"sources": 0, "active": 0}
    logger.info(
        "数据库 %s（数据源 %s 个，活跃条目 %s 条）",
        settings.database_path, stats["sources"], stats["active"],
    )
    # 把「数据源」页面手动放行的主机载入内存白名单（内置白名单在 sync.py 里写死）
    allowed = await asyncio.to_thread(load_allowed_hosts)
    if allowed:
        logger.info("已载入手动放行的数据源主机 %s 个", allowed)
    # v48：blocked / unsupported 的复检窗口拉长到 RETEST_AFTER_DAYS 之后，用户往
    # SPEED_ALLOW_PRIVATE_HOSTS（或数据源白名单）里补一台内网主机时，对应的 blocked 行
    # 可能还要等很久才会被重测。改 .env 必须 --force-recreate，所以把「名单生效」
    # 与「重新排队」绑在启动上：只清 checked_at，不碰 status/fail_count，幂等。
    requeued = await asyncio.to_thread(requeue_newly_trusted)
    if requeued:
        logger.info("放行名单变更：%s 条 blocked 线路已重新排队等待复测", requeued)
    await asyncio.to_thread(_mark_interrupted_runs)
    # 延时排队同步的后台任务引用（关闭时统一取消）
    background_tasks: list[asyncio.Task] = []
    # 区域保留策略：先检测 KEEP_REGION 是否变化（变化需强制同步重建存量），再应用当前策略
    policy_changed = await asyncio.to_thread(_update_region_policy_marker)
    pruned_by_policy = await asyncio.to_thread(db.apply_region_policy)
    if pruned_by_policy:
        bump_revision()
    if policy_changed:
        # KEEP_REGION 被改（含改成留空恢复全部区域）：排队一次强制同步，
        # 同步流程会按新策略重新激活条目，不用等到下一次每日计划同步
        async def resync_after_policy_change() -> None:
            await asyncio.sleep(settings.startup_sync_delay)
            await sync_manager.queue("region-policy")
        background_tasks.append(asyncio.create_task(resync_after_policy_change()))
    # 频道分组归一化：央视 / 地方台 / 其他（幂等，仅更新历史数据）
    if await asyncio.to_thread(db.normalize_groups):
        bump_revision()
    scheduler = asyncio.create_task(scheduler_loop())
    # 阻塞查询放线程里（lifespan 是协程，别在事件循环上直接查库）
    active = await asyncio.to_thread(
        db.row, "SELECT COUNT(*) AS count FROM source_entries WHERE active=1"
    )
    if not active or active["count"] == 0:
        async def initial_sync() -> None:
            await asyncio.sleep(settings.startup_sync_delay)
            await sync_manager.queue("initial")
        background_tasks.append(asyncio.create_task(initial_sync()))
    try:
        yield
    finally:
        scheduler.cancel()
        # 延时排队同步的游离任务也要取消：否则容器在 delay 窗口内停止时，
        # 任务会在 sync_manager.close() 之后才尝试 queue()
        for task in background_tasks:
            task.cancel()
        await sync_manager.close()


app = FastAPI(title=settings.app_name, version=settings.app_version, docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data: https:; style-src 'self'; script-src 'self'; connect-src 'self'; media-src 'none'; object-src 'none'; frame-ancestors 'none'"
    path = request.url.path
    if path.startswith("/static") or path == "/" or path.startswith("/api"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.get("/healthz")
def healthz():
    try:
        row = db.row("SELECT COUNT(*) AS count FROM sources")
        return {"status": "ok", "sources": row["count"] if row else 0}
    except Exception:
        # 不回显异常原文：这是唯一一处把未处理异常直接返回给调用方的地方，而 /healthz
        # 永远可用（docker healthcheck 依赖它）。sqlite3 的 OperationalError 会带上
        # 「no such table / unable to open database file: /data/... 」这类内部状态与路径，
        # 对未鉴权的局域网调用者属于免费的信息泄露。细节只进日志。
        logger.exception("/healthz 数据库检查失败")
        return JSONResponse(status_code=503, content={"status": "error", "detail": "database unavailable"})


@app.get("/api/meta")
def meta():
    return {
        "name": settings.app_name,
        "version": app.version,
        "auth_required": bool(settings.admin_token),
        "timezone": settings.timezone,
        "schedule": f"{settings.sync_hour:02d}:{settings.sync_minute:02d}",
        "syncing": bool(sync_manager.current_task and not sync_manager.current_task.done()),
        "current_run_id": sync_manager.current_run_id,
        "speed_testing": speed_tester.busy,
        # SQLite 临时文件目录：应为数据盘（如 /data/.sqlite-tmp）；若为 /tmp 说明回退到了 64MB tmpfs
        "sqlite_temp_dir": str(SQLITE_TEMP_DIR) if SQLITE_TEMP_DIR else "unavailable",
        # v42 导出过滤：开关与阈值（默认隐藏判死线路；详见 static_export._drop_dead_lines）
        "export_hide_dead": bool(settings.export_hide_dead),
        "export_dead_fail_count": settings.export_dead_fail_count,
        # v43 主订阅默认排除的数据源分组。两个值分开给，便于对账：
        #   main_exclude_group = **生效值**（默认导出真正会排除的那一组；'' = 不排除）
        #   main_exclude_group_configured = .env 里的原文（写错/该组已不存在时与生效值不同，
        #   一眼就能看出"我配了但没生效"，不用去翻日志）。
        "main_exclude_group": static_export.main_exclude_group(),
        "main_exclude_group_configured": settings.main_exclude_group,
        "maintenance": {
            "auto_speed_after_sync": settings.auto_speed_after_sync,
            "auto_speed_limit": settings.auto_speed_limit,
            "auto_speed_adaptive": settings.auto_speed_adaptive,
            "auto_speed_max": settings.auto_speed_max,
            "auto_speed_region": settings.auto_speed_region,
            "auto_prune_after_speed": settings.auto_prune_after_speed,
            "prune_max_fail": settings.prune_max_fail,
            "retest_after_days": settings.retest_after_days,
            "fail_retest_hours": settings.fail_retest_hours,
            "retest_ok_hours": settings.retest_ok_hours,
            "purge_interval_days": settings.purge_interval_days,
            "purge_min_revive": settings.purge_min_revive,
            # v48：长期失效清理。0 = 关闭（此时行为与加这个特性之前完全一致）。
            # 开启后，连续失效满 N 天的条目会在每周维护里被置为 active=0（可恢复，
            # 每频道保底留 1 条）；判据与首轮 dry-run 数字见 MERGED-CHANGES.md 的 v48 一节。
            "auto_cleanup_dead_days": settings.auto_cleanup_dead_days,
            # v48：两类「没测成」的复检窗口（小时）。blocked 用 retest_after_days*24，
            # unsupported 用它自己的长档（speed.UNSUPPORTED_RETEST_DAYS）。
            "blocked_retest_hours": settings.retest_after_days * 24,
            "unsupported_retest_hours": UNSUPPORTED_RETEST_DAYS * 24,
        },
    }


# 按「数据版本 + TTL」缓存的只读聚合结果。见 _cached() 的说明。
_REV_CACHE: dict[str, tuple[tuple[int, float], object]] = {}
_REV_CACHE_LOCK = threading.Lock()
_REV_CACHE_COMPUTE_LOCK = threading.Lock()


def _cached(key: str, ttl: float, compute: Callable[[], object]) -> object:
    """按数据版本缓存的只读聚合（带 single-flight）。

    这类端点的开销全在数据库聚合上（/api/stats 实测 0.81~0.95s CPU、/api/sources
    0.28s），而前端每次打开页面、每次 refreshAll 都会调一遍，返回的却全是纯数据库
    计数、不含任何进程内实时状态。任何会改变内容的动作都会 bump_revision()
    （同步、测速落库、剔除/复活、收藏、加删源），所以「版本号变化」就是精确的失效
    信号，TTL 只作兜底。进程内的实时状态（是否正在同步/测速）在 /api/meta 里，
    那个**不缓存**，前端靠它判断进度。
    """
    now = time.monotonic()
    rev = current_revision()
    with _REV_CACHE_LOCK:
        hit = _REV_CACHE.get(key)
    if hit and hit[0][0] == rev and hit[0][1] > now:
        return hit[1]
    with _REV_CACHE_COMPUTE_LOCK:
        # 等锁期间别人可能已经算好（single-flight，避免 N 个并发各算一遍）
        now = time.monotonic()
        with _REV_CACHE_LOCK:
            hit = _REV_CACHE.get(key)
        if hit and hit[0][0] == rev and hit[0][1] > now:
            return hit[1]
        value = compute()
        with _REV_CACHE_LOCK:
            _REV_CACHE[key] = ((rev, time.monotonic() + ttl), value)
        return value


@app.get("/api/stats", dependencies=[Depends(require_admin)])
def stats():
    """运行概览的各项计数（按数据版本缓存）。"""
    return _cached("stats", STATS_TTL, _compute_stats)


def _compute_stats():
    counts = db.row(
        """
        -- 原写法是 4 个 COUNT(DISTINCT ...) 一起算，每个都要建临时索引；
        -- 改成各自走 DISTINCT 子查询，复用索引顺序，实测 206ms -> 135ms
        SELECT
            (SELECT COUNT(*) FROM source_entries WHERE active=1) AS raw_entries,
            (SELECT COUNT(*) FROM (SELECT DISTINCT channel_key FROM source_entries WHERE active=1)) AS channels,
            (SELECT COUNT(*) FROM (SELECT DISTINCT stream_fingerprint FROM source_entries WHERE active=1)) AS streams,
            (SELECT COUNT(*) FROM (
                SELECT DISTINCT channel_key FROM source_entries WHERE active=1 AND region='domestic'
            )) AS domestic_channels,
            (SELECT COUNT(*) FROM (
                SELECT DISTINCT channel_key FROM source_entries WHERE active=1 AND region='international'
            )) AS international_channels
        """
    ) or {"raw_entries": 0, "channels": 0, "streams": 0, "domestic_channels": 0, "international_channels": 0}
    favorite = db.row("SELECT COUNT(*) AS count FROM favorites") or {"count": 0}
        # 必须限定「条目仍然 active」：stream_health 里会有条目已 inactive/消失的孤儿记录
    # （实测 7092 行只被 active=0 的条目引用），不筛的话 tested_streams 会大于总线路数，
    # 面板出现「已测速线路 > 总线路」这种自相矛盾的数字。
    tested = db.row(
        """
        SELECT COUNT(*) AS count FROM stream_health
        WHERE status='ok' AND fingerprint IN (SELECT DISTINCT stream_fingerprint FROM source_entries WHERE active=1)
        """
    ) or {"count": 0}
    # 用 COUNT(DISTINCT stream_fingerprint)：与 /api/channels/{key} 的 dead_streams 同口径。
    # 原来数的是 source_entries 行数（同一条流挂在多个源/频道上会重复计），
    # 导致汇总数 6901 比各频道累加值 6160 高约 12%。
    dead = db.row(
        """
        SELECT COUNT(DISTINCT stream_fingerprint) AS count FROM source_entries
        WHERE active=1 AND stream_fingerprint IN (
            SELECT fingerprint FROM stream_health WHERE status IN ('error', 'unsupported')
        )
        """
    ) or {"count": 0}
    # 「已剔除」必须只数**因连续失败被剔除**的条目，而不是所有 active=0 的行：
    # 后者把「区域策略过滤掉的国外线路」和「源站已不再列出的条目」也算进来 —— 实测
    # 面板显示 38956，而真正被剔除的是 0（当前没有任何指纹达到 fail_count>3）。
    # 与 dead/flagged 一样按指纹判定，口径才对得上「已从列表与订阅移除」这句说明。
    pruned = db.row(
        """
        SELECT COUNT(DISTINCT e.stream_fingerprint) AS count
        FROM source_entries e JOIN stream_health h ON h.fingerprint=e.stream_fingerprint
        WHERE e.active=0 AND h.fail_count > ?
        """,
        (settings.prune_max_fail,),
    ) or {"count": 0}
    flagged = db.row(
        """
        SELECT COUNT(DISTINCT stream_fingerprint) AS count FROM source_entries
        WHERE active=1 AND stream_fingerprint IN (
            SELECT fingerprint FROM stream_health
            WHERE status IN ('error', 'unsupported') AND fail_count BETWEEN 1 AND ?
        )
        """,
        (settings.prune_max_fail,),
    ) or {"count": 0}
    # 源级失效告警：上游悄悄挂掉/被反爬/换地址时，光看数据源页容易漏（这些都是"看起来正常但
    # 内容早就停止更新"的源）。判据只有两条，都能从 sources 表直接算出来：
    #   1) 状态是 error（最近一轮同步失败）；
    #   2) 距最后一次成功同步超过 SOURCE_STALE_DAYS 天（含从未成功过）。
    # v40 修误报：原判据 `status='error' OR COALESCE(last_success_at,'')='' OR last_success_at < ?`
    # 会把**刚入库、还没轮到同步**的新源（status='never'、last_success_at 为 NULL）算成异常 ——
    # 实测批量入库后首页红字报「8 个数据源异常」，其中 7 个是 status='never' 的新源，
    # 只有一个源（从这台机器连不上它的上游）是真故障。
    # 现在明确排除 status='never'：它表示"等待首次同步"，不是故障；首次同步跑完必然落入
    # ok / not_modified / error 之一，所以「这个源到底行不行」不会因此被永久漏掉。
    # 为什么不加"创建时间宽限"：sources 表没有创建时间列（never 源的 last_checked_at 也是 NULL），
    # 要按"卡了多久"宽限就得再加一列并回填，换来的只是"入库后长时间没轮到的源也报警"，
    # 而同步队列每轮都会把这些源扫一遍 —— 收益极小、改动面却变大，因此只做排除；
    # 等待首次同步的数量由下面的 pending_first_sync 单独报给界面做中性提示。
    # 注意 `last_success_at < ?` 对空串同样成立（'' 在字符串序里最小），所以"超过 N 天没成功"
    # 这一条本来就命中 never 源，必须靠 status <> 'never' 一起挡住，不能只删 COALESCE 那半句。
    # v58（停用即静默）：上面两条判据都只问"这个源行不行"，没问"运维还要不要它"。
    # 被**主动停用**的源（`enabled=0`，例如某个源的上游令牌失效后人工停用）
    # 会被这两条一直算成异常 —— 首页红字永远消不掉，而"停用"本身就是运维已经做过的处置。
    # 实测：停用后 `/api/stats` 仍报 1 个异常源，观感就像停用没生效。
    # 因此加 `enabled=1`：**告警只谈启用中的源**。下面的 pending_first_sync 取同一口径
    # （停用的新源同样不算"待首次同步"），否则"真异常数 + 待首次同步数"两个数字会互相矛盾。
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(days=settings.source_stale_days)).isoformat(timespec="seconds")
    alert_rows = db.rows(
        """
        SELECT slug, name, status, last_success_at, substr(COALESCE(error_message, ''), 1, 80) AS reason
        FROM sources
        WHERE enabled=1
          AND (status = 'error'
               OR (status <> 'never' AND (COALESCE(last_success_at, '') = '' OR last_success_at < ?)))
        ORDER BY COALESCE(last_success_at, ''), name
        """,
        (stale_cutoff,),
    )
    for row in alert_rows:
        row["stale_days"] = settings.source_stale_days
    # 等待首次同步的源数量（status='never'）：不是故障，但界面要能说明"新源还没核对"，
    # 否则刚批量入库的源在概览页完全不可见。口径与上面的排除条件互补
    # （真异常数 + 待首次同步数 = 所有还没确认正常的源），两边永远对得上。
    # v58：同样只看**启用中**的源 —— 停用源既不报警、也不算"待核对"。
    pending_first_sync = db.row("SELECT COUNT(*) AS count FROM sources WHERE enabled=1 AND status='never'") or {"count": 0}
    latest = db.row("SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT 1")
    # 测速欠账：从没测过 + 用旧测量方法测的（算法升级后旧数据会自动进入这个队列等待重测）。
    # 必须校验「对应条目仍然 active」——stream_health 里会有条目已 inactive/消失的孤儿记录，
    # 只按 method 计数会把它们算成欠账，导致欠账永远清不到 0。
    backlog = db.row(
        """
        SELECT
            (SELECT COUNT(*) FROM stream_health h
              WHERE COALESCE(h.method, '') <> ?
                AND EXISTS (SELECT 1 FROM source_entries e
                             WHERE e.active=1 AND e.stream_fingerprint=h.fingerprint)) AS outdated_method,
            (SELECT COUNT(*) FROM source_entries e
               LEFT JOIN stream_health h ON h.fingerprint=e.stream_fingerprint
              WHERE e.active=1 AND h.fingerprint IS NULL) AS never_tested
        """,
        (METHOD_VERSION,),
    ) or {"outdated_method": 0, "never_tested": 0}
    daily = max(1, auto_budget())
    # 真实队列长度（与选测速目标同口径）；下面的两个分项只作诊断用，合起来并不等于它
    # 同步调用：stats 本身是同步端点，FastAPI 会在线程池里跑，阻塞查询不该挪到事件循环上
    pending = pending_count()
    return {
        **counts,
        "favorites": favorite["count"],
        "tested_streams": tested["count"],
        "dead_streams": dead["count"],
        "flagged_streams": flagged["count"],
        "pruned_entries": pruned["count"],
        "purged_streams": purged_count(),
        "source_alerts": {
            "count": len(alert_rows),
            "stale_days": settings.source_stale_days,
            # 中性字段（v40）：等待首次同步的新源数量。前端在 count=0 且它非 0 时显示灰色
            # 提示，而不是走红色 showError —— 这些源只是还没轮到同步，不是坏了。
            "pending_first_sync": pending_first_sync["count"],
            "items": alert_rows[:12],      # 界面只展示前若干条，完整列表在「数据源」页
        },
        "latest_sync": latest,
        "speed_backlog": {
            "method": METHOD_VERSION,
            "pending": pending,
            "outdated_method": backlog["outdated_method"],
            "never_tested": backlog["never_tested"],
            "daily_budget": daily,
            "days_to_clear": (pending + daily - 1) // daily,
        },
    }


@app.get("/api/sources", dependencies=[Depends(require_admin)])
def sources():
    """数据源列表，附带每个源当前的线路可用情况（一眼看出哪个源这次不顶用）。

    **需要管理令牌**（设置了 `IPTV_ADMIN_TOKEN` 才真正生效）：这里的 `url` 字段是上游订阅
    地址，属于"可再生凭据"（拿到就能重新拉一份新列表）。令牌为空时 `require_admin` 直接
    放行，行为与改动前**完全一致**（局域网信任模式）。界面上的令牌入口在右上角状态处
    （「可信局域网模式 / 需要管理令牌」），填一次存在浏览器本地，之后所有接口自动带上。

    按数据版本缓存：这条查询要对 2.4 万条活跃条目按源做两种 DISTINCT 聚合，
    实测单次 0.28s CPU，而前端每次 refreshAll 都会调它 —— 是 refreshAll 里
    剩下最大的一笔开销。返回的同样是纯数据库聚合，无实时状态。

    v51：每条带 `enabled`（1=启用，0=已停用；切换见 `POST /api/sources/{slug}/enable`）。
    字段来自 `SELECT s.*`，停用的源仍然出现在列表里（源行保留是这一机制的设计前提：
    它的条目数、历史状态都要能看见，只是不再抓取、不再测速）。
    """
    return _cached("sources", STATS_TTL, _compute_sources)


def _compute_sources():
    rows = db.rows(
        """
        SELECT s.*,
               COALESCE(a.active_entries, 0) AS active_entries,
               COALESCE(a.playable_streams, 0) AS playable_streams,
               COALESCE(a.dead_streams, 0) AS dead_streams
        FROM sources s
        LEFT JOIN (
            SELECT e.source_slug,
                   COUNT(*) AS active_entries,
                   COUNT(DISTINCT CASE WHEN h.status IS NULL OR h.status='ok'
                                       THEN e.stream_fingerprint END) AS playable_streams,
                   COUNT(DISTINCT CASE WHEN h.status IN ('error', 'unsupported')
                                       THEN e.stream_fingerprint END) AS dead_streams
            FROM source_entries e
            LEFT JOIN stream_health h ON h.fingerprint=e.stream_fingerprint
            WHERE e.active=1
            GROUP BY e.source_slug
        ) a ON a.source_slug = s.slug
        ORDER BY s.priority
        """
    )
    # 在这里（缓存之前）脱敏：缓存里存的也是脱敏后的地址，不存在"某条路径漏出去"的可能。
    # 同步逻辑用的是数据库里的原始地址，不受影响。
    for row in rows:
        if "url" in row:
            row["url"] = mask_source_url(row["url"])
    return rows


@app.post("/api/sources", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_admin)])
async def add_source(
    url: Annotated[str, Query(min_length=1, description="数据源地址")],
    name: str = "",
    format: str = "",
    group: str = "",
):
    """手动添加一个数据源。

    流程与内置源完全一致，只是把「有效性检测」提前到入库之前：

      1. 真抓一次源并解析 —— 抓不到 / 解析不出条目就直接拒绝，不留下半个源；
      2. 把这次抓取**实际访问成功过的主机**（含重定向跳转）写进放行白名单；
      3. 写入源行，并排队一次「只同步这个源」的任务；
      4. 同步完成后自动维护会接着测速，测通的线路随即进入总源（订阅导出）。

    所以加完源不需要再手工点测速，等同步记录里这一轮跑完即可。

    `group`（v40）是可选的数据源分组：只有「发现」页的采用/批量入库会传
    DISCOVERED_SOURCE_GROUP，手动添加留空。它经 upsert_source 落库，
    最终同时影响数据源页的分区与订阅导出的 group-title。
    """
    url = url.strip()
    name = (name or "").strip()
    fmt = (format or "").strip().lower()
    # 分组名走查询串，先收敛再落库：它会原样出现在导出订阅的 group-title 里，
    # 不限长的话一个超长/带换行的 group 就能污染播放器的分组列表（M3U 的转义在渲染层做，
    # 这里只管长度与首尾空白）。空串 = 不分组，语义与旧版本完全一致。
    group = (group or "").strip()[:64]
    if fmt and fmt not in {"m3u", "json", "tvbox", "list"}:
        raise HTTPException(
            status_code=400,
            detail="format 只能是 m3u / json / tvbox / list，留空自动识别",
        )
    # 重复地址：语义仍是 409（一行都不写），但把**库内已有的那条源**一并返回。
    # 原先只回一句「这个地址已经添加过了」，前端只能显示「添加失败」——用户想要的结果其实
    # 已经达成，却在几十个源里找不到它，观感就是「加不进去」（v62 修的就是这个）。
    existing = await asyncio.to_thread(
        db.row,
        "SELECT slug, name, status, item_count, enabled FROM sources WHERE url=?",
        (url,),
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "这个地址已经在数据源里了",
                "slug": str(existing["slug"]),
                "name": str(existing["name"]),
                "status": str(existing["status"] or ""),
                "item_count": int(existing["item_count"] or 0),
                "enabled": int(existing["enabled"] if existing["enabled"] is not None else 1),
            },
        )

    try:
        probe = await probe_manual_source(url, fmt)
    except SourceUnavailableError as exc:
        # 上游侧失败：域名解析不出来 / 内容超限 / 内容为空 / 重定向打转 / 上游非 200。
        # 请求本身没写错，所以是 502（网关从上游拿不到有效响应），不是 400。
        # 必须放在 `except ValueError` **前面** —— SourceUnavailableError 是它的子类，
        # 顺序反了就永远落进 400 分支（v47 修的就是这个口径，见 netguard 的类说明）。
        message = str(exc)[:200] or type(exc).__name__
        raise HTTPException(status_code=502, detail=f"源不可用：{message}")
    except ValueError as exc:
        # 真正的输入校验失败：协议不是 http(s)、缺主机名、解析到环回/受限地址、
        # 地址不是直播列表/格式不认识。这类才该让调用方改请求。
        raise HTTPException(status_code=400, detail=str(exc) or "地址不合法")
    except Exception as exc:  # 网络错误等：httpx 超时异常 str() 可能是空串
        message = str(exc)[:200] or type(exc).__name__
        raise HTTPException(status_code=502, detail=f"源不可用：{message}")

    slug = "manual::" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    display = name or f"手动源 · {(urlsplit(url).hostname or url)[:40]}"
    approved = await asyncio.to_thread(approve_hosts, probe["hosts"], f"手动添加：{display}")
    # 必须用 upsert_source 返回的**实际生效 slug**：409 预检与 upsert 之间不是原子的，
    # 窗口内该 URL 可能被 TVBox 父配置同步登记成子源，upsert 会复用那个 slug ——
    # 继续用本地推算的 slug 就是排队同步一个不存在的源（v17 在 sync.py 修过的同款）。
    # 用关键字传 source_group：upsert_source 的位置参数已有 6 个，多一个位置参数容易传错位。
    slug = await asyncio.to_thread(upsert_source, slug, display, url, probe["format"], 60,
                                   source_group=group)
    bump_revision()
    run_id, queued = await sync_manager.queue("manual", slug)
    logger.info(
        "手动添加数据源 %s（%s），放行主机 %s，排队同步 run=%s queued=%s",
        display, url, approved, run_id, queued,
    )
    return {
        "slug": slug,
        "name": display,
        "url": url,
        "format": probe["format"],
        "entries": probe.get("entries", 0),
        "sub_sources": probe.get("sub_sources", 0),
        "hosts_approved": approved,
        "run_id": run_id,
        "sync_queued": queued,
    }


@app.post("/api/sources/inline", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_admin)])
async def add_inline_source(
    request: Request,
    name: Annotated[str, Query(min_length=1, description="清单名称，同名再导入=更新同一个源")],
):
    """粘贴导入一份频道清单（每行「名称,播放地址」，也接受 M3U / 全角逗号）。

    运营商内网源通常就是这种逐条清单，没有可抓取的播放列表地址。这里把内容原样存到
    `data/inline/<slug>.m3u`，登记成一个 `format=inline` 的本地源 —— 之后它与联网源
    走**同一条流水线**：同步（读文件解析）→ 测速 → 剔除 → 并入订阅导出。

    有效性在入库前就校验：解析不出任何条目直接拒绝，不会留下空源。
    用同一个名称再次导入 = 更新这个源的内容（而不是又建一个）。
    """
    # 不能先 await request.body() 再判长度：那会把整个请求体先缓冲进内存，长度上限
    # 形同虚设（container 没有内存上限，单个请求即可吃满并拖垮同进程的同步/测速）。
    # 先看 Content-Length，再按块读、边读边判。
    limit = settings.max_source_bytes
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail=f"清单超过上限 {limit} 字节")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(status_code=413, detail=f"清单超过上限 {limit} 字节")
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw.strip():
        raise HTTPException(status_code=400, detail="清单内容为空")

    name = name.strip()
    # Query(min_length=1) 只看**未 strip** 的长度：?name=%20 能过校验，strip 后却成了空串，
    # 而 slug = sha1(名称) 会让所有空白名共用 inline::da39a3ee5e —— 第二个无名清单会
    # 悄悄覆盖第一个。空名必须在 strip 之后再挡一次。
    if not name:
        raise HTTPException(status_code=400, detail="清单名称不能为空")
    try:
        # 必须走线程池：parse_channel_list 是纯 CPU 的同步函数（实测约 1200 行/秒），
        # 直接在这个协程里跑会把整个事件循环独占 —— 实测 53KB / 1500 行就能让并发的
        # /healthz 卡 7.6 秒，而上限是 16MiB（线性外推约 10 分钟全站不可用）。
        entries = await asyncio.to_thread(parse_channel_list, raw)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"清单解析失败：{str(exc)[:120] or type(exc).__name__}")
    if not entries:
        raise HTTPException(
            status_code=400,
            detail="清单里没有解析出任何有效条目（每行应为「名称,http(s)://地址」，或直接粘贴 M3U）",
        )

    existed = bool(await asyncio.to_thread(
        db.row, "SELECT 1 AS ok FROM sources WHERE slug LIKE 'inline::%' AND name=?", (name,)
    ))
    slug, url = await asyncio.to_thread(save_inline_source, name, raw)
    await asyncio.to_thread(upsert_source, slug, name, url, "inline", 60)
    bump_revision()
    run_id, queued = await sync_manager.queue("manual", slug)
    logger.info(
        "粘贴导入数据源 %s（%s 条，%s），排队同步 run=%s queued=%s",
        name, len(entries), "更新" if existed else "新增", run_id, queued,
    )
    return {
        "slug": slug,
        "name": name,
        "format": "inline",
        "entries": len(entries),
        "updated": existed,
        "run_id": run_id,
        "sync_queued": queued,
    }


@app.delete("/api/sources/{slug:path}", dependencies=[Depends(require_admin)])
async def remove_source(slug: str):
    """移除一个数据源。

    只允许删手动添加的源：内置源在每次启动时由 SOURCE_SEEDS 重新写入，删了也会回来，
    与其留一个「删不掉的错觉」，不如直接说清楚。

    删源行会 CASCADE 掉它的全部条目与健康记录（详见 db.delete_source）。
    已放行的主机名保留 —— 同一个主机可能还在给别的源提供列表，一并撤销会误伤。
    """
    row = await asyncio.to_thread(db.row, "SELECT name, format FROM sources WHERE slug=?", (slug,))
    if not row:
        raise HTTPException(status_code=404, detail="数据源不存在")
    # TVBox 父配置与内置源一样每次启动重新写入（db._seed_sources 种入 tvbox_seeds），
    # 删了下次重启照样回来 —— 同样直接拒绝。已从 TVBOX_CONFIGS 移除的父配置
    # （不在当前 tvbox_seeds 里）不在此列：它不会再被种回，允许删，且必须走下面的
    # 子源连删逻辑，否则留下永久冻结的子源。
    if slug in {seed[0] for seed in SOURCE_SEEDS} | {seed[0] for seed in tvbox_seeds()}:
        raise HTTPException(status_code=400, detail="内置数据源不能删除（重启会重新写入）")
    # 删 TVBox 父配置时，它名下的子源必须一起删：子源既不在顶层同步集合里
    # （顶层查询排除了 slug LIKE 'tvbox::%' 的非 tvbox 行），也没有父源再触发对账，
    # 单独留下的子源会**永久冻结** —— 条目继续留在订阅里，却再不会被同步或清理。
    children: list[str] = []
    if row["format"] == "tvbox":
        children = [
            r["slug"] for r in await asyncio.to_thread(
                db.rows, "SELECT slug FROM sources WHERE parent_slug=?", (slug,)
            )
        ]
        if not children:
            # parent_slug 是后加的列，历史子源要等父源下一次同步才补记；此时不能空手而归，
            # 否则删父源照样留下冻结子源（本机生产就是这种状态）。
            # 但这个兜底查询**认不出子源属于哪个父配置**：库里存在多个 TVBox 父配置时，
            # 它会把其他父配置尚未回填 parent_slug 的子源也一并删掉 —— 所以只在
            # 「全库仅此一个 TVBox 父配置」时才启用，多个父配置时宁可留孤儿也不连坐误删。
            tvbox_parents = await asyncio.to_thread(
                db.row, "SELECT COUNT(*) AS n FROM sources WHERE format='tvbox'"
            ) or {"n": 0}
            if tvbox_parents["n"] <= 1:
                children = [
                    r["slug"]
                    for r in await asyncio.to_thread(
                        db.rows,
                        "SELECT slug FROM sources WHERE slug LIKE 'tvbox::%' "
                        "AND format <> 'tvbox' AND parent_slug=''",
                    )
                ]
            else:
                orphan_count = await asyncio.to_thread(
                    db.row,
                    "SELECT COUNT(*) AS n FROM sources WHERE slug LIKE 'tvbox::%' "
                    "AND format <> 'tvbox' AND parent_slug=''",
                )
                logger.warning(
                    "删除 TVBox 父配置 %s：存在多个父配置，无法安全归属 %s 个未回填 parent_slug 的子源，"
                    "跳过兜底清理（请先同步一次各父配置再删除）",
                    slug, (orphan_count or {"n": 0})["n"],
                )
    removed, orphan_health = await asyncio.to_thread(delete_source, slug)
    removed_children: list[str] = []
    for child in children:
        child_removed, child_orphan = await asyncio.to_thread(delete_source, child)
        removed += child_removed
        orphan_health += child_orphan
        removed_children.append(child)
    if row["format"] == "inline":
        # 粘贴导入的源，内容在 data/inline/ 下，删源时一并清掉，别留孤儿文件
        await asyncio.to_thread(remove_inline_source, slug)
    bump_revision()
    logger.info(
        "移除数据源 %s（%s），连带删除条目 %s 条、孤儿健康记录 %s 条%s",
        row["name"], slug, removed, orphan_health,
        f"，同时删除子源 {removed_children}" if removed_children else "",
    )
    return {
        "removed": slug,
        "name": row["name"],
        "removed_entries": removed,
        "removed_health_rows": orphan_health,
        "removed_children": removed_children,
    }


@app.post("/api/sources/{slug:path}/enable", dependencies=[Depends(require_admin)])
def toggle_source_enabled(
    slug: str,
    value: Annotated[int, Query(ge=0, le=1, description="1=启用，0=停用")] = 1,
):
    """启用 / 停用一个数据源（v51）。停用不删任何数据，可逆且跨重启保留。

    与 `DELETE /api/sources/{slug}` 的区别正是这个接口存在的理由：

      * **删除**会 CASCADE 掉该源的条目与健康记录（线路从订阅与测速历史里一起消失），
        而且内置源根本删不掉 —— 它在每次启动时由 `SOURCE_SEEDS` 重新写入，删除接口
        对内置源直接返回 400（那条规则不变）；
      * **停用**只置 `sources.enabled=0`：源行、条目、健康记录全部保留，同步
        （`sync.select_sync_sources`）与测速（`speed._pending_sql`）跳过它，
        重新启用即刻回到原语义 —— 所以内置源**可以**被停用。

    停用是持久状态：`db._seed_sources` 的 upsert 刻意不更新 `enabled`，容器重启或
    `docker compose up -d --force-recreate` 都不会把它冲回 1（这是单测钉住的行为）。

    `value` 只接受 0/1，其他值由 FastAPI 校验成 422 —— 不做「非 0 即真」的宽松解释，
    那种接口用 `value=2` 静默当启用的行为会让调用方以为自己停用了。
    """
    row = db.row("SELECT name, enabled FROM sources WHERE slug=?", (slug,))
    if not row:
        raise HTTPException(status_code=404, detail="数据源不存在")
    # 同值重复调用**不写库、不自增版本**：SQLite 的 rowcount 记的是「匹配行数」，
    # 同值 UPDATE 也会算 True，所以这里必须自己比较前后值（v50 的编辑类接口同口径）。
    previous = int(row["enabled"] or 0)
    changed = previous != int(value)
    if changed:
        db_set_source_enabled(slug, bool(value))
        bump_revision()
    logger.info(
        "%s数据源 %s（enabled=%s，变更前=%s）",
        "启用" if value else "停用", slug, value, previous,
    )
    return {
        "slug": slug,
        "name": row["name"],
        "enabled": int(value),
        "previous": previous,
        "changed": changed,
    }


@app.get("/api/discovered", dependencies=[Depends(require_admin)])
def list_discovered(limit: Annotated[int, Query(ge=1, le=500)] = 100):
    """「发现」到的候选订阅地址（**只发现，不自动采用**）。

    爬虫只抓预置的几个索引页面、把像播放列表的地址记下来；这些地址在你点「采用」之前
    服务端不会去访问它们（所以这一步没有 SSRF 面）。采用时才走既有的添加源流程：
    真抓一次校验 → 把实际访问成功的主机写进放行白名单 → 入库 → 排队同步。
    """
    return discover.discovered_summary(limit)


# 「立即发现」的单飞守卫（v42，审计 P1-3）：一轮发现要串行抓 9 个站点、站间还睡 3 秒
# （30~60 秒），而这个接口不设防时每被点一次就整轮重跑 —— 并发调用会把对外请求放大成 N 倍，
# 既有被索引站封 IP 的风险，也让「每天一轮」的限速承诺失效。写法与 _DISCOVER_CHECK /
# _DISCOVER_ADOPT 一致：进程内标志 + try/finally，运行中直接回 started=False。
_DISCOVER_REFRESH = {"running": False}


@app.post("/api/discovered/refresh", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_admin)])
async def refresh_discovered():
    """立刻跑一轮发现（每轮逐站限速抓取，约 30~60 秒）。

    同一时间只允许一轮：运行中再来请求直接返回 started=False（见 _DISCOVER_REFRESH）。
    """
    if _DISCOVER_REFRESH["running"]:
        return {"started": False, "message": "已有一轮发现正在跑"}
    _DISCOVER_REFRESH["running"] = True
    try:
        stats = await discover.run_discovery()
        # 自动愈合：界面上不该长期留着以前检测出来的无效候选
        left = await asyncio.to_thread(purge_invalid_discovered)
        if left:
            logger.info("发现轮次后清理无效候选 %s 条", left)
        return {"started": True, "stats": stats, "summary": discover.discovered_summary(50)}
    finally:
        # 异常/取消也要解锁，否则一次失败会让「立即发现」永久卡在"正在跑"
        _DISCOVER_REFRESH["running"] = False


# 批量检测状态（进程内）：候选很多，逐个真抓会花几分钟，所以放后台跑、界面轮询看进度。
_DISCOVER_CHECK = {"running": False, "done": 0, "total": 0, "ok": 0, "removed": 0}


def _candidate_target(url: str, ref: str) -> str:
    """把前端传来的操作目标还原成**候选真实地址**。

    v42（审计 P1-5）：`/api/discovered` 回显的 url 已打码，打码串不能再当主键用，
    所以列表同时下发不透明的 `ref`（sha1 前 12 位），检测/采用/忽略带着 ref 回来，
    这里换回真实地址。`url` 直传仍然支持（curl / 旧脚本 / 批量逻辑不受影响）。

    注意：打码只发生在**回显**层；同步、入库、白名单用的始终是数据库里的原始地址。
    """
    if url:
        return url
    resolved = discover.resolve_candidate_ref(
        (row["url"] for row in db.rows("SELECT url FROM discovered")),
        ref,
    )
    if not resolved:
        raise HTTPException(status_code=404, detail="这个候选不在发现列表里")
    return resolved


async def _probe_candidate(url: str) -> tuple[str, int, str]:
    """检测一条候选：真抓一次并解析（**不写源、不放行主机**）。返回 (状态, 条目数, 说明)。"""
    try:
        probe = await probe_manual_source(url)
    except ValueError as exc:                      # 地址不合法 / 解析不出条目 / 是 TVBox 配置
        return "error", 0, str(exc)[:200] or "解析不出任何有效条目"
    except Exception as exc:  # noqa: BLE001 - 网络错误等
        return "error", 0, f"{type(exc).__name__}: {str(exc)[:160]}"
    if probe.get("format") == "tvbox":
        return "ok", int(probe.get("sub_sources") or 0), "TVBox 配置（子源 %s 个）" % probe.get("sub_sources")
    items = int(probe.get("entries") or 0)
    return ("ok" if items else "error"), items, f"格式 {probe.get('format')}，解析出 {items} 条"


@app.post("/api/discovered/check", dependencies=[Depends(require_admin)])
async def check_discovered(url: str = "", ref: str = ""):
    """检测单条候选（真抓一次并解析，不写源）。

    v42（审计 P2-b）：只允许检测 `state='new'` 的候选。原先这里没有任何 state 过滤，
    对一条**已采用**的候选点「检测」（或直接 curl）会走判死分支把它从 discovered 表删掉
    并写 30 天墓碑 —— 而批量检测（`state='new'`）与 purge 都保护 accepted，
    单条入口却能把用户的采用记录抹掉。非 new 一律 409，语义与批量入口对齐。
    """
    target = _candidate_target(url, ref)
    row = db.row("SELECT state FROM discovered WHERE url=?", (target,))
    if not row:
        raise HTTPException(status_code=404, detail="这个候选不在发现列表里")
    if row["state"] != "new":
        raise HTTPException(
            status_code=409,
            detail="这条候选已经处理过了（已采用/已忽略），不能再当未验证候选检测",
        )
    status, items, message = await _probe_candidate(target)
    if status == "ok":
        await asyncio.to_thread(save_discovered_check, target, status, items, message)
    else:
        # 判死：直接从候选列表删除（只留一条 30 天墓碑，免得下轮又把同一批垃圾抓回来）
        await asyncio.to_thread(reject_discovered, target, message)
        await asyncio.to_thread(bump_revision)
    return {"url": mask_source_url(target), "status": status, "items": items, "message": message,
            "removed": status != "ok"}


@app.post("/api/discovered/check-batch", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_admin)])
async def check_discovered_batch(limit: Annotated[int, Query(ge=1, le=200)] = 30):
    """批量检测还没测过（或测过但失败）的候选；后台串行跑，避免把上游打疼。

    串行 + 间隔 1 秒：一次最多 limit 条，界面轮询 /api/discovered/check-status 看进度。
    """
    if _DISCOVER_CHECK["running"]:
        return {"started": False, "state": dict(_DISCOVER_CHECK)}
    todo = [
        row["url"]
        for row in db.rows(
            "SELECT url FROM discovered WHERE state='new' AND check_status <> 'ok' "
            "ORDER BY checked_at, last_seen_at DESC LIMIT ?",
            (limit,),
        )
    ]
    if not todo:
        return {"started": False, "state": dict(_DISCOVER_CHECK), "message": "没有需要检测的候选"}

    async def worker() -> None:
        _DISCOVER_CHECK.update({"running": True, "done": 0, "total": len(todo),
                                "ok": 0, "removed": 0})
        try:
            for candidate in todo:
                status, items, message = await _probe_candidate(candidate)
                if status == "ok":
                    await asyncio.to_thread(save_discovered_check, candidate, status, items, message)
                    _DISCOVER_CHECK["ok"] += 1
                else:
                    # 判死即删：界面上不留「检测无效」的垃圾行（墓碑记下原因，30 天内不再重抓）
                    await asyncio.to_thread(reject_discovered, candidate, message)
                    _DISCOVER_CHECK["removed"] += 1
                _DISCOVER_CHECK["done"] += 1
                # 打码后再进日志（P1-5）：docker 日志是最容易长期留存、也最容易被翻看的地方，
                # 候选地址里的上游凭据不该以原文写进去。
                logger.info("候选检测 %s：%s（%s 条）", mask_source_url(candidate)[:70], status, items)
                await asyncio.sleep(1.0)
        except Exception:
            logger.exception("候选批量检测中断")
        finally:
            # 存量库里可能还留着以前检测出来的无效项，一并清掉
            try:
                left = await asyncio.to_thread(purge_invalid_discovered)
                if left:
                    logger.info("清理历史无效候选 %s 条", left)
            except Exception:  # noqa: BLE001 - 清理失败不该影响检测结果
                logger.exception("清理历史无效候选失败")
            _DISCOVER_CHECK["running"] = False
            bump_revision()

    asyncio.create_task(worker())
    return {"started": True, "state": dict(_DISCOVER_CHECK), "total": len(todo)}


@app.post("/api/discovered/purge-invalid", dependencies=[Depends(require_admin)])
async def purge_invalid_candidates():
    """清理所有「检测无效」的候选（列表里不再显示；留 30 天墓碑，不会被发现轮次重新抓回来）。"""
    left = await asyncio.to_thread(purge_invalid_discovered)
    if left:
        await asyncio.to_thread(bump_revision)
        logger.info("手动清理无效候选 %s 条", left)
    return {"removed": left}


@app.get("/api/discovered/check-status", dependencies=[Depends(require_admin)])
def discovered_check_status():
    """批量检测进度（界面轮询用）。"""
    return dict(_DISCOVER_CHECK)


# 批量入库状态（进程内，界面轮询看进度）
_DISCOVER_ADOPT = {"running": False, "done": 0, "total": 0, "ok": 0, "failed": 0,
                   "already": 0, "added": []}


@app.post("/api/discovered/adopt-batch", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_admin)])
async def adopt_discovered_batch(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    include_unchecked: bool = False,
):
    """**批量入库**：把所有「检测有效」的候选一次加入数据源，不用一个个点。

    - 默认只收 `check_status='ok'` 的（检测这一步就是为此存在的：没验过的不进）；
      `include_unchecked=true` 时连未检测的一起收（每条仍会在入库时真抓校验一遍）。
    - 串行执行、每条间隔 1 秒：入库本身会触发「同步 → 测速 → 并入总源」，一次性灌几十个源
      没有意义（同步队列只有 3 个位，测速预算每天也就一千多条）。
    - 每条都走**和单个采用完全相同**的路径：真抓校验 → 把实际访问成功的主机写进放行白名单 →
      入库 → 排队同步。所以安全模型不变。
    """
    if _DISCOVER_ADOPT["running"]:
        return {"started": False, "state": dict(_DISCOVER_ADOPT)}
    where = "state='new'" + ("" if include_unchecked else " AND check_status='ok'")
    todo = [
        row["url"]
        for row in db.rows(
            f"SELECT url FROM discovered WHERE {where} ORDER BY check_items DESC, last_seen_at DESC LIMIT ?",
            (limit,),
        )
    ]
    if not todo:
        return {"started": False, "state": dict(_DISCOVER_ADOPT),
                "message": "没有已检测有效的候选（可先点「检测未验证的」）"}

    async def worker() -> None:
        _DISCOVER_ADOPT.update({"running": True, "done": 0, "total": len(todo),
                                "ok": 0, "failed": 0, "already": 0, "added": []})
        try:
            for candidate in todo:
                try:
                    # 复用单个采用的整条路径（含校验、放行主机、入库、排队同步）。
                    # 分组同样传「发现源」：批量入库与单个采用必须落成同一状态，
                    # 否则同一批候选走两个入口会得到两种分组（前端分区与导出分组都会不一致）。
                    await add_source(url=candidate, name="", format="", group=DISCOVERED_SOURCE_GROUP)
                    await asyncio.to_thread(set_discovered_state, candidate, "accepted")
                    _DISCOVER_ADOPT["ok"] += 1
                    # 进度列表与日志都只放打码地址（P1-5）：/api/discovered/adopt-status 是
                    # 界面轮询接口，added 里的原文等于把候选凭据又回显一遍。
                    _DISCOVER_ADOPT["added"].append(mask_source_url(candidate))
                    logger.info("批量入库成功：%s", mask_source_url(candidate)[:80])
                except HTTPException as exc:
                    # 409 = 这个地址已经在数据源里（例如发现之后又在别处加过）。那不是失败：
                    # 候选已经被收进来了，标成 accepted 让列表归位、计入 already 而不是 failed ——
                    # 否则界面会一直显示"检测有效待入库 N"却永远入不进去（实测 20 条全 409、ok=0）。
                    if exc.status_code == 409:
                        await asyncio.to_thread(set_discovered_state, candidate, "accepted")
                        _DISCOVER_ADOPT["already"] += 1
                    else:
                        _DISCOVER_ADOPT["failed"] += 1
                        logger.warning("批量入库跳过 %s：%s", candidate[:80], exc.detail)

                except Exception as exc:  # noqa: BLE001
                    _DISCOVER_ADOPT["failed"] += 1
                    logger.warning("批量入库失败 %s：%s", mask_source_url(candidate)[:80], str(exc)[:120])
                _DISCOVER_ADOPT["done"] += 1
                await asyncio.sleep(1.0)
        finally:
            _DISCOVER_ADOPT["running"] = False
            # 收尾补一次全量同步：同步队列上限 3，批量入库的第 4 条起会被拒（日志可见
            # 「同步队列已满(3)，拒绝」），不补这一下这些新源会一直停在「从未同步」。
            # queue() 自带去重，全量任务最多排一个。
            # v42（审计 P1-3）：queue() 返回 queued=False 并不等于"已被覆盖"——正在跑的那轮
            # 全量早已读完源列表，或单源队列满把这次全量请求顶掉，本轮新入库的源都不在里面。
            # 这两种情况 SyncManager 会置 _full_dirty，队列跑空后自动补排一轮全量，
            # 所以这里只需要把结果记进日志，不必再自己逐个 slug 排队。
            try:
                run_id, queued = await sync_manager.queue("discover-adopt")
                if not queued:
                    logger.info(
                        "批量入库收尾的全量同步未入队（run=%s）：若因队列满/全量在跑被去重，"
                        "SyncManager 会在队列跑空后自动补一轮",
                        run_id or "-",
                    )
            except Exception:  # noqa: BLE001 - 排队失败不影响已入库的源
                logger.warning("批量入库后排队全量同步失败", exc_info=True)
            await asyncio.to_thread(bump_revision)

    asyncio.create_task(worker())
    return {"started": True, "state": dict(_DISCOVER_ADOPT), "total": len(todo)}


@app.get("/api/discovered/adopt-status", dependencies=[Depends(require_admin)])
def discovered_adopt_status():
    """批量入库进度（界面轮询用）。"""
    return dict(_DISCOVER_ADOPT)


@app.post("/api/discovered/accept", dependencies=[Depends(require_admin)])
async def accept_discovered(url: str = "", ref: str = ""):
    """采用一个候选：等价于用该地址调用「添加数据源」，校验/放行/入库/排队全部复用既有流程。

    与「手动添加」的唯一区别是打上「发现源」分组：这些源是爬虫批量发现、成批采用的，
    在数据源页与订阅导出里单独成组，才不会和手工挑选的源混在一起。

    v42：支持用 `ref`（列表下发的不透明句柄）代替 `url` —— 列表里的地址已打码，
    前端拿不到原文（见 _candidate_target 与 discover.candidate_ref）。
    """
    target = _candidate_target(url, ref)
    row = db.row("SELECT state FROM discovered WHERE url=?", (target,))
    if not row:
        raise HTTPException(status_code=404, detail="这个候选不在发现列表里")
    result = await add_source(url=target, name="", format="", group=DISCOVERED_SOURCE_GROUP)
    await asyncio.to_thread(set_discovered_state, target, "accepted")
    bump_revision()
    return result


@app.post("/api/discovered/ignore", dependencies=[Depends(require_admin)])
async def ignore_discovered(url: str = "", ref: str = ""):
    """忽略一个候选（之后发现轮次不会再把它标成 new，但记录仍在）。"""
    target = _candidate_target(url, ref)
    ok = await asyncio.to_thread(set_discovered_state, target, "ignored")
    if not ok:
        raise HTTPException(status_code=404, detail="这个候选不在发现列表里")
    # 回显同样打码：这个返回值会被界面 toast/日志看到
    return {"ignored": mask_source_url(target)}


@app.get("/api/allowed-hosts", dependencies=[Depends(require_admin)])
def list_allowed_hosts():
    """抓取白名单（内置 + 手动放行）的只读视图。

    **需要管理令牌**（设置 `IPTV_ADMIN_TOKEN` 后才真正生效；为空时行为与以前一致）：
    它暴露的是"这台机器允许去抓哪些主机"，等于把内网地址与你在用的上游站点列出来。
    它**不是鉴权本身** —— 鉴权是 `require_admin`；白名单是抓取前的出站门禁（见 sync.py
    里 `source host not approved` 那处检查），两者职责不同。
    界面不调用这个接口（添加源成功时提示里的"已放行主机"来自添加接口的返回值），
    所以收紧它不影响任何前端功能。
    """
    return {
        "builtin": sorted(ALLOWED_SOURCE_HOSTS_BUILTIN),
        "manual": db.allowed_hosts(),
    }


@app.delete("/api/allowed-hosts/{host:path}", dependencies=[Depends(require_admin)])
async def revoke_allowed_host(host: str):
    """撤销一个手动放行的主机（内置白名单不受影响）。"""
    if host.strip().lower() in ALLOWED_SOURCE_HOSTS_BUILTIN:
        raise HTTPException(status_code=400, detail="这是内置白名单主机，只能改代码/配置")
    removed = await asyncio.to_thread(revoke_host, host)
    if not removed:
        raise HTTPException(status_code=404, detail="该主机不在手动放行列表里")
    return {"revoked": host.strip().lower()}


@app.get("/api/groups", dependencies=[Depends(require_admin)])
def groups():
    return db.rows(
        """
        SELECT group_title AS name, COUNT(DISTINCT channel_key) AS count
        FROM source_entries WHERE active=1
        GROUP BY group_title ORDER BY count DESC, name ASC LIMIT 200
        """
    )


def _channel_filters(q: str, source: str, group: str, country: str, favorite: bool | None, region: str = "") -> tuple[str, list[object]]:
    clauses = ["e.active=1"]
    params: list[object] = []
    if q:
        clauses.append("(e.normalized_name LIKE ? OR e.tvg_id LIKE ?)")
        token = f"%{q.casefold().strip()}%"
        params.extend([token, token])
    if source:
        clauses.append("e.source_slug=?")
        params.append(source)
    if group:
        clauses.append("e.group_title=?")
        params.append(group)
    if country:
        clauses.append("e.country=?")
        params.append(country.lower())
    if region in {"domestic", "international"}:
        clauses.append("e.region=?")
        params.append(region)
    if favorite is True:
        clauses.append("f.channel_key IS NOT NULL")
    elif favorite is False:
        clauses.append("f.channel_key IS NULL")
    return " AND ".join(clauses), params


_CHANNEL_DEFAULT_ORDER = "r.favorite DESC, r.group_title, r.name"
_CHANNEL_ORDERS = {
    # 速度排序：先按三大组（央视 → 地方台 → 其他），组内再按速度降序；
    # 本地 relay/内网源测速虚高，不能让它把央视挤出前排
    "speed": "CASE r.group_title WHEN '央视' THEN 0 WHEN '地方台' THEN 1 ELSE 2 END, f.best_speed DESC NULLS LAST, r.favorite DESC, r.group_title, r.name",
    "name": "r.name",
}
# 每频道全局最快速度（不受列表筛选影响，与原有语义保持一致）
_FAST_SPEED_CTE = """fast AS (
        SELECT y.channel_key, MAX(h.speed_kbps) AS best_speed
        FROM source_entries y
        JOIN stream_health h ON h.fingerprint=y.stream_fingerprint
        WHERE y.active=1 AND h.status='ok' AND h.speed_kbps IS NOT NULL
        GROUP BY y.channel_key
    )"""


def _channel_keys_all(where: str, params: list[object], sort: str) -> list[str]:
    """「频道目录」的全量有序 channel_key 列表（阶段 1），带版本缓存。

    阶段 1 是这条接口的绝对大头：它要把 2.4 万条活跃条目按 channel_key 聚合、排序出
    全部 5865 个频道键，而**每个分页/翻页/搜索请求都要重算一遍**（默认排序实测
    0.5~1.4s，占单词请求的 90% 以上）。它只依赖三样东西：筛选条件、排序方式、数据版本。
    数据版本（current_revision）在任何会改变目录的动作后自增：同步、测速落库、剔除、
    复活、收藏——所以版本号就是天然的失效信号，另有 60 秒 TTL 兜底。

    顺带修掉一个长期问题：有序列表在一个版本内是**快照**，同一批翻页不再因为测速
    分批落库导致 OFFSET 窗口平移（原先实测遍历 59 页会有 91 个频道重复、91 个漏掉）。

    内存：5865 个键约 286KB/份，上限 6 份 ≈ 1.7MB。
    """
    cache_key = (where, tuple(params), sort, current_revision())
    now = time.monotonic()
    with _CHANNEL_KEYS_LOCK:
        hit = _CHANNEL_KEYS_CACHE.get(cache_key)
    if hit and hit[0] > now:
        return hit[1]
    # single-flight：这条聚合适配全站最贵（默认排序 0.5~1.4s CPU），缓存 miss 时
    # 并发请求（前端 refreshAll / 多标签页）会各自完整跑一遍同一聚合。
    # 用一把全局计算锁即可 —— 不同筛选条件串行的代价远小于并行叠加同一批 CPU 密集查询；
    # 持锁后必须再查一次缓存（等锁期间别人可能已算好）。
    with _CHANNEL_KEYS_COMPUTE_LOCK:
        now = time.monotonic()
        with _CHANNEL_KEYS_LOCK:
            hit = _CHANNEL_KEYS_CACHE.get(cache_key)
        if hit and hit[0] > now:
            return hit[1]
        keys = _channel_keys_compute(where, params, sort)
        now = time.monotonic()
        with _CHANNEL_KEYS_LOCK:
            _CHANNEL_KEYS_CACHE[cache_key] = (now + CHANNEL_KEYS_TTL, keys)
            while len(_CHANNEL_KEYS_CACHE) > CHANNEL_KEYS_CACHE_MAX:
                _CHANNEL_KEYS_CACHE.pop(next(iter(_CHANNEL_KEYS_CACHE)), None)
        return keys


def _channel_keys_compute(where: str, params: list[object], sort: str) -> list[str]:
    """阶段 1 的实际计算（调用方须持有 _CHANNEL_KEYS_COMPUTE_LOCK）。"""
    order = _CHANNEL_ORDERS.get(sort, _CHANNEL_DEFAULT_ORDER)
    speed_sort = sort == "speed"
    head = f"WITH {_FAST_SPEED_CTE}," if speed_sort else "WITH"
    fast_join = "LEFT JOIN fast f ON f.channel_key=r.channel_key" if speed_sort else ""
    keys = [
        row["channel_key"]
        for row in db.rows(
            f"""
            {head}
            filtered AS (
                SELECT e.channel_key, e.name, e.group_title, e.source_slug, s.priority,
                       CASE WHEN f.channel_key IS NULL THEN 0 ELSE 1 END AS favorite
                FROM source_entries e
                JOIN sources s ON s.slug=e.source_slug
                LEFT JOIN favorites f ON f.channel_key=e.channel_key
                WHERE {where}
            ), reps AS (
                -- 取每频道 (priority, source_slug, name) 最小的行作为代表。
                -- 用 MIN() 的 "bare column" 语义替代 ROW_NUMBER() 窗口函数，省掉对 2.4 万行的
                -- 排序（实测 357ms -> 237ms，全量 5199 个频道结果逐字段一致）。
                -- priority 补零 + source_slug 定宽，保证拼接后的字典序等价于三元组顺序。
                SELECT channel_key, name, group_title, favorite,
                       MIN(printf('%08d', priority) || printf('%-32s', source_slug) || name) AS _rank
                FROM filtered
                GROUP BY channel_key
            )
            SELECT r.channel_key FROM reps r {fast_join}
            ORDER BY {order}
            """,
            params,
        )
    ]
    return keys


def _channel_page_rows(where: str, params: list[object], sort: str, page: int, page_size: int) -> list[dict]:
    """两阶段取「频道目录」的一页，结果与原单条 SQL 逐字段一致。

    原实现一条 SQL 同时做了：整表物化宽行、两种 DISTINCT 聚合、每频道窗口函数、
    最终排序——为了返回 30 行要把 2.4 万行 × 20 列灌进临时表（实测峰值约 23MB，
    是这次 database or disk is full 故障的主要来源），单次约 1.7s。

    拆成两阶段：
      1) 只用窄列算出「当页的 channel_key」，窗口函数与 ORDER BY 的排序语义不变；
      2) 只对当页 ≤100 个 channel_key 做聚合与取详情。
    实测 7 组筛选/排序组合全量遍历 5199 个频道：集合、字段、顺序全部一致；
    首页 30 条从 1.7s 降到 0.4~0.9s。

    阶段 1 的结果走 _channel_keys_all 的版本缓存（同一版本只算一次），分页在内存里切片。
    """
    keys_all = _channel_keys_all(where, params, sort)
    keys = keys_all[(page - 1) * page_size: page * page_size]
    if not keys:
        return []
    values = ", ".join("(?)" for _ in keys)
    rows = db.rows(
        f"""
        WITH page(channel_key) AS (VALUES {values}),
        filtered AS MATERIALIZED (
            SELECT e.*, s.priority, s.name AS source_name,
                   CASE WHEN f.channel_key IS NULL THEN 0 ELSE 1 END AS favorite
            FROM source_entries e
            JOIN sources s ON s.slug=e.source_slug
            LEFT JOIN favorites f ON f.channel_key=e.channel_key
            WHERE e.channel_key IN (SELECT channel_key FROM page) AND {where}
        ), per_channel AS (
            SELECT channel_key,
                   COUNT(DISTINCT stream_fingerprint) AS stream_count,
                   GROUP_CONCAT(DISTINCT source_slug) AS sources
            FROM filtered GROUP BY channel_key
        ), fast AS (
            SELECT y.channel_key, MAX(h.speed_kbps) AS best_speed
            FROM source_entries y
            JOIN stream_health h ON h.fingerprint=y.stream_fingerprint
            WHERE y.active=1 AND h.status='ok' AND h.speed_kbps IS NOT NULL
              AND y.channel_key IN (SELECT channel_key FROM page)
            GROUP BY y.channel_key
        ), ranked AS (
            SELECT *, ROW_NUMBER() OVER(PARTITION BY channel_key ORDER BY priority, source_slug, name) AS rn
            FROM filtered
        )
        SELECT r.channel_key, r.name, r.tvg_id, r.logo, r.group_title, r.country, r.language,
               r.quality, r.url, r.source_slug, r.source_name, r.favorite, r.region,
               p.stream_count, p.sources, f.best_speed
        FROM ranked r
        JOIN per_channel p ON p.channel_key=r.channel_key
        LEFT JOIN fast f ON f.channel_key=r.channel_key
        WHERE r.rn=1
        """,
        # 绑定顺序按 SQL 文本：先 page 的 VALUES 占位符，再 where 的占位符
        [*keys, *params],
    )
    by_key = {row["channel_key"]: row for row in rows}
    # 详情按第一阶段算出的页码顺序返回
    return [by_key[key] for key in keys if key in by_key]


@app.get("/api/channels", dependencies=[Depends(require_admin)])
def channels(
    q: str = "",
    source: str = "",
    group: str = "",
    country: str = "",
    favorite: bool | None = None,
    region: str = "",
    sort: str = "default",
    page: Annotated[int, Query(ge=1, le=2000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 30,
):
    # region/source 规范化后再进 _channel_keys_all 的缓存键：任意字符串会让缓存被
    # 随机值打散（每个新键 = 一次全量聚合），也是深分页之外唯一能放大开销的入口。
    source = _normalize_source(source)
    region = _normalize_region(region)
    # group/country/q 同样进缓存键，必须收敛键空间（strip + 限长），
    # 否则 ?group=junk1、?group=junk2… 与当初 ?region=junkN 是同一个缓存穿透漏洞
    group = (group or "").strip()[:128]
    country = (country or "").strip().lower()[:8]
    q = (q or "").strip()[:64]
    # sort 也进缓存键：未知值本就静默回退默认排序，若不收敛，?sort=x1、?sort=x2…
    # 与 ?region=junkN 是同一个缓存穿透漏洞（每个新键 = 一次全量聚合）
    if sort not in _CHANNEL_ORDERS:
        sort = "default"
    where, params = _channel_filters(q, source, group, country, favorite, region)
    # total 直接取阶段 1 的缓存列表长度：两者是同一次聚合（同一 where 条件下的
    # DISTINCT channel_key），原先为它单独再跑一条 COUNT(DISTINCT ...) 全量查询 ——
    # 每次翻页把同一份聚合做两遍。已逐组合核对过两条路径结果完全一致。
    keys_all = _channel_keys_all(where, params, sort)
    total = len(keys_all)
    rows = _channel_page_rows(where, params, sort, page, page_size)
    return {"items": rows, "total": total, "page": page, "page_size": page_size}


@app.get("/api/channels/{channel_key:path}", dependencies=[Depends(require_admin)])
def channel_detail(channel_key: str):
    """频道详情：同一流指纹只保留一条，按「已验证 → 快 → 稳 → 延迟低」排序并标记角色。

    role 语义：
      primary —— 前 line_limit 条可用线路，就是当前订阅导出实际使用的那几条；
      backup  —— 其余可用线路，主用失效时会被自动顶上（导出每次都按当前健康度重算）；
      dead    —— 已判定不可用（error/unsupported 或连续失败达阈值），不会出现在订阅里。
    央视系给 MAX_LINES_CCTV 条（默认 10），其余给 MAX_LINES_PER_CHANNEL 条（默认 5）。
    """
    meta = db.row(
        """
        SELECT e.name, e.group_title FROM source_entries e
        JOIN sources s ON s.slug=e.source_slug
        WHERE e.active=1 AND e.channel_key=?
        ORDER BY s.priority, e.source_slug, e.name LIMIT 1
        """,
        (channel_key,),
    )
    if not meta:
        raise HTTPException(status_code=404, detail="频道不存在")
    limit = static_export.channel_line_limit(meta["name"], meta["group_title"])
    items = db.rows(
        """
        WITH ranked AS (
            SELECT e.*, s.name AS source_name, s.priority,
                   h.status AS health_status, h.speed_kbps, h.latency_ms,
                   h.checked_at AS health_checked_at, h.error AS health_error,
                   h.fail_count AS fail_count, h.last_fail_at AS last_fail_at,
                   h.ok_total, h.fail_total,
                   CASE WHEN f.channel_key IS NULL THEN 0 ELSE 1 END AS favorite,
                   ROW_NUMBER() OVER (
                       PARTITION BY e.stream_fingerprint
                       ORDER BY s.priority, e.quality DESC, e.url
                   ) AS fp_rn
            FROM source_entries e
            JOIN sources s ON s.slug=e.source_slug
            LEFT JOIN stream_health h ON h.fingerprint=e.stream_fingerprint
            LEFT JOIN favorites f ON f.channel_key=e.channel_key
            WHERE e.active=1 AND e.channel_key=?
        )
        SELECT * FROM ranked WHERE fp_rn=1
        ORDER BY CASE WHEN health_status='ok' AND speed_kbps IS NOT NULL THEN 0 ELSE 1 END,
                 COALESCE(speed_kbps, 0) DESC,
                 COALESCE(fail_total, 0) ASC,
                 COALESCE(ok_total, 0) DESC,
                 COALESCE(latency_ms, 1000000) ASC,
                 priority, quality DESC, url
        LIMIT ?
        """,
        (channel_key, limit + settings.backup_lines_shown),
    )
    # 前 limit 条可用线路是「主用」，其余可用线路是「备选」，已判定不通的标为「失效」；
    # 守卫拦截（测不了）单列一档：既不是可用也不是失效，不能占用主用/备选名额。
    primary = 0
    for item in items:
        fails = item["fail_count"] or 0
        if item["health_status"] == "purged":
            # 季度清理永久停用的线路：既不是可用、也不是「本轮失效」，单列一档。
            # 漏掉这一档会让它落进 primary/backup（fail_count=0 且 status 不在 error 里），
            # 于是「已永久停用」的线路在频道详情里被显示成「主用」并计入可用。
            item["role"] = "purged"
        elif item["health_status"] == "blocked":
            item["role"] = "blocked"
        elif item["health_status"] in {"error", "unsupported"} or fails >= settings.prune_max_fail:
            item["role"] = "dead"
        elif primary < limit:
            item["role"] = "primary"
            primary += 1
        else:
            item["role"] = "backup"
    total = db.row(
        "SELECT COUNT(DISTINCT stream_fingerprint) AS n FROM source_entries WHERE active=1 AND channel_key=?",
        (channel_key,),
    ) or {"n": 0}
    dead = db.row(
        """
        SELECT COUNT(DISTINCT e.stream_fingerprint) AS n
        FROM source_entries e
        LEFT JOIN stream_health h ON h.fingerprint=e.stream_fingerprint
        WHERE e.active=1 AND e.channel_key=?
          AND (h.status IN ('error', 'unsupported') OR COALESCE(h.fail_count, 0) >= ?)
        """,
        (channel_key, settings.prune_max_fail),
    ) or {"n": 0}
    blocked = db.row(
        """
        SELECT COUNT(DISTINCT e.stream_fingerprint) AS n
        FROM source_entries e
        JOIN stream_health h ON h.fingerprint=e.stream_fingerprint
        WHERE e.active=1 AND e.channel_key=? AND h.status='blocked'
        """,
        (channel_key,),
    ) or {"n": 0}
    purged = db.row(
        """
        SELECT COUNT(DISTINCT e.stream_fingerprint) AS n
        FROM source_entries e
        JOIN stream_health h ON h.fingerprint=e.stream_fingerprint
        WHERE e.active=1 AND e.channel_key=? AND h.status='purged'
        """,
        (channel_key,),
    ) or {"n": 0}
    return {
        "channel": items[0],
        "streams": items,
        "line_limit": limit,
        "total_streams": total["n"],
        # 可用 = 总数 - 失效 - 守卫拦截 - 已永久停用；漏任何一档都会把不可用算成可用
        "usable_streams": max(0, total["n"] - dead["n"] - blocked["n"] - purged["n"]),
        "dead_streams": dead["n"],
        "blocked_streams": blocked["n"],
        "purged_streams": purged["n"],
        "primary_count": primary,
        "backup_count": sum(1 for item in items if item["role"] == "backup"),
    }


# ---------------------------------------------------------------------------
# v49：「全部线路判死」频道清单（只读）
# ---------------------------------------------------------------------------
#
# 判据（用户拍死的口径，前后端只读它，`_ALIVE_ENTRY_SQL` 是唯一实现）：
#   某频道**所有** active=1 条目都「判死」——即它对应的 stream_health 行
#   `status IN ('error','unsupported')`，**或**该 stream_fingerprint 根本没有健康行。
#   只要还有**一条**条目的健康行存在且状态不是 error/unsupported（也就是 ok、
#   空串/NULL、blocked、purged），这个频道就**不算**全死。
#
# 为什么把「未测」（status 为空 / NULL）排除在判死之外 —— 没测 ≠ 坏：
#   * `status=''` 只表示这条线路**还没排到复测**（刚入库的新源、刚复活、测速预算没轮到），
#     把"没测"当"坏"会凭空造出「整频道不可用」的假象，用户照着这份清单删源就是误删；
#   * 反方向的漏报几乎没有代价：清单只是给人看的只读视图，多等一轮测速再判定，
#     最坏只是"晚一点出现"，而误报会直接导致删掉还能用的源 —— 两种错误的代价不对称。
#   * 同理 blocked（出站守卫拦截，压根没测成）与 purged（季度清理的人工停用）也不算判死：
#     前者是"没测"，后者是"人工决定不用了"，都不等于"测出来是坏的"。两者都会让该频道
#     **不出现**在清单里（宁可漏报，不误报）。线上当前没有这两种状态的活跃条目，
#     取严格口径不会改变任何一行结果，但语义上必须写清楚。
#
# 纯查询：下面这一段没有任何 INSERT/UPDATE/DELETE，也不 bump_revision —— 清单只复述
# 库里已经测出来的事实。用户要的是"看清单"来决定删源还是补源，不是"自动删"，
# 所以这条接口刻意不提供任何破坏性入口。
_ALIVE_ENTRY_SQL = (
    "h.fingerprint IS NOT NULL AND COALESCE(h.status,'') NOT IN ('error','unsupported')"
)

# 阶段 1：符合条件的 channel_key 全量有序列表（带展示字段）。一次聚合扫过全部活跃条目
# （线上 29210 条 / 表 72538 行），是这条接口的全部开销：实测冷 71ms、热进程重算 66ms
# （应用连接带 mmap_size，同一 SQL 不开 mmap 要 138ms）。
# 它只依赖数据版本，所以按 current_revision() 缓存 —— 同步、测速落库、剔除、复活
# 都会 bump_revision，旧键自然作废；TTL 与容量上限只是兜底。
_DEAD_KEYS_TTL = 60.0
_DEAD_KEYS_CACHE_MAX = 2
_DEAD_KEYS_LOCK = threading.Lock()
_DEAD_KEYS_CACHE: dict[int, tuple[float, list[dict]]] = {}

# 走的是既有索引（EXPLAIN QUERY PLAN 实测，见 /api/dead-channels 的 docstring）：
#   SEARCH e USING INDEX idx_entries_active_channel (active=?)
#   SEARCH h USING INDEX sqlite_autoindex_stream_health_1 (fingerprint=?) LEFT-JOIN
# 即"按 active 扫条目 + 按主键点查健康行"，没有全表扫描，**不需要新建索引**。
# group by 走 idx_entries_active_channel 的 (active, channel_key) 前缀，不额外排序；
# 只有 3514 个分组结果要过一遍临时 B 树做 ORDER BY。
_DEAD_KEYS_SQL = f"""
SELECT e.channel_key,
       -- 展示用的代表行（分组/名称/区域）与排序键合成同一个 blob：MIN() 的 bare-column
       -- 语义保证三个字段来自**同一行**，而按 blob 排序等价于按 (group_title, name, region)
       -- 排序（char(31) 小于任何可打印字符）。写成三个 MIN() 会各自挑不同的行，
       -- 出现"分组取自 A 源、名称取自 B 源"的错配。
       MIN(e.group_title || char(31) || e.name || char(31) || e.region) AS rep
FROM source_entries e
LEFT JOIN stream_health h ON h.fingerprint=e.stream_fingerprint
WHERE e.active=1
GROUP BY e.channel_key
HAVING SUM(CASE WHEN {_ALIVE_ENTRY_SQL} THEN 1 ELSE 0 END)=0
ORDER BY rep, e.channel_key
"""


def _dead_channel_rows() -> list[dict]:
    """阶段 1：符合判据的频道全量有序列表（channel_key + 分组/名称/区域），带 revision 缓存。

    缓存的 key 就是 current_revision()：任何会改变健康度或目录的动作都会让版本号自增，
    旧条目自然作废。计算放在锁内（单飞）：这条聚合是全站第二贵的查询，缓存 miss 时让
    并发请求各跑一遍只会白白叠加 CPU；持锁的开销远小于重复计算。
    """
    revision = current_revision()
    with _DEAD_KEYS_LOCK:
        hit = _DEAD_KEYS_CACHE.get(revision)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        rows: list[dict] = []
        for row in db.rows(_DEAD_KEYS_SQL):
            # rep 一定是三段（三个字段都用 char(31) 拼的，字段本身不含该控制字符）
            group_title, name, region = row["rep"].split("\x1f", 2)
            rows.append({
                "channel_key": row["channel_key"],
                "name": name,
                "group_title": group_title,
                "region": region,
            })
        _DEAD_KEYS_CACHE[revision] = (time.monotonic() + _DEAD_KEYS_TTL, rows)
        while len(_DEAD_KEYS_CACHE) > _DEAD_KEYS_CACHE_MAX:
            _DEAD_KEYS_CACHE.pop(next(iter(_DEAD_KEYS_CACHE)), None)
        return rows


def _dead_channel_items(page_rows: list[dict]) -> list[dict]:
    """阶段 2：只对本页 ≤200 个频道补明细（实测 ~1ms，与全量聚合无关）。

    dead_streams 与判据用的是同一个定义（error/unsupported 或没有健康行），
    所以在判据成立时它恒等于 stream_count —— 这正是"全部线路判死"的定义，
    两个字段都返回是为了让调用方能自校验，也方便前端两种列表共用一套渲染。
    best_speed 与频道目录同口径（只看 status='ok' 且 speed 非空的最快值）：
    判据保证本清单里没有 ok 线路，因此它恒为 null，字段保留只为契约完整。
    """
    if not page_rows:
        return []
    keys = [row["channel_key"] for row in page_rows]
    values = ", ".join("(?)" for _ in keys)
    agg = {
        row["channel_key"]: row
        for row in db.rows(
            f"""
            WITH page(channel_key) AS (VALUES {values})
            SELECT e.channel_key,
                   COUNT(DISTINCT e.stream_fingerprint) AS stream_count,
                   COUNT(DISTINCT CASE WHEN h.fingerprint IS NULL
                                         OR h.status IN ('error','unsupported')
                                        THEN e.stream_fingerprint END) AS dead_streams,
                   MAX(h.checked_at) AS last_checked_at,
                   MAX(CASE WHEN h.status='ok' AND h.speed_kbps IS NOT NULL
                            THEN h.speed_kbps END) AS best_speed
            FROM source_entries e
            LEFT JOIN stream_health h ON h.fingerprint=e.stream_fingerprint
            WHERE e.active=1 AND e.channel_key IN (SELECT channel_key FROM page)
            GROUP BY e.channel_key
            """,
            keys,
        )
    }
    # 源名单独取：GROUP_CONCAT(DISTINCT ...) 不允许自定义分隔符，源名里带逗号就会切错；
    # 这里按 (priority, source_slug) 定序取前 3 个，另用 source_count 给出真实总数。
    sources: dict[str, list[str]] = {}
    source_count: dict[str, int] = {}
    for row in db.rows(
        f"""
        WITH page(channel_key) AS (VALUES {values})
        SELECT DISTINCT e.channel_key, s.priority, e.source_slug, s.name AS source_name
        FROM source_entries e
        JOIN sources s ON s.slug=e.source_slug
        WHERE e.active=1 AND e.channel_key IN (SELECT channel_key FROM page)
        ORDER BY e.channel_key, s.priority, e.source_slug
        """,
        keys,
    ):
        names = sources.setdefault(row["channel_key"], [])
        source_count[row["channel_key"]] = source_count.get(row["channel_key"], 0) + 1
        if len(names) < 3:
            names.append(row["source_name"])
    items: list[dict] = []
    for page_row in page_rows:      # 严格按阶段 1 的排序返回
        detail = agg.get(page_row["channel_key"])
        if detail is None:          # 同一版本内键集合稳定，理论上不会走到
            continue
        items.append({
            **page_row,
            "stream_count": detail["stream_count"],
            "dead_streams": detail["dead_streams"],
            "best_speed": detail["best_speed"],
            "last_checked_at": detail["last_checked_at"] or "",
            "sources": sources.get(page_row["channel_key"], []),
            "source_count": source_count.get(page_row["channel_key"], 0),
        })
    return items


@app.get("/api/dead-channels", dependencies=[Depends(require_admin)])
def dead_channels(
    page: Annotated[int, Query(ge=1, le=2000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
):
    """「全部线路判死」频道清单（只读，v49）。

    判据：某频道所有 active=1 条目的 stream_health 状态都属于判死
    （`error` / `unsupported`）**或**没有健康行；只要有**一条** `ok` 或**未测**
    （status 为空/NULL）就不算 —— **没测 ≠ 坏**，把没测当坏会凭空造出"整频道不可用"，
    用户据此删源就是误删。`blocked`（守卫拦截，没测成）与 `purged`（人工季度停用）
    同样不算判死，两者都让该频道不出现（宁可漏报，不误报）。判据的完整论证见本文件
    `_ALIVE_ENTRY_SQL` 上方注释。

    返回 `{count, items, page, page_size}`，items 每项：
      * `channel_key` / `name` / `group_title` / `region`
      * `stream_count`：该频道活跃线路数（同一 stream_fingerprint 跨源只算一条）
      * `dead_streams`：判死线路数（与判据同定义，成立时恒等于 stream_count）
      * `best_speed`：与频道目录同口径（只看 ok 线路），本清单里恒为 null
      * `last_checked_at`：所有线路里最近一次检查时间，没有检查记录时为空串
      * `sources`：涉及的源名（按 priority/source_slug 前 3 个）、`source_count` 总数

    分页默认 50、上限 200（`page` ≤ 2000）；排序固定为 (分组, 名称, channel_key)，
    同一数据版本内翻页稳定，不会漏频道或重复。

    **纯查询**：不写库、不 bump_revision、不触发任何后台任务；破坏性操作一律没有 ——
    用户要的是"看清单"来决定删源还是补源，不是"自动删"。鉴权与其他只读接口一致
    （`Depends(require_admin)`）。

    性能（生产库实测，72538 行 source_entries / 29210 条活跃条目 / 3514 个命中频道）：
    阶段 1 冷 71ms、热进程重算 66~68ms（命中 revision 缓存后为 0）；阶段 2 每页 50 键约 1.5ms；
    端到端 冷 81ms / 缓存过期重算 99ms / 缓存命中 7ms。EXPLAIN QUERY PLAN（阶段 1）：
        SEARCH e USING INDEX idx_entries_active_channel (active=?)
        SEARCH h USING INDEX sqlite_autoindex_stream_health_1 (fingerprint=?) LEFT-JOIN
        USE TEMP B-TREE FOR ORDER BY   -- 只排 3514 个分组结果，不是 29210 行条目
    既有索引够用，未新增任何索引。
    """
    rows = _dead_channel_rows()
    count = len(rows)
    # 深分页上界与 /api/channels 对齐：越界就是空页，但 count 照常返回，
    # 前端能据此把页码收敛回来（否则会停在"第 196 / 194 页"的空列表上）。
    page_rows = rows[(page - 1) * page_size: page * page_size]
    return {
        "count": count,
        "items": _dead_channel_items(page_rows),
        "page": page,
        "page_size": page_size,
    }


@app.put("/api/favorites/{channel_key:path}", dependencies=[Depends(require_admin)])
def add_favorite(channel_key: str):
    exists = db.row("SELECT 1 AS ok FROM source_entries WHERE active=1 AND channel_key=? LIMIT 1", (channel_key,))
    if not exists:
        raise HTTPException(status_code=404, detail="频道不存在")
    db.execute("INSERT OR IGNORE INTO favorites(channel_key, created_at) VALUES (?, ?)", (channel_key, utc_now()))
    bump_revision()  # 收藏会影响 favorite=true 的导出内容，版本号必须自增
    return {"ok": True}


@app.delete("/api/favorites/{channel_key:path}", dependencies=[Depends(require_admin)])
def remove_favorite(channel_key: str):
    db.execute("DELETE FROM favorites WHERE channel_key=?", (channel_key,))
    bump_revision()
    return {"ok": True}


@app.post("/api/sync", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_admin)])
async def start_sync(source: str = ""):
    if source and not await asyncio.to_thread(db.row, "SELECT 1 AS ok FROM sources WHERE slug=?", (source,)):
        raise HTTPException(status_code=404, detail="数据源不存在")
    run_id, queued = await sync_manager.queue("manual", source)
    if not queued:
        raise HTTPException(status_code=409, detail={"message": "已有同步任务运行中", "run_id": run_id})
    return {"run_id": run_id, "status": "queued"}


@app.get("/api/sync-runs", dependencies=[Depends(require_admin)])
def list_sync_runs(limit: int = 30):
    rows = db.rows(
        "SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT ?",
        (max(1, min(limit, 200)),),
    )
    return rows


@app.post("/api/maintenance", dependencies=[Depends(require_admin)])
async def run_maintenance():
    """手动触发一次「每周维护」：清理历史数据 + WAL checkpoint + VACUUM。

    平时由同步任务末尾按 7 天节流自动执行，这里用于按需立即跑一次。
    """
    stats = await run_maintenance_now()
    return {"ok": True, **stats}


@app.post("/api/sync-runs/clear", dependencies=[Depends(require_admin)])
async def clear_sync_runs(keep: int = 0):
    """手动清理同步历史记录（保留最近 keep 条；进行中的任务不受影响）。"""
    keep = max(0, min(keep, 1000))
    # 与每周维护共用同一段 SQL（db.prune_sync_runs），否则两处口径迟早对不上
    deleted = await asyncio.to_thread(db.prune_sync_runs, keep)
    return {"deleted": deleted}


@app.post("/api/speed-test", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_admin)])
async def start_speed_test(
    scope: str = "favorites",
    channel_key: str = "",
    refresh: bool = False,
    region: str = "",
    limit: Annotated[int, Query(ge=1, le=5000)] = 300,
):
    if scope not in {"favorites", "all", "channel"}:
        raise HTTPException(status_code=400, detail="scope 只能是 favorites、all 或 channel")
    if region not in {"", "domestic", "international"}:
        raise HTTPException(status_code=400, detail="region 只能是 domestic 或 international")
    if scope == "channel" and not channel_key:
        raise HTTPException(status_code=400, detail="scope=channel 需要提供 channel_key")
    started = await speed_tester.start(scope, channel_key, limit, refresh, region)
    if not started:
        raise HTTPException(status_code=409, detail={"message": "已有测速任务运行中"})
    return {"status": "queued", "progress": speed_tester.progress}


@app.get("/api/speed-test", dependencies=[Depends(require_admin)])
def speed_test_status():
    return speed_tester.progress


@app.post("/api/prune", dependencies=[Depends(require_admin)])
def prune_streams(apply: bool = False, min_fail: Annotated[int | None, Query(ge=1, le=20)] = None):
    """剔除被判定失效的线路。apply=false 时只统计不落库。

    默认阈值 = PRUNE_MAX_FAIL + 1，即「标记 N 次后，下一次仍失败才剔除」。
    """
    threshold = min_fail if min_fail is not None else settings.prune_max_fail + 1
    dead = db.row(
        """
        SELECT COUNT(*) AS entries, COUNT(DISTINCT channel_key) AS channels
        FROM source_entries
        WHERE active=1 AND stream_fingerprint IN (
            SELECT fingerprint FROM stream_health WHERE status IN ('error', 'unsupported') AND fail_count >= ?
        )
        """,
        (threshold,),
    ) or {"entries": 0, "channels": 0}
    alive = db.row(
        """
        SELECT COUNT(*) AS entries FROM source_entries
        WHERE active=1 AND stream_fingerprint IN (SELECT fingerprint FROM stream_health WHERE status='ok')
        """
    ) or {"entries": 0}
    if not apply:
        return {"dry_run": True, "threshold": threshold, "would_remove_entries": dead["entries"], "affected_channels": dead["channels"], "kept_healthy_entries": alive["entries"]}
    with db.write() as conn:
        cursor = conn.execute(
            """
            UPDATE source_entries SET active=0
            WHERE active=1 AND stream_fingerprint IN (
                SELECT fingerprint FROM stream_health WHERE status IN ('error', 'unsupported') AND fail_count >= ?
            )
            """,
            (threshold,),
        )
        # removed_entries 必须取本次 UPDATE 的实际行数：统计查询在写事务**之外**先算好，
        # 两者之间若有测速落库/同步完成，旧计数与真实删除数会对不上
        removed = cursor.rowcount
    if removed:
        bump_revision()
    return {"dry_run": False, "threshold": threshold, "removed_entries": removed, "affected_channels": dead["channels"], "kept_healthy_entries": alive["entries"]}


# v50：定向恢复每个维度最多接受的 token 数（拆完逗号之后）。
# `?fingerprint=a&fingerprint=b&…` 可以把 URL 长度直接变成 SQL 里 IN(...) 的长度，
# 没有上限时一个请求就能拼出几十万项的 IN 列表。500 对人工/脚本回退都够用
# （v50 的 CCTV2 清理一共 20 条）。
_RESTORE_FILTER_MAX = 500


def _restore_filter(tokens: list[str] | None, field: str) -> list[str]:
    """把 `?fingerprint=a&fingerprint=b` 与 `?fingerprint=a,b` 两种写法归一成有序去重列表。

    空 token（`?fingerprint=` 或 `a,,b`）一律 400，**不能**静默当成"没给"：
    "一个参数都不传" = 恢复全部（v50 之前的旧行为，必须保留），而"传了但是空值"
    几乎只能是调用方拼串拼错了 —— 两者混淆会让一次笔误变成全库 4 万多条一起复活。
    """
    if not tokens:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for chunk in tokens:
        for piece in str(chunk).split(","):
            value = piece.strip()
            if not value:
                raise HTTPException(status_code=400, detail=f"{field} 含空值")
            if value not in seen:
                seen.add(value)
                out.append(value)
    if len(out) > _RESTORE_FILTER_MAX:
        raise HTTPException(status_code=400, detail=f"{field} 最多 {_RESTORE_FILTER_MAX} 项")
    return out


@app.post("/api/prune/restore", dependencies=[Depends(require_admin)])
def restore_streams(
    fingerprint: Annotated[list[str] | None, Query(description="按线路指纹定向恢复，可重复或逗号分隔")] = None,
    channel_key: Annotated[list[str] | None, Query(description="按频道键定向恢复，可重复或逗号分隔")] = None,
    source: Annotated[list[str] | None, Query(description="按数据源 slug 定向恢复，可重复或逗号分隔")] = None,
):
    """恢复被剔除的线路（下次同步也会按源重新激活）。

    配置了 KEEP_REGION 时只恢复该区域：另一区域的 inactive 条目大多是区域策略
    主动剔除的，一并恢复会让它们短暂回到订阅里，直到下次同步才被重新剔除。

    **v50：支持定向恢复**（`?fingerprint=` / `?channel_key=` / `?source=`，
    每个参数可重复出现、也可写成逗号分隔；同一维度内是 OR，跨维度是 AND）。
    不传任何参数时语义与 v50 之前**完全一致**（恢复全部 inactive，仍遵守 KEEP_REGION）。

    为什么必须能定向：清理类操作（v48 长期失效清理、v50 的重复死频道清理）都是
    「置 active=0」，而在 v50 之前这里只有"全恢复"一个入口 —— 线上 43380 条 inactive
    里绝大多数是「源站已不再列出」或「区域策略剔除」的行，按一次就把它们一起拉回
    订阅与测速队列；想只回退自己刚清的那几十条，除了整包还原没有别的办法。

    过滤值打错时返回 **400**（整体没匹配到任何条目）而不是 `restored_entries=0`：
    静默的 0 会让人以为"已经恢复过了"，而真相是参数写错了。参数合法但目标当前
    都是 active=1（没东西可恢复）时仍返回 200 + `restored_entries=0`。
    """
    fingerprints = _restore_filter(fingerprint, "fingerprint")
    channel_keys = _restore_filter(channel_key, "channel_key")
    sources = _restore_filter(source, "source")
    filtered = bool(fingerprints or channel_keys or sources)
    keep = settings.keep_region if settings.keep_region in {"domestic", "international"} else ""

    # 只拼列名与 `?` 占位符，值全部走参数绑定 —— 这里不接受任何来自请求的 SQL 片段
    clauses: list[str] = []
    params: list[object] = []
    for column, values in (("stream_fingerprint", fingerprints), ("channel_key", channel_keys), ("source_slug", sources)):
        if values:
            clauses.append(f"{column} IN ({', '.join('?' * len(values))})")
            params.extend(values)
    scope = f" AND {' AND '.join(clauses)}" if clauses else ""

    matched = 0
    if filtered:
        matched = db.row(f"SELECT COUNT(*) AS n FROM source_entries WHERE 1=1{scope}", params)["n"]
        if not matched:
            raise HTTPException(status_code=400, detail="未匹配到任何条目")

    with db.write() as conn:
        where = f"active=0{scope}" + (" AND region=?" if keep else "")
        cursor = conn.execute(f"UPDATE source_entries SET active=1 WHERE {where}", params + ([keep] if keep else []))
        if keep:
            # 与 UPDATE 同口径：本次过滤器命中的行里，还有多少因为区域策略留在 inactive
            skipped = conn.execute(
                f"SELECT COUNT(*) AS n FROM source_entries WHERE active=0 AND region<>?{scope}",
                [keep] + params,
            ).fetchone()["n"]
        else:
            skipped = 0
    if cursor and cursor.rowcount:
        bump_revision()
    result: dict[str, object] = {"restored_entries": cursor.rowcount if cursor else 0}
    if filtered:
        result["filters"] = {name: values for name, values in
                             (("fingerprint", fingerprints), ("channel_key", channel_keys), ("source", sources)) if values}
        result["matched_entries"] = matched
    if keep:
        result["kept_inactive_by_region_policy"] = skipped
        result["keep_region"] = keep
    return result


@app.post("/api/prune/revive", dependencies=[Depends(require_admin)])
def revive_streams():
    """复活冷却期满（默认 14 天）的失效线路，重新纳入检测并清空标记。"""
    revived = revive_expired()
    if revived:
        bump_revision()
    return {"revived_entries": revived}


@app.get("/api/purged-streams", dependencies=[Depends(require_admin)])
def list_purged_streams():
    """季度清理永久停用的线路概况（可撤销，所以只给计数与样例）。"""
    total = purged_count()
    samples = db.rows(
        "SELECT url, error FROM stream_health WHERE status='purged' ORDER BY checked_at DESC LIMIT 20"
    )
    return {
        "count": total,
        "sample": samples,
        "restore_hint": "POST /api/purged-streams/restore 可把它们放回复测周期",
    }


@app.post("/api/purged-streams/restore", dependencies=[Depends(require_admin)])
async def restore_purged():
    """撤销季度清理：把已停用的线路放回正常失效态，重新参与复测周期。"""
    restored = await asyncio.to_thread(restore_purged_streams)
    if restored:
        bump_revision()
    logger.info("撤销季度清理：恢复 %s 条线路", restored)
    return {"restored_streams": restored}


@app.post("/api/prune/purge-revived", dependencies=[Depends(require_admin)])
async def purge_revived(apply: bool = False):
    """季度清理：永久停用「复活过、复活后仍然失效」的线路（apply=false 只统计）。

    判定 = revive_count >= PURGE_MIN_REVIVE 且 fail_count >= 1（复活会清零，所以它表示
    「复活后又失败」）且当前 status 为 error/unsupported。
    停用是置 status='purged' 的墓碑语义，同步再抓到同一 URL 也不会复活；可撤销。
    """
    result = await asyncio.to_thread(purge_revived_streams, apply)
    if apply and result.get("purged"):
        bump_revision()
    logger.info("季度清理（apply=%s）：%s", apply, result)
    return result


@app.post("/api/apply-region-policy", dependencies=[Depends(require_admin)])
def apply_region_policy(region: str = ""):
    """按区域保留策略剔除另一区域的线路（国外剔除 = region=domestic）。

    显式传 region 时立即按该区域执行；留空则跟随 KEEP_REGION 配置。
    剔除采用 active=0 语义（记录保留，可随下次同步/恢复操作重新激活）。
    """
    # 配置值也要归一化：数据层 apply_region_policy 内部会 strip().lower()，
    # 端点这里不处理的话，KEEP_REGION="Domestic "（带空格/大写）走 API 就是静默空操作，
    # 同一配置两条路径行为相反。
    target = region.strip().lower() if region.strip() else (settings.keep_region or "").strip().lower()
    if target not in {"domestic", "international"}:
        return {"removed_entries": 0, "keep_region": target or "all", "message": "未指定有效区域，未做任何操作"}
    removed = db.apply_region_policy(target)
    if removed:
        bump_revision()
    return {"removed_entries": removed, "keep_region": target}


@app.post("/api/speed-test/cancel", dependencies=[Depends(require_admin)])
async def cancel_speed_test():
    await speed_tester.cancel()
    return {"ok": True}


def _trim_export_cache(now: float) -> None:
    """注意：调用方必须持有 _EXPORT_CACHE_LOCK。

    sum(...values()) 与 pop(next(iter(...))) 都在遍历/取键，而端点跑在 FastAPI 线程池里，
    并发命中时会被另一线程的写入改变字典大小 → RuntimeError: dictionary changed size
    during iteration → 播放器定时刷新偶发 500（实测 8 线程压测可复现）。
    """
    for key, cached in [item for item in list(_EXPORT_CACHE.items()) if item[1][0] <= now]:
        _EXPORT_CACHE.pop(key, None)
    while len(_EXPORT_CACHE) > 1 and (
        len(_EXPORT_CACHE) > EXPORT_CACHE_MAX
        or sum(len(entry[1]) for entry in _EXPORT_CACHE.values()) > EXPORT_CACHE_MAX_BYTES
    ):
        _EXPORT_CACHE.pop(next(iter(_EXPORT_CACHE)), None)
    # 渲染锁字典按同一套键限容：key 里含用户自定义的 source/region 字符串，
    # 不清理就等于给每个随机组合留一把锁，内存无上限增长。
    # 正在渲染（locked）的锁跳过，由下一轮清理接手。
    with _EXPORT_RENDER_GUARD:
        for key in [k for k in _EXPORT_RENDER_LOCKS if k not in _EXPORT_CACHE]:
            if not _EXPORT_RENDER_LOCKS[key].locked():
                _EXPORT_RENDER_LOCKS.pop(key, None)


@app.get("/epg.xml")
@app.get("/epg")
def epg_proxy():
    """**本机 EPG 代理**：导出的 M3U 头部指向这里，上游 EPG 地址（含个人令牌）不再外泄。

    以前 `x-tvg-url` 直接写 `EPG_URL`，于是每个拿到订阅的人（播放器、被分享的列表）
    都拿到了那个**长期有效的 EPG 令牌**。现在播放器只访问本机 `/epg.xml`，令牌只留在服务端。

    设计要点：
      * **缓存落盘** `data/epg-cache.xml`：XMLTV 动辄几十 MB，常驻内存不划算；落盘还能跨重启复用。
      * TTL（`EPG_PROXY_TTL_HOURS`，默认 6 小时）内直接回缓存，过期才回源。
      * **回源失败回退旧缓存**（stale-while-error）：节目单旧一点无所谓，不该因此看不了电视；
        两份都没有才 503，并给出原因。
      * **单飞**：多个设备/播放器会同时来拉，用锁保证只触发一次回源。
      * 只抓 `EPG_URL` 这一个运维配置的地址，**不接受任何请求参数** → 不存在 SSRF 面。
      * 上游若直接给 `.xml.gz`（没有 Content-Encoding），这里自己解压后按明文 XML 下发，
        播放器无需额外支持 gzip；解压同样设上限，防解压炸弹。
    """
    if not settings.epg_url:
        raise HTTPException(status_code=404, detail="未配置 EPG_URL，本机 EPG 代理不可用")
    cache = settings.epg_cache_path
    ttl = settings.epg_proxy_ttl_hours * 3600
    fresh = cache.exists() and (time.time() - cache.stat().st_mtime) < ttl
    if not fresh:
        # **非阻塞**踢一次后台回源：请求线程绝不等待它。以前是在请求线程里持锁同步回源，
        # 每个等待者都占住一个 anyio 线程池线程（上限 40）—— 上游卡住时 /healthz 与所有
        # def 端点一起被堵（实测 44 并发时 /healthz 从 ~10ms 变 2.4s，docker healthcheck 会超时）。
        _epg_refresh_in_background()
    if not cache.exists():
        detail = f"：{_EPG_LAST_ERROR}" if _EPG_LAST_ERROR else "，正在首次抓取，请稍后重试"
        raise HTTPException(status_code=503, detail=f"EPG 暂不可用{detail}")
    cache_headers = {
        # 陈旧内容（回源失败时的回退）**不能**带长 max-age：否则播放器会把过期节目单
        # 再缓存 TTL 那么久，上游恢复了也看不到新的。
        "Cache-Control": f"public, max-age={ttl}" if fresh else "no-cache",
    }
    try:
        size = cache.stat().st_size
    except OSError:
        raise HTTPException(status_code=503, detail="EPG 缓存不可用") from None
    if size <= _EPG_SNAPSHOT_MAX_BYTES:
        # 小文件先整份读进内存再下发：`FileResponse` 是先 stat 再 open，中间被后台
        # `os.replace` 换掉就会出现 Content-Length 与实体不一致（压测可复现，播放器拿到截断 EPG）。
        # 一次读入 = 一个原子快照，彻底没有这个窗口。
        try:
            body = cache.read_bytes()
        except OSError:
            raise HTTPException(status_code=503, detail="EPG 缓存不可用") from None
        return Response(content=body, media_type="application/xml; charset=utf-8",
                        headers=cache_headers)
    # 超过阈值（几十 MB 的节目单）不占内存，接受极低概率的竞态
    return FileResponse(cache, media_type="application/xml; charset=utf-8", headers=cache_headers)


def _epg_refresh_in_background() -> None:
    """非阻塞单飞：已在抓取、或距上次尝试不足退避时间，就直接返回。"""
    global _EPG_REFRESHING, _EPG_LAST_ATTEMPT
    now = time.monotonic()
    with _EPG_FETCH_LOCK:
        if _EPG_REFRESHING or (now - _EPG_LAST_ATTEMPT) < _EPG_BACKOFF_SECONDS:
            return
        _EPG_REFRESHING = True
        _EPG_LAST_ATTEMPT = now

    def worker() -> None:
        global _EPG_REFRESHING, _EPG_LAST_ERROR
        try:
            error = _epg_refresh()
            _EPG_LAST_ERROR = error
            if error:
                logger.warning("EPG 回源失败：%s", error)
        except Exception:  # noqa: BLE001 - 后台线程不能把异常抛出去
            logger.exception("EPG 回源线程异常")
        finally:
            with _EPG_FETCH_LOCK:
                _EPG_REFRESHING = False

    threading.Thread(target=worker, name="epg-refresh", daemon=True).start()


def _epg_refresh() -> str:
    """回源更新 EPG 磁盘缓存；返回错误说明（成功为空串）。**同步阻塞**，由线程池执行。"""
    cache = settings.epg_cache_path
    body = b""
    current = settings.epg_url
    try:
        timeout = httpx.Timeout(settings.request_timeout, connect=12)
        headers = {"User-Agent": SOURCE_USER_AGENT, "Accept": "application/xml, text/xml, */*"}
        # **不能用 follow_redirects=True**：上游一个 302 就能把回源打到内网地址，
        # 而且响应体会被写进缓存、再公开下发给所有播放器（实测可拿到 /api/stats 的 JSON）。
        # 手动逐跳跟随，并且每一跳都要求目标是公网地址（EPG 是运维配置的公开地址，
        # 跳到内网/环回只可能是被利用，所以这里连内网一起拦）。
        with httpx.Client(timeout=timeout, follow_redirects=False, headers=headers) as client:
            for _ in range(_EPG_MAX_REDIRECTS + 1):
                host = (urlsplit(current).hostname or "").lower()
                if not host or host in LOOPBACK_HOSTS:
                    return f"拒绝的目标主机：{host or 'missing'}"
                blocked = restricted_address(host, include_private=True)
                if blocked:
                    return f"目标解析到受限地址 {host} -> {blocked}，已拒绝"
                with client.stream("GET", current) as response:
                    if response.status_code in REDIRECT_STATUSES:
                        location = response.headers.get("location", "")
                        if not location:
                            return f"HTTP {response.status_code} 响应缺少 Location 头"
                        current = urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        return f"HTTP {response.status_code}"
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > settings.epg_proxy_max_bytes:
                            return f"内容超过上限 {settings.epg_proxy_max_bytes} 字节"
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    break
            else:
                return f"跳转次数超过 {_EPG_MAX_REDIRECTS}"
    except Exception as exc:  # noqa: BLE001 - 回源失败要回落旧缓存，不能把异常抛给播放器
        return f"{type(exc).__name__}: {str(exc)[:120]}"
    if not body:
        return "上游返回空内容"
    if body[:2] == b"\x1f\x8b":
        try:
            with gzip.open(io.BytesIO(body)) as fh:
                body = fh.read(settings.epg_proxy_max_bytes + 1)
        except Exception as exc:  # noqa: BLE001
            return f"gzip 解压失败：{type(exc).__name__}"
        if len(body) > settings.epg_proxy_max_bytes:
            return "解压后超过大小上限"
    head = body[:512].lstrip()
    if not (head.startswith(b"<?xml") or b"<tv" in head):
        # 上游 200 返回 HTML 登录页/错误页时若写进缓存，就会按 application/xml 下发满 TTL，
        # 播放器拿不到节目单还以为拉到了（实测 HTML 入库后 TTL 内不再回源）。
        return "内容不是 XMLTV（缺少 <?xml / <tv）"
    try:
        tmp = cache.with_name(cache.name + ".tmp")
        tmp.write_bytes(body)
        os.replace(tmp, cache)      # 原子替换：播放器随时来拉都读到完整文件
    except Exception as exc:  # noqa: BLE001
        return f"写缓存失败：{type(exc).__name__}"
    logger.info("EPG 缓存已更新：%s 字节 -> %s", len(body), cache.name)
    return ""


def _epg_proxy_url_for(request: Request) -> str:
    """导出头部要用的本机 EPG 地址（`{base}/epg.xml`）。

    优先 `PUBLIC_BASE_URL`：静态文件与所有客户端一致，也让导出缓存键稳定；
    没配就按**请求的 Host** 推断 —— 客户端用哪个地址拉订阅，就拿回哪个地址的 EPG，
    局域网 IP、主机名、反向代理域名都能自适应。未配置 EPG_URL 时返回空（不写 x-tvg-url）。
    """
    if not settings.epg_url:
        return ""
    if settings.public_base_url:
        return f"{settings.public_base_url}/epg.xml"
    base = str(request.base_url).rstrip("/")
    return f"{base}/epg.xml" if base else ""


def _export_plain(
    key: tuple,
    filename: str,
    make: Callable[[], tuple[bytes, str]],
    if_none_match: str = "",
    substitute: tuple[str, str] | None = None,
    stats: dict | None = None,
) -> Response:
    """订阅导出：内存 TTL 缓存 + ETag 条件请求（304）。

    - 60 秒内同参数请求直接复用已生成内容（注意：TTL 内数据版本变化**不会**主动失效缓存，
      刚同步/测速完最长 60 秒内仍可能返回旧内容 —— 这是刻意的取舍，测速期间每批落库都会
      bump_revision，把版本号放进缓存键等于让缓存在测速期间全废）；
    - 客户端带 If-None-Match 且匹配时返回 304，几乎零开销；
    - v42：`stats` 是渲染层写回的过滤统计（隐藏了多少判死线路 / 有多少频道靠保底留下）。
      它随缓存一起保存，所以**缓存命中时也能给出同一组数字**（否则观测字段时有时无）。
    """
    now = time.monotonic()
    with _EXPORT_CACHE_LOCK:
        cached = _EXPORT_CACHE.get(key)
    if cached and cached[0] > now:
        payload, media_type, etag = cached[1], cached[2], cached[3]
        if stats is not None and len(cached) > 4:
            stats.update(cached[4])
    else:
        with _export_render_lock(key):
            # 等锁期间别人可能已经把这份渲染好并写进缓存了 —— 这就是 single-flight 的意义
            now = time.monotonic()
            with _EXPORT_CACHE_LOCK:
                cached = _EXPORT_CACHE.get(key)
            if cached and cached[0] > now:
                payload, media_type, etag = cached[1], cached[2], cached[3]
                if stats is not None and len(cached) > 4:
                    stats.update(cached[4])
            else:
                # 限制同时在飞的渲染数：见 _EXPORT_RENDER_SLOTS 的说明（内存上限的根因）
                with _EXPORT_RENDER_SLOTS:
                    payload, media_type = make()
                etag = f'"{hashlib.sha1(payload).hexdigest()}-{current_revision()}"'
                with _EXPORT_CACHE_LOCK:
                    _EXPORT_CACHE[key] = (now + EXPORT_CACHE_TTL, payload, media_type, etag,
                                          dict(stats or {}))
                    _trim_export_cache(now)
    # If-None-Match 可以是逗号分隔的列表，也可能是 "*" 或带弱校验前缀 W/；
    # 只比第一个值会让部分客户端永远拿不到 304
    if if_none_match:
        candidates = {token.strip().removeprefix("W/") for token in if_none_match.split(",")}
        if "*" in candidates or etag in candidates:
            # RFC 7232：304 应携带与 200 一致的 ETag / Cache-Control
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
    if substitute:
        # 占位符 → 本次请求的 EPG 地址。缓存里始终存占位符，所以缓存键与 ETag 都不随
        # 请求 Host 变化（否则换个 Host 就是一个新键，未鉴权者能反复触发全量渲染）。
        payload = payload.replace(substitute[0].encode(), substitute[1].encode())
    return PlainTextResponse(
        payload,
        media_type=media_type,
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "ETag": etag,
            "Cache-Control": "no-cache",
        },
    )


_EXPORT_MODES = ("single", "multi")
_EXPORT_REGIONS = ("", "domestic", "international")
# 分组名长度上限：分组名会进导出缓存键（每个新键都要全量重渲染一次），
# 库里真实的分组名只有几个汉字，64 字符足够，超长值没有任何合法用途。
# 与 static_export.EXPORT_GROUP_MAXLEN 是同一个数（配置值校验也用它，避免两处漂移）。
_EXPORT_GROUP_MAXLEN = static_export.EXPORT_GROUP_MAXLEN


def _export_dead_key() -> tuple[bool, int]:
    """导出缓存键里的"判死过滤"配置（v42）。

    开关/阈值必须进缓存键：它们直接改变产物内容，若不进键，改了配置后最多 60 秒内
    仍会拿到旧内容，之后新旧还可能混搭（m3u 与 txt 各自缓存、TTL 到期时间不同）。
    """
    return (bool(settings.export_hide_dead), settings.export_dead_fail_count)


def _export_catchup_key() -> tuple[bool, str, int, str]:
    """导出缓存键里的回看配置（v52）：开关、模板、天数、允许的源 slug。

    与 `_export_dead_key` 同理 —— 这四个值直接改变 M3U 产物（属性串、模式 append/default、
    天数、哪些源沾光），不进键就会出现「改了 EXPORT_CATCHUP_SOURCE，60 秒内拉到的还是
    旧模板」甚至新老混搭。**只进 M3U 的键**：TXT 表达不了回看属性，产物与这几个配置无关，
    塞进 TXT 的键只会让 TXT 缓存在改配置时白失效一次。
    """
    return (
        bool(settings.export_catchup),
        (settings.export_catchup_source or "").strip(),
        int(settings.export_catchup_days),
        (settings.export_catchup_source_slugs or "").strip(),
    )


def _export_prefer_key() -> str:
    """导出缓存键里的「偏好源」配置（v53，见 static_export._line_order）。

    与 `_export_dead_key` / `_export_catchup_key` 同理，但作用面更大：偏好源改变的是
    **每个频道选中的那几条线路**（谁排线路 1、哪几条进 MAX_LINES_* 的 top-N），所以
    M3U 与 TXT 两个产物都受影响 —— 两个缓存键都必须带上它。不进键就会出现"改了配置，
    60 秒内拉到的还是旧排序"，而且 m3u/txt 两份缓存的 TTL 到期时刻不同，可能一新一旧。
    """
    return (settings.export_prefer_sources or "").strip()


def _with_export_stats(response: Response, stats: dict, group: str = "",
                       exclude_group: str = "") -> Response:
    """把过滤统计与本次生效的分组参数写进响应头（可观测；数字来自本次渲染或缓存里的那份）。

    放在响应头而不是 body：订阅体是给播放器吃的，不能掺任何非标准内容；
    头部字段名固定，用户/脚本 `curl -sI` 就能对账。

    v43：分组名是中文，而 HTTP 头只能装 latin-1 —— 直接写 '发现源' 会让 Starlette 在
    编码响应头时抛 UnicodeEncodeError（整个请求 500），所以**按 URL 编码**写
    （'发现源' → %E5%8F%91%E7%8E%B0%E6%BA%90），与查询参数的编码方式一致；
    `python -c "import urllib.parse;print(urllib.parse.unquote('%E5%8F%91...'))"` 可还原。
    没生效时写空串，一眼能看出"这次没按分组过滤"。
    """
    response.headers["X-Export-Filtered-Dead"] = str(int(stats.get("filtered_dead", 0)))
    response.headers["X-Export-Kept-Alive-Channels"] = str(int(stats.get("kept_alive_channels", 0)))
    response.headers["X-Export-Hide-Dead"] = "1" if settings.export_hide_dead else "0"
    response.headers["X-Export-Dead-Fail-Count"] = str(settings.export_dead_fail_count)
    response.headers["X-Export-Group"] = quote(group)
    response.headers["X-Export-Exclude-Group"] = quote(exclude_group)
    return response


def _normalize_region(region: str) -> str:
    value = (region or "").strip().lower()
    if value not in _EXPORT_REGIONS:
        raise HTTPException(status_code=400, detail="region 只能是空 / domestic / international")
    return value


def _normalize_source(source: str) -> str:
    value = (source or "").strip()
    if value and not db.row("SELECT 1 AS ok FROM sources WHERE slug=?", (value,)):
        # 不存在的源只会算出空结果，却按新缓存键把全量聚合完整跑一遍
        raise HTTPException(status_code=404, detail="数据源不存在")
    return value


def _normalize_group(value: str, *, field: str = "group") -> str:
    """规范化数据源分组名（group 与 exclude_group 共用，v43）。

    合法取值 = `SELECT DISTINCT source_group FROM sources WHERE source_group<>''`
    里实际存在的分组名（同 `_normalize_source` 的口径）。处理顺序：strip → 限长 64 →
    必须存在，否则 400。

    为什么不存在就 400、而不是"返回空列表"：
      * 分组名会进导出缓存键，而**每个新键都要全量重渲染一次**（1.8~3.5s CPU + 1.5MB 出网，
        见 `_export_params` 的说明）。若允许任意字符串通过，`?group=junk1`、`?group=junk2`…
        循环下去就能把缓存全部打散并稳定烧 CPU —— 那正是 `region` 当初被枚举化的原因；
      * 正向筛选用不存在的分组名只会得到空订阅，在播放器里表现为"什么都没有"，
        属于最难排查的一类故障；400 + 明确 detail 至少能一眼看出参数写错了。
    特意**不**做"不存在就返回空列表"：空产物没有任何用处，却把键空间放大到任意字符串。

    代价（已在测试与报告里写明）：某个分组名以后被删干净（库里再无该 source_group）时，
    带该参数的旧订阅地址会 400，而不是悄悄退化成"不过滤"。去掉该参数、或换成现存分组名即可恢复；
    响应头 `X-Export-Group` / `X-Export-Exclude-Group` 会回显本次真正生效的值，便于对账。

    v43 追加：判存 SQL 与长度上限收敛在 `static_export.existing_group`（配置值走它的宽松
    口径：无效就忽略），这里只负责把"无效"翻译成 400。两条路径因此不会出现判存口径漂移。
    """
    text = (value or "").strip()
    if len(text) > _EXPORT_GROUP_MAXLEN:
        raise HTTPException(status_code=400, detail=f"{field} 最长 {_EXPORT_GROUP_MAXLEN} 个字符")
    if text and not static_export.existing_group(text):
        raise HTTPException(status_code=400, detail=f"{field} 分组不存在")
    return text


def _export_params(mode: str, source: str, region: str, group: str,
                   exclude_group: str) -> tuple[str, str, str, str, str]:
    """规范化导出/筛选参数：只接受枚举内的取值，非法值直接 400。

    这些参数会进导出缓存键，而**每个新键都要全量重渲染一次**（实测 1.8~3.5s CPU +
    1.5MB 出网）。原先任何字符串都被接受，`?region=junk1`、`?region=junk2`… 循环下去
    就能把缓存全部打散并稳定烧 CPU（缓存上限 12 条反而保证互相驱逐），而 region 取值
    非 domestic/international 时根本不过滤、每次都是全量 1.7 万行。
    规范化后键空间收敛为「2 模式 × 3 区域 × 收藏 × 真实存在的源」。

    v43：新增 `group`（只要这个分组）/ `exclude_group`（去掉这个分组）两个数据源分组维度，
    取值同样必须**收敛到库里真实存在的分组名**（见 `_normalize_group`），键空间因此变成
    「… × 真实存在的分组 × 真实存在的排除分组」。两者语义正交、可以叠加：
    同时传同一个分组名 = 空集（这里不额外报 400，交给 SQL 自然求交），
    同时传两个不同分组名 = 「只留 A 且不要 B」。
    """
    mode = (mode or "multi").strip().lower()
    if mode not in _EXPORT_MODES:
        raise HTTPException(status_code=400, detail="mode 只能是 single / multi")
    return (mode, _normalize_source(source), _normalize_region(region),
            _normalize_group(group, field="group"),
            _normalize_group(exclude_group, field="exclude_group"))


def _explicit_group_params(request: Request) -> bool:
    """请求里是否**显式**带了分组参数（哪怕值是空串）。

    判定必须看 query_params，不能看函数默认值：FastAPI 里"没传 group"和"传了 group=''"
    都会落到默认的 ''，只看值分不出这两种情况 —— 而这正是"默认导出"与"用户显式覆盖"的
    分界线（见 `_apply_default_exclude_group`）。
    """
    return "group" in request.query_params or "exclude_group" in request.query_params


def _apply_default_exclude_group(request: Request, group: str,
                                 exclude_group: str) -> tuple[str, str]:
    """给「不带分组参数的默认导出」套上 MAIN_EXCLUDE_GROUP（v43）。

    语义只在这一处，不再散落到各路由：
      * 请求里出现 group 或 exclude_group（**含空值**）= 显式调用，原样使用、配置不介入 ——
        `?exclude_group=` 或 `?group=` 因此是"不改服务端配置就取回全量订阅"的逃生口；
      * 两者都没出现 = 默认导出，配了 MAIN_EXCLUDE_GROUP 就等价于 `?exclude_group=<该组>`；
        没配、或配置的分组已不存在（static_export.main_exclude_group() 返回 ''）时不做任何
        过滤 —— 行为与改动前逐字节一致。

    返回的 exclude_group 照常进 `_export_params` 并进导出缓存键：配置从空改成某分组时
    缓存键跟着变，不会出现"改了配置、60 秒内还发旧内容"的串味。
    `/discovered.m3u|txt` 不走这里：它们的分组是固定常量，属于"显式出口"。
    """
    if _explicit_group_params(request):
        return group, exclude_group
    return group, static_export.main_exclude_group()


def _export_m3u_response(request: Request, mode: str, favorite: bool, source: str, region: str,
                         group: str, exclude_group: str) -> Response:
    """M3U 订阅的公共实现（参数须已过 `_export_params` 规范化）。

    `/iptv`、`/playlist.m3u` 与固定出口 `/discovered.m3u` 共用它，保证三者的过滤、
    缓存、ETag 行为完全一致。
    """
    # 头部写**本机 EPG 代理**地址（不再写上游 EPG_URL，那个带个人令牌）。
    # 它进缓存键：没配 PUBLIC_BASE_URL 时不同 Host 会得到各自的地址，
    # 缓存按 Host 分开是对的（否则先来的客户端会把 127.0.0.1 的地址带给所有人）。
    tvg_url = _epg_proxy_url_for(request)

    def make() -> tuple[bytes, str]:
        # 渲染时写占位符（而不是真实地址）：缓存键与 ETag 因此都与请求 Host 无关
        text = static_export.build_m3u(mode, favorite, source, region,
                                       EXPORT_TVG_PLACEHOLDER if tvg_url else "", stats,
                                       group=group, exclude_group=exclude_group)
        return text.encode("utf-8"), "audio/x-mpegurl; charset=utf-8"

    # v42：过滤统计既进响应头也随缓存一起保存（缓存命中时同样能给出数字）
    stats: dict = {}
    # `top` 只保留在 HTTP 签名里做**兼容**（老订阅地址可能带它），实际不参与任何计算：
    # 渲染层 build_m3u 曾收下这个参数却从不读它，已删；缓存键里也不放它 ——
    # 否则客户端随便换个 top 就能把缓存全部打空（实测不同 top 输出 md5 完全相同）。
    # v43：group / exclude_group **必须**进缓存键 —— 它们直接改变产物内容，不进键就会串味
    #（换个分组参数却拿到上一份渲染结果），这正是 ETag/60s TTL 缓存最容易出错的地方。
    response = _export_plain(
        ("m3u", mode, favorite, source, region, group, exclude_group, _export_dead_key(),
         _export_catchup_key(), _export_prefer_key()),
        "iptv-hub.m3u", make,
        request.headers.get("if-none-match", ""),
        substitute=(EXPORT_TVG_PLACEHOLDER, tvg_url) if tvg_url else None,
        stats=stats,
    )
    return _with_export_stats(response, stats, group, exclude_group)


def _export_txt_response(request: Request, mode: str, favorite: bool, source: str, region: str,
                         group: str, exclude_group: str) -> Response:
    """TXT 订阅的公共实现（参数须已过 `_export_params` 规范化），与 M3U 同源同过滤。"""
    def make() -> tuple[bytes, str]:
        text = static_export.build_txt(mode, favorite, source, region, stats,
                                       group=group, exclude_group=exclude_group)
        return text.encode("utf-8"), "text/plain; charset=utf-8"

    stats: dict = {}
    # v43：与 M3U 同理，分组参数进缓存键（m3u 与 txt 各自缓存，键里已带 mode 标识）
    response = _export_plain(
        ("txt", mode, favorite, source, region, group, exclude_group, _export_dead_key(),
         _export_prefer_key()),
        "iptv-hub.txt", make,
        request.headers.get("if-none-match", ""),
        stats=stats,
    )
    return _with_export_stats(response, stats, group, exclude_group)


@app.get("/iptv")
@app.get("/playlist.m3u")
def export_m3u(request: Request, mode: str = "multi", favorite: bool = False, source: str = "",
               region: str = "", top: int = 5, group: str = "", exclude_group: str = ""):
    """主订阅（M3U）。v43 起支持按数据源分组收窄：

    - `?group=<分组名>`：**只要**这一组（如 `?group=发现源`）；
    - `?exclude_group=<分组名>`：**去掉**这一组（如 `?exclude_group=发现源`）；
    - 都不传 = 与改动前逐字节一致的全量订阅（老订阅地址不受影响）。两个参数可叠加。
    - 都不传且服务端配了 `MAIN_EXCLUDE_GROUP` 时，默认导出自动排除该组（等价于自动带上
      `?exclude_group=<该组>`）；显式带任一分组参数即可覆盖（带空值 = 取回全量）。
    播放器入口，**不需要令牌**（与改动前一致）。
    """
    group, exclude_group = _apply_default_exclude_group(request, group, exclude_group)
    mode, source, region, group, exclude_group = _export_params(mode, source, region, group, exclude_group)
    return _export_m3u_response(request, mode, favorite, source, region, group, exclude_group)


@app.get("/discovered.m3u")
def export_discovered_m3u(request: Request, mode: str = "multi", favorite: bool = False,
                          source: str = "", region: str = "", top: int = 5):
    """「发现源」分组的独立订阅出口（播放器可直接粘贴的固定地址）。

    内部等价于 `/iptv?group=发现源`：共用 `_export_m3u_response`，缓存键里的分组值也是
    同一个 '发现源'，所以两者命中同一条缓存、产物逐字节相同（见 tests/test_export_group_outlets.py）。

    为什么这里的 group **不走** `_normalize_group` 的存在性校验：这个分组名是服务端常量，
    不是用户输入，进缓存键的值恒为 '发现源'，不存在"任意字符串把缓存打散"的风险（那正是
    `group=` 必须校验的原因）。万一 4 个「发现源」源全被删掉、库里再无该分组，本出口会退化成
    **空订阅**而不是 400 —— 已经粘进播放器的固定地址不该因为一次源调整就变成报错。
    其余参数（mode/favorite/source/region）与 /iptv 完全同义；本出口同样**不需要令牌**。
    """
    mode, source, region, _group, _exclude = _export_params(mode, source, region, "", "")
    return _export_m3u_response(request, mode, favorite, source, region, DISCOVERED_SOURCE_GROUP, "")


@app.get("/txt")
def export_txt(request: Request, mode: str = "multi", favorite: bool = False, source: str = "",
               region: str = "", top: int = 5, group: str = "", exclude_group: str = ""):
    """主订阅（TXT）。分组参数与 /iptv 完全同义（见 export_m3u，含 MAIN_EXCLUDE_GROUP 默认排除），同样免令牌。"""
    group, exclude_group = _apply_default_exclude_group(request, group, exclude_group)
    mode, source, region, group, exclude_group = _export_params(mode, source, region, group, exclude_group)
    return _export_txt_response(request, mode, favorite, source, region, group, exclude_group)


@app.get("/discovered.txt")
def export_discovered_txt(request: Request, mode: str = "multi", favorite: bool = False,
                          source: str = "", region: str = "", top: int = 5):
    """「发现源」分组的独立 TXT 出口（等同 /txt?group=发现源，说明见 export_discovered_m3u）。"""
    mode, source, region, _group, _exclude = _export_params(mode, source, region, "", "")
    return _export_txt_response(request, mode, favorite, source, region, DISCOVERED_SOURCE_GROUP, "")


@app.get("/")
def index():
    """返回控制台首页，并把 __APP_VERSION__ 占位符替换成 settings.app_version。

    版本号只有一处来源（settings.app_version），页面标题与 /api/meta 的 version
    都取它，避免再出现「页面 v6、API 1.1.0」这种两套编号。
    按请求读文件（11KB，开销可忽略），这样改 index.html 无需重启容器即可生效。
    """
    html = INDEX_FILE.read_text(encoding="utf-8")
    return HTMLResponse(
        html.replace("__APP_VERSION__", settings.app_version),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )
