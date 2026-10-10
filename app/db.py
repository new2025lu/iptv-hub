from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Iterable, Sequence
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger("iptv-hub.db")

# 同步记录的保留策略 SQL：每周维护（prune_history）与「清空同步记录」端点（prune_sync_runs）
# 必须永远一致，所以抽成常量共用 —— 两处曾经各写一遍，改一处漏一处就会让两个入口口径不同。
_SQL_SYNC_RUNS_KEEP = """
    DELETE FROM sync_runs WHERE status <> 'running' AND id NOT IN (
        SELECT id FROM sync_runs WHERE status <> 'running' ORDER BY started_at DESC LIMIT ?
    )
"""
_SQL_SYNC_RUNS_CLEAR = "DELETE FROM sync_runs WHERE status <> 'running'"

# entry_key 口径版本：与 parsers.entry_key_for() 的公式一一对应。
# 改动那条公式时必须同时改这里，否则存量库会被误判为「已迁移」而跳过重算。
ENTRY_KEY_FORMULA = "base_id+fp:v1"


def _entry_key_sample_ok(conn: sqlite3.Connection, entry_key_for) -> bool:
    """抽查约 400 行，确认存量键确实符合统一公式。

    「已迁移」标记命中时用它兜底：只有当所有抽样行都对得上才跳过全量重算。
    这能挡住「跑过旧版本代码、把旧口径的键写回来」的库（那种库会有大量行不符合）。
    """
    total = conn.execute("SELECT COUNT(*) FROM source_entries").fetchone()[0]
    if not total:
        return True
    step = max(1, total // 4)
    for offset in (0, step, step * 2, step * 3):
        for ek, tvg, fp in conn.execute(
            "SELECT entry_key, tvg_id, stream_fingerprint FROM source_entries LIMIT 100 OFFSET ?",
            (offset,),
        ):
            if entry_key_for(tvg, fp) != ek:
                return False
    return True


def _prepare_sqlite_temp() -> Path | None:
    """把 SQLite 临时文件从容器 /tmp 挪到数据盘，返回生效目录（失败返回 None）。

    线上故障：compose 里 /tmp 是 64MB 的 tmpfs，而「频道目录」查询用到了
    MATERIALIZED CTE、COUNT/GROUP_CONCAT(DISTINCT) 与窗口函数，实测单次查询
    峰值临时空间约 23MB；前端 refreshAll / 测速轮询会用 Promise.all 并发请求
    /api/stats 与 /api/channels，再叠加多个标签页，几次并发就写满 tmpfs，
    SQLite 抛出 `sqlite3.OperationalError: database or disk is full`
    （磁盘其实还有 100GB 空闲）。

    SQLite 只在初始化时读取一次 SQLITE_TMPDIR 并长期缓存，所以必须在
    `import sqlite3` 之前设置；运行期再改环境变量不会生效。
    """
    configured = os.environ.get("SQLITE_TMPDIR", "").strip()
    candidate = Path(configured) if configured else Path(os.getenv("DATA_DIR", "/data")) / ".sqlite-tmp"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    # 与 SQLite 内部 unixTempFileDir() 的 osAccess(zDir, 07) 判定保持一致
    if not os.access(candidate, os.W_OK | os.X_OK):
        return None
    # 上次进程被强杀可能残留 etilqs_* 临时文件，启动时清掉，避免长期堆积
    for stale in candidate.glob("etilqs_*"):
        try:
            stale.unlink()
        except OSError:
            pass
    os.environ["SQLITE_TMPDIR"] = str(candidate)
    return candidate


SQLITE_TEMP_DIR = _prepare_sqlite_temp()

import sqlite3  # noqa: E402  必须在 _prepare_sqlite_temp() 之后导入
import threading  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from typing import Iterator, Sequence  # noqa: E402

from .config import settings  # noqa: E402

SOURCE_SEEDS = ()
# —— 干净基线（2026-10-10）：内置固定源已全部清空，项目启动后数据源为零，
# 由使用者在「数据源」页面手动添加。SOURCE_SEEDS 的语义与写入路径
# （db._seed_sources）完全保留，只是这里不再声明任何内置源。
# 需要恢复内置源：从带日期的 db.py.bak-* 取回原定义即可。

# 内置固定源的**数据源分组**（slug → source_group），在每次 initialize() 里幂等补写。
#
# 为什么不做成 seed 元组里的第 6 个字段：SOURCE_SEEDS 与 tvbox_seeds() 都是 5 元组，
# 为一个分组改元组形状要同时动两条生成路径（以及所有解包点），而分组对种子来说只是
# 「行存在之后要写一次」的属性 —— 用白名单式 UPDATE 达成同样效果，且天然向后兼容
# （新代码读旧格式、旧代码读新格式都不会炸）。
#
# 语义与理由（三条都必须成立，否则删掉这段、改回元组方案）：
#   * 每次启动都跑（不是「为空才写」）：内置源是**固定源**，用户在界面上把它清空/改掉后，
#     重启要能回到这里声明的值 —— 这正是「固定」的含义（同 _seed_sources 每次覆盖
#     name/url/format/priority 的口径一致）；
#   * 只按 slug 匹配：种子因「URL 已被别的 slug 占用」被跳过时（见 _seed_sources 的预检），
#     这里匹配不到行、等于空转，绝不会把分组写到别人的源上；
#   * 分组名与手动源用过的 '本地源' 保持同一口径：同一个本地中继从手动源升级为内置源后，
#     分组不变，导出的 group-title 与前端分组视图都不会跳变。
SOURCE_SEED_GROUPS: dict[str, str] = {}


def tvbox_seeds() -> tuple[tuple[str, str, str, str, int], ...]:
    """把 TVBOX_CONFIGS 里声明的配置地址转成数据源种子（format='tvbox'）。

    slug 由 URL 哈希得出，因此给配置改个显示名不会产生重复源；
    配置里 lives 新增的直播列表会在同步时自动跟进、各自成为一个子源。
    """
    seeds: list[tuple[str, str, str, str, int]] = []
    for index, raw in enumerate(part.strip() for part in settings.tvbox_configs.split(",")):
        if not raw:
            continue
        # 畸形配置地址（如不配对括号、非法端口）会让 urlsplit 抛 ValueError；
        # 本函数在 initialize() 启动路径上被调用，不兜住就是容器启动崩溃循环
        try:
            host = urlsplit(raw).hostname or raw
        except ValueError:
            logger.warning("TVBOX_CONFIGS 里的地址无法解析，已跳过：%s", raw[:120])
            continue
        slug = "tvbox::" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
        seeds.append((slug, f"TVBox 配置 · {host}", raw, "tvbox", 30 + index))
    return tuple(seeds)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    format TEXT NOT NULL,
    priority INTEGER NOT NULL,
    etag TEXT NOT NULL DEFAULT '',
    last_modified TEXT NOT NULL DEFAULT '',
    content_sha256 TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'never',
    last_http_status INTEGER,
    last_checked_at TEXT,
    last_success_at TEXT,
    last_changed_at TEXT,
    item_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT NOT NULL DEFAULT '',
    -- TVBox 子源所属的父配置 slug（'' = 不是子源）。用于对账清理父配置里已删除的孤儿子源。
    parent_slug TEXT NOT NULL DEFAULT '',
    -- TVBox 孤儿子源的「两轮确认」标记。**不能放 error_message**：父源任何异常都会
    -- 整条覆盖它，标记一丢孤儿就永远清不掉（详见 sync.py 的对账逻辑）。
    orphan_note TEXT NOT NULL DEFAULT '',
    -- 数据源分组（'' = 未分组）。用途：把「发现」页批量采用进来的源单独归为一组，
    -- 前端按它分区展示，导出订阅时**只由该组源提供的频道**也单独成组（见 static_export）。
    -- 空串是默认值，存量库迁移后与旧行为完全一致（默认区块 / 保持原有分组）。
    source_group TEXT NOT NULL DEFAULT '',
    -- v51 数据源停用标记（1=启用，0=已停用）。停用是**持久的用户意图**，不是删除：
    -- 内置源删不掉（删了会被 SOURCE_SEEDS 在下次启动重新写回，接口直接 400），而
    -- 「这个源现在不可用，别再抓取、也别再测速」需要跨重启保留 —— 所以单独一列。
    -- 停用语义见 _seed_sources（种子绝不覆盖本列）、sync.select_sync_sources 与
    -- speed._pending_sql；条目与健康记录一行不删，重新启用即刻恢复原语义。
    enabled INTEGER NOT NULL DEFAULT 1
);

