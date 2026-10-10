"""v48 长期失效清理（AUTO_CLEANUP_DEAD_DAYS）与「没测成」归类的回归测试。

覆盖三块：
  1. `speed.cleanup_long_dead_streams()` 的判据与保底规则
     —— 未达天数不动 / 达天数停用 / 保底不删空频道 / 配置为 0 时行为不变 / 幂等；
  2. `speed.probe()` 的四类归因（域名解析不出来 → unsupported、内网地址 → blocked、
     非 HTTP 协议 → unsupported），以及它们都带 `not_measured` 标记；
  3. `speed.requeue_newly_trusted()`：用户把主机补进放行名单后，对应的 blocked 行
     必须在启动时被重新排队（`checked_at` 清空），否则要等满 RETEST_AFTER_DAYS 才会重测。

**隔离硬约束**：只在系统临时目录下建一个空库（`Database(path).initialize()`），
并把各模块里的 `db` 全局替换成它，断言 `_in_system_tmp(path)` 之后再动任何 SQL，
绝不碰生产库 `/data/iptv.sqlite3`。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from dataclasses import replace as dataclass_replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import db as db_module
from app import speed
from app.config import settings as real_settings
from app.db import utc_now


def _in_system_tmp(path) -> bool:
    """路径是否在系统临时目录下（跨平台隔离判据，与其余测试文件同一口径）。"""
    return os.path.realpath(path).startswith(os.path.realpath(tempfile.gettempdir()) + os.sep)


def _days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


class IsolatedDbTestCase(unittest.TestCase):
    """**每个用例**一个空库。

    刻意不用 setUpClass 共库：清理策略的状态是跨行的（保底规则要看"这个频道还剩几条活的"），
    共库会让上一个用例留下的 active/健康行污染下一个用例的计数 —— 实测就是这么踩到的
    （candidates 期望 1 实得 3）。每例建库约 10ms，换来用例之间零耦合，值得。
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="iptv-hub-cleanup-")
        self.db_path = Path(self._tmpdir.name) / "isolated.sqlite3"
        assert _in_system_tmp(self.db_path), f"隔离库必须在系统临时目录下：{self.db_path}"
        self.addCleanup(self._tmpdir.cleanup)
        self.isolated = db_module.Database(self.db_path)
        self.isolated.initialize()
        self._patches = [
            mock.patch.object(db_module, "db", self.isolated),
            mock.patch.object(speed, "db", self.isolated),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    # --- 便捷构造 ---

    def add_entry(self, channel_key: str, fingerprint: str, *, active: int = 1,
                  slug: str | None = None, speed_kbps: float | None = None) -> tuple[str, str]:
        # slug / entry_key 必须逐指纹唯一：只用前缀会在同一测试类里撞主键
        uniq = hashlib.sha1(fingerprint.encode()).hexdigest()[:10]
        slug = slug or f"src-{uniq}"
        with self.isolated.write() as conn:
            conn.execute(
                "INSERT INTO sources(slug, name, url, format, priority) VALUES (?, ?, ?, 'm3u', 50) "
                "ON CONFLICT(slug) DO NOTHING",
                (slug, slug, f"http://{slug}.example/list.m3u"),
            )
            conn.execute(
                "INSERT INTO source_entries(source_slug, entry_key, stream_fingerprint, channel_key, "
                "name, normalized_name, url, active, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (slug, f"ek-{uniq}", fingerprint, channel_key,
                 channel_key, channel_key.casefold(), f"http://{fingerprint[:8]}.example/x.m3u",
                 active, utc_now(), utc_now()),
            )
            if speed_kbps is not None:
                conn.execute("UPDATE stream_health SET speed_kbps=? WHERE fingerprint=?", (speed_kbps, fingerprint))
        return slug, f"ek-{uniq}"

    def add_health(self, fingerprint: str, status: str, *, dead_since: str = "",
                   fail_count: int = 0, speed_kbps: float | None = None,
                   last_fail_at: str = "") -> None:
        with self.isolated.write() as conn:
            conn.execute(
                "INSERT INTO stream_health(fingerprint, url, status, fail_count, last_fail_at, "
                "dead_since, checked_at, method) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(fingerprint) DO UPDATE SET status=excluded.status, "
                "fail_count=excluded.fail_count, dead_since=excluded.dead_since, "
                "speed_kbps=excluded.speed_kbps",
                (fingerprint, f"http://{fingerprint[:8]}.example/x.m3u", status, fail_count,
                 last_fail_at, dead_since, utc_now(), speed.METHOD_VERSION),
            )
            if speed_kbps is not None:
                conn.execute("UPDATE stream_health SET speed_kbps=? WHERE fingerprint=?", (speed_kbps, fingerprint))

    def active(self, slug: str, entry_key: str) -> int:
        row = self.isolated.row(
            "SELECT active FROM source_entries WHERE source_slug=? AND entry_key=?", (slug, entry_key)
        )
        return int(row["active"])

    def run_cleanup(self, days: int, *, dry_run: bool | None = None) -> dict:
        """按给定天数跑清理。

        `settings` 是 frozen dataclass，不能直接 setattr —— 用 dataclasses.replace
        造一份副本再换掉 speed 模块里的引用（与其余测试文件同一套路）。
        """
        fake = dataclass_replace(speed.settings, auto_cleanup_dead_days=days)
        with mock.patch.object(speed, "settings", fake):
            return speed.cleanup_long_dead_streams(dry_run=dry_run)


