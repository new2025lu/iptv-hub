"""v51：数据源停用（`sources.enabled`）的隔离库测试。

这一列解决的是一个**产品语义**缺口，不是实现细节：内置源删不掉（`SOURCE_SEEDS`
每次启动把它写回来，`DELETE /api/sources/{slug}` 对内置源直接 400），而「这个源现在
不可用，别再抓取、也别再测速」是需要跨重启保留的用户意图。停用是它的最小实现：
只置一个状态位，条目与健康记录一行不删，随时可逆。

覆盖点（每条都对应一个真实的回归风险）：

  * **迁移**：旧库（无 `enabled` 列）启动后补列、存量行补 1、第二次启动不重复 ALTER
    （重复 ADD COLUMN 会抛 duplicate column name → 容器启动崩溃循环）；
  * **种子不覆盖**：`_seed_sources` 的 upsert 一旦把 `enabled` 写进 ON CONFLICT 的
    更新列表，用户刚停用的内置源会在下一次启动（含 `up -d --force-recreate`）被悄悄
    启用回来 —— 这条是整列存在的前提，必须钉死；
  * **同步跳过**：`sync.select_sync_sources` 在全量与单源两条路径都排除 `enabled=0`，
    单源请求停用源时返回被跳过的 slug（供日志/调用方区分「不存在」与「已停用」）；
  * **待测队列排除**：`speed._pending_sql` 生成的 SQL 真的不再返回停用源的条目，
    且 `refresh=True`（手动全量重测）也排不掉它；启用后**不需要重新同步**就立刻回来；
  * **HTTP**：`/api/sources` 暴露 `enabled`、切换接口的鉴权（401 且不写库）/非法值（422）
    /未知源（404）、内置源可停用而仍不可删除、停用会 `bump_revision()`（导出缓存失效）。

隔离硬约束：只在 `/tmp` 下建库（先 assert 再动任何 SQL），并且**同时**替换
`app.db`、`app.sync`、`app.speed`、`app.main` 四个模块里的 `db` 引用 —— 因为
`from .db import db` 是值绑定，只 patch 其中一个，另一个模块的函数仍会写到生产库
`data/iptv.sqlite3`。
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace as dataclass_replace
from pathlib import Path
from unittest import mock

from app import db as db_module
from app import speed as speed_module
from app import sync as sync_module
from app.config import settings as real_settings
from app.revision import bump_revision, current_revision

try:  # 宿主机没有 FastAPI 时只跳过 HTTP 一组（其余用例与容器无关）
    from fastapi.testclient import TestClient

    from app import main as main_module
except ModuleNotFoundError:  # pragma: no cover - 宿主机环境
    TestClient = None
    main_module = None


# 隔离硬约束：先在 /tmp 下建库并断言路径，再执行任何 SQL
_TMPDIR = tempfile.TemporaryDirectory(prefix="iptv-hub-enabled-", dir="/tmp")
_DB_PATH = Path(_TMPDIR.name) / "enabled.sqlite3"
assert str(_DB_PATH).startswith("/tmp/"), _DB_PATH
assert "/data/iptv.sqlite3" not in str(_DB_PATH), _DB_PATH
_ISOLATED = db_module.Database(_DB_PATH)
_ISOLATED.initialize()

ADMIN_TOKEN = "test-admin-token-v51"
# 与 tests/test_restore_targeted.py 同口径：用 dataclass_replace 造一份真 settings，
# 不跟着部署环境的 admin_token / keep_region 走。
_AUTH_SETTINGS = dataclass_replace(real_settings, admin_token=ADMIN_TOKEN, keep_region="")

TS = "2026-10-05T02:00:00+00:00"

# v51 之前的 sources 表（没有 enabled，其余列与上一版一致）—— 用来真跑一次迁移
_LEGACY_SOURCES_DDL = """
CREATE TABLE sources (
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
    parent_slug TEXT NOT NULL DEFAULT '',
    orphan_note TEXT NOT NULL DEFAULT '',
    source_group TEXT NOT NULL DEFAULT ''
)
"""


def _columns(database: db_module.Database, table: str) -> set[str]:
    with database.connect() as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _reset_sources() -> None:
    """把实验库恢复成「两个源、一个频道、一条健康记录」的干净状态。"""
    with _ISOLATED.write() as conn:
        conn.execute("DELETE FROM stream_health")
        conn.execute("DELETE FROM source_entries")
        conn.execute("DELETE FROM favorites")
        conn.execute("DELETE FROM sources")
        conn.execute(
            "INSERT INTO sources(slug, name, url, format, priority, enabled) "
            "VALUES ('on-src', '启用源', 'http://on.example/x.m3u', 'm3u', 1, 1), "
            "       ('off-src', '停用源', 'http://off.example/x.m3u', 'm3u', 2, 0)"
        )
        conn.execute(
            "INSERT INTO source_entries(source_slug, entry_key, stream_fingerprint, channel_key, "
            "name, normalized_name, group_title, url, region, active, first_seen_at, last_seen_at) "
            "VALUES ('on-src', 'e-on', 'fp-on', 'ck-1', '频道一', '频道一', '测试', "
            "        'http://on.example/1.m3u8', 'domestic', 1, ?, ?), "
            "       ('off-src', 'e-off', 'fp-off', 'ck-1', '频道一', '频道一', '测试', "
            "        'http://off.example/1.m3u8', 'domestic', 1, ?, ?)",
            (TS, TS, TS, TS),
        )


class LegacyMigrationTests(unittest.TestCase):
    """旧库补列：幂等、存量行补 1、运行期状态不受影响。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="iptv-hub-enabled-legacy-", dir="/tmp")
        self.db_path = Path(self._tmpdir.name) / "legacy.sqlite3"
        assert str(self.db_path).startswith("/tmp/"), self.db_path
        assert "/data/iptv.sqlite3" not in str(self.db_path), self.db_path
        self.addCleanup(self._tmpdir.cleanup)

    def _make_legacy(self) -> db_module.Database:
        """建一个「v51 之前」的库：sources 表没有 enabled 列，且已有一行同步过的源。"""
        conn = sqlite3.connect(self.db_path)
        conn.executescript(_LEGACY_SOURCES_DDL)
        conn.execute(
            "INSERT INTO sources(slug, name, url, format, priority, status, item_count, source_group) "
            "VALUES ('ysptp-cctv', '央视频官方直播', 'http://192.0.2.10:8766/', 'm3u', 3, "
            "        'not_modified', 27, '')"
        )
        conn.commit()
        conn.close()
        database = db_module.Database(self.db_path)
        self.assertNotIn("enabled", _columns(database, "sources"), "前置条件：旧库必须没有 enabled")
        return database

    def test_migration_adds_column_with_default_enabled(self):
        database = self._make_legacy()
        database.initialize()
        self.assertIn("enabled", _columns(database, "sources"))
        row = database.row("SELECT * FROM sources WHERE slug='ysptp-cctv'")
        self.assertEqual(row["enabled"], 1, "存量源一律补 1：升级当天不能有任何源被停用")
        # 运行期状态与分组不属于迁移职责
        self.assertEqual(row["status"], "not_modified")
        self.assertEqual(row["item_count"], 27)

    def test_migration_is_idempotent_and_keeps_disabled_state(self):
        database = self._make_legacy()
        database.initialize()
        database.execute("UPDATE sources SET enabled=0 WHERE slug='ysptp-cctv'")
        database.initialize()   # 第二次启动 = 容器重启
        row = database.row("SELECT enabled FROM sources WHERE slug='ysptp-cctv'")
        self.assertEqual(row["enabled"], 0, "重启不能把停用状态冲回启用")


