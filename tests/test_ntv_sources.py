"""nTv 源适配器接入（v56）的回归护栏。

本次接入的全部要点是**「两条 seed 指向本机适配器，而不是 GitHub」**：

* 上游 `buhanzhe/NativeWasmTv` 与 `buhanzhe/webSourceM3U8` 里能进 IPTV-Hub 的只有
  20 条央视直连 HLS + 48 条电台流，其余（`webview.txt` 的 482 条页面 URL、
  `TvWasm/cjs` 的 `.cjs` 壳地址）必须先由 nTv 壳解析成流地址，**不是可索引的源**；
* 归一化（丢掉 `webview://`、把 `名称,地址` 清单式转成标准 M3U、按 URL 去重）只发生在
  适配器里，hub 侧没有这个能力 —— 所以把 seed 改成直连上游等于「既丢转换又变慢」
  （本机到 GitHub release 实测 26s，LAN 是毫秒级）。

因此下面的用例既锁「声明值」，也锁「指向哪儿」和「白名单里有没有它」——
后两者任一被改坏，症状都是**同步静默失败**（源行在、条目永远 0），没有报错。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import db as db_module
from app.parsers import region_of

SEED_BY_SLUG = {seed[0]: seed for seed in db_module.SOURCE_SEEDS}

CCTV_SLUG, FM_SLUG = "ntv-cctv", "ntv-fm"
ADAPTER_HOST = "192.168.31.110:8769"
CCTV_URL = f"http://{ADAPTER_HOST}/ntv-cctv.m3u"
FM_URL = f"http://{ADAPTER_HOST}/ntv-fm.m3u"
GROUP = "本地源"


@unittest.skipUnless(
    db_module.SOURCE_SEEDS,
    "干净基线（2026-10-10）：内置种子已清空，本组用例不适用；恢复内置源后自动重新生效",
)
class NtvSeedDeclarationTest(unittest.TestCase):
    """种子声明值（不建库，纯常量断言）。"""

    def test_both_seeds_declared(self):
        for slug, url, priority in ((CCTV_SLUG, CCTV_URL, 4), (FM_SLUG, FM_URL, 7)):
            with self.subTest(slug=slug):
                seed = SEED_BY_SLUG.get(slug)
                self.assertIsNotNone(seed, f"SOURCE_SEEDS 里必须有 {slug}")
                self.assertEqual(seed[2], url)
                self.assertEqual(seed[3], "m3u")
                self.assertEqual(seed[4], priority)
                self.assertIn("nTv", seed[1])

    def test_seeds_point_at_local_adapter_not_upstream(self):
        """护栏：必须指向本机适配器，不能改成直连 GitHub / raw.githubusercontent。

        直连会同时丢掉两样东西：格式归一化（`webview://` 过滤、清单式转 M3U）与速度
        （上游 release 实测 26s）。要改这条得先让 hub 自己具备转换能力，并另行验证。
        """
        for slug in (CCTV_SLUG, FM_SLUG):
            with self.subTest(slug=slug):
                url = SEED_BY_SLUG[slug][2]
                self.assertTrue(url.startswith(f"http://{ADAPTER_HOST}/"), url)
                self.assertNotIn("github", url.lower())
                self.assertNotIn("raw.githubusercontent", url.lower())

    def test_seeds_do_not_displace_ysp_live_as_most_prioritised(self):
        """priority 数值越小越优先；新 seed 不能抢掉 ysp-live(2) 的最优位。"""
        ysp = SEED_BY_SLUG["ysp-live"][4]
        self.assertEqual(ysp, min(seed[4] for seed in db_module.SOURCE_SEEDS))
        self.assertGreater(SEED_BY_SLUG[CCTV_SLUG][4], ysp)
        self.assertGreater(SEED_BY_SLUG[FM_SLUG][4], ysp)

    def test_cctv_precedes_public_sources_and_fm_precedes_ifansclub_local(self):
        """取值理由要可复核：两条都是本机 LAN 源，理应排在公网源前面。

        ntv-cctv(4) 夹在 tvgo-local(3) 与 cctv-live(5) 之间；
        ntv-fm(7) 在 ifansclub-local(8) 之前（电台要尽早拿到第一轮健康数据）。
        """
        self.assertLess(SEED_BY_SLUG["tvgo-local"][4], SEED_BY_SLUG[CCTV_SLUG][4])
        self.assertLess(SEED_BY_SLUG[CCTV_SLUG][4], SEED_BY_SLUG["cctv-live"][4])
        self.assertLess(SEED_BY_SLUG[FM_SLUG][4], SEED_BY_SLUG["ifansclub-local"][4])

    def test_both_grouped_into_local_source(self):
        for slug in (CCTV_SLUG, FM_SLUG):
            with self.subTest(slug=slug):
                self.assertEqual(db_module.SOURCE_SEED_GROUPS.get(slug), GROUP)

    def test_slugs_and_urls_stay_unique(self):
        slugs = [seed[0] for seed in db_module.SOURCE_SEEDS]
        urls = [seed[2] for seed in db_module.SOURCE_SEEDS]
        self.assertEqual(len(set(slugs)), len(slugs), f"seed slug 有重复：{slugs}")
        self.assertEqual(len(set(urls)), len(urls), f"seed URL 有重复：{urls}")


class NtvRegionPolicyTest(unittest.TestCase):
    """区域判定：白名单是 46 条 SomaFM 不被 KEEP_REGION=domestic 丢掉的原因。"""

    def test_ascii_radio_names_survive_only_because_of_whitelist(self):
        # 真实形态：group_title="SomaFM"、频道名纯 ASCII
        self.assertEqual(region_of(FM_SLUG, "", "SomaFM", "Groove Salad"), "domestic")
        # 对照组：同一个频道换一个不在白名单的 slug → 判成 international（会被丢掉）
        self.assertEqual(region_of("some-other-source", "", "SomaFM", "Groove Salad"), "international")

    def test_domestic_radio_group_is_kept_regardless(self):
        self.assertEqual(region_of(FM_SLUG, "", "国内电台", "洛阳音乐广播"), "domestic")

    def test_cctv_source_is_domestic(self):
        self.assertEqual(region_of(CCTV_SLUG, "", "央视频道", "CCTV-1 综合"), "domestic")


@unittest.skipUnless(
    db_module.SOURCE_SEEDS,
    "干净基线（2026-10-10）：内置种子已清空，本组用例不适用；恢复内置源后自动重新生效",
)
class NtvSeedDatabaseTest(unittest.TestCase):
    """真建库跑 initialize()：两条 seed 落库带分组，重启不回退运行期状态。

    **隔离硬约束**：只在 /tmp 下新建空库（先 assert 路径再动任何 SQL）。
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="iptv-hub-ntv-", dir="/tmp")
        self.db_path = Path(self._tmpdir.name) / "ntv.sqlite3"
        assert str(self.db_path).startswith("/tmp/"), f"隔离库必须在 /tmp 下：{self.db_path}"
        self.addCleanup(self._tmpdir.cleanup)

    def _initialize(self) -> db_module.Database:
        isolated = db_module.Database(self.db_path)
        isolated.initialize()
        return isolated

    def test_initialize_creates_both_with_group(self):
        isolated = self._initialize()
        for slug, url, priority in ((CCTV_SLUG, CCTV_URL, 4), (FM_SLUG, FM_URL, 7)):
            with self.subTest(slug=slug):
                row = isolated.row("SELECT * FROM sources WHERE slug=?", (slug,))
                self.assertIsNotNone(row, f"initialize() 必须写入 {slug}")
                self.assertEqual(row["url"], url)
                self.assertEqual(row["format"], "m3u")
                self.assertEqual(row["priority"], priority)
                self.assertEqual(row["source_group"], GROUP)
                self.assertEqual(row["name"], SEED_BY_SLUG[slug][1])

    def test_restart_restores_declared_values_without_resetting_runtime_state(self):
        isolated = self._initialize()
        with isolated.write() as conn:
            conn.execute(
                "UPDATE sources SET name='被改过', priority=99, source_group='', "
                "status='ok', item_count=48 WHERE slug=?",
                (FM_SLUG,),
            )
        self._initialize()   # 重启
        row = isolated.row("SELECT * FROM sources WHERE slug=?", (FM_SLUG,))
        self.assertEqual(row["name"], SEED_BY_SLUG[FM_SLUG][1])
        self.assertEqual(row["priority"], 7)
        self.assertEqual(row["source_group"], GROUP)
        # 运行期状态不归种子管
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["item_count"], 48)


class NtvAdapterHostAllowlistTest(unittest.TestCase):
    """适配器主机必须在**内置**白名单里，否则同步会以 `source host not approved` 静默失败。

    这里断言的是内置白名单（写死在 app/sync.py），不是库里的手动放行表 —— 后者是本机
    历史遗留（ysp-live 当年作为手动源时写进去的），删掉它不该影响这两条 seed。
    """

    def test_adapter_host_is_builtin_allowlisted(self):
        try:
            from app import sync as sync_module
        except ImportError as exc:  # pragma: no cover - 宿主缺 httpx 时跳过（与既有 skipped 同口径）
            self.skipTest(f"app.sync 不可导入：{exc}")
        host = ADAPTER_HOST.split(":")[0]
        self.assertIn(host, sync_module.ALLOWED_SOURCE_HOSTS_BUILTIN)


if __name__ == "__main__":   # pragma: no cover
    unittest.main()