class CleanupLongDeadTests(IsolatedDbTestCase):
    """判据 + 保底 + 幂等。"""

    def test_config_zero_is_a_noop_and_touches_nothing(self):
        """AUTO_CLEANUP_DEAD_DAYS=0：与加这个特性之前完全一致（连查询都不做）。"""
        slug, ek = self.add_entry("ch-zero", "fp-zero-1")
        self.add_health("fp-zero-1", "error", dead_since=_days_ago(400))
        with mock.patch.object(speed, "db") as spy:
            stats = self.run_cleanup(0)
        self.assertEqual(stats["candidates"], 0)
        self.assertEqual(stats["deactivated"], 0)
        self.assertEqual(stats["enabled"], 0)
        # 关闭时不应产生任何库调用
        spy.rows.assert_not_called()
        self.assertEqual(self.active(slug, ek), 1)

    def test_below_threshold_is_untouched(self):
        """未达天数：dead_since 只过了 5 天，30 天阈值下必须原样保留。"""
        slug, ek = self.add_entry("ch-recent", "fp-recent-1")
        self.add_health("fp-recent-1", "error", dead_since=_days_ago(5), fail_count=3)
        stats = self.run_cleanup(30)
        self.assertEqual(stats["candidates"], 0)
        self.assertEqual(stats["deactivated"], 0)
        self.assertEqual(self.active(slug, ek), 1)

    def test_over_threshold_is_deactivated_and_restorable(self):
        """达天数 + 该频道还有别的活线路 -> 停用（只置 active=0，不删行）。"""
        slug, ek_dead = self.add_entry("ch-a", "fp-old-dead")
        self.add_health("fp-old-dead", "error", dead_since=_days_ago(45), fail_count=3)
        # 同频道一条健康线路 -> 不会触发保底
        slug2, ek_ok = self.add_entry("ch-a", "fp-fresh-ok", slug="src-fresh-ok")
        self.add_health("fp-fresh-ok", "ok", speed_kbps=900.0)

        stats = self.run_cleanup(30)
        self.assertEqual(stats["candidates"], 1)
        self.assertEqual(stats["deactivated"], 1)
        self.assertEqual(stats["kept_alive_channels"], 0)
        self.assertEqual(self.active(slug, ek_dead), 0, "达 30 天的判死线路应被停用")
        self.assertEqual(self.active(slug2, ek_ok), 1, "ok 线路不得被牵连")

        row = self.isolated.row(
            "SELECT COUNT(*) AS n FROM source_entries WHERE source_slug=? AND entry_key=?",
            (slug, ek_dead),
        )
        self.assertEqual(row["n"], 1, "停用不是删除：条目行必须还在（可恢复）")

    def test_keep_alive_leaves_one_line_for_a_channel_that_would_go_empty(self):
        """保底：频道只剩判死线路时，留 1 条（挑速度最快/优先级最好的那条）。"""
        slug_slow, ek_slow = self.add_entry("ch-lonely", "fp-lonely-slow")
        self.add_health("fp-lonely-slow", "error", dead_since=_days_ago(90), speed_kbps=100.0)
        slug_fast, ek_fast = self.add_entry("ch-lonely", "fp-lonely-fast", slug="src-fast")
        self.add_health("fp-lonely-fast", "unsupported", dead_since=_days_ago(90), speed_kbps=800.0)

        stats = self.run_cleanup(30)
        self.assertEqual(stats["candidates"], 2)
        self.assertEqual(stats["deactivated"], 1, "两条候选里只停用 1 条")
        self.assertEqual(stats["kept_alive_channels"], 1)
        # 留下的是"更好"的那条（速度 800 > 100）
        self.assertEqual(self.active(slug_fast, ek_fast), 1, "保底应留下可恢复性最好的那条")
        self.assertEqual(self.active(slug_slow, ek_slow), 0)

    def test_blocked_is_never_cleaned(self):
        """blocked 表示「我们没测成」而不是「线路坏」，不参与长期失效清理。"""
        slug, ek = self.add_entry("ch-blocked", "fp-blocked")
        self.add_health("fp-blocked", "blocked", dead_since=_days_ago(400))
        stats = self.run_cleanup(30)
        self.assertEqual(stats["candidates"], 0)
        self.assertEqual(self.active(slug, ek), 1)

    def test_purged_rows_are_not_reprocessed(self):
        """已永久停用（status='purged'）的健康行不重复处理 —— 保证幂等。"""
        slug, ek = self.add_entry("ch-purged", "fp-purged")
        self.add_health("fp-purged", "purged", dead_since=_days_ago(400))
        stats = self.run_cleanup(30)
        self.assertEqual(stats["candidates"], 0)
        self.assertEqual(self.active(slug, ek), 1)

    def test_dry_run_reports_without_writing(self):
        """dry_run=True 只统计不落库。"""
        slug, ek = self.add_entry("ch-dry", "fp-dry-dead")
        self.add_health("fp-dry-dead", "error", dead_since=_days_ago(60))
        slug_ok, ek_ok = self.add_entry("ch-dry", "fp-dry-ok", slug="src-dry-ok")
        self.add_health("fp-dry-ok", "ok", speed_kbps=500.0)

        stats = self.run_cleanup(30, dry_run=True)
        self.assertEqual(stats["candidates"], 1)
        self.assertEqual(stats["deactivated"], 1, "预览也要报出'将会停用多少条'")
        self.assertEqual(self.active(slug, ek), 1, "dry-run 不得改动任何条目")

    def test_idempotent_second_run_deactivates_nothing(self):
        """第二次跑同一条不会重复计数（active=1 已不成立）。"""
        slug, ek = self.add_entry("ch-idem", "fp-idem-dead")
        self.add_health("fp-idem-dead", "error", dead_since=_days_ago(60))
        slug_ok, _ = self.add_entry("ch-idem", "fp-idem-ok", slug="src-idem-ok")
        self.add_health("fp-idem-ok", "ok", speed_kbps=500.0)

        first = self.run_cleanup(30)
        second = self.run_cleanup(30)
        self.assertEqual(first["deactivated"], 1)
        self.assertEqual(second["candidates"], 0)
        self.assertEqual(second["deactivated"], 0)
        self.assertEqual(self.active(slug, ek), 0)


