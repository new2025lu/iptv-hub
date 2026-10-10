"""v49「全部线路判死」只读清单（GET /api/dead-channels）的回归测试。

判据（服务端 `app/main.py` 的 `_ALIVE_ENTRY_SQL`，唯一实现）：
  某频道**所有** active=1 条目都判死 —— 健康行 `status IN ('error','unsupported')`
  **或**该 stream_fingerprint 根本没有健康行；只要有**一条** `ok` 或**未测**
  （status 为空/NULL）就不算 —— **没测 ≠ 坏**（把没测当坏会凭空造出"整频道不可用"，
  用户据此删源就是误删）。blocked / purged 同样不算判死（宁可漏报，不误报）。

需要 FastAPI（导入 app.main）：容器内 `PYTHONPATH=. python -m unittest discover -s tests`
跑全部用例；宿主机没装 FastAPI 时这一组整体 skip，其余测试不受影响。

隔离硬约束：只在 `/tmp` 下建库（先 assert 再动任何 SQL），并把 `app.main` 里的 `db`
全局换成它；TestClient **不进入 lifespan**，不会触发启动期写库。生产库
`data/iptv.sqlite3` 与本组测试无关。
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace as dataclass_replace
from pathlib import Path
from unittest import mock

from app import db as db_module
from app.config import settings as real_settings
from app.revision import current_revision

try:  # 宿主机没有 FastAPI 时只跳过这一组（其余用例与容器无关）
    from fastapi.testclient import TestClient

    from app import main as main_module
except ModuleNotFoundError:  # pragma: no cover - 宿主机环境
    TestClient = None
    main_module = None


# 隔离硬约束：先在 /tmp 下建库并断言路径，再执行任何 SQL
_TMPDIR = tempfile.TemporaryDirectory(prefix="iptv-hub-dead-", dir="/tmp")
_DB_PATH = Path(_TMPDIR.name) / "dead.sqlite3"
assert str(_DB_PATH).startswith("/tmp/"), _DB_PATH
assert "/data/iptv.sqlite3" not in str(_DB_PATH), _DB_PATH
_ISOLATED = db_module.Database(_DB_PATH)
_ISOLATED.initialize()

ADMIN_TOKEN = "test-admin-token-v49"
_AUTH_SETTINGS = dataclass_replace(real_settings, admin_token=ADMIN_TOKEN)
_OPEN_SETTINGS = dataclass_replace(real_settings, admin_token="")

TS_EARLY = "2026-10-05T01:00:00+00:00"
TS_LATE = "2026-10-05T03:00:00+00:00"

# 分页填充频道的条数：判死频道总数 = 3（核心全死样本）+ 1（多源样本）+ FILLERS
FILLERS = 58
DEAD_TOTAL = 3 + 1 + FILLERS   # 62

# 期望的清单顺序：排序键 = (group_title, name, channel_key)
#   组「分页」< 组「多源」< 组「测试」（UTF-8 字节序），组内再按名称排：
#   组「测试」里 不支持测速频道 < 全死频道 < 缺健康行频道
EXPECTED_FIRST_KEY = "ck-fill-00"
MULTI_SOURCE_KEY = "ck-many-sources"
TAIL_KEYS = ["ck-unsupported", "ck-all-dead", "ck-missing"]
EXPECTED_ORDER = (
    [f"ck-fill-{index:02d}" for index in range(FILLERS)] + [MULTI_SOURCE_KEY] + TAIL_KEYS
)


def _add_source(slug: str, name: str, priority: int) -> None:
    with _ISOLATED.write() as conn:
        conn.execute(
            "INSERT INTO sources(slug, name, url, format, priority, source_group) "
            "VALUES (?, ?, ?, 'auto', ?, '') "
            "ON CONFLICT(slug) DO UPDATE SET name=excluded.name, priority=excluded.priority",
            (slug, name, f"http://{slug}.example/x.m3u", priority),
        )


def _add_entry(slug: str, entry_key: str, channel_key: str, name: str, *,
               group_title: str = "测试", region: str = "domestic") -> str:
    """插入一条 active 条目，返回它的 stream_fingerprint（与 entry_key 一一对应）。"""
    fingerprint = f"fp-{slug}-{entry_key}"
    with _ISOLATED.write() as conn:
        conn.execute(
            "INSERT INTO source_entries(source_slug, entry_key, stream_fingerprint, channel_key, "
            "name, normalized_name, group_title, url, region, active, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?) "
            "ON CONFLICT(source_slug, entry_key) DO UPDATE SET channel_key=excluded.channel_key",
            (slug, entry_key, fingerprint, channel_key, name, name, group_title,
             f"http://{slug}.example/{entry_key}", region, TS_EARLY, TS_EARLY),
        )
    return fingerprint


def _set_health(fingerprint: str, status: str, *, speed: float | None = None,
                checked_at: str = TS_EARLY) -> None:
    with _ISOLATED.write() as conn:
        conn.execute(
            "INSERT INTO stream_health(fingerprint, url, status, speed_kbps, checked_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(fingerprint) DO UPDATE SET status=excluded.status, "
            "speed_kbps=excluded.speed_kbps, checked_at=excluded.checked_at",
            (fingerprint, f"http://example/{fingerprint}", status, speed, checked_at),
        )


def _clear_health(fingerprint: str) -> None:
    """删掉健康行 —— 覆盖「该线路完全没有健康行」这一档判死。"""
    with _ISOLATED.write() as conn:
        conn.execute("DELETE FROM stream_health WHERE fingerprint=?", (fingerprint,))


def _seed() -> None:
    with _ISOLATED.write() as conn:
        conn.execute("DELETE FROM source_entries")
        conn.execute("DELETE FROM stream_health")
        conn.execute("DELETE FROM sources")
    _add_source("s1", "源一", 1)
    _add_source("s2", "源二", 2)
    _add_source("s3", "源三", 3)
    _add_source("s4", "源四", 4)

    # 1) 全死：error + unsupported，两条都已测 → 列出；last_checked_at 取较晚的那条
    fp = _add_entry("s1", "e1", "ck-all-dead", "全死频道")
    _set_health(fp, "error", checked_at=TS_EARLY)
    fp = _add_entry("s2", "e2", "ck-all-dead", "全死频道")
    _set_health(fp, "unsupported", checked_at=TS_LATE)

    # 2) 缺健康行：没有健康行也算判死 → 列出
    _add_entry("s1", "e3", "ck-missing", "缺健康行频道")

    # 3) 只有 unsupported
    fp = _add_entry("s1", "e4", "ck-unsupported", "不支持测速频道")
    _set_health(fp, "unsupported")

    # 4) 全死但涉及 4 个源：sources 只返回前 3 个，source_count 是 4
    for index, slug in enumerate(("s1", "s2", "s3", "s4"), start=1):
        fp = _add_entry(slug, f"e-multi-{index}", MULTI_SOURCE_KEY, "多源全死频道", group_title="多源")
        _set_health(fp, "error")

    # 5) 含一条 ok → **不**列出
    fp = _add_entry("s1", "e5", "ck-mixed-ok", "含正常线路", group_title="不列出")
    _set_health(fp, "error")
    fp = _add_entry("s2", "e6", "ck-mixed-ok", "含正常线路", group_title="不列出")
    _set_health(fp, "ok", speed=900.0)

    # 6) 含一条**未测**（status=''）→ **不**列出（没测 ≠ 坏）
    fp = _add_entry("s1", "e7", "ck-mixed-untested", "含未测线路", group_title="不列出")
    _set_health(fp, "error")
    fp = _add_entry("s2", "e8", "ck-mixed-untested", "含未测线路", group_title="不列出")
    _set_health(fp, "", speed=None)

    # 6b) 含一条**完全没有健康行**、但另一条 ok → 不列出（活口优先）
    fp = _add_entry("s1", "e9a", "ck-alive-plus-missing", "活口加缺行", group_title="不列出")
    _set_health(fp, "ok", speed=1200.0)
    _add_entry("s2", "e9b", "ck-alive-plus-missing", "活口加缺行", group_title="不列出")

    # 7) 只有 blocked（守卫拦截，没测成）→ 不列出
    fp = _add_entry("s1", "e9", "ck-blocked", "只有守卫拦截", group_title="不列出")
    _set_health(fp, "blocked")

    # 8) 只有 purged（人工季度停用）→ 不列出
    fp = _add_entry("s1", "e10", "ck-purged", "只有人工停用", group_title="不列出")
    _set_health(fp, "purged")

    # 9) 全 ok → 不列出
    fp = _add_entry("s1", "e11", "ck-ok", "全部正常", group_title="不列出")
    _set_health(fp, "ok", speed=500.0)

    # 10) 分页填充：每个频道 1 条 error 线路
    for index in range(FILLERS):
        key = f"ck-fill-{index:02d}"
        fp = _add_entry("s1", f"e-fill-{index:02d}", key, f"填空频道{index:02d}", group_title="分页")
        _set_health(fp, "error")


class DeadChannelTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _seed()

    def setUp(self):
        self._patches = [
            mock.patch.object(main_module, "db", _ISOLATED),
            mock.patch.object(main_module, "settings", _OPEN_SETTINGS),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)
        # 阶段 1 的 revision 缓存是模块级全局：每条用例从干净状态开始，避免串味
        main_module._DEAD_KEYS_CACHE.clear()
        self.addCleanup(main_module._DEAD_KEYS_CACHE.clear)


@unittest.skipIf(TestClient is None, "宿主机没有 FastAPI：这一组在容器内运行")
class DeadChannelCriteriaTests(DeadChannelTestCase):
    """判据本身：谁被列出、谁不被列出。"""

    @staticmethod
    def payload(**kwargs) -> dict:
        return main_module.dead_channels(**{"page": 1, "page_size": 200, **kwargs})

    def keys(self, page_size: int = 200) -> list[str]:
        return [item["channel_key"] for item in self.payload(page_size=page_size)["items"]]

    def test_count_and_membership(self):
        data = self.payload()
        self.assertEqual(data["count"], DEAD_TOTAL)
        self.assertEqual(len(data["items"]), DEAD_TOTAL)
        keys = set(self.keys())
        # 全死 / 缺健康行 / 只有 unsupported / 多源全死 —— 都在清单里
        self.assertIn("ck-all-dead", keys)
        self.assertIn("ck-missing", keys)
        self.assertIn("ck-unsupported", keys)
        self.assertIn(MULTI_SOURCE_KEY, keys)
        # 含 ok、含未测、只有 blocked/purged、全 ok —— 一个都不许出现
        for absent in ("ck-mixed-ok", "ck-mixed-untested", "ck-alive-plus-missing",
                       "ck-blocked", "ck-purged", "ck-ok"):
            self.assertNotIn(absent, keys, absent)

    def test_channel_with_ok_line_is_excluded(self):
        row = _ISOLATED.row(
            "SELECT COUNT(*) AS n FROM source_entries WHERE channel_key='ck-mixed-ok'")["n"]
        self.assertEqual(row, 2)          # 样本本身有 2 条线路（1 死 1 活）
        self.assertNotIn("ck-mixed-ok", self.keys())

    def test_channel_with_untested_line_is_excluded(self):
        """未测（status=''）不算判死：没测 ≠ 坏。"""
        row = _ISOLATED.row(
            "SELECT h.status FROM source_entries e JOIN stream_health h "
            "ON h.fingerprint=e.stream_fingerprint "
            "WHERE e.channel_key='ck-mixed-untested' ORDER BY h.status")["status"]
        self.assertEqual(row, "")         # 确实存在一条空状态的线路
        self.assertNotIn("ck-mixed-untested", self.keys())

    def test_missing_health_row_counts_as_dead(self):
        row = _ISOLATED.row(
            "SELECT COUNT(*) AS n FROM source_entries e LEFT JOIN stream_health h "
            "ON h.fingerprint=e.stream_fingerprint "
            "WHERE e.channel_key='ck-missing' AND h.fingerprint IS NULL")["n"]
        self.assertEqual(row, 1)          # 这条线路确实没有健康行
        self.assertIn("ck-missing", self.keys())

    def test_all_dead_row_fields(self):
        item = next(i for i in self.payload()["items"] if i["channel_key"] == "ck-all-dead")
        self.assertEqual(item["name"], "全死频道")
        self.assertEqual(item["group_title"], "测试")
        self.assertEqual(item["region"], "domestic")
        self.assertEqual(item["stream_count"], 2)
        self.assertEqual(item["dead_streams"], 2)
        self.assertIsNone(item["best_speed"])                  # 没有 ok 线路 → 恒为空
        self.assertEqual(item["last_checked_at"], TS_LATE)     # 取最晚的一条
        self.assertEqual(item["sources"], ["源一", "源二"])     # 按 priority 排序
        self.assertEqual(item["source_count"], 2)

    def test_dead_streams_equals_stream_count(self):
        """判据成立时「判死线路数」恒等于「活跃线路数」—— 这就是"全死"的定义。"""
        for item in self.payload()["items"]:
            self.assertEqual(item["dead_streams"], item["stream_count"], item["channel_key"])

    def test_sources_capped_at_three(self):
        item = next(i for i in self.payload()["items"] if i["channel_key"] == MULTI_SOURCE_KEY)
        self.assertEqual(len(item["sources"]), 3)
        self.assertEqual(item["sources"], ["源一", "源二", "源三"])
        self.assertEqual(item["source_count"], 4)

    def test_order_is_group_then_name(self):
        """排序固定为 (分组, 名称, channel_key)：同一版本内翻页不漏不重。"""
        keys = self.keys()
        self.assertEqual(keys, EXPECTED_ORDER)
        self.assertEqual(keys[0], EXPECTED_FIRST_KEY)          # 组「分页」排在「多源/测试」前
        self.assertEqual(len(keys), len(set(keys)))            # 不重不漏


@unittest.skipIf(TestClient is None, "宿主机没有 FastAPI：这一组在容器内运行")
class DeadChannelPaginationTests(DeadChannelTestCase):
    def test_default_page_size_is_50(self):
        data = main_module.dead_channels()
        self.assertEqual(data["count"], DEAD_TOTAL)
        self.assertEqual(data["page"], 1)
        self.assertEqual(data["page_size"], 50)
        self.assertEqual(len(data["items"]), 50)

    def test_second_page_and_out_of_range(self):
        data = main_module.dead_channels(page=2, page_size=50)
        self.assertEqual(len(data["items"]), DEAD_TOTAL - 50)
        self.assertEqual(data["items"][0]["channel_key"], f"ck-fill-{50:02d}")
        # 越界页：items 空但 count 照常返回（前端据此收敛页码）
        empty = main_module.dead_channels(page=99, page_size=50)
        self.assertEqual(empty["items"], [])
        self.assertEqual(empty["count"], DEAD_TOTAL)

    def test_page_size_upper_bound(self):
        data = main_module.dead_channels(page=1, page_size=200)
        self.assertEqual(len(data["items"]), DEAD_TOTAL)       # 上限本身可用

    def test_read_only_no_writes(self):
        """纯查询：调用前后库内行数与数据版本号都不许变。"""
        before = {
            table: _ISOLATED.row(f"SELECT COUNT(*) AS n FROM {table}")["n"]
            for table in ("source_entries", "stream_health", "sources", "favorites")
        }
        revision_before = current_revision()
        main_module._DEAD_KEYS_CACHE.clear()
        data = main_module.dead_channels(page=1, page_size=50)
        main_module._DEAD_KEYS_CACHE.clear()
        main_module.dead_channels(page=1, page_size=50)
        self.assertEqual(data["count"], DEAD_TOTAL)
        after = {
            table: _ISOLATED.row(f"SELECT COUNT(*) AS n FROM {table}")["n"]
            for table in ("source_entries", "stream_health", "sources", "favorites")
        }
        self.assertEqual(before, after)
        self.assertEqual(current_revision(), revision_before)


@unittest.skipIf(TestClient is None, "宿主机没有 FastAPI：这一组在容器内运行")
class DeadChannelHttpTests(DeadChannelTestCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(main_module.app)

    def test_requires_admin_token(self):
        # 令牌非空：无头 / 错头一律 401，正确头 200。
        # 用 with 而不是 addCleanup —— 退出 with 后才恢复空令牌，最后那条"不校验"的断言
        # 必须跑在**没打补丁**的状态下（否则测的还是鉴权分支）。
        with mock.patch.object(main_module, "settings", _AUTH_SETTINGS):
            self.assertEqual(self.client.get("/api/dead-channels").status_code, 401)
            self.assertEqual(
                self.client.get("/api/dead-channels",
                                headers={"Authorization": "Bearer wrong-token"}).status_code, 401)
            ok = self.client.get("/api/dead-channels",
                                 headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
            self.assertEqual(ok.status_code, 200)
            self.assertEqual(ok.json()["count"], DEAD_TOTAL)
        # 令牌为空时后端不校验（可信局域网模式），与其他只读接口一致
        self.assertEqual(self.client.get("/api/dead-channels").status_code, 200)

    def test_contract_shape(self):
        payload = self.client.get("/api/dead-channels?page=1&page_size=200").json()
        self.assertEqual(sorted(payload), ["count", "items", "page", "page_size"])
        self.assertEqual(payload["count"], DEAD_TOTAL)
        item = payload["items"][0]
        self.assertEqual(
            sorted(item),
            ["best_speed", "channel_key", "dead_streams", "group_title", "last_checked_at",
             "name", "region", "source_count", "sources", "stream_count"])
        self.assertIsInstance(item["sources"], list)

    def test_query_bounds_return_422(self):
        for query in ("page=0", "page=2001", "page_size=0", "page_size=201"):
            response = self.client.get(f"/api/dead-channels?{query}")
            self.assertEqual(response.status_code, 422, query)

    def test_catalog_endpoint_unchanged(self):
        """新接口不影响既有目录接口：/api/channels 仍按原契约返回。"""
        payload = self.client.get("/api/channels?page=1&page_size=5").json()
        self.assertEqual(sorted(payload), ["items", "page", "page_size", "total"])
        self.assertEqual(payload["page_size"], 5)
