"""v43 订阅出口按数据源分组过滤（group / exclude_group）的回归测试。

隔离硬约束（同 test_export_dead_filter.py）：只在 `/tmp` 下建库，`assert` 先于任何 SQL，
绝不碰生产库 `/data/iptv.sqlite3`；过滤本身是**纯查询**，这里顺带断言它不写库。

覆盖：
  * `group=` 只出该组、`exclude_group=` 去掉该组、两者叠加（含同值 → 空集）；
  * `group` / `exclude_group` 与既有 `source` / `region` 过滤叠加时仍然是交集；
  * **不传参数时与改动前逐字节一致**，两道独立证明：
      ① `_fetch_lines` 默认参数生成的 SQL 与改动前的 SQL 逐字符相同（冻结在 `_LEGACY_SQL`）；
      ② 固定 fixture 上的整份 M3U / TXT 产物等于冻结的黄金字节。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from dataclasses import replace as dataclass_replace
from pathlib import Path
from unittest import mock

from app import db as db_module
from app import static_export
from app.config import settings as real_settings

# 隔离硬约束：先在 /tmp 下建库，断言通过之后才允许执行任何 SQL
_TMPDIR = tempfile.TemporaryDirectory(prefix="iptv-hub-group-", dir="/tmp")
_DB_PATH = Path(_TMPDIR.name) / "group.sqlite3"
assert str(_DB_PATH).startswith("/tmp/"), _DB_PATH
_ISOLATED = db_module.Database(_DB_PATH)
_ISOLATED.initialize()

# 渲染参数固定下来：宿主机与容器（env 里 KEEP_REGION=domestic、MAX_LINES_*、MAIN_EXCLUDE_GROUP）
# 跑出来的黄金字节必须一致，否则"逐字节一致"就成了环境依赖的断言。MAIN_EXCLUDE_GROUP 尤其要
# 显式钉成空串：容器里 .env 配了 `MAIN_EXCLUDE_GROUP=发现源`，不钉住的话默认产物会少一整组。
_FIXED_SETTINGS = dataclass_replace(
    real_settings, keep_region="", export_hide_dead=True, export_dead_fail_count=3,
    max_lines_per_channel=5, max_lines_cctv=10, main_exclude_group="",
)

# 改动前（v42）`_fetch_lines` 在默认参数下生成的那条 SQL，逐字符冻结。
# 说明来源：只加 group/exclude_group 两个条件时 SQL 才变化，默认路径的字符串在 v43 里
# 一字未动；这条断言的作用是"以后谁再动默认查询，测试立刻红"。
# v52 唯一一次**有意**改动 SELECT：多取一列 `e.source_slug`（回看属性必须按源判定，不能按
# URL 猜，见 tests/test_export_catchup.py）。WHERE 子句与参数顺序一个字未动，因此行集、
# 行序、非 ysp-live 线路的产物仍逐字节一致（该文件另有断言证明）。
_LEGACY_SQL = """
        SELECT e.name, e.group_title, e.url, e.referrer, e.user_agent, e.logo, e.tvg_id,
               e.stream_fingerprint,
               e.source_slug,
               COALESCE(s.source_group, '') AS source_group,
               h.speed_kbps, h.latency_ms, h.ok_total, h.fail_total,
               CASE WHEN COALESCE(h.fail_count, 0) >= ? THEN 1 ELSE 0 END AS is_dead
        FROM source_entries e
        LEFT JOIN stream_health h ON h.fingerprint=e.stream_fingerprint
        LEFT JOIN sources s ON s.slug=e.source_slug
        WHERE e.active=1 AND (h.status IS NULL OR h.status='ok')
        """


class _RecordingDb:
    """记录 `_fetch_lines` 真正执行的 SQL（转发给隔离库，不改变行为）。"""

    def __init__(self, real: db_module.Database):
        self._real = real
        self.calls: list[tuple[str, tuple]] = []

    def rows_raw(self, sql: str, params=()):
        self.calls.append((sql, tuple(params)))
        return self._real.rows_raw(sql, params)


def _add_source(slug: str, source_group: str) -> None:
    with _ISOLATED.write() as conn:
        conn.execute(
            "INSERT INTO sources(slug, name, url, format, priority, source_group) "
            "VALUES (?, ?, ?, 'auto', 1, ?) ON CONFLICT(slug) DO UPDATE SET source_group=excluded.source_group",
            (slug, slug, f"http://{slug}.example/x.m3u", source_group),
        )


def _add_entry(slug: str, key: str, name: str, url: str, *, region: str = "domestic",
               group_title: str = "央视", speed: float = 100.0, active: int = 1) -> None:
    with _ISOLATED.write() as conn:
        conn.execute(
            "INSERT INTO source_entries(source_slug, entry_key, stream_fingerprint, channel_key, "
            "name, normalized_name, group_title, url, region, active, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-10-05T00:00:00+00:00', "
            "'2026-10-05T00:00:00+00:00') "
            "ON CONFLICT(source_slug, entry_key) DO UPDATE SET url=excluded.url, active=excluded.active",
            (slug, key, f"fp-{key}", key, name, name, group_title, url, region, active),
        )


def _add_health(fingerprint: str, speed: float) -> None:
    with _ISOLATED.write() as conn:
        conn.execute(
            "INSERT INTO stream_health(fingerprint, url, status, fail_count, speed_kbps, latency_ms, "
            "ok_total, fail_total, checked_at) VALUES (?, ?, 'ok', 0, ?, 10, 1, 0, "
            "'2026-10-05T00:00:00+00:00') ON CONFLICT(fingerprint) DO UPDATE SET speed_kbps=excluded.speed_kbps",
            (fingerprint, f"http://{fingerprint}/x.m3u8", speed),
        )


class IsolatedFixtureTestCase(unittest.TestCase):
    """一个 fixture 贯穿全文件：三个分组（发现源 / 别的组 / 未分组）+ 一个国外条目。"""

    @classmethod
    def setUpClass(cls):
        with _ISOLATED.write() as conn:
            conn.execute("DELETE FROM source_entries")
            conn.execute("DELETE FROM stream_health")
            conn.execute("DELETE FROM sources")
        _add_source("g-disc", "发现源")
        _add_source("g-misc", "别的组")
        _add_source("g-plain", "")
        # 每台都是不同 URL/指纹，避免指纹去重把线路吃掉
        _add_entry("g-disc", "d1", "CCTV1", "http://d/1", speed=300)
        _add_entry("g-disc", "d2", "CCTV2", "http://d/2", speed=200)
        _add_entry("g-misc", "m1", "CCTV1", "http://m/1", speed=400)
        _add_entry("g-plain", "p1", "CCTV1", "http://p/1", speed=500)
        _add_entry("g-plain", "p2", "测试台", "http://p/2", group_title="其他", speed=100)
        # 国外条目：用来验证分组过滤与 region 过滤叠加
        _add_entry("g-disc", "d3", "海外台", "http://d/3", region="international", group_title="其他", speed=50)
        for fp, speed in (("fp-d1", 300), ("fp-d2", 200), ("fp-m1", 400),
                          ("fp-p1", 500), ("fp-p2", 100), ("fp-d3", 50)):
            _add_health(fp, speed)

    def setUp(self):
        self._patches = [
            mock.patch.object(static_export, "db", _ISOLATED),
            mock.patch.object(static_export, "settings", _FIXED_SETTINGS),
            # 组织路径走无模版兜底：容器里 /data/channel-template.txt 存在，不屏蔽的话
            # 黄金字节会依赖生产模版文件
            mock.patch.object(static_export.template, "snapshot", lambda: None),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    # --- 便捷方法 ---

    @staticmethod
    def urls(region: str = "", group: str = "", exclude_group: str = "", source: str = "") -> list[str]:
        """直接按行序取出线路 URL（**不过** `_resolve_region`，避免 KEEP_REGION 影响断言）。"""
        return [r["url"] for r in static_export._fetch_lines(False, source, region, group, exclude_group)]

    @staticmethod
    def render_m3u(**kwargs) -> str:
        return static_export.build_m3u("multi", False, "", "", "", None, **kwargs)

    @staticmethod
    def render_txt(**kwargs) -> str:
        return static_export.build_txt("multi", False, "", "", None, **kwargs)


class DefaultSqlIsUnchangedTests(IsolatedFixtureTestCase):
    """不传新参数 = 改动前的 SQL（因此行集、产物都不变）。"""

    def test_default_sql_is_byte_identical_to_legacy(self):
        recorder = _RecordingDb(_ISOLATED)
        with mock.patch.object(static_export, "db", recorder):
            static_export._fetch_lines(False, "", "")
        self.assertEqual(len(recorder.calls), 1)
        sql, params = recorder.calls[0]
        self.assertEqual(sql, _LEGACY_SQL, "默认参数下的 SQL 必须与改动前逐字符相同")
        # 参数只有 export_dead_fail_count 一个（group/exclude_group 为空时不追加）
        self.assertEqual(params, (_FIXED_SETTINGS.export_dead_fail_count,))

    def test_empty_string_filters_behave_like_no_filter(self):
        """显式传空串（?group=&exclude_group=）与不传等价 —— 键空间里两者是同一个键。"""
        self.assertEqual(self.urls(), self.urls(group="", exclude_group=""))

    def test_legacy_positional_call_still_works(self):
        """老调用方（只传前 4 个位置参数）行为不变：build_m3u/build_txt 的新参数都有默认值。"""
        self.assertEqual(
            static_export.build_m3u("multi", False, "", "", "", None),
            static_export.build_m3u("multi", False, "", "", "", None, group="", exclude_group=""),
        )


class GroupFilterTests(IsolatedFixtureTestCase):
    def test_group_keeps_only_that_group(self):
        self.assertEqual(sorted(self.urls(group="发现源")),
                         ["http://d/1", "http://d/2", "http://d/3"])

    def test_group_other_group(self):
        self.assertEqual(self.urls(group="别的组"), ["http://m/1"])

    def test_exclude_group_drops_that_group(self):
        self.assertEqual(sorted(self.urls(exclude_group="发现源")),
                         ["http://m/1", "http://p/1", "http://p/2"])

    def test_exclude_group_only_affects_that_group(self):
        """排除一个分组不能连带把未分组（source_group=''）的线路删掉。"""
        kept = self.urls(exclude_group="别的组")
        self.assertIn("http://p/1", kept, "未分组源必须保留")
        self.assertNotIn("http://m/1", kept)

    def test_group_and_exclude_stack(self):
        """叠加 = 交集：只留 A，且不要 B。"""
        self.assertEqual(self.urls(group="发现源", exclude_group="别的组"),
                         ["http://d/1", "http://d/2", "http://d/3"])

    def test_same_group_in_both_params_is_empty(self):
        """同一分组同时"只要"和"不要" = 空集（不额外报错，交给 SQL 求交）。"""
        self.assertEqual(self.urls(group="发现源", exclude_group="发现源"), [])

    def test_group_stacks_with_region_and_source(self):
        self.assertEqual(self.urls(group="发现源", region="domestic"),
                         ["http://d/1", "http://d/2"])
        self.assertEqual(self.urls(group="发现源", region="international"), ["http://d/3"])
        self.assertEqual(self.urls(group="发现源", source="g-disc"),
                         ["http://d/1", "http://d/2", "http://d/3"])
        self.assertEqual(self.urls(group="发现源", source="g-misc"), [])

    def test_inactive_entries_never_leak_in(self):
        _add_entry("g-disc", "d4", "停用台", "http://d/4", active=0)
        self.assertNotIn("http://d/4", self.urls(group="发现源"))

    def test_render_m3u_only_contains_the_group(self):
        """整份产物层面：group= 的 M3U 里没有别的分组的 URL，且总条数等于该组条数。"""
        text = self.render_m3u(group="发现源")
        urls = [line for line in text.splitlines() if line and not line.startswith("#")]
        self.assertEqual(sorted(urls), ["http://d/1", "http://d/2", "http://d/3"])
        # 这几条线路全部来自「发现源」→ v40 的整频道分组覆盖会把它们归到「发现源」组
        self.assertNotIn("group-title=\"央视\"", text)

    def test_render_txt_matches_m3u_row_set(self):
        m3u = [line for line in self.render_m3u(group="发现源").splitlines()
               if line and not line.startswith("#")]
        txt = [line.split(",", 1)[1] for line in self.render_txt(group="发现源").splitlines()
               if line and not line.endswith(",#genre#")]
        self.assertEqual(sorted(m3u), sorted(txt))

    def test_no_writes_happen(self):
        """过滤是纯查询：跑一遍各种组合后，库里行数分毫不动。"""
        before = _ISOLATED.row("SELECT COUNT(*) AS c FROM source_entries")["c"]
        for kwargs in ({"group": "发现源"}, {"exclude_group": "发现源"},
                       {"group": "发现源", "exclude_group": "发现源"}):
            self.urls(**kwargs)
        self.assertEqual(_ISOLATED.row("SELECT COUNT(*) AS c FROM source_entries")["c"], before)


class GoldenOutputTests(IsolatedFixtureTestCase):
    """不传参数时的产物逐字节冻结（改动前 = 改动后）。

    黄金字节的来源：这段产物由**改动前的代码路径**生成（渲染逻辑 v43 未动，只多了两个
    WHERE 条件，而这里的 SQL 已由上面的 `_LEGACY_SQL` 断言证明与改动前逐字符相同）。
    行序也核对过：央视组内按速度降序（500 → 400 → 300），CCTV2 / 海外台 因"整频道只由
    「发现源」提供"被 v40 的分组覆盖搬到「发现源」组。
    """

    GOLDEN_M3U = (
        "#EXTM3U\n"
        '#EXTINF:-1 tvg-id="CCTV1" tvg-name="CCTV1" group-title="央视",CCTV1\n'
        "http://p/1\n"
        '#EXTINF:-1 tvg-id="CCTV1" tvg-name="CCTV1" group-title="央视",CCTV1\n'
        "http://m/1\n"
        '#EXTINF:-1 tvg-id="CCTV1" tvg-name="CCTV1" group-title="央视",CCTV1\n'
        "http://d/1\n"
        '#EXTINF:-1 tvg-id="测试台" tvg-name="测试台" group-title="其他",测试台\n'
        "http://p/2\n"
        '#EXTINF:-1 tvg-id="CCTV2" tvg-name="CCTV2" group-title="发现源",CCTV2\n'
        "http://d/2\n"
        '#EXTINF:-1 tvg-id="海外台" tvg-name="海外台" group-title="发现源",海外台\n'
        "http://d/3\n"
    )
    GOLDEN_TXT = (
        "央视,#genre#\n"
        "CCTV1,http://p/1\n"
        "CCTV1,http://m/1\n"
        "CCTV1,http://d/1\n"
        "其他,#genre#\n"
        "测试台,http://p/2\n"
        "发现源,#genre#\n"
        "CCTV2,http://d/2\n"
        "海外台,http://d/3\n"
    )

    def test_default_m3u_is_frozen_byte_for_byte(self):
        self.assertEqual(self.render_m3u(), self.GOLDEN_M3U)

    def test_default_txt_is_frozen_byte_for_byte(self):
        self.assertEqual(self.render_txt(), self.GOLDEN_TXT)

    def test_group_output_is_a_subset_of_default_lines(self):
        """任何分组筛选都只能减少线路，不会凭空造出新线路。"""
        base = set(line for line in self.render_m3u().splitlines() if line.startswith("http"))
        filtered = set(line for line in self.render_m3u(group="发现源").splitlines() if line.startswith("http"))
        self.assertTrue(filtered <= base, "筛选结果必须是全量产物的子集")


class MainExcludeGroupConfigTests(IsolatedFixtureTestCase):
    """MAIN_EXCLUDE_GROUP：配置值让「默认导出」排除某分组，**静态导出同口径**。

    静态文件是"不带任何参数的主订阅"，如果只改 HTTP 出口不改它，直接喂 data/live.m3u 的
    用户就会看到多出一整组 —— 所以这两条路径共用 `static_export.main_exclude_group()`。
    写文件也必须落在 /tmp（每个用例一个临时 data_dir，先断言再写）。
    """

    @staticmethod
    def urls(text: str) -> list[str]:
        return [line for line in text.splitlines() if line and not line.startswith("#")]

    @staticmethod
    def txt_urls(text: str) -> list[str]:
        return [line.split(",", 1)[1] for line in text.splitlines()
                if line and not line.endswith(",#genre#")]

    def _static_settings(self, **overrides):
        data_dir = Path(tempfile.mkdtemp(prefix="iptv-hub-static-", dir="/tmp"))
        assert str(data_dir).startswith("/tmp/"), data_dir
        self.addCleanup(shutil.rmtree, data_dir, ignore_errors=True)
        return dataclass_replace(_FIXED_SETTINGS, data_dir=data_dir, **overrides)

    def _write(self, **overrides):
        """用指定配置跑一次 write_static_exports，返回 (结果, m3u 文本, txt 文本)。"""
        configured = self._static_settings(**overrides)
        with mock.patch.object(static_export, "settings", configured):
            result = static_export.write_static_exports()
            m3u = (configured.data_dir / "live.m3u").read_text(encoding="utf-8")
            txt = (configured.data_dir / "live.txt").read_text(encoding="utf-8")
        return result, m3u, txt

    def test_configured_group_is_excluded_from_static_exports(self):
        result, m3u, txt = self._write(main_exclude_group="发现源")
        # 发现源的 d1/d2/d3 三条全部消失，其余（别的组 + 未分组）保留
        self.assertEqual(sorted(self.urls(m3u)), ["http://m/1", "http://p/1", "http://p/2"])
        self.assertEqual(sorted(self.txt_urls(txt)), ["http://m/1", "http://p/1", "http://p/2"])
        self.assertNotIn("发现源", m3u, "配置的分组不得出现在静态产物里（连分组名都不该有）")
        self.assertEqual(result["m3u"], 3)
        self.assertEqual(result["txt"], 3)

    def test_empty_config_keeps_the_old_static_output(self):
        """默认（没配）= 与改动前一致：静态行集与 build_m3u 的默认产物完全相同。"""
        _result, m3u, _txt = self._write(main_exclude_group="")
        self.assertEqual(sorted(self.urls(m3u)), sorted(self.urls(self.render_m3u())))

    def test_invalid_configured_group_is_ignored(self):
        """配置写错（分组不存在）→ 忽略该配置，不 400、不空产物。"""
        result, m3u, _txt = self._write(main_exclude_group="不存在的组")
        self.assertEqual(sorted(self.urls(m3u)), sorted(self.urls(self.render_m3u())))
        self.assertEqual(result["m3u"], 6)

    def test_overlong_configured_group_is_ignored(self):
        """超长（>64）同样忽略，且不得把异常带进静态导出（result 不会是 -1）。"""
        result, m3u, _txt = self._write(main_exclude_group="x" * 65)
        self.assertEqual(sorted(self.urls(m3u)), sorted(self.urls(self.render_m3u())))
        self.assertGreater(result["m3u"], 0, "配置无效时必须正常导出，而不是 -1")

    def test_existing_group_helper_matches_the_strict_path(self):
        """宽松口径（配置）与严格口径（用户传参）判存结果一致：同一个 SQL、同一个上限。"""
        self.assertEqual(static_export.existing_group("发现源"), "发现源")
        self.assertEqual(static_export.existing_group("  别的组  "), "别的组")
        for bad in ("", "   ", "不存在的组", "x" * 65):
            self.assertEqual(static_export.existing_group(bad), "", bad)


if __name__ == "__main__":
    unittest.main()