class SaveBatchDeadSinceTests(IsolatedDbTestCase):
    """dead_since（连续失效起点）由 _save_batch 维护：首次失败写入，成功/没测成清空。"""

    def save(self, fingerprint: str, result: dict) -> dict:
        return speed.speed_tester._save_batch([(fingerprint, "http://h.example/x.m3u8", result)])

    def dead_since(self, fingerprint: str) -> str:
        return self.isolated.row(
            "SELECT dead_since FROM stream_health WHERE fingerprint=?", (fingerprint,)
        )["dead_since"]

    def test_first_failure_starts_the_clock_and_it_is_not_refreshed(self):
        self.save("fp-clock", {"status": "error"})
        first = self.dead_since("fp-clock")
        self.assertTrue(first, "首次判定失败必须写下起点")
        self.save("fp-clock", {"status": "error"})
        self.assertEqual(self.dead_since("fp-clock"), first, "连续失败期间起点不得被刷新")

    def test_success_and_not_measured_clear_the_clock(self):
        self.save("fp-clear", {"status": "error"})
        self.assertTrue(self.dead_since("fp-clear"))
        self.save("fp-clear", {"status": "unsupported", "error": "域名无法解析：dead.example",
                               "not_measured": True})
        self.assertEqual(self.dead_since("fp-clear"), "", "没测成不能算「坏」，起点要清空")
        self.save("fp-clear", {"status": "error"})
        self.assertTrue(self.dead_since("fp-clear"))
        self.save("fp-clear", {"status": "ok", "speed_kbps": 100.0})
        self.assertEqual(self.dead_since("fp-clear"), "", "成功必须清空起点")

    def test_not_measured_does_not_advance_fail_count(self):
        """not_measured（域名解析不出来等）与 blocked 同待遇：不计失败、不改 last_fail_at。"""
        self.save("fp-nm", {"status": "error"})
        self.save("fp-nm", {"status": "error"})
        row = self.isolated.row("SELECT fail_count, last_fail_at FROM stream_health WHERE fingerprint='fp-nm'")
        self.assertEqual(row["fail_count"], 2)
        self.save("fp-nm", {"status": "unsupported", "error": "域名无法解析：dead.example",
                            "not_measured": True})
        row = self.isolated.row(
            "SELECT status, fail_count, last_fail_at, dead_since FROM stream_health WHERE fingerprint='fp-nm'"
        )
        self.assertEqual(row["status"], "unsupported")
        self.assertEqual(row["fail_count"], 0, "没测成不构成失败证据，计数应清零")
        self.assertEqual(row["last_fail_at"], "")
        self.assertEqual(row["dead_since"], "")

    def test_unknown_status_falls_back_to_error(self):
        self.save("fp-bogus", {"status": ""})
        self.save("fp-none", {"status": None})
        for fp in ("fp-bogus", "fp-none"):
            row = self.isolated.row("SELECT status, fail_count FROM stream_health WHERE fingerprint=?", (fp,))
            self.assertEqual(row["status"], "error")
            self.assertEqual(row["fail_count"], 1)

    def test_fail_count_is_capped(self):
        with self.isolated.write() as conn:
            conn.execute(
                "INSERT INTO stream_health(fingerprint, url, status, fail_count, checked_at) "
                "VALUES ('fp-cap', 'http://h/x.m3u8', 'error', ?, ?)",
                (speed.FAIL_COUNT_MAX, utc_now()),
            )
        self.save("fp-cap", {"status": "error"})
        row = self.isolated.row("SELECT fail_count FROM stream_health WHERE fingerprint='fp-cap'")
        self.assertEqual(row["fail_count"], speed.FAIL_COUNT_MAX, "fail_count 必须有上限")


