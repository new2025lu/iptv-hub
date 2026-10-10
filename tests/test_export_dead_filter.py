"""v42 导出死线路过滤（EXPORT_HIDE_DEAD）的回归测试。

两条硬要求：
  * 过滤**绝不写库**（这里用 /tmp 隔离库，只调 `_fetch_lines` 这种纯查询）；
  * 某个频道若所有线路都被判死，**至少保留 1 条**（否则用户会看到"频道不见了"）。

同时覆盖"开关关闭时行为与改动前完全一致"。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import db as db_module
from app import static_export
from app.static_export import Line, _drop_dead_lines, _organize_rows, _line_order

# 隔离硬约束：只在 /tmp 下建库，先断言再动任何 SQL，绝不碰生产库 /data/iptv.sqlite3
_TMPDIR = tempfile.TemporaryDirectory(prefix="iptv-hub-export-", dir="/tmp")
_DB_PATH = Path(_TMPDIR.name) / "export.sqlite3"
assert str(_DB_PATH).startswith("/tmp/"), _DB_PATH
_ISOLATED = db_module.Database(_DB_PATH)
_ISOLATED.initialize()


def row(name: str, url: str, *, dead: bool = False, speed: float | None = 100.0,
        group: str = "央视", fingerprint: str | None = None, source_group: str = "") -> dict:
    return {
        "name": name,
        "group_title": group,
        "url": url,
        "referrer": "",
        "user_agent": "",
        "logo": "",
        "tvg_id": "",
        "stream_fingerprint": fingerprint or url,
        "source_group": source_group,
        "speed_kbps": speed,
        "latency_ms": 10,
        "ok_total": 0 if dead else 1,
        "fail_total": 5 if dead else 0,
        "is_dead": 1 if dead else 0,
    }


class DropDeadLinesTests(unittest.TestCase):
    def test_switch_off_keeps_everything(self):
        lines = [Line(100.0, 10, 5, 0, "u1", "", "", "", "", "f1", True),
                 Line(200.0, 10, 0, 1, "u2", "", "", "", "", "f2", False)]
        with mock.patch.object(static_export, "_hide_dead_enabled", lambda: False):
            self.assertEqual(_drop_dead_lines(lines), lines)

    def test_mixed_channel_drops_dead_and_counts(self):
        lines = [Line(200.0, 10, 0, 1, "live", "", "", "", "", "f1", False),
                 Line(100.0, 10, 5, 0, "dead", "", "", "", "", "f2", True)]
        stats: dict = {}
        with mock.patch.object(static_export, "_hide_dead_enabled", lambda: True):
            kept = _drop_dead_lines(lines, stats)
        self.assertEqual([line.url for line in kept], ["live"])
        self.assertEqual(stats["filtered_dead"], 1)
        self.assertNotIn("kept_alive_channels", stats)

    def test_all_dead_channel_keeps_best_line(self):
        """全死频道保底 1 条，且必须是排序最优的那条（速度快的在前）。"""
        lines = [Line(50.0, 10, 5, 0, "slow-dead", "", "", "", "", "f1", True),
                 Line(900.0, 10, 5, 0, "fast-dead", "", "", "", "", "f2", True)]
        lines.sort(key=_line_order)
        stats: dict = {}
        with mock.patch.object(static_export, "_hide_dead_enabled", lambda: True):
            kept = _drop_dead_lines(lines, stats)
        self.assertEqual([line.url for line in kept], ["fast-dead"])
        self.assertEqual(stats["kept_alive_channels"], 1)
        self.assertNotIn("filtered_dead", stats)

    def test_empty_channel_is_not_counted(self):
        with mock.patch.object(static_export, "_hide_dead_enabled", lambda: True):
            self.assertEqual(_drop_dead_lines([], {}), [])


class OrganizeIntegrationTests(unittest.TestCase):
    """走完整组织路径（无模版兜底），验证"全死频道不会消失"与开关关闭时的等价性。"""

    def _organize(self, rows, enabled):
        with mock.patch.object(static_export, "_hide_dead_enabled", lambda: enabled), \
             mock.patch.object(static_export.template, "snapshot", lambda: None):
            stats: dict = {}
            groups = _organize_rows(rows, "multi", stats)
            return groups, stats

    def test_channel_with_only_dead_lines_survives(self):
        rows = [
            row("全死台", "http://a/1", dead=True, group="地方台"),
            row("正常台", "http://b/1", dead=False, group="地方台"),
        ]
        groups, stats = self._organize(rows, True)
        names = [entry.name for _, entries in groups for entry in entries]
        self.assertIn("全死台", names, "全死频道必须保底留 1 条，不能凭空消失")
        self.assertIn("正常台", names)
        self.assertEqual(stats["kept_alive_channels"], 1)

    def test_dead_lines_of_a_live_channel_are_hidden(self):
        rows = [
            row("混合台", "http://a/1", dead=False, speed=500),
            row("混合台", "http://a/2", dead=True, speed=900),
        ]
        groups, stats = self._organize(rows, True)
        urls = [entry.url for _, entries in groups for entry in entries]
        self.assertEqual(urls, ["http://a/1"], "判死线路不该进订阅（哪怕它测速更快）")
        self.assertEqual(stats["filtered_dead"], 1)

    def test_switch_off_is_byte_for_byte_compatible(self):
        """关掉开关 = 改动前行为：判死线路照旧参与输出与排序。"""
        rows = [
            row("混合台", "http://a/1", dead=False, speed=500),
            row("混合台", "http://a/2", dead=True, speed=900),
        ]
        groups, stats = self._organize(rows, False)
        urls = [entry.url for _, entries in groups for entry in entries]
        self.assertEqual(urls, ["http://a/2", "http://a/1"], "关闭时按原排序输出全部线路")
        self.assertEqual(stats, {})


class FetchLinesSqlTests(unittest.TestCase):
    """`is_dead` 列来自 SQL：验证阈值口径，并确认状态级判死行依旧不进组织器。"""

    def setUp(self):
        self._patch = mock.patch.object(static_export, "db", _ISOLATED)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        with _ISOLATED.write() as conn:
            conn.execute("DELETE FROM source_entries")
            conn.execute("DELETE FROM stream_health")
            conn.execute(
                "INSERT INTO sources(slug, name, url, format, priority, source_group) "
                "VALUES ('s1', '源一', 'http://s1.example/x.m3u', 'auto', 1, '') "
                "ON CONFLICT(slug) DO NOTHING"
            )

    def _add_entry(self, key: str, url: str, fingerprint: str, name: str = "测试台") -> None:
        with _ISOLATED.write() as conn:
            conn.execute(
                "INSERT INTO source_entries(source_slug, entry_key, stream_fingerprint, channel_key, "
                "name, normalized_name, group_title, url, region, active, first_seen_at, last_seen_at) "
                "VALUES ('s1', ?, ?, ?, ?, ?, '央视', ?, 'domestic', 1, '2026-10-05T00:00:00+00:00', "
                "'2026-10-05T00:00:00+00:00')",
                (key, fingerprint, key, name, name, url),
            )

    def _add_health(self, fingerprint: str, status: str, fail_count: int) -> None:
        with _ISOLATED.write() as conn:
            conn.execute(
                "INSERT INTO stream_health(fingerprint, url, status, fail_count, checked_at) "
                "VALUES (?, ?, ?, ?, '2026-10-05T00:00:00+00:00')",
                (fingerprint, f"http://{fingerprint}/x.m3u8", status, fail_count),
            )

    def _line(self, fingerprint: str):
        rows = static_export._fetch_lines(False, "", "domestic")
        for r in rows:
            if r["stream_fingerprint"] == fingerprint:
                return r
        return None

    def test_fail_count_at_threshold_is_flagged_dead(self):
        self._add_entry("e1", "http://a/1", "fp-ok")
        self._add_health("fp-ok", "ok", 0)
        self._add_entry("e2", "http://a/2", "fp-warn")
        self._add_health("fp-warn", "ok", settings_fail_count() - 1)
        self._add_entry("e3", "http://a/3", "fp-dead")
        self._add_health("fp-dead", "ok", settings_fail_count())

        self.assertEqual(self._line("fp-ok")["is_dead"], 0)
        self.assertEqual(self._line("fp-warn")["is_dead"], 0, "差一次不算死")
        self.assertEqual(self._line("fp-dead")["is_dead"], 1, "达到阈值即判死")

    def test_status_dead_rows_stay_out_of_the_query(self):
        self._add_entry("e4", "http://a/4", "fp-err")
        self._add_health("fp-err", "error", 1)
        self.assertIsNone(self._line("fp-err"), "状态级判死（error）不进组织器，保持既有行为")


def settings_fail_count() -> int:
    from app.config import settings
    return settings.export_dead_fail_count


if __name__ == "__main__":
    unittest.main()
