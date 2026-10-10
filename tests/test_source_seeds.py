"""内置固定源（SOURCE_SEEDS / SOURCE_SEED_GROUPS）的隔离库测试。

**隔离硬约束**：只在 `/tmp` 下新建空库（先 assert 路径再动任何 SQL），
绝不碰生产库 `/data/iptv.sqlite3`。

覆盖点：
  * 新 seed `ysp-live` 的声明值：URL、format、priority、分组；
  * priority 必须是全部种子里最优（`ysp-live=2`），且所有 seed 的 slug / URL 互不重复
    ——URL 重复不是风格问题：`sources.url` 上有 UNIQUE，重复会让 `initialize()` 抛
    IntegrityError，而它在 lifespan 里跑，后果是容器启动崩溃循环（`_seed_sources`
    的预检就是为这事加的兜底）；
  * 固定源语义（重启不丢）：同一路径再 `initialize()` 一次后行仍在、name/format/priority
    被重新声明回种子值、`source_group` 回到 `本地源`（被人为清空也能自愈），
    而 status / item_count 这类「运行期状态」不被种子覆盖；
  * URL 去重：该地址已被**别的 slug**（手动源）占用时，seed 跳过且库里始终只有一行该 URL，
    也不会把内置源的分组写到占用它的那个源上。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import db as db_module

SEED_BY_SLUG = {seed[0]: seed for seed in db_module.SOURCE_SEEDS}
YSP_SLUG = "ysp-live"
YSP_URL = "http://192.0.2.130:8767/all.m3u"
YSP_GROUP = "本地源"


@unittest.skipUnless(
    db_module.SOURCE_SEEDS,
    "干净基线（2026-10-10）：内置种子已清空，本组用例不适用；恢复内置源后自动重新生效",
)
class SeedConstantsTest(unittest.TestCase):
    """SOURCE_SEEDS / SOURCE_SEED_GROUPS 的声明值（不建库，纯常量断言）。"""

    def test_ysp_live_seed_declared_fields(self):
        seed = SEED_BY_SLUG.get(YSP_SLUG)
        self.assertIsNotNone(seed, f"SOURCE_SEEDS 里必须有 {YSP_SLUG}")
        slug, name, url, fmt, priority = seed
        self.assertEqual(slug, YSP_SLUG)
        self.assertEqual(url, YSP_URL)
        self.assertEqual(fmt, "m3u")
        self.assertEqual(priority, 2)
        self.assertIn("央视频", name)
        self.assertEqual(db_module.SOURCE_SEED_GROUPS.get(YSP_SLUG), YSP_GROUP)

    def test_ysp_live_priority_is_best_among_seeds(self):
        """priority 数值越小越优先；ysp-live(=2) 必须是全部种子里的最优。"""
        mine = SEED_BY_SLUG[YSP_SLUG][4]
        self.assertEqual(mine, min(seed[4] for seed in db_module.SOURCE_SEEDS))
        others = [seed[4] for seed in db_module.SOURCE_SEEDS if seed[0] != YSP_SLUG]
        self.assertTrue(others, "除 ysp-live 之外应当还有其它种子源")
        self.assertLess(mine, min(others))

    def test_ysptp_cctv_seed_removed(self):
        """v54：8766 内置固定源已下线（源码种子 + ysptp 容器一并移除），不得被种回。

        回归护栏：这条一旦失败，说明有人把 `ysptp-cctv` 加回了 SOURCE_SEEDS ——
        每次启动都会重新种入一个上游已断（`app/start HTTP 400`）的源。
        """
        self.assertNotIn("ysptp-cctv", SEED_BY_SLUG)
        self.assertNotIn("ysptp-cctv", db_module.SOURCE_SEED_GROUPS)
        self.assertNotIn(
            "http://192.0.2.10:8766/",
            [seed[2] for seed in db_module.SOURCE_SEEDS],
        )

    def test_seed_slugs_and_urls_unique(self):
        slugs = [seed[0] for seed in db_module.SOURCE_SEEDS]
        urls = [seed[2] for seed in db_module.SOURCE_SEEDS]
        self.assertEqual(len(set(slugs)), len(slugs), f"seed slug 有重复：{slugs}")
        self.assertEqual(len(set(urls)), len(urls), f"seed URL 有重复：{urls}")

    def test_seed_groups_point_at_real_seeds(self):
        """分组映射的键必须都是真实 seed：写错 slug 只会静默空转，没有报错。"""
        for slug in db_module.SOURCE_SEED_GROUPS:
            self.assertIn(slug, SEED_BY_SLUG)


@unittest.skipUnless(
    db_module.SOURCE_SEEDS,
    "干净基线（2026-10-10）：内置种子已清空，本组用例不适用；恢复内置源后自动重新生效",
)
class SeedDatabaseTest(unittest.TestCase):
    """真建库跑 initialize()：种子写入 + 分组补写 + 重启保持。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="iptv-hub-seed-", dir="/tmp")
        self.db_path = Path(self._tmpdir.name) / "seed.sqlite3"
        # 硬约束：实验库必须在 /tmp 下。断言放在最前面，失败就根本不会执行任何写操作。
        assert str(self.db_path).startswith("/tmp/"), f"隔离库必须在 /tmp 下：{self.db_path}"
        self.addCleanup(self._tmpdir.cleanup)

    def _initialize(self) -> db_module.Database:
        isolated = db_module.Database(self.db_path)
        isolated.initialize()
        return isolated

    @staticmethod
    def _row(isolated: db_module.Database, slug: str) -> dict | None:
        return isolated.row("SELECT * FROM sources WHERE slug=?", (slug,))

    def test_initialize_creates_builtin_source_with_group(self):
        isolated = self._initialize()
        row = self._row(isolated, YSP_SLUG)
        self.assertIsNotNone(row, "initialize() 必须写入内置源 ysp-live")
        self.assertEqual(row["url"], YSP_URL)
        self.assertEqual(row["format"], "m3u")
        self.assertEqual(row["priority"], 2)
        self.assertEqual(row["source_group"], YSP_GROUP)
        self.assertEqual(row["name"], SEED_BY_SLUG[YSP_SLUG][1])

    def test_restart_keeps_source_and_restores_declared_values(self):
        """第二次 initialize()（= 容器重启）后：行还在、声明值被重新写回、分组不丢。

        这里刻意把三样东西改坏再重启：name（种子每次覆盖）、priority（同上）、
        source_group（由 _seed_source_groups 每次补写）；同时把 status / item_count
        设成「同步过的源」该有的样子，断言它们**不被**种子流程重置。
        """
        isolated = self._initialize()
        with isolated.write() as conn:
            conn.execute(
                "UPDATE sources SET name='被改过的名字', priority=99, source_group='', "
                "status='ok', item_count=63 WHERE slug=?",
                (YSP_SLUG,),
            )
        self._initialize()   # 重启
        row = self._row(isolated, YSP_SLUG)
        self.assertIsNotNone(row, "重启后内置源不能消失")
        self.assertEqual(row["name"], SEED_BY_SLUG[YSP_SLUG][1])
        self.assertEqual(row["priority"], 2)
        self.assertEqual(row["source_group"], YSP_GROUP)
        self.assertEqual(row["url"], YSP_URL)
        # 运行期状态不属于种子职责：重启不该把已经同步好的源打回 never
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["item_count"], 63)

    def test_url_owned_by_other_slug_is_skipped_without_group_leak(self):
        """URL 去重：同地址已被手动源占用时，seed 跳过、不产生第二行、不污染别人的分组。"""
        isolated = self._initialize()
        # 模拟「先手动接入同一个地址」：删掉种子行，再用另一个 slug 占住这个 URL
        with isolated.write() as conn:
            conn.execute("DELETE FROM sources WHERE slug=?", (YSP_SLUG,))
            conn.execute(
                "INSERT INTO sources(slug, name, url, format, priority, source_group) "
                "VALUES ('manual::deadbeef', '手动源 · 测试', ?, 'm3u', 60, '手工分组')",
                (YSP_URL,),
            )
        self._initialize()   # 重启：种子预检命中，跳过
        rows = isolated.rows("SELECT slug, source_group FROM sources WHERE url=?", (YSP_URL,))
        self.assertEqual(len(rows), 1, f"同一 URL 只能有一行：{rows}")
        self.assertEqual(rows[0]["slug"], "manual::deadbeef")
        self.assertEqual(rows[0]["source_group"], "手工分组", "内置源的分组不能写到占用该 URL 的源上")
        self.assertIsNone(self._row(isolated, YSP_SLUG), "URL 被占用时不该再建一个同地址的种子行")


if __name__ == "__main__":   # pragma: no cover
    unittest.main()