class ProbeClassificationTests(IsolatedDbTestCase):
    """probe() 的四类归因：DNS 死 → unsupported（不是 blocked），内网 → blocked。"""

    def test_unresolvable_host_is_unsupported_not_blocked(self):
        async def run():
            with mock.patch.object(speed, "_resolve", return_value=()):
                return await speed.probe(mock.Mock(), "http://nxdomain.example/live.m3u8")

        result = __import__("asyncio").run(run())
        self.assertEqual(result["status"], "unsupported",
                         "域名解析不出来是「测不了」，不是「被安全拦截」")
        self.assertTrue(result["not_measured"])
        self.assertIn("域名无法解析", result["error"])
        self.assertNotIn("SSRF", result["error"], "文案不得再把它说成 SSRF 拦截")

    def test_private_address_is_still_blocked_with_accurate_wording(self):
        async def run():
            with mock.patch.object(speed, "_resolve", return_value=("192.168.1.10",)):
                return await speed.probe(mock.Mock(), "http://lan.example/live.m3u8")

        result = __import__("asyncio").run(run())
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["not_measured"])
        self.assertIn("内网/保留地址被策略拦截", result["error"])

    def test_non_http_scheme_is_unsupported_and_not_measured(self):
        result = __import__("asyncio").run(speed.probe(mock.Mock(), "rtmp://h.example/live"))
        self.assertEqual(result["status"], "unsupported")
        self.assertTrue(result["not_measured"])
        self.assertIn("非 HTTP 协议", result["error"])


