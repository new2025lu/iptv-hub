"""v59：`revive_expired()` 只复活**启用中**的数据源（`enabled=1`）。

背景（生产实测，2026-10-08）：v58 让 `/api/stats` 的源级告警与「待首次同步」只看
`enabled=1`，但自动复活这条路径没跟上 —— 停用源里因失效被剔除
（`active=0`）的条目，会在**每轮全量同步后的自动维护**（`sync._auto_maintenance` →
`revive_expired`）里被重新激活。后果：

  * 停用源不会再测速，复活只是把一批状态仍是 `error` 的条目塞回
    `/api/channels`、`/api/dead-channels`、`/api/groups` 与 stats 的数字里（不进订阅）；
  * 正好抵消用户按 README 指引手工做的「把死线置 `active=0`」；
  * `revive_count` 被白白 +1，污染季度清理（`purge_revived_streams`）的判据。
  实测：被停用的那个源有 89 个指纹满足复活条件，按真实配置会在 10-19 ~ 10-21 起被复活。

同一指纹可能同时挂在启用源与停用源上，所以 SELECT（挑指纹）与 UPDATE（激活条目）
**两处**都必须带 `enabled` 条件。本文件把这三件事钉住（隔离库，不联网、不碰生产库）：

1. 启用源条目复活、停用源条目不动；
2. 指纹被两侧共享时，只激活启用源那一侧；
3. KEEP_REGION 分支（`region=?`）同样不碰停用源。
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace as dataclass_replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import db as db_module
from app import speed as speed_module
from app.config import settings as real_settings

# 隔离硬约束：先在 /tmp 下建库并断言路径，再执行任何 SQL
_TMPDIR = tempfile.TemporaryDirectory(prefix="iptv-hub-revive-", dir="/tmp")
_DB_PATH = Path(_TMPDIR.name) / "revive.sqlite3"
assert str(_DB_PATH).startswith("/tmp/"), _DB_PATH
assert "/data/iptv.sqlite3" not in str(_DB_PATH), _DB_PATH
_ISOLATED = db_module.Database(_DB_PATH)
_ISOLATED.initialize()

# 3 天前：`retest_after_days=1` 时一定早于复活窗口
OLD_FAIL = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
TS = "2026-10-05T02:00:00+00:00"
FAIL_COUNT = real_settings.prune_max_fail + 1     # > 阈值 = "当初确实是被测速剔除的"


def _reset(
    sources: list[tuple[str, int]],
    entries: list[tuple[str, str, str, str, int]],
    health: list[tuple[str, str, int]],
) -> None:
    """重建三张表。

    sources : (slug, enabled)
    entries : (source_slug, entry_key, fingerprint, region, active)
    health  : (fingerprint, status, fail_count)
    """
    with _ISOLATED.write() as conn:
        conn.execute("DELETE FROM stream_health")
        conn.execute("DELETE FROM source_entries")
        conn.execute("DELETE FROM sources")
        conn.executemany(
            "INSERT INTO sources(slug, name, url, format, priority, enabled) VALUES (?,?,?,?,?,?)",
            [(slug, slug, f"http://{slug}.example/x.m3u", "m3u", 50, enabled)
             for slug, enabled in sources],
        )
        conn.executemany(
            "INSERT INTO source_entries(source_slug, entry_key, stream_fingerprint, channel_key, "
            "name, normalized_name, group_title, url, region, active, first_seen_at, last_seen_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(slug, f"{slug}:{key}", fp, "ck-1", "频道一", "频道一", "测试",
              f"http://example.test/{key}.m3u8", region, active, TS, TS)
             for slug, key, fp, region, active in entries],
        )
        conn.executemany(
            "INSERT INTO stream_health(fingerprint, url, status, fail_count, last_fail_at, checked_at) "
            "VALUES (?,?,?,?,?,?)",
            [(fp, f"http://example.test/{fp}.m3u8", status, fail_count, OLD_FAIL, OLD_FAIL)
             for fp, status, fail_count in health],
        )


def _active(slug: str) -> int:
    return _ISOLATED.row(
        "SELECT COUNT(*) AS n FROM source_entries WHERE source_slug=? AND active=1", (slug,)
    )["n"]


def _health(fingerprint: str) -> dict:
    return _ISOLATED.row("SELECT * FROM stream_health WHERE fingerprint=?", (fingerprint,))


class ReviveEnabledOnlyTests(unittest.TestCase):
    def _patch(self, **overrides):
        overrides.setdefault("keep_region", "")
        settings = dataclass_replace(
            real_settings, retest_after_days=1, **overrides
        )
        for patch in (
            mock.patch.object(speed_module, "db", _ISOLATED),
            mock.patch.object(speed_module, "settings", settings),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_disabled_source_is_not_revived(self):
        self._patch()
        _reset(
            sources=[("on-src", 1), ("off-src", 0)],
            entries=[
                ("on-src", "e-on", "fp-on", "domestic", 0),
                ("off-src", "e-off", "fp-off", "domestic", 0),
            ],
            health=[("fp-on", "error", FAIL_COUNT), ("fp-off", "error", FAIL_COUNT)],
        )
        revived = speed_module.revive_expired()
        self.assertEqual(revived, 1, "只该复活启用源那一条")
        self.assertEqual(_active("on-src"), 1)
        self.assertEqual(_active("off-src"), 0, "停用源的条目不能被复活")
        on_health = _health("fp-on")
        self.assertEqual(on_health["fail_count"], 0)
        self.assertEqual(on_health["revive_count"], 1)
        off_health = _health("fp-off")
        self.assertEqual(off_health["fail_count"], FAIL_COUNT, "没复活就不该清零/累计 revive_count")
        self.assertEqual(off_health["revive_count"], 0)

    def test_shared_fingerprint_only_activates_enabled_entry(self):
        self._patch()
        _reset(
            sources=[("on-src", 1), ("off-src", 0)],
            entries=[
                ("on-src", "e-on", "fp-shared", "domestic", 0),
                ("off-src", "e-off", "fp-shared", "domestic", 0),
            ],
            health=[("fp-shared", "error", FAIL_COUNT)],
        )
        revived = speed_module.revive_expired()
        self.assertEqual(revived, 1, "指纹只该激活启用源那一行")
        self.assertEqual(_active("on-src"), 1)
        self.assertEqual(_active("off-src"), 0, "同指纹的停用源条目必须保持 active=0")

    def test_region_branch_also_skips_disabled(self):
        self._patch(keep_region="domestic")
        _reset(
            sources=[("on-src", 1), ("off-src", 0)],
            entries=[
                ("on-src", "e-on", "fp-shared", "domestic", 0),
                ("off-src", "e-off", "fp-shared", "domestic", 0),
            ],
            health=[("fp-shared", "error", FAIL_COUNT)],
        )
        revived = speed_module.revive_expired()
        self.assertEqual(revived, 1)
        self.assertEqual(_active("on-src"), 1)
        self.assertEqual(_active("off-src"), 0)

    def test_no_enabled_candidate_returns_zero(self):
        self._patch()
        _reset(
            sources=[("off-a", 0), ("off-b", 0)],
            entries=[
                ("off-a", "e-a", "fp-a", "domestic", 0),
                ("off-b", "e-b", "fp-b", "domestic", 0),
            ],
            health=[("fp-a", "error", FAIL_COUNT), ("fp-b", "error", FAIL_COUNT)],
        )
        self.assertEqual(speed_module.revive_expired(), 0)
        self.assertEqual(_active("off-a"), 0)
        self.assertEqual(_active("off-b"), 0)


if __name__ == "__main__":
    unittest.main()
