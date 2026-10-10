"""v53 偏好源（`EXPORT_PREFER_SOURCES`）：让有回看能力的源拿到频道的「线路 1」。

背景：v52 给 `ysp-live` 的线路挂上了 catchup 属性，但生产 6,159 条线路里只有 22 条来自它
—— 回看入口在播放器里通常只挂在**线路 1**（TiviMate/Kodi 按频道取第一条线路），线路 1 是
别的源时那个频道就等于没有回看。本特性在 `_line_order` 的排序键**最前面**加一个偏好源权重。

覆盖（对照 / 硬约束都在这一个文件里）：
  * 偏好源的线路排到最前，其后**仍按原有键**（速度降序等）——不是把原排序整个打乱；
  * 判定按 `source_slug` 精确匹配：同 host 的另一个 slug 不沾光；
  * **没有该源覆盖的频道逐字节不变**（按频道分块比对 EXTINF 与地址行）；
  * 频道的分组归属与频道顺序不变（偏好只改频道**内部**的线路顺序）；
  * 每频道线路条数上限行为不变，偏好源只有 1 条时其余名次照旧补足，不会只剩 1 条；
  * `EXPORT_PREFER_SOURCES=` 空值 → 与改动前（用旧排序键渲染）**逐字节相同**；
  * 配置进导出缓存键：M3U 与 TXT 都要（偏好改的是"选中哪几条线路"，TXT 也受影响）。

隔离硬约束（同其它导出测试）：只在 `/tmp` 下建库，`assert` 先于任何 SQL，绝不碰生产库。
"""

from __future__ import annotations

import re
import tempfile
import unittest
from dataclasses import replace as dataclass_replace
from pathlib import Path
from unittest import mock

from app import db as db_module
from app import static_export
from app.config import settings as real_settings

try:  # 宿主机没有 FastAPI 时只跳过缓存键那一组（其余用例与容器无关）
    from fastapi.testclient import TestClient

    from app import main as main_module
except ModuleNotFoundError:  # pragma: no cover - 宿主机环境
    TestClient = None
    main_module = None

# 隔离硬约束：先在 /tmp 下建库并断言路径，再执行任何 SQL
_TMPDIR = tempfile.TemporaryDirectory(prefix="iptv-hub-prefer-", dir="/tmp")
_DB_PATH = Path(_TMPDIR.name) / "prefer.sqlite3"
assert str(_DB_PATH).startswith("/tmp/"), _DB_PATH
_ISOLATED = db_module.Database(_DB_PATH)
_ISOLATED.initialize()

# 渲染参数钉死：容器 .env 里的 KEEP_REGION/MAX_LINES_*/MAIN_EXCLUDE_GROUP 不能让断言随环境漂移。
# 线路条数上限刻意取小（3）：这样"偏好源把某条挤出去 / 条数不变"能被一眼看出来。
_FIXED_SETTINGS = dataclass_replace(
    real_settings, keep_region="", export_hide_dead=True, export_dead_fail_count=3,
    max_lines_per_channel=3, max_lines_cctv=3, main_exclude_group="", epg_url="",
    public_base_url="", export_catchup=True, export_catchup_source_slugs="ysp-live",
    export_prefer_sources="ysp-live",
)

YSP_HOST = "192.0.2.130:8767"
PREFER = "ysp-live"