class BlockedStatusBackfillTests(IsolatedDbTestCase):
    """v48 归因修正的存量补齐（`db.Database._migrate`）。

    早期版本把守卫拦截（内网/保留地址）写成了 `status='unsupported'`，只留下文案
    「内网/保留地址被策略拦截」。下游全部按 status 精确匹配，写错会改变结论：
    `/api/dead-channels` 会把这类频道当成「全线路判死」建议删源，`/api/channels/{key}`
    会标成 dead 而不是 blocked —— 与「blocked = 没测成 ≠ 线路坏」的口径相反。
    补齐必须：只改这一类行、幂等、不动 checked_at、不改变导出产物。
    """

    def test_reason_prefix_matches_the_backfill_literal(self):
        """迁移 SQL 里写的是字面量，文案改了会静默失配 —— 这里把两者钉在一起。"""
        self.assertEqual(speed.BlockedTarget.reason_prefix, "内网/保留地址被策略拦截")

    def seed_health(self, fingerprint: str, status: str, error: str) -> str:
        checked = utc_now()
        with self.isolated.write() as conn:
            conn.execute(
                "INSERT INTO stream_health(fingerprint, url, status, error, fail_count, "
                "last_fail_at, dead_since, checked_at, method) "
                "VALUES (?, ?, ?, ?, 0, '', '', ?, ?)",
                (fingerprint, f"http://{fingerprint}.example/x.m3u8", status, error, checked,
                 speed.METHOD_VERSION),
            )
        return checked

    def statuses(self) -> dict[str, str]:
        return {
            row["fingerprint"]: row["status"]
            for row in self.isolated.rows("SELECT fingerprint, status FROM stream_health")
        }

    def test_legacy_guard_blocked_rows_are_reclassified_to_blocked(self):
        self.seed_health(
            "fp-lan", "unsupported",
            f"{speed.BlockedTarget.reason_prefix}：lan.example -> 192.168.1.10",
        )
        self.isolated.initialize()  # 等价于下一次容器启动
        self.assertEqual(self.statuses()["fp-lan"], "blocked")
        # 幂等：再启动一次结果不变，且不会把已经正确的 blocked 行改坏
        self.isolated.initialize()
        self.assertEqual(self.statuses()["fp-lan"], "blocked")

    def test_dns_protocol_and_plain_errors_are_untouched(self):
        """只有守卫拦截那一类要改：DNS 死 / 非 HTTP 协议仍是 unsupported，真失败仍是 error。"""
        self.seed_health("fp-blocked", "unsupported",
                         f"{speed.BlockedTarget.reason_prefix}：lan.example -> 10.0.0.1")
        self.seed_health("fp-dns", "unsupported", "域名无法解析：nxdomain.example")
        self.seed_health("fp-rtmp", "unsupported", "非 HTTP 协议：rtmp")
        self.seed_health("fp-err", "error", "ConnectTimeout")
        self.seed_health("fp-ok", "ok", "")
        self.isolated.initialize()
        self.assertEqual(
            self.statuses(),
            {"fp-blocked": "blocked", "fp-dns": "unsupported", "fp-rtmp": "unsupported",
             "fp-err": "error", "fp-ok": "ok"},
        )

    def test_checked_at_is_preserved(self):
        """不动 checked_at：不额外消耗测速预算，也不把旧行立刻变成「待测」。"""
        checked = self.seed_health(
            "fp-lan", "unsupported",
            f"{speed.BlockedTarget.reason_prefix}：lan.example -> 192.168.1.10",
        )
        self.isolated.initialize()
        row = self.isolated.row(
            "SELECT checked_at, fail_count, dead_since FROM stream_health WHERE fingerprint='fp-lan'"
        )
        self.assertEqual(row["checked_at"], checked, "补齐不得重置复检时间戳")
        self.assertEqual(int(row["fail_count"]), 0, "「没测成」不得被算成失败")
        self.assertEqual(row["dead_since"], "", "「没测成」不得写连续失效起点")

    def test_export_line_set_is_unchanged_by_the_backfill(self):
        """导出只收 ok / 未测，blocked 与 unsupported 一视同仁 —— 补齐前后行集合逐条一致。"""
        from app import static_export

        self.add_entry("id:ch", "fp-lan")
        self.add_entry("id:ch", "fp-ok")
        self.seed_health("fp-lan", "unsupported",
                         f"{speed.BlockedTarget.reason_prefix}：lan.example -> 192.168.1.10")
        self.seed_health("fp-ok", "ok", "")
        with mock.patch.object(static_export, "db", self.isolated):
            before = sorted(row["url"] for row in static_export._fetch_lines(False, "", "", "", ""))
            self.isolated.initialize()
            after = sorted(row["url"] for row in static_export._fetch_lines(False, "", "", "", ""))
        self.assertEqual(before, after, "状态补齐不得改变导出的线路集合")
        self.assertEqual(len(before), 1, "blocked（原 unsupported）依旧不进导出")