class SeedProtectionTests(unittest.TestCase):
    """`_seed_sources` 绝不覆盖 enabled —— 这是「停用能跨重启保留」的唯一保障。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="iptv-hub-enabled-seed-", dir="/tmp")
        self.db_path = Path(self._tmpdir.name) / "seed.sqlite3"
        assert str(self.db_path).startswith("/tmp/"), self.db_path
        self.addCleanup(self._tmpdir.cleanup)
        self.database = db_module.Database(self.db_path)
        # `set_source_enabled` 是 db.py 的模块级函数，用的是模块级单例 `db` ——
        # 不替换它，这里的写操作会落到**真实数据目录**上（第一版就是这么错的：
        # 报 "no such table: sources"，因为单例指向的是另一个空库）。
        patch = mock.patch.object(db_module, "db", self.database)
        patch.start()
        self.addCleanup(patch.stop)
        self.database.initialize()

    def test_disabled_builtin_stays_disabled_across_restart(self):
        if not db_module.SOURCE_SEEDS:
            self.skipTest("干净基线（2026-10-10）：内置种子已清空，无内置源可供本用例使用")
        self.assertTrue(db_module.set_source_enabled("ysp-live", False))
        self.database.initialize()   # 重启：种子 upsert 会跑，但不得碰 enabled
        row = self.database.row("SELECT * FROM sources WHERE slug='ysp-live'")
        self.assertEqual(row["enabled"], 0, "种子把停用状态冲回了启用")
        # 种子的职责部分仍然照常自愈
        self.assertEqual(row["priority"], 2)
        self.assertEqual(row["source_group"], "本地源")

    def test_seeded_source_defaults_to_enabled(self):
        """种子写入的行不带 enabled 时取 schema 默认 1。

        刻意不写死某个 slug：`ysptp-cctv` 已随 v54 下线，硬编码会让这条测试在
        种子集合变动时误报（断言的是一个不存在的行）。这里取任意一个非 ysp-live 的
        种子（ysp-live 被上面的用例停用过，口径不同）。
        """
        if not db_module.SOURCE_SEEDS:
            self.skipTest("干净基线（2026-10-10）：内置种子已清空，无内置源可供本用例使用")
        seed_slug = next(seed[0] for seed in db_module.SOURCE_SEEDS if seed[0] != "ysp-live")
        row = self.database.row("SELECT enabled FROM sources WHERE slug=?", (seed_slug,))
        self.assertIsNotNone(row, f"种子 {seed_slug} 应当已被 initialize() 写入")
        self.assertEqual(row["enabled"], 1)

    def test_new_rows_get_enabled_default_from_schema(self):
        self.database.execute(
            "INSERT INTO sources(slug, name, url, format, priority) "
            "VALUES ('fresh', '新源', 'http://fresh.example/x.m3u', 'm3u', 60)"
        )
        row = self.database.row("SELECT enabled FROM sources WHERE slug='fresh'")
        self.assertEqual(row["enabled"], 1, "建表默认值必须是 1，否则新加的源会静默不工作")


class _IsolatedDbTestCase(unittest.TestCase):
    """把三个模块的 `db` 都指到实验库上（值绑定：漏一个就会写到生产库）。"""

    def setUp(self):
        _reset_sources()
        # 实验库换了内容 → 让 /api/sources 这类「按数据版本缓存」的只读接口立刻失效，
        # 否则同名测试可能命中上一组用例留下的缓存（revision 未变时 TTL 内直接复用）。
        bump_revision()
        self._patches = [
            mock.patch.object(db_module, "db", _ISOLATED),
            mock.patch.object(sync_module, "db", _ISOLATED),
            mock.patch.object(speed_module, "db", _ISOLATED),
        ]
        if main_module is not None:
            self._patches.append(mock.patch.object(main_module, "db", _ISOLATED))
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)


class SyncSelectionTests(_IsolatedDbTestCase):
    """同步入口：停用源不抓，且日志要能自证。"""

    def test_bulk_selection_excludes_disabled_source(self):
        with self.assertLogs("iptv-hub.sync", level="INFO") as logs:
            rows, skipped = sync_module.select_sync_sources("")
        slugs = [row["slug"] for row in rows]
        self.assertIn("on-src", slugs)
        self.assertNotIn("off-src", slugs, "停用源不该进全量同步集合")
        self.assertEqual(skipped, "")
        self.assertTrue(any("跳过已停用源" in line for line in logs.output), logs.output)

    def test_single_source_entry_skips_disabled_source(self):
        with self.assertLogs("iptv-hub.sync", level="INFO") as logs:
            rows, skipped = sync_module.select_sync_sources("off-src")
        self.assertEqual(rows, [])
        self.assertEqual(skipped, "off-src")
        self.assertTrue(any("跳过已停用源 off-src" in line for line in logs.output), logs.output)

    def test_single_source_entry_still_syncs_enabled_source(self):
        rows, skipped = sync_module.select_sync_sources("on-src")
        self.assertEqual([row["slug"] for row in rows], ["on-src"])
        self.assertEqual(skipped, "")

    def test_unknown_slug_is_not_reported_as_disabled(self):
        """不存在的 slug 与「存在但停用」必须区分开：前者不该打「跳过已停用源」。"""
        rows, skipped = sync_module.select_sync_sources("nope")
        self.assertEqual(rows, [])
        self.assertEqual(skipped, "", "不存在 ≠ 已停用")


class PendingQueueTests(_IsolatedDbTestCase):
    """待测队列：停用源的条目一条都不该进来；启用后无需重新同步立刻回来。"""

    def _pending(self, scope: str = "all", channel_key: str = "", refresh: bool = False) -> set[str]:
        sql, params = speed_module._pending_sql(scope, channel_key, refresh)
        with _ISOLATED.connect() as conn:
            return {row[0] for row in conn.execute(sql, params).fetchall()}

    def test_disabled_source_entries_are_not_pending(self):
        pending = self._pending()
        self.assertIn("fp-on", pending)
        self.assertNotIn("fp-off", pending, "停用源的条目不该进待测队列")

    def test_refresh_does_not_sneak_disabled_entries_back_in(self):
        """refresh=True 会绕过冷却窗口，但**不能**绕过停用过滤。"""
        self.assertNotIn("fp-off", self._pending(refresh=True))

    def test_channel_and_favorites_scopes_also_exclude_disabled(self):
        with _ISOLATED.write() as conn:
            conn.execute("INSERT INTO favorites(channel_key, created_at) VALUES ('ck-1', ?)", (TS,))
        self.assertNotIn("fp-off", self._pending(scope="channel", channel_key="ck-1"))
        self.assertNotIn("fp-off", self._pending(scope="favorites"))

    def test_reenabling_restores_queue_without_resync(self):
        db_module.set_source_enabled("off-src", True)
        self.assertIn("fp-off", self._pending(), "启用后应立刻回到待测队列（不需要重新同步）")

    def test_disabled_source_entries_stay_active(self):
        """停用是**源级状态**，不改条目：active 仍为 1，重新启用即刻恢复。"""
        row = _ISOLATED.row("SELECT active FROM source_entries WHERE source_slug='off-src'")
        self.assertEqual(row["active"], 1)


@unittest.skipIf(TestClient is None, "宿主机没有 FastAPI：这一组在容器内运行")
class SourceEnableApiTests(_IsolatedDbTestCase):
    """/api/sources 暴露字段 + 切换接口的鉴权/校验/内置源语义。"""

    def setUp(self):
        super().setUp()
        # 不用 with：TestClient 只有在 with 里才跑 lifespan（会起调度线程并写库）
        self.client = TestClient(main_module.app)
        patch = mock.patch.object(main_module, "settings", _AUTH_SETTINGS)
        patch.start()
        self.addCleanup(patch.stop)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {ADMIN_TOKEN}"}

    def test_sources_endpoint_exposes_enabled(self):
        response = self.client.get("/api/sources", headers=self._headers())
        self.assertEqual(response.status_code, 200, response.text)
        by_slug = {row["slug"]: row for row in response.json()}
        self.assertEqual(by_slug["on-src"]["enabled"], 1)
        self.assertEqual(by_slug["off-src"]["enabled"], 0, "停用的源仍要出现在列表里，且带 enabled=0")
        self.assertIn("active_entries", by_slug["off-src"], "原有统计字段不能少")

    def test_disable_and_reenable(self):
        before_rev = current_revision()
        response = self.client.post("/api/sources/on-src/enable?value=0", headers=self._headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"slug": "on-src", "name": "启用源", "enabled": 0,
                                           "previous": 1, "changed": True})
        self.assertEqual(_ISOLATED.row("SELECT enabled FROM sources WHERE slug='on-src'")["enabled"], 0)
        self.assertGreater(current_revision(), before_rev, "停用会改变导出内容，必须 bump_revision")

        again = self.client.post("/api/sources/on-src/enable?value=0", headers=self._headers())
        self.assertEqual(again.json()["changed"], False, "重复停用不算变更")
        rev_after = current_revision()
        self.client.post("/api/sources/on-src/enable?value=1", headers=self._headers())
        self.assertEqual(_ISOLATED.row("SELECT enabled FROM sources WHERE slug='on-src'")["enabled"], 1)
        self.assertEqual(self.client.post("/api/sources/on-src/enable?value=1",
                                          headers=self._headers()).json()["changed"], False)
        self.assertEqual(current_revision(), rev_after + 1, "只有真正改了行才自增版本")

    def test_invalid_value_is_rejected_without_writing(self):
        for query in ("value=2", "value=-1", "value=yes", "value="):
            with self.subTest(query=query):
                response = self.client.post(f"/api/sources/on-src/enable?{query}", headers=self._headers())
                self.assertIn(response.status_code, (422,), response.text)
        self.assertEqual(_ISOLATED.row("SELECT enabled FROM sources WHERE slug='on-src'")["enabled"], 1)

    def test_unknown_slug_is_404(self):
        response = self.client.post("/api/sources/nope/enable?value=0", headers=self._headers())
        self.assertEqual(response.status_code, 404, response.text)

    def test_requires_admin_token(self):
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            with self.subTest(headers=headers):
                response = self.client.post("/api/sources/on-src/enable?value=0", headers=headers)
                self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(_ISOLATED.row("SELECT enabled FROM sources WHERE slug='on-src'")["enabled"], 1,
                         "401 不该写库")

    def test_builtin_source_can_be_disabled_but_not_deleted(self):
        """内置源的语义分界：停用允许（状态位），删除仍 400（种子会重建，删了是错觉）。"""
        if not db_module.SOURCE_SEEDS:
            self.skipTest("干净基线（2026-10-10）：内置种子已清空，无内置源可供本用例使用")
        _ISOLATED.initialize()   # 本组 setUp 会清表，这里把内置种子源写回来
        self.assertEqual(_ISOLATED.row("SELECT enabled FROM sources WHERE slug='ysp-live'")["enabled"], 1)
        response = self.client.post("/api/sources/ysp-live/enable?value=0", headers=self._headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(_ISOLATED.row("SELECT enabled FROM sources WHERE slug='ysp-live'")["enabled"], 0)

        deleted = self.client.delete("/api/sources/ysp-live", headers=self._headers())
        self.assertEqual(deleted.status_code, 400, deleted.text)
        self.assertIn("内置数据源不能删除", deleted.text)

        # 重启（种子 upsert 再跑一遍）后仍是停用
        _ISOLATED.initialize()
        self.assertEqual(_ISOLATED.row("SELECT enabled FROM sources WHERE slug='ysp-live'")["enabled"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