# 频道名 → （分组, 线路列表）。每条线路 = (slug, 条目 key, 速度 kbps, 路径, 可选指纹)
# 所有 CCTV* 用 group_title='央视'，其余用 '地方台'（与无模版兜底路径的分桶一致）。
_LINES: dict[str, list[tuple[str, str, int, str]]] = {
    # A：偏好源最慢，改动前**被上限挤出订阅**（线路 1 是别的源 → 该频道没有回看）
    "CCTV1": [
        ("ysp-live", "a1", 800, "/cctv1.m3u8"),
        ("fast-src", "a2", 5000, "/a2.m3u8"),
        ("fast-src", "a3", 4000, "/a3.m3u8"),
        ("mid-src", "a4", 3000, "/a4.m3u8"),
        ("mid-src", "a5", 2000, "/a5.m3u8"),
        ("other-local", "a6", 100, "/cctv9.m3u8"),  # 同 host、不同 slug：不许被偏好
    ],
    # B：偏好源只比其中一条快，改动后应排在最前、其余按速度补足到上限
    "CCTV5": [
        ("ysp-live", "b1", 600, "/cctv5.m3u8"),
        ("fast-src", "b2", 4500, "/b2.m3u8"),
        ("mid-src", "b3", 1200, "/b3.m3u8"),
    ],
    # C：偏好源只有 1 条，其余名次照旧补足（不许因为偏好只剩 1 条线路）
    "CCTV9": [
        ("ysp-live", "c1", 700, "/cctv9.m3u8"),
        ("fast-src", "c2", 6000, "/c2.m3u8"),
        ("mid-src", "c3", 5500, "/c3.m3u8"),
    ],
    # D：偏好源与别的源**同一条流**（同指纹）：去重后保留偏好源那条地址（它才是能回看的）
    "CCTV13": [
        ("ysp-live", "d1", 900, "/cctv13.m3u8"),
        ("fast-src", "d2", 6000, "/d2.m3u8"),
    ],
    # E：**没有**偏好源覆盖的频道 —— 整个分块必须逐字节不变
    "湖南卫视": [
        ("fast-src", "e1", 3000, "/e1.m3u8"),
        ("mid-src", "e2", 2000, "/e2.m3u8"),
        ("mid-src", "e3", 1000, "/e3.m3u8"),
        ("fast-src", "e4", 500, "/e4.m3u8"),
    ],
}


def _add_source(slug: str) -> None:
    with _ISOLATED.write() as conn:
        conn.execute(
            "INSERT INTO sources(slug, name, url, format, priority, source_group) "
            "VALUES (?, ?, ?, 'auto', 1, '') "
            "ON CONFLICT(slug) DO UPDATE SET url=excluded.url",
            (slug, slug, f"http://{slug}.example/x.m3u"),
        )


def _host_for(slug: str) -> str:
    """ysp-live 与 other-local 落在**同一个** host:port 上（判定只许按 slug）。"""
    return YSP_HOST if slug in {PREFER, "other-local"} else f"{slug}.example"


def _add_entry(slug: str, key: str, name: str, url: str, group_title: str,
               fingerprint: str) -> None:
    with _ISOLATED.write() as conn:
        conn.execute(
            "INSERT INTO source_entries(source_slug, entry_key, stream_fingerprint, channel_key, "
            "name, normalized_name, group_title, url, region, active, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'domestic', 1, '2026-10-05T00:00:00+00:00', "
            "'2026-10-05T00:00:00+00:00') "
            "ON CONFLICT(source_slug, entry_key) DO UPDATE SET url=excluded.url",
            (slug, key, fingerprint, key, name, name, group_title, url),
        )


def _populate() -> None:
    """模块导入时就写好 fixture（缓存键那一组按字母序可能先跑）。"""
    with _ISOLATED.write() as conn:
        conn.execute("DELETE FROM source_entries")
        conn.execute("DELETE FROM stream_health")
        conn.execute("DELETE FROM sources")
    for slug in ("ysp-live", "fast-src", "mid-src", "other-local"):
        _add_source(slug)
    for name, lines in _LINES.items():
        group_title = "央视" if name.startswith("CCTV") else "地方台"
        for slug, key, speed, path in lines:
            # D 频道两条线路共用同一个流指纹（同一条流被两个源收录）
            fingerprint = "fp-shared-d" if name == "CCTV13" else f"fp-{key}"
            url = f"http://{_host_for(slug)}{path}"
            _add_entry(slug, key, name, url, group_title, fingerprint)
            with _ISOLATED.write() as conn:
                conn.execute(
                    "INSERT INTO stream_health(fingerprint, url, status, speed_kbps, latency_ms, "
                    "ok_total, fail_total, checked_at) "
                    "VALUES (?, ?, 'ok', ?, 20, 10, 0, '2026-10-05T00:00:00+00:00') "
                    "ON CONFLICT(fingerprint) DO UPDATE SET speed_kbps=excluded.speed_kbps",
                    (fingerprint, url, float(speed)),
                )


_populate()


def _legacy_order(item: static_export.Line):
    """改动前（v52）的排序键，逐项照抄 —— 作为"空配置回到原样"的对照口径。"""
    return (
        item.speed is None,
        -(item.speed or 0),
        item.fail_total,
        -item.ok_total,
        item.latency if item.latency is not None else 10 ** 9,
        item.url,
    )