class RequeueNewlyTrustedTests(IsolatedDbTestCase):
    """放行名单变更后的强制重排队（v48：blocked 窗口拉长到 14 天后的补偿机制）。"""

    def add_blocked(self, fingerprint: str, url: str) -> None:
        with self.isolated.write() as conn:
            conn.execute(
                "INSERT INTO stream_health(fingerprint, url, status, checked_at) "
                "VALUES (?, ?, 'blocked', ?)",
                (fingerprint, url, utc_now()),
            )

    def checked_at(self, fingerprint: str) -> str:
        return self.isolated.row(
            "SELECT checked_at FROM stream_health WHERE fingerprint=?", (fingerprint,)
        )["checked_at"]

    def test_trusted_host_is_requeued_and_others_are_left_alone(self):
        self.add_blocked("fp-lan", "http://192.0.2.10:50089/live.m3u")
        self.add_blocked("fp-stranger", "http://10.9.9.9/live.m3u")
        with mock.patch.object(speed, "_trusted_hosts", return_value={"192.0.2.10"}):
            n = speed.requeue_newly_trusted()
        self.assertEqual(n, 1)
        self.assertEqual(self.checked_at("fp-lan"), "", "名单内的 blocked 必须被重新排队")
        self.assertNotEqual(self.checked_at("fp-stranger"), "", "名单外的不动")

    def test_no_blocked_rows_is_a_pure_read(self):
        with mock.patch.object(speed, "db") as spy:
            spy.rows.return_value = []
            self.assertEqual(speed.requeue_newly_trusted(), 0)
            spy.write.assert_not_called()

    def test_non_blocked_rows_are_ignored(self):
        with self.isolated.write() as conn:
            conn.execute(
                "INSERT INTO stream_health(fingerprint, url, status, checked_at) "
                "VALUES ('fp-ok', 'http://192.0.2.10/x.m3u8', 'ok', ?)", (utc_now(),)
            )
        with mock.patch.object(speed, "_trusted_hosts", return_value={"192.0.2.10"}):
            self.assertEqual(speed.requeue_newly_trusted(), 0)
        self.assertNotEqual(self.checked_at("fp-ok"), "")


