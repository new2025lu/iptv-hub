"""v58：被**主动停用**的数据源不该继续触发「源级失效告警」（停用即静默）。

背景（线上实测，2026-10-08）：`/api/stats` 的 `source_alerts` 原来只按
「`status='error'` 或 距上次成功超过 `SOURCE_STALE_DAYS` 天」算，**不看 `enabled`**。
于是把一个源（上游令牌被对方停用）人工停用后，首页仍然红字报「1 个数据源异常」——
观感就像"停用没生效"，而停用本身就是运维已经做过的处置。

v58 给两条判据都加 `enabled=1`：
  * `source_alerts` 只谈**启用中**的源；
  * `pending_first_sync`（待首次同步的中性提示）取同口径 —— 否则
    「真异常数 + 待首次同步数」两个数字会互相矛盾。

本文件只碰 `/tmp` 下的独立库：不联网、不读也不写生产库。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import db as db_module
from app import speed as speed_module
from app.config import settings as real_settings

try:  # 宿主机缺 FastAPI 时跳过（容器内会跑）
    from app import main as main_module
except ModuleNotFoundError:  # pragma: no cover - 宿主机环境
    main_module = None

# 隔离硬约束：先在 /tmp 下建库并断言路径，再执行任何 SQL
_TMPDIR = tempfile.TemporaryDirectory(prefix="iptv-hub-alerts-", dir="/tmp")
_DB_PATH = Path(_TMPDIR.name) / "alerts.sqlite3"
assert str(_DB_PATH).startswith("/tmp/"), _DB_PATH
_ISOLATED = db_module.Database(_DB_PATH)
_ISOLATED.initialize()


def _insert(slug: str, *, enabled: int, status: str, last_success_at: str | None,
            error_message: str = "") -> None:
    with _ISOLATED.write() as conn:
        conn.execute(
            "INSERT INTO sources(slug, name, url, format, priority, status, "
            "last_success_at, error_message, enabled) VALUES (?,?,?,?,?,?,?,?,?)",
            (slug, slug, f"http://example.test/{slug}.m3u", "m3u", 50, status,
             last_success_at, error_message, enabled),
        )


_STALE = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
_FRESH = datetime.now(timezone.utc).isoformat(timespec="seconds")


def _seed():
    with _ISOLATED.write() as conn:
        conn.execute("DELETE FROM sources")
    # 启用中且出错 → 必须告警
    _insert("err-on", enabled=1, status="error", last_success_at=_FRESH, error_message="HTTP 403")
    # 已停用且出错 → v58 起**不再**告警
    _insert("err-off", enabled=0, status="error", last_success_at=_STALE, error_message="HTTP 403")
    # 启用中但 30 天没成功 → 必须告警
    _insert("stale-on", enabled=1, status="ok", last_success_at=_STALE)
    # 已停用且 30 天没成功 → 不告警
    _insert("stale-off", enabled=0, status="ok", last_success_at=_STALE)
    # 启用中、从未成功（status='never'）→ 计入 pending_first_sync，不算异常
    _insert("never-on", enabled=1, status="never", last_success_at=None)
    # 已停用、从未成功 → v58 起连"待首次同步"也不算
    _insert("never-off", enabled=0, status="never", last_success_at=None)
    # 启用中且正常 → 既不告警也不待同步
    _insert("ok-on", enabled=1, status="ok", last_success_at=_FRESH)


@unittest.skipIf(main_module is None, "宿主机没有 FastAPI：这一组在容器内运行")
class SourceAlertsEnabledTests(unittest.TestCase):
    def setUp(self):
        _seed()
        self._patches = [
            mock.patch.object(main_module, "db", _ISOLATED),
            # /api/stats 里的「待测速队列」由 speed 自己算，它用的是 speed 模块里的 db
            mock.patch.object(speed_module, "db", _ISOLATED),
            mock.patch.object(main_module, "settings", real_settings),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _stats(self) -> dict:
        return main_module._compute_stats()

    def test_disabled_sources_are_silent(self):
        stats = self._stats()
        alerts = stats["source_alerts"]
        self.assertEqual(
            sorted(item["slug"] for item in alerts["items"]),
            ["err-on", "stale-on"],
            "告警必须只包含**启用中**的问题源：停用的 err-off/stale-off 不该出现",
        )
        self.assertEqual(alerts["count"], 2)

    def test_pending_first_sync_ignores_disabled(self):
        stats = self._stats()
        self.assertEqual(
            stats["source_alerts"]["pending_first_sync"], 1,
            "启用中的 never 源只有 never-on；停用的 never-off 不该算进来",
        )

    def test_enabled_healthy_source_is_not_alerted(self):
        stats = self._stats()
        slugs = {item["slug"] for item in stats["source_alerts"]["items"]}
        self.assertNotIn("ok-on", slugs)


if __name__ == "__main__":
    unittest.main()