class PreferSourceTestCase(unittest.TestCase):

    def setUp(self):
        self._patches = [
            mock.patch.object(static_export, "db", _ISOLATED),
            mock.patch.object(static_export, "settings", _FIXED_SETTINGS),
            # 走无模版兜底分组：容器里 /data/channel-template.txt 存在，不屏蔽的话产物会依赖
            # 生产模版文件（与其它导出测试同口径）
            mock.patch.object(static_export.template, "snapshot", lambda: None),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    # --- 便捷方法 ---

    def render(self, **overrides) -> str:
        cfg = dataclass_replace(_FIXED_SETTINGS, **overrides)
        with mock.patch.object(static_export, "settings", cfg):
            return static_export.build_m3u("multi", False, "", "", "", None)

    def render_legacy(self) -> str:
        """用改动前的排序键渲染（偏好源配置为空）—— 旧产物的等价物。"""
        with mock.patch.object(static_export, "_line_order", _legacy_order):
            return self.render(export_prefer_sources="")

    @staticmethod
    def _blocks(m3u: str) -> dict[str, list[str]]:
        """按频道分块：{频道标签: [该频道全部行（EXTINF 与地址行交错）]}。

        注意同一频道有多条线路时会有**多条** EXTINF（label 相同），所以这里是 append
        而不是覆盖 —— 每条线路各占一组 `#EXTINF + 地址`。
        """
        blocks: dict[str, list[str]] = {}
        current: str | None = None
        for line in m3u.splitlines():
            if line.startswith("#EXTINF"):
                # 逗号后的显示名即 label（_safe_field 处理过，测试数据里不含逗号）
                current = line.rsplit(",", 1)[-1]
                blocks.setdefault(current, []).append(line)
            elif current is not None and line and not line.startswith("#"):
                blocks[current].append(line)
        return blocks

    @staticmethod
    def _urls(block: list[str]) -> list[str]:
        """分块里的地址行（去掉 #EXTINF / #EXTVLCOPT）。"""
        return [line for line in block if not line.startswith("#")]

    @staticmethod
    def _label_order(m3u: str) -> list[str]:
        return [line.rsplit(",", 1)[-1] for line in m3u.splitlines()
                if line.startswith("#EXTINF")]

    @staticmethod
    def _group_headers(m3u: str) -> list[str]:
        """产物里分组表头出现的顺序（连续同组只记一次）。"""
        out: list[str] = []
        for line in m3u.splitlines():
            if not line.startswith("#EXTINF"):
                continue
            match = re.search(r'group-title="([^"]*)"', line)
            group = match.group(1) if match else ""
            if not out or out[-1] != group:
                out.append(group)
        return out

    # --- 核心行为 ---

    def test_prefer_line_becomes_first_and_tail_keeps_speed_order(self):
        """CCTV1：偏好源最慢，仍必须排线路 1；其后两条按原键（速度降序）。"""
        urls = self._urls(self._blocks(self.render())["CCTV1"])
        self.assertEqual(urls[0], f"http://{YSP_HOST}/cctv1.m3u8", urls)
        self.assertEqual(urls[1:], [f"http://fast-src.example/a2.m3u8",
                                    f"http://fast-src.example/a3.m3u8"], urls)

    def test_without_prefer_the_same_channel_had_no_catchup_line(self):
        """反向确认这条改动是有意义的：不偏好时 ysp-live 的线路被上限挤出去了。"""
        urls = self._urls(self._blocks(self.render(export_prefer_sources=""))["CCTV1"])
        self.assertEqual(urls, [f"http://fast-src.example/a2.m3u8",
                                f"http://fast-src.example/a3.m3u8",
                                f"http://mid-src.example/a4.m3u8"], urls)
        self.assertNotIn("ysp", "".join(urls))

    def test_line_count_per_channel_unchanged(self):
        """每频道条数上限行为不变：偏好只是换人，不会把频道变少（也不会变多超过上限）。"""
        on, off = self._blocks(self.render()), self._blocks(self.render(export_prefer_sources=""))
        for name in ("CCTV1", "CCTV5", "CCTV9"):
            self.assertLessEqual(len(self._urls(on[name])), 3, name)
            self.assertLessEqual(len(self._urls(off[name])), 3, name)
            # 有 3 条以上可选的频道，两边都必须是满上限（偏好没有吃掉名额）
            self.assertEqual(len(self._urls(on[name])), 3, name)
            self.assertEqual(len(self._urls(off[name])), 3, name)
        # 偏好源只有 1 条的频道，其余名次照旧补足 —— 不是"只剩 1 条"
        self.assertGreater(len(self._urls(self._blocks(self.render())["CCTV9"])), 1)

    def test_other_lines_fill_up_to_the_limit_after_the_prefer_line(self):
        """CCTV5：偏好源 1 条 + 其余按速度补齐到上限，且顺序仍是速度降序。"""
        urls = self._urls(self._blocks(self.render())["CCTV5"])
        self.assertEqual(urls, [f"http://{YSP_HOST}/cctv5.m3u8",
                                "http://fast-src.example/b2.m3u8",
                                "http://mid-src.example/b3.m3u8"], urls)

    def test_same_host_other_slug_is_not_preferred(self):
        """同 host:port、slug 不同的线路不许沾光：100kbps 那条仍进不了 CCTV1 的前三名。"""
        block = self._blocks(self.render())["CCTV1"]
        self.assertNotIn("http://192.0.2.130:8767/cctv9.m3u8", block, block)
        # 空配置下它同样不在（证明"不在"不是偏好造成的，而是它本来就慢）
        self.assertNotIn("http://192.0.2.130:8767/cctv9.m3u8",
                         self._blocks(self.render(export_prefer_sources=""))["CCTV1"])

    def test_dedupe_still_collapses_the_same_stream_to_one_line(self):
        """同一条流被两个源收录：去重后仍只有 1 条线路（偏好不改变去重口径与条数）。"""
        on = self._urls(self._blocks(self.render())["CCTV13"])
        off = self._urls(self._blocks(self.render(export_prefer_sources=""))["CCTV13"])
        self.assertEqual(len(on), 1, on)
        self.assertEqual(len(off), 1, off)
        # 偏好在场时留下的必须是偏好源那条地址（它才是能回看的那个地址）
        self.assertEqual(on, [f"http://{YSP_HOST}/cctv13.m3u8"])

    # --- 硬约束：非偏好频道逐字节不变 ---

    def test_channels_without_prefer_source_are_byte_identical(self):
        """湖南卫视（没有 ysp-live 覆盖）：分块逐字节相同（EXTINF 行 + 地址行，含顺序）。"""
        on = self._blocks(self.render())["湖南卫视"]
        off = self._blocks(self.render(export_prefer_sources=""))["湖南卫视"]
        self.assertEqual(on, off)
        self.assertEqual(self._urls(on), ["http://fast-src.example/e1.m3u8",
                                          "http://mid-src.example/e2.m3u8",
                                          "http://mid-src.example/e3.m3u8"])
        # 非偏好频道连属性都不该有（回看属性只按 slug 列表挂，偏好不改这个口径）
        self.assertNotIn("catchup", "\n".join(on))

    def test_channel_order_and_grouping_unchanged(self):
        """偏好只改频道**内部**的线路顺序：频道序列与分组表头逐项相同。"""
        self.assertEqual(self._label_order(self.render()),
                         self._label_order(self.render(export_prefer_sources="")))
        self.assertEqual(self._group_headers(self.render()),
                         self._group_headers(self.render(export_prefer_sources="")))
        self.assertEqual(self._group_headers(self.render()), ["央视", "地方台"])

    # --- 空值 = 回到改动前 ---

    def test_empty_setting_is_byte_identical_to_legacy_order(self):
        """`EXPORT_PREFER_SOURCES=` 空值 → 与改动前的排序键渲染出的产物逐字节相同。"""
        self.assertEqual(self.render(export_prefer_sources=""), self.render_legacy())

    def test_slug_matching_is_exact_and_parses_lists(self):
        """多处空格/多个 slug/空串的解析口径；"ysp-live-2" 这种前缀相同的 slug 不算命中。"""
        self.assertEqual(static_export._slug_set(" ysp-live , other "),
                         frozenset({"ysp-live", "other"}))
        self.assertEqual(static_export._slug_set(""), frozenset())
        self.assertEqual(static_export._slug_set(" , , "), frozenset())
        with mock.patch.object(static_export, "settings",
                               dataclass_replace(_FIXED_SETTINGS,
                                                 export_prefer_sources=" ysp-live-2 ,")):
            urls = self._urls(self._blocks(static_export.build_m3u("multi", False, "", "", "", None))["CCTV1"])
        self.assertEqual(urls, ["http://fast-src.example/a2.m3u8",
                                "http://fast-src.example/a3.m3u8",
                                "http://mid-src.example/a4.m3u8"], urls)

    def test_line_order_key_puts_prefer_first_then_speed(self):
        """直接对排序键断言：第一段是权重，第二段起仍是原来的键。"""
        fast_other = static_export.Line(5000.0, 20, 0, 10,
                                        "http://fast-src.example/a2.m3u8", "", "", "", "",
                                        "fp-a2", False, "fast-src")
        slow_prefer = static_export.Line(800.0, 20, 0, 10,
                                         f"http://{YSP_HOST}/cctv1.m3u8", "", "", "", "",
                                         "fp-a1", False, PREFER)
        self.assertLess(static_export._line_order(slow_prefer),
                        static_export._line_order(fast_other))
        with mock.patch.object(static_export, "_prefer_slugs", lambda: frozenset()):
            self.assertLess(static_export._line_order(fast_other),
                            static_export._line_order(slow_prefer))


@unittest.skipIf(TestClient is None, "宿主机没有 FastAPI（容器内会跑这一组）")
class PreferSourceCacheKeyTests(unittest.TestCase):
    """配置进导出缓存键：改偏好后立刻拿到新产物，不会在 60 秒 TTL 里串味（M3U 与 TXT 都要）。"""

    def setUp(self):
        main_module.db = _ISOLATED  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(main_module, "db", db_module.db))
        main_module._EXPORT_CACHE.clear()
        self.addCleanup(main_module._EXPORT_CACHE.clear)
        for patch in (
            mock.patch.object(main_module, "settings", _FIXED_SETTINGS),
            mock.patch.object(static_export, "db", _ISOLATED),
            mock.patch.object(static_export, "template", mock.MagicMock(snapshot=lambda: None)),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.client = TestClient(main_module.app)

    def _settings(self, **overrides):
        return dataclass_replace(_FIXED_SETTINGS, **overrides)

    def test_m3u_cache_key_changes_with_prefer_config(self):
        first = self.client.get("/iptv")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self._line_for(first.text, "CCTV1")[1][0],
                         f"http://{YSP_HOST}/cctv1.m3u8")

        with mock.patch.object(main_module, "settings", self._settings(export_prefer_sources="")):
            with mock.patch.object(static_export, "settings", self._settings(export_prefer_sources="")):
                second = self.client.get("/iptv")
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(first.text, second.text, "改偏好源后必须重新渲染（配置要进缓存键）")
        self.assertEqual(self._line_for(second.text, "CCTV1")[1][0],
                         "http://fast-src.example/a2.m3u8")

    def test_txt_cache_key_changes_with_prefer_config(self):
        """TXT 也受偏好影响（它输出的就是选中线路的地址），键里同样要带。"""
        first = self.client.get("/txt")
        self.assertEqual(first.status_code, 200)
        self.assertIn(f"CCTV1,http://{YSP_HOST}/cctv1.m3u8", first.text)

        with mock.patch.object(main_module, "settings", self._settings(export_prefer_sources="")):
            with mock.patch.object(static_export, "settings", self._settings(export_prefer_sources="")):
                second = self.client.get("/txt")
        self.assertIn("CCTV1,http://fast-src.example/a2.m3u8", second.text)
        keys = [k for k in main_module._EXPORT_CACHE if k[0] == "txt"]
        self.assertEqual(len(keys), 2, f"两种偏好配置必须是两条独立缓存：{keys}")
        # 键的最后一段就是偏好源配置本身：两条键分别对应 'ysp-live' 与空串
        self.assertEqual({key[-1] for key in keys}, {"ysp-live", ""}, keys)

    @staticmethod
    def _line_for(m3u: str, label_suffix: str) -> tuple[str, list[str]]:
        lines = m3u.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("#EXTINF") and line.rsplit(",", 1)[-1].endswith(label_suffix):
                return line, [lines[i + 1]]
        raise AssertionError(f"产物里没有 {label_suffix}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