class PendingWindowTests(IsolatedDbTestCase):
    """复检窗口分流：blocked / unsupported 走长档，正常线路仍是 RETEST_OK_HOURS。"""

    def _pending_for(self, fingerprint: str) -> bool:
        sql, params = speed._pending_sql("all", "", False, "")
        rows = self.isolated.rows(
            f"SELECT fingerprint AS fp FROM ({sql}) WHERE fingerprint=?", [*params, fingerprint]
        )
        return bool(rows)

    def _entry(self, channel: str, fingerprint: str, group: str = "") -> None:
        self.add_entry(channel, fingerprint)

    def test_blocked_inside_long_window_is_not_queued_but_old_one_is(self):
        self._entry("ch-w", "fp-w-blocked-fresh")
        self.add_health("fp-w-blocked-fresh", "blocked", fail_count=0)
        self._entry("ch-w2", "fp-w-blocked-old")
        self.add_health("fp-w-blocked-old", "blocked", fail_count=0)
        # 只把后者改成"很久以前测过"
        long_ago = _days_ago(settings_blocked_days() + 1)
        with self.isolated.write() as conn:
            conn.execute("UPDATE stream_health SET checked_at=? WHERE fingerprint='fp-w-blocked-old'", (long_ago,))
        self.assertFalse(self._pending_for("fp-w-blocked-fresh"),
                         f"blocked 在 {settings_blocked_days()} 天窗口内不该被排进队列")
        self.assertTrue(self._pending_for("fp-w-blocked-old"), "超过窗口必须重新排队")

    def test_unsupported_uses_its_own_longer_window(self):
        self._entry("ch-u", "fp-u-unsup")
        self.add_health("fp-u-unsup", "unsupported", fail_count=0)
        # 15 天：超过 blocked 的 14 天档，但还没到 unsupported 的 30 天档
        with self.isolated.write() as conn:
            conn.execute("UPDATE stream_health SET checked_at=? WHERE fingerprint='fp-u-unsup'",
                         (_days_ago(15),))
        self.assertFalse(self._pending_for("fp-u-unsup"),
                         "unsupported 的窗口长于 blocked，15 天时不该入队")
        with self.isolated.write() as conn:
            conn.execute("UPDATE stream_health SET checked_at=? WHERE fingerprint='fp-u-unsup'",
                         (_days_ago(speed.UNSUPPORTED_RETEST_DAYS + 1),))
        self.assertTrue(self._pending_for("fp-u-unsup"))

    def test_ok_line_still_uses_retest_ok_hours(self):
        self._entry("ch-ok", "fp-ok-72")
        self.add_health("fp-ok-72", "ok", speed_kbps=500.0)
        self.assertFalse(self._pending_for("fp-ok-72"), "刚测过的 ok 线路不该入队")
        with self.isolated.write() as conn:
            conn.execute("UPDATE stream_health SET checked_at=? WHERE fingerprint='fp-ok-72'",
                         (_days_ago(4),))
        self.assertTrue(self._pending_for("fp-ok-72"), "超过 RETEST_OK_HOURS 必须入队")


def settings_blocked_days() -> int:
    return real_settings.retest_after_days


if __name__ == "__main__":
    unittest.main()