-- 手动放行的数据源主机。代码里的 ALLOWED_SOURCE_HOSTS 是内置白名单；
-- 用户在「数据源」页面手动添加源时，经管理员确认抓取成功后，主机写到这里，
-- 之后同步与测速都认它。这样加源不需要改配置重启，同时仍然只有明确放行过的主机
-- 才会被服务端请求（浏览器无法凭一个 URL 就让服务器去抓任意地址）。
CREATE TABLE IF NOT EXISTS allowed_hosts (
    host TEXT PRIMARY KEY,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS source_entries (
    source_slug TEXT NOT NULL REFERENCES sources(slug) ON DELETE CASCADE,
    entry_key TEXT NOT NULL,
    stream_fingerprint TEXT NOT NULL,
    channel_key TEXT NOT NULL,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    tvg_id TEXT NOT NULL DEFAULT '',
    logo TEXT NOT NULL DEFAULT '',
    group_title TEXT NOT NULL DEFAULT '未分类',
    country TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    quality TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL,
    user_agent TEXT NOT NULL DEFAULT '',
    referrer TEXT NOT NULL DEFAULT '',
    labels TEXT NOT NULL DEFAULT '',
    -- '' = 尚未判定区域；启动时的回填会用 region_of 判定并写入，两处定义必须一致
    region TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (source_slug, entry_key)
);

-- 注意：region 相关索引在 _migrate 中补齐列之后再创建，旧库直接建会失败

CREATE INDEX IF NOT EXISTS idx_entries_active_channel ON source_entries(active, channel_key);
CREATE INDEX IF NOT EXISTS idx_entries_active_stream ON source_entries(active, stream_fingerprint);
-- idx_entries_name / idx_entries_group / idx_entries_country 已删除：三者从未被任何查询
-- 用到（EQP 逐一验证），却要让每次条目写入维护三棵 B 树。详见 _migrate 里的 DROP。

-- 键值元数据表：记录一次性迁移的口径版本，让已完成的迁移在后续启动时能 O(1) 跳过。
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 「发现」候选：只记地址与来源页，是否采用由人工决定（见 discover.py 的边界说明）。
CREATE TABLE IF NOT EXISTS discovered (
    url TEXT PRIMARY KEY,
    page TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    hits INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'new',
    -- 「先检测再决定」的结果：真抓一次并解析（不写任何源），把结论记在这里给界面看
    check_status TEXT NOT NULL DEFAULT '',      -- '' 未检测 / 'ok' / 'error'
    check_items INTEGER NOT NULL DEFAULT 0,     -- 解析出的条目数
    check_message TEXT NOT NULL DEFAULT '',     -- 失败原因 / 格式说明
    checked_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_discovered_state ON discovered(state, last_seen_at DESC);

-- 检测无效的候选：直接从发现列表里删掉，但留一条「别再抓它」的墓碑。
-- 不留墓碑的话，下一轮发现会把同一批垃圾地址重新插回 new，再检测、再删，天天白跑。
-- 墓碑有保质期（REJECT_RETENTION_DAYS）：源站后来修好了还能重新被发现一次。
CREATE TABLE IF NOT EXISTS discovered_rejected (
    url TEXT PRIMARY KEY,
    reason TEXT NOT NULL DEFAULT '',
    rejected_at TEXT NOT NULL,
    hits INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS favorites (
    channel_key TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stream_health (
    fingerprint TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    status TEXT NOT NULL,
    latency_ms INTEGER,
    speed_kbps REAL,
    http_status INTEGER,
    bytes_read INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    fail_count INTEGER NOT NULL DEFAULT 0,
    last_fail_at TEXT NOT NULL DEFAULT '',
    -- 被「复活」过几次。复活过又再次失效的线路说明等回来也没用，
    -- 由季度清理按这个计数永久停用（status='purged'），不再无限循环白测。
    revive_count INTEGER NOT NULL DEFAULT 0,
    -- 历史累计成功/失败次数：fail_count 是「连续失败」用于剔除，考核稳定性要看长期比例
    ok_total INTEGER NOT NULL DEFAULT 0,
    fail_total INTEGER NOT NULL DEFAULT 0,
    -- 测量方法版本：测速算法变更后旧数据自动失效并重测，不用人工清库
    method TEXT NOT NULL DEFAULT '',
    -- v48：本条线路「连续失效」的起始时间（UTC ISO）。成功 / 没测成（blocked、
    -- unsupported 的 not_measured）时清空，判定失败时若原为空则填入本次时间。
    -- 为什么必须新加一列：'持续失效多少天' 用既有列推不出来 ——
    --   * last_fail_at / checked_at 每次复测都会被刷新，记的是「最近一次」而不是「起点」；
    --   * fail_count 是连续失败次数，理论上 ×FAIL_RETEST_HOURS 可当时长，但线上实测它
    --     被钉死在 PRUNE_MAX_FAIL=3（2669 行停在 3），根本涨不上去，拿它当尺子必然失效。
    -- 因此用一个显式的时间戳记录「什么时候开始坏的」，长期失效清理（AUTO_CLEANUP_DEAD_DAYS）
    -- 只信这一列。存量行一律留空：它们在 v48 之后第一次测得失败时才拿到起点，
    -- 于是清理最早也要 N 天之后才可能命中 —— 升级不会造成批量停用。
    dead_since TEXT NOT NULL DEFAULT '',
    checked_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_health_status ON stream_health(status, speed_kbps DESC);
CREATE INDEX IF NOT EXISTS idx_health_checked ON stream_health(checked_at);
CREATE INDEX IF NOT EXISTS idx_health_last_fail ON stream_health(last_fail_at);

CREATE TABLE IF NOT EXISTS sync_runs (
    id TEXT PRIMARY KEY,
    trigger TEXT NOT NULL,
    source_filter TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    sources_ok INTEGER NOT NULL DEFAULT 0,
    sources_not_modified INTEGER NOT NULL DEFAULT 0,
    sources_failed INTEGER NOT NULL DEFAULT 0,
    items_seen INTEGER NOT NULL DEFAULT 0,
    items_added INTEGER NOT NULL DEFAULT 0,
    items_removed INTEGER NOT NULL DEFAULT 0,
    auto_speed_json TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_sync_runs_started ON sync_runs(started_at DESC);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert_source(slug: str, name: str, url: str, fmt: str = "m3u", priority: int = 50, parent_slug: str = "",
                  source_group: str = "") -> str:
    """插入或更新一个数据源行，返回**实际生效的 slug**。

    用于 TVBox 配置里**动态发现**的子源：配置里新增一个直播列表，就会自动多出一个源行，
    从而复用完整的同步/测速/剔除/导出流程，也能在「数据源」页面单独看到状态。
    同步阻塞操作，调用方需放到线程里执行。

    `sources.url` 上有 UNIQUE：如果这个地址已经作为**另一个 slug** 存在（比如 TVBox
    子源的地址恰好也被配成了普通源 / 另一个 TVBox 配置也列出了它），这里不新建行，
    而是复用已有 slug 并返回它 —— 调用方应当同步返回的这个 slug。

    `source_group`（v40）是数据源分组：只有「发现」页采用候选时会传「发现源」，
    手动添加与 TVBox 子源都留空。更新时采用**非空才覆盖**的语义 —— TVBox 父配置每轮
    同步都会 upsert 一遍子源（传默认空串），若按 `excluded` 无条件覆盖，任何已打上的
    分组都会被反复冲掉；而「把分组清空」目前没有任何产品入口，宁可保留也不误清。
    """
    with db.write() as conn:
        owner = conn.execute("SELECT slug FROM sources WHERE url=?", (url,)).fetchone()
        if owner and owner[0] != slug:
            logger.info("地址 %s 已由数据源 %s 使用，复用该源而不新建", url, owner[0])
            return str(owner[0])
        conn.execute(
            """
            INSERT INTO sources(slug, name, url, format, priority, parent_slug, source_group)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                name=excluded.name, url=excluded.url, format=excluded.format,
                priority=excluded.priority, parent_slug=excluded.parent_slug,
                source_group=CASE WHEN excluded.source_group <> '' THEN excluded.source_group
                                  ELSE sources.source_group END
            """,
            (slug, name, url, fmt, priority, parent_slug, (source_group or "").strip()),
        )
    return slug


def upsert_discovered(url: str, page: str) -> bool:
    """记一条候选；返回是否是**第一次**见到（决定要不要提示用户有新候选）。"""
    now = utc_now()
    with db.write() as conn:
        row = conn.execute("SELECT state FROM discovered WHERE url=?", (url,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO discovered(url, page, first_seen_at, last_seen_at, hits, state) "
                "VALUES (?, ?, ?, ?, 1, 'new')",
                (url, page, now, now),
            )
            return True
        # 已经采用/忽略过的不再回到 new（否则每轮都会重新冒出来）
        conn.execute(
            "UPDATE discovered SET last_seen_at=?, hits=hits+1 WHERE url=?", (now, url)
        )
        return False


def save_discovered_check(url: str, status: str, items: int, message: str) -> None:
    """记录一条候选的检测结果（真抓一次并解析，不写源）。"""
    with db.write() as conn:
        conn.execute(
            "UPDATE discovered SET check_status=?, check_items=?, check_message=?, checked_at=? "
            "WHERE url=?",
            (status, items, message[:200], utc_now(), url),
        )


def set_meta(key: str, value: str) -> None:
    """写一条 meta 键值（发现轮次时间等轻量状态用它，别为一次性标记建表）。"""
    with db.write() as conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def get_meta(key: str) -> str:
    """读一条 meta 键值；没有就返回空串。"""
    row = db.row("SELECT value FROM meta WHERE key=?", (key,))
    return row["value"] if row else ""


def reject_discovered(url: str, reason: str) -> bool:
    """把一个候选判死：从发现列表删除，只留墓碑（返回是否真删了一行）。

    墓碑记录 URL 与原因，`discover.run_discovery` 在保质期内不再把它写回候选列表 ——
    否则垃圾地址每天都会以「新发现」的身份重新出现，白跑一次次网络请求。
    """
    now = utc_now()
    with db.write() as conn:
        conn.execute(
            "INSERT INTO discovered_rejected(url, reason, rejected_at, hits) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(url) DO UPDATE SET reason=excluded.reason, "
            "rejected_at=excluded.rejected_at, hits=discovered_rejected.hits + 1",
            (url, reason[:200], now),
        )
        cur = conn.execute("DELETE FROM discovered WHERE url=?", (url,))
    return bool(cur.rowcount)


def rejected_discovered_urls(days: int = 30) -> set[str]:
    """保质期内的墓碑 URL 集合（发现轮次据此跳过，不再重新检测同一批垃圾）。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    return {row["url"] for row in db.rows(
        "SELECT url FROM discovered_rejected WHERE rejected_at >= ?", (cutoff,)
    )}


def purge_invalid_discovered() -> int:
    """清掉所有「检测无效」的候选（check_status=error 且没被采用/忽略过），返回清掉的条数。

    v42（审计 P2-a）两处修正：

    * 谓词加 `state <> 'ignored'`：用户显式点过「忽略」的候选原先也会被这里删掉 ——
      忽略决定丢失，而且 30 天后墓碑过期，同一个地址会重新冒回候选列表；而 ignored 的行
      本来就不显示（discovered_summary 过滤掉了），删它没有任何收益。
      accepted 同样受保护（原有条件）。
    * 删掉原先的 `days: int = 30` 形参：它**完全没被用到**（实测 days=0 仍清空全部），
      留着只会让调用方以为"可以按时效过滤"。这个函数的契约就是"立即愈合、把无效候选全清掉"
      （见 main.py 里三处调用），按时间过滤反而会让界面继续挂着历史垃圾；
      发现轮次的 30 天时效由墓碑那条独立逻辑负责（rejected_discovered_urls）。
    """
    rows = db.rows(
        "SELECT url, check_message FROM discovered "
        "WHERE check_status='error' AND state NOT IN ('accepted', 'ignored')"
    )
    for row in rows:
        reject_discovered(row["url"], row["check_message"] or "检测无效")
    return len(rows)


def set_discovered_state(url: str, state: str) -> bool:
    """把候选标成 accepted / ignored / new（返回是否命中一行）。"""
    with db.write() as conn:
        cur = conn.execute("UPDATE discovered SET state=? WHERE url=?", (state, url))
    return bool(cur.rowcount)


def set_source_orphan_note(slug: str, note: str) -> None:
    """记录/清除 TVBox 孤儿子源的「两轮确认」标记（独立列，不会被 error_message 覆盖）。"""
    with db.write() as conn:
        conn.execute("UPDATE sources SET orphan_note=? WHERE slug=?", (note, slug))


def set_source_parent(slug: str, parent_slug: str) -> None:
    """记录 TVBox 子源所属的父配置（孤儿对账用）。"""
    with db.write() as conn:
        conn.execute("UPDATE sources SET parent_slug=? WHERE slug=?", (parent_slug, slug))


def set_source_enabled(slug: str, enabled: bool) -> bool:
    """v51：写入数据源的启用状态，返回是否命中一行（slug 不存在时 False）。

    注意返回值口径：SQLite 的 `rowcount` 是**匹配行数**，把 `enabled` 更新成它已有的值
    同样算 True。所以它回答的是「这个源在不在」，不是「值变没变」—— 调用方要判断
    「是否真的变更」（决定要不要 bump_revision）必须自己比较更新前的值，
    见 `main.toggle_source_enabled`。

    停用（`enabled=0`）= 不再抓取（`sync.select_sync_sources` 过滤）+ 不再参与测速
    （`speed._pending_sql` 过滤）；**条目、健康记录一行不删**，重新启用即刻回到原语义。

    与 `delete_source` 的区别是这个特性的全部理由：删除会 CASCADE 掉该源的条目与健康
    记录（线路从订阅里消失），而内置源删不掉 —— 它由 `SOURCE_SEEDS` 在每次启动重新写入。
    停用只是一个可逆、跨重启保留的状态位，所以内置源也能停。
    """
    with db.write() as conn:
        cursor = conn.execute(
            "UPDATE sources SET enabled=? WHERE slug=?", (1 if enabled else 0, slug)
        )
        return cursor.rowcount > 0


def delete_source(slug: str) -> tuple[int, int]:
    """删除一个数据源，返回 (被连带删除的条目数, 顺手清掉的孤儿健康行数)。

    注意：`source_entries.source_slug` 对 `sources.slug` 是 ON DELETE CASCADE，删源行会
    一并删掉条目；但 **`stream_health` 没有任何外键**（它以 fingerprint 为主键），
    删条目不会连带删健康记录 —— 那些行会变成孤儿，原本要等下一次每周维护才被清。
    这里立刻清一遍，既避免「删了源状态还留着」，也避免重新添加同一地址时静默继承
    旧状态（包括 status='purged' 的墓碑，会让新加的源永不测速）。

    需要历史就重新添加同一地址，测量会重新做一遍。
    """
    with db.write() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM source_entries WHERE source_slug=?", (slug,)
        ).fetchone()
        removed = int(row["n"]) if row else 0
        conn.execute("DELETE FROM sources WHERE slug=?", (slug,))
        orphan = conn.execute(
            "DELETE FROM stream_health WHERE fingerprint NOT IN "
            "(SELECT DISTINCT stream_fingerprint FROM source_entries)"
        ).rowcount
    return removed, (orphan or 0)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._write_lock = threading.RLock()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")  # 持久设置，只需设一次
            conn.executescript(SCHEMA)
            self._migrate(conn)
            # 注意：sources.url 上有 UNIQUE。种子行按 slug 做 upsert，
            # 但如果某个种子的 URL 已被**另一个 slug** 占用（例如把 TVBOX_CONFIGS 写成
            # 某个已有源的地址），INSERT 会直接违反 url UNIQUE 抛 IntegrityError ——
            # 而这里在 lifespan 里跑且没有 try/except，后果是容器陷入启动崩溃循环。
            # 所以逐条预检：同地址已存在就跳过并告警，让启动照常完成。
            self._seed_sources(conn, SOURCE_SEEDS + tvbox_seeds())
            # 内置固定源的分组必须在 _seed_sources 之后补：新库首次启动时源行刚由上一步插入，
            # 顺序反过来就是 UPDATE 打在空表上（分组丢失且不自愈）。幂等，每次启动都跑。
            self._seed_source_groups(conn, SOURCE_SEED_GROUPS)
            conn.commit()
        # 统计信息（sqlite_stat1）必须在迁移/建索引之后刷新一次：没有它，SQLite 只能按
        # 「每列 10 个不同值」的默认假设估代价，于是在 selectivity 差异极大的两个候选索引
        # 之间**选错**。线上实测：频道详情那条 SQL（WHERE e.active=1 AND e.channel_key=?）
        # 被规划成 `idx_entries_active_stream(active=?)` —— 只用到索引首列，等于扫全部
        # 7 万活跃行再逐行回表；有了 stat1（active=1 → 35096 行、channel_key 4096 个不同值、
        # stream_fingerprint 2 个/值）规划器才改用 `idx_entries_active_channel(active=? AND
        # channel_key=?)`。实测单次 52ms → 2.2ms，/api/channels/{key} 端到端 110ms → 10ms。
        # ANALYZE 实测 0.09s（7 万行），随启动只跑一次；maintain() 里也会定期再跑。
        self.analyze()

    @staticmethod
    def _seed_sources(conn: sqlite3.Connection, seeds: Sequence[tuple[str, str, str, str, int]]) -> None:
        """写入内置种子源（幂等）。同 URL 属于别的 slug 时跳过，避免 url UNIQUE 冲突。"""
        for slug, name, url, fmt, priority in seeds:
            owner = conn.execute("SELECT slug FROM sources WHERE url=?", (url,)).fetchone()
            if owner and owner[0] != slug:
                logger.warning(
                    "跳过种子源 %s（%s）：该地址已作为数据源 %s 存在，避免 url 唯一约束冲突",
                    slug, url, owner[0],
                )
                continue
            # v51：`enabled` **刻意不在 ON CONFLICT 的更新列里**。种子每次启动都会跑
            # （`docker compose restart` 与 `up -d --force-recreate` 都算），一旦把它
            # 写进更新列表，用户刚停用的内置源会被下一次启动悄悄启用回来 —— 停用状态
            # 必须跨重启保留，这是这一列存在的唯一理由。运行期状态（status/item_count）
            # 同理不在更新列里，口径一致。
            conn.execute(
                """
                INSERT INTO sources(slug, name, url, format, priority)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    name=excluded.name,
                    url=excluded.url,
                    format=excluded.format,
                    priority=excluded.priority
                """,
                (slug, name, url, fmt, priority),
            )

    @staticmethod
    def _seed_source_groups(conn: sqlite3.Connection, groups: dict[str, str]) -> None:
        """给内置固定源补写 source_group（幂等；口径与理由见 SOURCE_SEED_GROUPS 的注释）。

        只按 slug 更新：种子被 _seed_sources 的 URL 预检跳过时这里匹配不到行，是空转而不是
        「把分组写到占用该 URL 的源上」。返回被改动的行数没有意义（每次启动都会「改」一次
        同样的值），所以不返回。
        """
        for slug, group in groups.items():
            conn.execute("UPDATE sources SET source_group=? WHERE slug=?", (group, slug))

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """为旧库补齐 region 列并回填（老数据没有区域字段）。"""
        from .parsers import region_of

        columns = {row[1] for row in conn.execute("PRAGMA table_info(source_entries)")}
        if "region" not in columns:
            # 默认空串，便于下面统一回填；新数据由同步流程显式写入
            conn.execute("ALTER TABLE source_entries ADD COLUMN region TEXT NOT NULL DEFAULT ''")
        # 只判定「尚未判定」（region=''）的行。
        # 原实现是把**所有** region='international' 的行重新跑一遍 region_of，而
        # region_of 对中文名的国外频道会判成 domestic —— 于是每次触发回填，都可能把
        # 已被区域策略剔除的国外线路又拉回国内池（只在出现 '' 行时才跑，属潜伏缺陷）。
        # 现在改为精确回填：不碰已有的 domestic/international 判定。
        rows = conn.execute(
            "SELECT rowid, source_slug, country, group_title, name FROM source_entries WHERE region=''"
        ).fetchall()
        if rows:
            # 一次 executemany 落库：原实现逐行 UPDATE，6 万行就是 6 万次独立 SQL 调用
            conn.executemany(
                "UPDATE source_entries SET region=? WHERE rowid=?",
                [(region_of(row[1], row[2] or "", row[3] or "", row[4] or ""), row[0]) for row in rows],
            )
        # ---- entry_key 口径变更迁移（旧：tvg-id+显示名+指纹 → 新：tvg-id+指纹）----
        # 目的是让「同一线路改名」不再产生新条目。老库需要一次性重算：
        #   1. 按新公式算出每行的目标 key；
        #   2. 同一源内算出相同目标 key 的多行 = 历史上因改名产生的重复，保留
        #      active 优先、first_seen_at 最早的那条，其余删除（健康记录按指纹共享，不受影响）；
        #   3. 幸存行再统一改名（先删后改，避免主键中途冲突）。
        from .parsers import entry_key_for  # 延迟导入，避免模块级循环依赖

        entry_rows_sql = (
            "SELECT rowid, source_slug, entry_key, stream_fingerprint, tvg_id, active, first_seen_at "
            "FROM source_entries"
        )
        # 「已迁移」短路：这个迁移在稳态下 100% 空转，却要重算 62791 行的 sha256
        # （实测 1.30s CPU/次启动：SELECT 0.545s + Python 循环 0.726s）。用 meta 表里的
        # 口径版本号做标记，命中就整段跳过；再抽查约 400 行兜底 —— 万一有人跑过旧版本
        # 代码、把旧口径的键写回来了，抽样能发现并回退到全量重算，不会漏掉。
        marker = conn.execute(
            "SELECT value FROM meta WHERE key='entry_key_formula'"
        ).fetchone()
        changed = 0
        if marker and marker[0] == ENTRY_KEY_FORMULA and _entry_key_sample_ok(conn, entry_key_for):
            pass
        else:
            # 第一遍**流式**统计是否需要迁移：稳态（已经迁移过）下 changed=0，直接跳过。
            # 原来的写法不管有没有活干都把 62791 行 fetchall 进内存、再建一个同样大的分组字典
            # （实测峰值约 20MB，是启动内存尖峰的主要来源），而迁移早已收敛、每次启动都是空跑。
            # 这里用游标迭代不物化行，也不建字典；只有真需要迁移（罕见）才走第二遍。
            changed = sum(
                1
                for _rowid, _ss, ek, fp, tvg, _active, _first in conn.execute(entry_rows_sql)
                # 与 make_entry() 共用同一条公式（历史事故：这里内联过一份带
                # split('@')/casefold 的版本，与写入口径不一致 → 迁移永不收敛）
                if entry_key_for(tvg, fp) != ek
            )
            if changed:
                groups: dict[tuple[str, str], list[tuple]] = {}
                for rowid, ss, ek, fp, tvg, active, first in conn.execute(entry_rows_sql):
                    new_key = entry_key_for(tvg, fp)
                    groups.setdefault((ss, new_key), []).append((rowid, ek, active or 0, first or ""))
                dropped = 0
                updates: list[tuple[str, str, str, int]] = []
                for (ss, new_key), rows in groups.items():
                    rows.sort(key=lambda r: (-r[2], r[3], r[0]))     # active 优先、first_seen_at 最早
                    winner = rows[0]
                    for loser in rows[1:]:
                        conn.execute("DELETE FROM source_entries WHERE rowid=?", (loser[0],))
                        dropped += 1
                    if winner[1] != new_key:
                        updates.append((new_key, min([winner[3]] + [r[3] for r in rows[1:] if r[3]] or [winner[3]]), ss, winner[0]))
                # 两阶段改名：先统一改成保证唯一的临时键，再改成目标键。
                # 单阶段 executemany 存在跨组链式碰撞窗口 —— 行 A 的目标键恰好等于行 B
                # 的当前键、而 A 先于 B 执行时，会撞 (source_slug, entry_key) 主键约束，
                # IntegrityError 从 initialize() 抛出让容器陷入启动崩溃循环。
                conn.executemany(
                    "UPDATE source_entries SET entry_key=? WHERE rowid=?",
                    [(f"\x00migr{new_key}", rowid) for new_key, _first, _ss, rowid in updates],
                )
                conn.executemany(
                    "UPDATE source_entries SET entry_key=?, first_seen_at=? WHERE source_slug=? AND rowid=?",
                    updates,
                )
                logger.info(
                    "entry_key 口径迁移：重算 %s 条，合并同源重复 %s 条", changed, dropped
                )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('entry_key_formula', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (ENTRY_KEY_FORMULA,),
            )

        # (region, active) 与 (active, region) 在等值谓词下互相替代，实测查询计划只用后者，
        # 前者纯属多维护一棵 B 树（每次批量 upsert 都要跟着写）。删掉不再重建。
        conn.execute("DROP INDEX IF EXISTS idx_entries_region")
        # 这三个索引是纯写入税：EQP 逐一验证（q= 的 LIKE '%…%'、group= / country= 等值过滤、
        # /api/groups 聚合、频道页两阶段）后发现**没有任何查询用到它们** —— 前导 % 让
        # normalized_name 索引用不上，等值过滤与聚合都走 idx_entries_active_channel /
        # idx_entries_active_stream。删掉后 2 万行整行 upsert 的 CPU 从 1.395s 降到
        # 1.110s（−20.4%），六种查询结果与计划逐项一致。
        for unused_index in ("idx_entries_name", "idx_entries_group", "idx_entries_country"):
            conn.execute(f"DROP INDEX IF EXISTS {unused_index}")
        health_columns = {row[1] for row in conn.execute("PRAGMA table_info(stream_health)")}
        if "fail_count" not in health_columns:
            conn.execute("ALTER TABLE stream_health ADD COLUMN fail_count INTEGER NOT NULL DEFAULT 0")
            # 历史失败记录补一次计数，使其可被剔除规则命中
            conn.execute("UPDATE stream_health SET fail_count=1 WHERE fail_count=0 AND status IN ('error', 'unsupported')")
        if "last_fail_at" not in health_columns:
            conn.execute("ALTER TABLE stream_health ADD COLUMN last_fail_at TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE stream_health SET last_fail_at=checked_at WHERE last_fail_at='' AND status IN ('error', 'unsupported')")
        source_columns = {row[1] for row in conn.execute("PRAGMA table_info(sources)")}
        if "parent_slug" not in source_columns:
            conn.execute("ALTER TABLE sources ADD COLUMN parent_slug TEXT NOT NULL DEFAULT ''")
        disc_columns = {row[1] for row in conn.execute("PRAGMA table_info(discovered)")}
        if disc_columns and "check_status" not in disc_columns:
            conn.execute("ALTER TABLE discovered ADD COLUMN check_status TEXT NOT NULL DEFAULT ''")
            conn.execute("ALTER TABLE discovered ADD COLUMN check_items INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE discovered ADD COLUMN check_message TEXT NOT NULL DEFAULT ''")
            conn.execute("ALTER TABLE discovered ADD COLUMN checked_at TEXT NOT NULL DEFAULT ''")
        if "orphan_note" not in source_columns:
            conn.execute("ALTER TABLE sources ADD COLUMN orphan_note TEXT NOT NULL DEFAULT ''")
        # v40：数据源分组列。存量库统一补空串 = 「未分组」，与旧行为完全一致
        # （前端把它归入默认区块，导出也不改这些源提供的频道的分组）。
        # 检查方式与上面的 parent_slug / orphan_note 相同：读 PRAGMA 后判断列是否已存在，
        # 幂等，重复启动不会重复 ALTER（重复 ADD COLUMN 会抛 duplicate column name）。
        if "source_group" not in source_columns:
            conn.execute("ALTER TABLE sources ADD COLUMN source_group TEXT NOT NULL DEFAULT ''")
        # v51：数据源停用标记。存量库一律补 1（启用）—— 与加这一列之前的行为逐字节一致，
        # 升级当天不会有任何源被意外停用。检查方式与 parent_slug / orphan_note /
        # source_group 相同（PRAGMA 后判断列是否存在），幂等，重复启动不会重复 ALTER。
        if "enabled" not in source_columns:
            conn.execute("ALTER TABLE sources ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
        # TVBox 子源此前一律按 m3u 登记，遇到「名称,URL」清单会永久解析失败。
        # 父配置本身 format='tvbox'，所以「slug 以 tvbox:: 开头且 format='m3u'」就是子源。
        conn.execute(
            "UPDATE sources SET format='auto' WHERE format='m3u' AND slug LIKE 'tvbox::%'"
        )
        if "revive_count" not in health_columns:
            # 存量行一律从 0 开始：历史上没记录过复活次数，无法可靠回填，
            # 与其猜一个数字去删线路，不如让计数从本次升级之后开始累计。
            conn.execute("ALTER TABLE stream_health ADD COLUMN revive_count INTEGER NOT NULL DEFAULT 0")
        if "ok_total" not in health_columns:
            conn.execute("ALTER TABLE stream_health ADD COLUMN ok_total INTEGER NOT NULL DEFAULT 0")
        if "fail_total" not in health_columns:
            conn.execute("ALTER TABLE stream_health ADD COLUMN fail_total INTEGER NOT NULL DEFAULT 0")
        if "method" not in health_columns:
            # 旧数据 method=''，与当前 METHOD_VERSION 不一致 → 会被当作「待重测」，自动自愈
            conn.execute("ALTER TABLE stream_health ADD COLUMN method TEXT NOT NULL DEFAULT ''")
        if "dead_since" not in health_columns:
            # v48：连续失效起点。**刻意不回填**：既有列里没有「起点」信息，
            # 用 checked_at 之类的近似值回填会让一堆早就判死的行立刻满足清理条件，
            # 升级当天就批量停用（正是要避免的事）。留空 = 下次测得失败时才开始计时。
            conn.execute("ALTER TABLE stream_health ADD COLUMN dead_since TEXT NOT NULL DEFAULT ''")
        # v48 归因修正的**存量补齐**：`speed.BlockedTarget`（出站守卫拦下内网/保留地址）
        # 在早期版本里被写成了 status='unsupported'，只有文案留着「内网/保留地址被策略拦截」。
        # 两者语义完全不同：blocked = 我们没测成（不是线路坏），unsupported = 连不上/协议不支持。
        # 下游按 status 精确匹配，写错会直接改变结论：
        #   * /api/dead-channels（_ALIVE_ENTRY_SQL）把 status IN ('error','unsupported') 判为
        #     「全线路死」，于是这些频道被当成「该删源」的候选 —— 与 main.py 里明写的
        #     「blocked 不算判死（宁可漏报，不误报）」正好相反。线上实测有 30 个频道因此
        #     被误列，含 id:上海新闻、id:xinjiangtv1.cn 等。
        #   * /api/channels/{key} 的角色分档会把它们标成 dead 而不是 blocked。
        # 文案是唯一可用的判据（status 已经写错），故按 speed.BlockedTarget.reason_prefix 匹配；
        # tests/test_cleanup_dead.py 里有用例钉住这个常量，改文案时会一起失败。
        # 幂等：第二次启动命中 0 行。**不动 checked_at**：既不额外消耗测速预算，也不改变导出
        # （blocked 与 unsupported 在 _fetch_lines 里被同一条 `h.status IS NULL OR h.status='ok'`
        # 过滤掉，订阅产物逐字节不变）；fail_count / dead_since 本就是「没测成」口径（0 / 空），
        # 无需回滚，也不会因此进入剔除或长期失效清理。
        conn.execute(
            "UPDATE stream_health SET status='blocked' "
            "WHERE status='unsupported' AND error LIKE '内网/保留地址被策略拦截%'"
        )
        run_columns = {row[1] for row in conn.execute("PRAGMA table_info(sync_runs)")}
        if "auto_speed_json" not in run_columns:
            conn.execute("ALTER TABLE sync_runs ADD COLUMN auto_speed_json TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_health_status ON stream_health(status, speed_kbps DESC)")
        # 旧库补关键索引：测速待测查询（checked_at 过滤 + 排序）、失效复测（last_fail_at）、同步记录排序、区域筛选
        conn.execute("CREATE INDEX IF NOT EXISTS idx_health_checked ON stream_health(checked_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_health_last_fail ON stream_health(last_fail_at)")
        # v48：长期失效清理按 dead_since 的范围扫描（`dead_since <> '' AND dead_since <= ?`）。
        # 绝大多数行这一列是空串，索引很小；不建的话每周要全表扫 4 万行健康记录。
        conn.execute("CREATE INDEX IF NOT EXISTS idx_health_dead_since ON stream_health(dead_since)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_runs_started ON sync_runs(started_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_active_region ON source_entries(active, region)")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        # 这里只放「每连接必须重设」的 PRAGMA。journal_mode 是写进库文件头的持久设置，
        # 已在 initialize() 里设过一次；放在每次连接上会白白多取一次锁、多一次往返。
        conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            # 关于 PRAGMA secure_delete：本构建编译期默认开启，审计建议关掉以省「零填充写放大」。
            # 实测后**不关**：纯删除 1.5 万行 ON 0.046s vs OFF 0.010s（快 4.6 倍），但绝对值只有
            # 0.036s；按本库每日约 3500 次条目删除折算 ≈ 0.008s CPU/天，完全可忽略；行变短的
            # UPDATE 只差 5%。为这点收益承担「已删除的条目可从库文件空闲页恢复」不划算。
            # mmap 让 SQLite 走操作系统页缓存，省掉 read() 系统调用与页拷贝。
            # 用它而不是调大 cache_size：cache_size 是**每连接**的，anyio 线程池高并发下会成倍吃内存；
            # mmap 的物理页由 OS 共享且可回收（映射大小受库文件大小限制，实际约等于库大小）。
            if settings.sqlite_mmap_bytes > 0:
                conn.execute(f"PRAGMA mmap_size={int(settings.sqlite_mmap_bytes)}")
        except Exception:
            # PRAGMA 设置失败（如非法 mmap 参数）时必须关连接，否则泄漏文件句柄与 WAL 读标记
            conn.close()
            raise
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def rows(self, sql: str, params: Sequence[object] = ()) -> list[dict]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def rows_raw(self, sql: str, params: Sequence[object] = ()) -> list[sqlite3.Row]:
        """返回 sqlite3.Row 列表，**不做 dict 转换**。

        导出路径专用：把 1.7 万行转成 dict 实测要 0.167s CPU，占冷导出的 19.3%，
        而调用方只用 `row["列名"]` 取值 —— sqlite3.Row 本身就支持按下标取列，
        转换纯属白花。注意 sqlite3.Row 没有 `.get()`，调用方不能当 dict 用。
        """
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def row(self, sql: str, params: Sequence[object] = ()) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def execute(self, sql: str, params: Sequence[object] = ()) -> None:
        with self.write() as conn:
            conn.execute(sql, params)

    def prune_sync_runs(self, keep: int) -> int:
        """只保留最近 `keep` 条同步记录（进行中的不动），返回删除行数。

        这段 SQL 原先在「每周维护」和 `POST /api/sync-runs/clear` 里各写了一遍，必须永远
        语义一致，所以收到数据层共用。keep<=0 表示清空全部已完成记录。
        """
        with self.write() as conn:
            if keep > 0:
                cursor = conn.execute(_SQL_SYNC_RUNS_KEEP, (keep,))
            else:
                cursor = conn.execute(_SQL_SYNC_RUNS_CLEAR)
        return cursor.rowcount or 0

    def prune_history(self, runs_keep: int, inactive_days: int) -> dict[str, int]:
        """清理历史数据（每周维护调用一次，幂等）：

        1. `sync_runs` 只保留最近 runs_keep 条（进行中的不动）；
        2. 删除连续 inactive_days 天没再出现过的 `active=0` 条目 —— 线路换地址后旧行只会被
           置为 inactive，长期累积会让库越来越大；
        3. 删除已无对应条目的孤儿 `stream_health` 记录。

        同步阻塞操作，调用方请放到线程里执行。
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=inactive_days)).isoformat(timespec="seconds")
        with self.write() as conn:
            runs = conn.execute(_SQL_SYNC_RUNS_KEEP, (runs_keep,)).rowcount
            entries = conn.execute(
                "DELETE FROM source_entries WHERE active=0 AND last_seen_at <> '' AND last_seen_at < ?",
                (cutoff,),
            ).rowcount
            health = conn.execute(
                "DELETE FROM stream_health WHERE fingerprint NOT IN (SELECT DISTINCT stream_fingerprint FROM source_entries)"
            ).rowcount
        return {
            "sync_runs_deleted": runs or 0,
            "entries_deleted": entries or 0,
            "health_deleted": health or 0,
        }

    def analyze(self) -> None:
        """刷新 SQLite 查询规划统计（`sqlite_stat1`）。幂等，重复调用只是重算一遍。

        为什么必须有它：本项目在两个"前导列相同、选择性差 4 个数量级"的索引之间做选择 ——
        `idx_entries_active_stream(active, stream_fingerprint)`（每个 active 值平均只有
        2 行）与 `idx_entries_active_channel(active, channel_key)`（平均 4 行）、
        `idx_entries_active_region(active, region)`（平均 23397 行）。没有 stat1 时 SQLite
        按默认假设估代价，会**选错**：线上实测频道详情 SQL 走
        `idx_entries_active_stream(active=?)`，只用得上首列 → 扫全部 7 万活跃行 + 逐行回表，
        单次 52ms、`/api/channels/{key}` 端到端 110ms。有了 stat1 之后改用
        `idx_entries_active_channel(active=? AND channel_key=?)`，单次 2.2ms、端到端约 10ms。

        实测代价：7 万行库 ANALYZE 用 0.09s，只在启动（initialize）与每周维护（maintain）
        各跑一次，相对收益可以忽略。ANALYZE 与 VACUUM 一样是 autocommit 下的独立语句，
        不要放进 BEGIN IMMEDIATE 事务。
        """
        with self._write_lock, self.connect() as conn:
            conn.execute("ANALYZE")

    def maintain(self) -> None:
        """数据库定期维护：收缩 WAL 文件、VACUUM 去碎片、刷新查询规划统计。

        必须在本连接（autocommit）下执行，不能在 BEGIN IMMEDIATE 事务内调用；
        建议放在每日同步完成后的维护阶段（此时无其他写者）。

        ANALYZE 放在 VACUUM 之后：VACUUM 会重写整库、页分布变了，
        统计信息应当在页分布稳定之后采集才准。
        """
        with self._write_lock, self.connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
            conn.execute("ANALYZE")

    def apply_region_policy(self, keep: str | None = None) -> int:
        """按区域保留策略剔除不匹配区域的线路。

        keep 传 None 时跟随 KEEP_REGION 配置；也可显式传 'domestic' / 'international'。
        把另一区域的条目置 active=0（从列表与订阅移除，记录保留可恢复），
        幂等操作：未配置有效区域时不做任何事。
        """
        keep = (keep or settings.keep_region or "").strip().lower()
        if keep not in {"domestic", "international"}:
            return 0
        other = "international" if keep == "domestic" else "domestic"
        with self.write() as conn:
            cursor = conn.execute(
                "UPDATE source_entries SET active=0 WHERE active=1 AND region=?",
                (other,),
            )
        return cursor.rowcount if cursor else 0

    def allowed_hosts(self) -> list[str]:
        """已手动放行的数据源主机（不含代码里写死的内置白名单）。"""
        return [row["host"] for row in self.rows("SELECT host FROM allowed_hosts ORDER BY host")]

    def add_allowed_hosts(self, hosts: Iterable[str], note: str = "") -> list[str]:
        """放行一批数据源主机（幂等），返回实际写入的主机名。

        只接受规范化主机名（小写、无端口、无协议）；地址安全性由调用方先校验。
        """
        cleaned = sorted({(h or "").strip().lower() for h in hosts if (h or "").strip()})
        if not cleaned:
            return []
        with self.write() as conn:
            conn.executemany(
                "INSERT INTO allowed_hosts(host, note, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(host) DO UPDATE SET note=excluded.note",
                [(h, note, utc_now()) for h in cleaned],
            )
        return cleaned

    def remove_allowed_host(self, host: str) -> bool:
        """撤销一个主机的放行；返回是否真的删掉了一行。"""
        target = (host or "").strip().lower()
        if not target:
            return False
        with self.write() as conn:
            cursor = conn.execute("DELETE FROM allowed_hosts WHERE host=?", (target,))
        return bool(cursor and cursor.rowcount)

    def normalize_groups(self) -> int:
        """把存量频道的 group_title 归一化为 央视/地方台/其他/国外（幂等）。

        新同步的条目在写入时已归一化；此处只处理历史数据，重复执行无副作用。
        SQL 预过滤掉已是归一名的行——normalize_group 只返回这 4 个值，
        所以命中过滤的行一定不需要更新；否则每次启动都要把几万行拉进 Python 白扫一遍。
        """
        from .parsers import normalize_group

        rows = self.rows(
            "SELECT rowid, name, group_title, region FROM source_entries "
            "WHERE active=1 AND group_title NOT IN ('央视', '地方台', '其他', '国外')"
        )
        updates: list[tuple[str, int]] = []
        for row in rows:
            ng = normalize_group(row["name"], row["group_title"], row["region"])
            if ng != row["group_title"]:
                updates.append((ng, row["rowid"]))
        if not updates:
            return 0
        with self.write() as conn:
            conn.executemany("UPDATE source_entries SET group_title=? WHERE rowid=?", updates)
        return len(updates)


db = Database(settings.database_path)
