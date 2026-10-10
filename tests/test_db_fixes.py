"""v42 追加修复的隔离库测试（①304 成功时间 / ②护栏回滚 / ④purge 保留 ignored）。

**隔离硬约束**：这些用例只在 `/tmp` 下新建一个空库（`Database(path).initialize()`），
并把各模块里的 `db` 全局替换成它 —— 断言 `path.startswith("/tmp/")` 之后再动任何 SQL，
绝不碰生产库 `/data/iptv.sqlite3`。
"""

from __future__ import annotations

import inspect
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import db as db_module
from app import speed, sync
from app.db import utc_now


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


class IsolatedDbTestCase(unittest.TestCase):
    """每个测试类一个 /tmp 空库；类内各用例共用，用例之间用不同 slug/url 隔离。"""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(prefix="iptv-hub-v42-", dir="/tmp")
        cls.db_path = Path(cls._tmpdir.name) / "isolated.sqlite3"
        # 硬约束：实验库必须在 /tmp 下。断言放在最前面，失败就根本不会执行任何写操作。
        assert str(cls.db_path).startswith("/tmp/"), f"隔离库必须在 /tmp 下：{cls.db_path}"
        cls.isolated = db_module.Database(cls.db_path)
        cls.isolated.initialize()

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def setUp(self):
        self._patches = [
            mock.patch.object(db_module, "db", self.isolated),
            mock.patch.object(speed, "db", self.isolated),
            mock.patch.object(sync, "db", self.isolated),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    # --- 便捷断言/构造 ---

    def add_source(self, slug: str, status: str, last_success_at: str | None) -> None:
        # 用 upsert：zbds-domestic / iptv-org 这类 slug 在 initialize() 里已经被内置种子占用，
        # 这里直接改成用例需要的状态（真实感更强：审计报的正是这两个源）。
        with self.isolated.write() as conn:
            conn.execute(
                "INSERT INTO sources(slug, name, url, format, priority, status, last_success_at, last_checked_at, error_message) "
                "VALUES (?, ?, ?, 'auto', 100, ?, ?, '', '') "
                "ON CONFLICT(slug) DO UPDATE SET status=excluded.status, "
                "last_success_at=excluded.last_success_at, error_message=''",
                (slug, slug, f"http://{slug}.example/list.m3u", status, last_success_at),
            )

    def source_row(self, slug: str) -> dict:
        return self.isolated.row("SELECT * FROM sources WHERE slug=?", (slug,))

    def stale_alerts(self, days: int) -> list[str]:
        """与 main.py 首页告警完全同口径的谓词（status='error' 或超过 N 天没成功同步）。"""
        return [
            row["slug"]
            for row in self.isolated.rows(
                """
                SELECT slug FROM sources
                WHERE status = 'error'
                   OR (status <> 'never' AND (COALESCE(last_success_at, '') = '' OR last_success_at < ?))
                ORDER BY slug
                """,
                (_cutoff(days),),
            )
        ]


class NotModifiedLastSuccessTests(IsolatedDbTestCase):
    """审计①（最紧急·时间炸弹）：304 也是同步成功，必须推进 last_success_at。"""

    def test_304_advances_last_success_at(self):
        old = _cutoff(4)                      # 4 天前成功过：已经越过 3 天告警线
        self.add_source("zbds-domestic", "not_modified", old)
        self.assertEqual(self.stale_alerts(3), ["zbds-domestic"], "前置条件：不推进就会被误报")

        checked = utc_now()
        sync._mark_source_not_modified("zbds-domestic", checked, 304)

        row = self.source_row("zbds-domestic")
        self.assertEqual(row["last_success_at"], checked)
        self.assertEqual(row["status"], "not_modified")
        self.assertEqual(row["last_http_status"], 304)
        self.assertEqual(row["last_checked_at"], checked)
        self.assertEqual(self.stale_alerts(3), [], "推进后不该再出现在「源异常」告警里")

    def test_200_same_hash_branch_keeps_the_same_semantics(self):
        """200 + 内容哈希一致的分支本来就推进；两处口径必须一致（防回归漂移）。"""
        self.add_source("iptv-org", "not_modified", _cutoff(4))
        checked = utc_now()
        sync._mark_source_not_modified("iptv-org", checked, 200, etag="W/abc", last_modified="Mon")
        row = self.source_row("iptv-org")
        self.assertEqual(row["last_success_at"], checked)
        self.assertEqual(row["etag"], "W/abc")
        self.assertEqual(self.stale_alerts(3), [])


class PurgeInvalidDiscoveredTests(IsolatedDbTestCase):
    """审计④：purge 不得删掉用户显式「忽略」的候选；days 死形参已删除。"""

    def add_candidate(self, url: str, state: str, check_status: str) -> None:
        now = utc_now()
        with self.isolated.write() as conn:
            conn.execute(
                "INSERT INTO discovered(url, page, first_seen_at, last_seen_at, hits, state, check_status, check_message) "
                "VALUES (?, 'https://index.example/', ?, ?, 1, ?, ?, '检测无效')",
                (url, now, now, state, check_status),
            )

    def test_ignored_and_accepted_survive_purge(self):
        self.add_candidate("http://dead.example/x.m3u", "new", "error")
        self.add_candidate("http://ignored.example/x.m3u", "ignored", "error")
        self.add_candidate("http://accepted.example/x.m3u", "accepted", "error")

        removed = db_module.purge_invalid_discovered()

        self.assertEqual(removed, 1)
        left = {row["url"]: row["state"] for row in self.isolated.rows("SELECT url, state FROM discovered")}
        self.assertEqual(left, {
            "http://ignored.example/x.m3u": "ignored",
            "http://accepted.example/x.m3u": "accepted",
        }, "用户的忽略/采用决定不能被 purge 抹掉")
        tombs = {row["url"] for row in self.isolated.rows("SELECT url FROM discovered_rejected")}
        self.assertIn("http://dead.example/x.m3u", tombs)

    def test_days_parameter_is_gone(self):
        """`days` 原先完全没用到（days=0 仍清空全部），留着会误导调用方；已删除。"""
        params = inspect.signature(db_module.purge_invalid_discovered).parameters
        self.assertNotIn("days", params)


class RollbackRoundFailuresTests(IsolatedDbTestCase):
    """审计②：全灭轮的失败计数必须回滚，否则"只推迟"等于"晚一轮删除"。"""

    def add_health(self, fingerprint: str, status: str, fail_count: int) -> None:
        with self.isolated.write() as conn:
            conn.execute(
                "INSERT INTO stream_health(fingerprint, url, status, fail_count, checked_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (fingerprint, f"http://{fingerprint}.example/x.m3u8", status, fail_count, utc_now()),
            )

    def fail_count(self, fingerprint: str) -> int:
        return int(self.isolated.row(
            "SELECT fail_count FROM stream_health WHERE fingerprint=?", (fingerprint,)
        )["fail_count"])

    def test_round_failure_is_rolled_back_and_ok_rows_are_untouched(self):
        self.add_health("fp-old-fail", "error", 4)      # 本轮 +1 → 5，超过阈值 4 会被剔除
        self.add_health("fp-ok", "ok", 0)
        items = [
            ("fp-old-fail", "http://a.example/x.m3u8", {"status": "error", "error": "timeout"}),
            ("fp-ok", "http://b.example/x.m3u8", {"status": "ok", "speed_kbps": 120.0}),
        ]
        counts = speed.speed_tester._save_batch(items)
        self.assertEqual(counts["fp-old-fail"], 5, "前置条件：本轮确实把计数推过了阈值")
        self.assertEqual(counts["fp-ok"], 0)

        rolled = speed._rollback_round_failures(["fp-old-fail", "fp-ok"])

        self.assertEqual(rolled, 1, "只回滚 error/unsupported 行")
        self.assertEqual(self.fail_count("fp-old-fail"), 4, "回到本轮之前的计数")
        self.assertEqual(self.fail_count("fp-ok"), 0, "ok 行不得被减成负数")

    def test_rollback_floors_at_zero(self):
        self.add_health("fp-first-fail", "error", 0)
        speed.speed_tester._save_batch([("fp-first-fail", "http://c.example/x.m3u8", {"status": "error"})])
        self.assertEqual(self.fail_count("fp-first-fail"), 1)
        speed._rollback_round_failures(["fp-first-fail"])
        self.assertEqual(self.fail_count("fp-first-fail"), 0)

    def test_rollback_chunks_beyond_sqlite_variable_limit(self):
        """一轮可达 SPEED_MAX_ITEMS=5000 条；一次性 IN 会撞上 SQLite 的变量数上限
        （旧版 999），所以实现是分批的 —— 这里用 1000 条验证分批确实生效。"""
        fps = [f"fp-{i:04d}" for i in range(1000)]
        with self.isolated.write() as conn:
            conn.executemany(
                "INSERT INTO stream_health(fingerprint, url, status, fail_count, checked_at) "
                "VALUES (?, ?, 'error', 1, ?)",
                [(fp, f"http://{fp}.example/x.m3u8", utc_now()) for fp in fps],
            )
        rolled = speed._rollback_round_failures(fps)
        self.assertEqual(rolled, 1000)
        remaining = self.isolated.row("SELECT SUM(fail_count) AS total FROM stream_health WHERE fingerprint LIKE 'fp-%'")
        self.assertEqual(int(remaining["total"]), 0)

    def test_empty_input_is_a_noop(self):
        self.assertEqual(speed._rollback_round_failures([]), 0)


if __name__ == "__main__":
    unittest.main()
