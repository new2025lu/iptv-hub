"""v59：测速「全灭护栏」的回滚必须把**整条判定**作废，而不是只减计数。

背景（生产实测，2026-10-08）：`_run_inner` 在某轮 ok 比例 <20% 时会调用
`_rollback_round_failures()`，意图是让这一轮等价于"没有判定过"。但 v42 的实现只做了
`fail_count = MAX(0, fail_count - 1)`，**没有回退同一次 `_save_batch` 写进去的
`status='error'` / `checked_at=now` / `dead_since=now`**。于是：

  * 导出只放行 `h.status IS NULL OR h.status='ok'` → 线路**立刻从订阅里消失**；
  * `_pending_sql` 的失效档要求 `fail_count > 0`、正常档要求 `checked_at < now-72h`
    → 被回退成 `fail_count=0` + 全新 `checked_at` **72 小时不重测**。

生产库当时有 328 条 active 条目处于 `status='error' AND fail_count=0`，`checked_at`
集中在 10-05 的两次低可用率轮次 —— 只有"只回退计数"能造成。

本文件钉四件事（隔离库，不联网、不碰生产库）：

1. 本轮**首次失败**（旧 `fail_count=1`）→ 计数归零、`status='ok'`、`error`/`dead_since`/
   `checked_at` 全部清空；
2. 已经失败过一轮以上（旧 `fail_count>=2`）→ 只减计数，判定与时间戳保持不变；
3. `unsupported`（`not_measured`，计数本就为 0）不会被误改成 `ok`；
4. 分批阈值（每批 400）之后的行为一致。
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace as dataclass_replace
from pathlib import Path
from unittest import mock

from app import db as db_module
from app import speed as speed_module

# 隔离硬约束：先在 /tmp 下建库并断言路径，再执行任何 SQL
_TMPDIR = tempfile.TemporaryDirectory(prefix="iptv-hub-rollback-", dir="/tmp")
_DB_PATH = Path(_TMPDIR.name) / "rollback.sqlite3"
assert str(_DB_PATH).startswith("/tmp/"), _DB_PATH
assert "/data/iptv.sqlite3" not in str(_DB_PATH), _DB_PATH
_ISOLATED = db_module.Database(_DB_PATH)
_ISOLATED.initialize()

TS = "2026-10-05T15:39:44+00:00"


def _reset_health(rows: list[tuple[str, str, int, str]]) -> None:
    """重建 stream_health：(fingerprint, status, fail_count, error)。

    `last_fail_at` 一律写成 TS：回滚**刻意不回滚**它（本轮确实探测失败过），
    用它来验证"只回退判定、不动失败时间戳"。
    """
    with _ISOLATED.write() as conn:
        conn.execute("DELETE FROM stream_health")
        conn.executemany(
            "INSERT INTO stream_health(fingerprint, url, status, error, fail_count, "
            "last_fail_at, dead_since, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (fp, f"http://example.test/{fp}.m3u8", status, error, fail_count, TS, TS, TS)
                for fp, status, fail_count, error in rows
            ],
        )


def _health(fingerprint: str) -> dict:
    row = _ISOLATED.row("SELECT * FROM stream_health WHERE fingerprint=?", (fingerprint,))
    assert row is not None, fingerprint
    return row


class RollbackRoundFailuresTests(unittest.TestCase):
    def setUp(self):
        # `_rollback_round_failures` 用的是 `speed` 模块里的 db 引用（from .db import db
        # 是值绑定），只 patch app.db 不够 —— 必须 patch speed.db。
        patch = mock.patch.object(speed_module, "db", _ISOLATED)
        patch.start()
        self.addCleanup(patch.stop)

    def test_first_failure_is_fully_undone(self):
        _reset_health([("fp-first", "error", 1, "样本不足(49B/5s)")])
        rolled = speed_module._rollback_round_failures(["fp-first"])
        self.assertEqual(rolled, 1)
        row = _health("fp-first")
        self.assertEqual(row["fail_count"], 0, "首次失败必须回退成 0")
        self.assertEqual(row["status"], "ok", "不能留下 status='error' —— 否则线路立刻从订阅消失")
        self.assertEqual(row["error"], "", "判定作废后不该留着失败文案")
        self.assertEqual(row["dead_since"], "", "连续失效起点必须一起清空")
        self.assertEqual(row["checked_at"], "", "checked_at 清空后下一轮会立即重测（而不是等 72h）")
        # 与 v42 的核心回归点：last_fail_at 故意不回滚（本轮确实探测失败过）。
        self.assertEqual(row["last_fail_at"], TS)

    def test_second_failure_keeps_previous_verdict(self):
        _reset_health([("fp-second", "error", 2, "HTTP 502")])
        speed_module._rollback_round_failures(["fp-second"])
        row = _health("fp-second")
        self.assertEqual(row["fail_count"], 1, "只回退本轮那一次")
        self.assertEqual(row["status"], "error", "之前就失败过，判定必须保留")
        self.assertEqual(row["error"], "HTTP 502")
        self.assertEqual(row["dead_since"], TS)
        self.assertEqual(row["checked_at"], TS)

    def test_unsupported_is_not_turned_ok(self):
        # not_measured（unsupported/blocked）在 _save_batch 里走成功一侧、计数为 0，
        # 回滚不该把它误判成"本轮首次失败"而恢复成 ok。
        _reset_health([("fp-unsupported", "unsupported", 0, "非 HTTP 协议：rtmp")])
        speed_module._rollback_round_failures(["fp-unsupported"])
        row = _health("fp-unsupported")
        self.assertEqual(row["status"], "unsupported")
        self.assertEqual(row["fail_count"], 0)
        self.assertEqual(row["error"], "非 HTTP 协议：rtmp")
        self.assertEqual(row["checked_at"], TS)

    def test_missing_fingerprints_and_batching(self):
        rows = [("fp-first", "error", 1, "x")]
        rows += [(f"fp-ok-{i}", "ok", 0, "") for i in range(5)]
        rows += [(f"fp-many-{i}", "error", 3, "y") for i in range(450)]
        _reset_health(rows)
        rolled = speed_module._rollback_round_failures([r[0] for r in rows] + ["fp-not-here"])
        # 匹配行 = fail_count=1 的 1 条 + fail_count=3 的 450 条（ok/unsupported 不计）
        self.assertEqual(rolled, 451, "分批（每批 400）不改变匹配行数")
        self.assertEqual(_health("fp-first")["status"], "ok")
        for i in range(450):
            row = _health(f"fp-many-{i}")
            self.assertEqual(row["fail_count"], 2, "第二次以上失败只减计数")
            self.assertEqual(row["status"], "error")
            self.assertEqual(row["checked_at"], TS)

    def test_empty_input_is_noop(self):
        _reset_health([("fp-first", "error", 1, "x")])
        self.assertEqual(speed_module._rollback_round_failures([]), 0)
        self.assertEqual(_health("fp-first")["fail_count"], 1)


if __name__ == "__main__":
    unittest.main()
