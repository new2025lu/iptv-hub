"""v42 审计③：批量入库收尾的「全量同步」被去重丢弃后的补偿（SyncManager._full_dirty）。

场景：批量入库的新源在 `add_source` 里各自排了单源同步，队列上限 3 → 第 4 条起被拒；
收尾的 `queue("discover-adopt")` 又可能因为「全量正在跑」被 `queue()` 去重丢弃，
而正在跑的那轮全量早在 `_run_inner` 开头就读完了源列表 → 新源停在 status='never'，
最长要等次日定时同步。

这里不碰数据库、不建连接池：把 `SyncManager.run` 换成一个只记录调用的替身，
只验证"队列语义"本身。
"""

from __future__ import annotations

import asyncio
import unittest

from app import sync


def _bare_manager() -> sync.SyncManager:
    """跳过 __init__（它会真的创建 httpx.AsyncClient 连接池），只装配队列状态。"""
    manager = sync.SyncManager.__new__(sync.SyncManager)
    manager._lock = asyncio.Lock()
    manager._queue_guard = asyncio.Lock()
    manager.current_task = None
    manager.current_run_id = None
    manager.current_source_slug = ""
    manager._pending = []
    manager._closing = False
    manager._full_dirty = False
    return manager


class FullDirtyTests(unittest.TestCase):
    def test_full_request_during_running_full_round_is_requeued(self):
        async def scenario():
            manager = _bare_manager()
            started: list[tuple[str, str]] = []

            async def fake_run(run_id, trigger, source_slug=""):
                started.append((trigger, source_slug))
                await asyncio.sleep(0.05)

            manager.run = fake_run

            run_id, queued = await manager.queue("manual")
            self.assertTrue(queued)
            await asyncio.sleep(0.01)          # 让 _runner 起来：current_run_id 已置位
            self.assertEqual(manager.current_source_slug, "", "前置条件：正在跑的是全量轮")

            # 全量正在跑 → 这次请求被去重丢弃，但必须留下补偿标志
            run_id2, queued2 = await manager.queue("discover-adopt")
            self.assertEqual(run_id2, run_id)
            self.assertFalse(queued2)
            self.assertTrue(manager._full_dirty)

            await asyncio.sleep(0.25)          # 等第一轮跑完 + 自动补排
            return manager, started

        manager, started = asyncio.run(scenario())
        self.assertEqual([t for t, _ in started], ["manual", "discover-adopt+dirty"],
                         "被去重丢弃的全量请求必须在队列跑空后补排一轮")
        self.assertFalse(manager._full_dirty, "标志消费一次后必须清掉，避免无限补排")

    def test_full_request_when_queue_is_full_marks_dirty(self):
        """单源队列满（MAX_PENDING_SYNCS=3）把全量请求顶掉时同样要补排。"""

        async def scenario():
            manager = _bare_manager()
            manager._pending = [
                ("id1", "add-source", "s1"),
                ("id2", "add-source", "s2"),
                ("id3", "add-source", "s3"),
            ]
            manager.current_run_id = "running-single"
            manager.current_source_slug = "s0"
            return manager, await manager.queue("discover-adopt")

        manager, (run_id, queued) = asyncio.run(scenario())
        self.assertEqual(run_id, "")
        self.assertFalse(queued)
        self.assertTrue(manager._full_dirty)

    def test_single_source_request_rejected_by_full_queue_does_not_mark_dirty(self):
        """单源请求被队列满拒掉只是"这一条这次没排上"，不该因此多跑一轮全量
        （收尾的全量请求会兜住它）。"""

        async def scenario():
            manager = _bare_manager()
            manager._pending = [
                ("id1", "add-source", "s1"),
                ("id2", "add-source", "s2"),
                ("id3", "add-source", "s3"),
            ]
            manager.current_run_id = "running-single"
            manager.current_source_slug = "s0"
            return manager, await manager.queue("add-source", "s9")

        manager, (run_id, queued) = asyncio.run(scenario())
        self.assertFalse(queued)
        self.assertFalse(manager._full_dirty)

    def test_pending_full_round_does_not_need_compensation(self):
        """已经排队、还没开跑的全量轮会在开跑时读到最新源列表 → 不该再补一轮。"""

        async def scenario():
            manager = _bare_manager()
            manager._pending = [("id1", "manual", "")]
            manager.current_run_id = "running-single"
            manager.current_source_slug = "s0"
            return manager, await manager.queue("discover-adopt")

        manager, (run_id, queued) = asyncio.run(scenario())
        self.assertEqual(run_id, "id1")
        self.assertFalse(queued)
        self.assertFalse(manager._full_dirty)


if __name__ == "__main__":
    unittest.main()
