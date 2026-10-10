"""v52 订阅里的回放（catch-up）属性：只给指定数据源的线路挂，可一键关闭。

隔离硬约束（同 test_export_group_filter.py）：只在 `/tmp` 下建库，`assert` 先于任何 SQL，
绝不碰生产库 `/data/iptv.sqlite3`；渲染本身是**纯查询**，这里顺带断言它不写库。

覆盖：
  * **按 source_slug 判定，不按 URL 猜**：slug 不在允许列表、但地址 host 与 ysp-live 完全
    相同的线路，一个属性都不许有（这是整个特性的正确性根基）；
  * 开关关闭 / 模板为空 → 产物与改动前**逐字节相同**（"不影响直播"的兜底承诺）；
  * 两种模板形状 → 两种 catchup 模式（`?…` = append、`http://…` = default），
    `{slug}` 在导出时替换、`{utc}`/`${end}` 等占位符原样留给播放器；
  * `{slug}` 推不出来时**放弃挂属性**（不生成取不到的回看地址）；
  * 属性不污染本项目自己的解析器：`parsers.parse_m3u` 读回的频道/线路与不带属性时完全一致；
  * `/txt` 一个字节都不带属性（TXT 格式表达不了）；
  * 静态导出 data/live.m3u 与 HTTP 出口同一份产物；
  * 配置进导出缓存键（HTTP 层用 TestClient 实测：改模板后立刻拿到新产物，不吃 60 秒旧缓存）。
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace as dataclass_replace
from pathlib import Path
from unittest import mock

from app import db as db_module
from app import parsers, static_export
from app.config import DEFAULT_CATCHUP_SOURCE
from app.config import settings as real_settings

try:  # 宿主机没有 FastAPI 时只跳过缓存键那一组（其余用例与容器无关）
    from fastapi.testclient import TestClient

    from app import main as main_module
except ModuleNotFoundError:  # pragma: no cover - 宿主机环境
    TestClient = None
    main_module = None

# 隔离硬约束：先在 /tmp 下建库并断言路径，再执行任何 SQL
_TMPDIR = tempfile.TemporaryDirectory(prefix="iptv-hub-catchup-", dir="/tmp")
_DB_PATH = Path(_TMPDIR.name) / "catchup.sqlite3"
assert str(_DB_PATH).startswith("/tmp/"), _DB_PATH
_ISOLATED = db_module.Database(_DB_PATH)
_ISOLATED.initialize()

# 渲染参数钉死（口径同其它导出测试）：容器 .env 里的 KEEP_REGION/MAX_LINES_*/MAIN_EXCLUDE_GROUP
# 不能让断言随环境漂移。回看的四个配置也一起钉住，避免跑测的机器上恰好设了环境变量。
_FIXED_SETTINGS = dataclass_replace(
    real_settings, keep_region="", export_hide_dead=True, export_dead_fail_count=3,
    max_lines_per_channel=5, max_lines_cctv=10, main_exclude_group="", epg_url="",
    public_base_url="", export_catchup=True, export_catchup_source_slugs="ysp-live",
    export_catchup_days=7, export_catchup_source=DEFAULT_CATCHUP_SOURCE,
)

YSP_HOST = "192.0.2.130:8767"


def _add_source(slug: str) -> None:
    with _ISOLATED.write() as conn:
        conn.execute(
            "INSERT INTO sources(slug, name, url, format, priority, source_group) "
            "VALUES (?, ?, ?, 'auto', 1, '') "
            "ON CONFLICT(slug) DO UPDATE SET url=excluded.url",
            (slug, slug, f"http://{slug}.example/x.m3u"),
        )


def _add_entry(slug: str, key: str, name: str, url: str, *, group_title: str = "央视") -> None:
    with _ISOLATED.write() as conn:
        conn.execute(
            "INSERT INTO source_entries(source_slug, entry_key, stream_fingerprint, channel_key, "
            "name, normalized_name, group_title, url, region, active, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'domestic', 1, '2026-10-05T00:00:00+00:00', "
            "'2026-10-05T00:00:00+00:00') "
            "ON CONFLICT(source_slug, entry_key) DO UPDATE SET url=excluded.url",
            (slug, key, f"fp-{key}", key, name, name, group_title, url),
        )


def _populate() -> None:
    """在**模块导入时**就把 fixture 写进隔离库。

    不能放在 TestCase.setUpClass 里：`CatchupCacheKeyTests` 按字母序先跑，那时 fixture
    还没建，HTTP 导出会拿回一份空产物（断言"必须有 catchup 属性"就会失败）。
    """
    with _ISOLATED.write() as conn:
        conn.execute("DELETE FROM source_entries")
        conn.execute("DELETE FROM stream_health")
        conn.execute("DELETE FROM sources")
    _add_source("ysp-live")
    # 关键对照源：slug 不同，但地址落在**同一个** host:port 上（同一条本地中继）。
    # 按 URL 猜的实现会在这里露馅。
    _add_source("other-local")
    _add_source("plain-src")
    _add_entry("ysp-live", "y1", "CCTV1", f"http://{YSP_HOST}/cctv1.m3u8")
    _add_entry("ysp-live", "y2", "CCTV5", f"http://{YSP_HOST}/cctv5.m3u8")
    _add_entry("other-local", "o1", "CCTV1", f"http://{YSP_HOST}/cctv9.m3u8")
    _add_entry("plain-src", "p1", "CCTV1", "http://plain.example/cctv1.m3u8")
    _add_entry("plain-src", "p2", "CCTV2", "http://plain.example/cctv2.m3u8", group_title="其他")


_populate()


class CatchupExportTestCase(unittest.TestCase):

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

    def render_m3u(self, **overrides) -> str:
        """按需覆盖回看配置渲染一份 M3U（其余设置沿用 _FIXED_SETTINGS）。"""
        cfg = dataclass_replace(_FIXED_SETTINGS, **overrides)
        with mock.patch.object(static_export, "settings", cfg):
            return static_export.build_m3u("multi", False, "", "", "", None)

    def render_txt(self, **overrides) -> str:
        cfg = dataclass_replace(_FIXED_SETTINGS, **overrides)
        with mock.patch.object(static_export, "settings", cfg):
            return static_export.build_txt("multi", False, "", "", None)

    @staticmethod
    def _extinf_by_url(m3u: str) -> dict[str, str]:
        """{线路地址: 它上面那条 #EXTINF 行}（导出结构固定：#EXTINF 后跟地址行）。"""
        lines = m3u.splitlines()
        pairs: dict[str, str] = {}
        for i, line in enumerate(lines):
            if line.startswith("#EXTINF") and i + 1 < len(lines):
                pairs[lines[i + 1]] = line
        return pairs

    # --- 按源判定 ---

    def test_only_allowed_source_lines_get_attributes(self):
        extinf = self._extinf_by_url(self.render_m3u())
        assert len(extinf) >= 4, extinf
        for url, line in extinf.items():
            if url.startswith(f"http://{YSP_HOST}/"):
                # ysp-live 的两条（cctv1/cctv5）必须有属性；同 host 的 other-local（cctv9）不许有
                if url.endswith(("/cctv1.m3u8", "/cctv5.m3u8")):
                    self.assertIn('catchup="append"', line, line)
                    self.assertIn('catchup-days="7"', line, line)
                    self.assertIn(f'catchup-source="{DEFAULT_CATCHUP_SOURCE}"', line, line)
                else:
                    self.assertNotIn("catchup", line, f"非 ysp-live 源不许有属性: {line}")
            else:
                self.assertNotIn("catchup", line, f"非 ysp-live 源不许有属性: {line}")

    def test_same_host_from_another_source_is_not_touched(self):
        """同 host:port 的 other-local 线路一个属性都没有 —— 判定按 source_slug，不按 URL。"""
        url = f"http://{YSP_HOST}/cctv9.m3u8"
        line = self._extinf_by_url(self.render_m3u())[url]
        self.assertEqual(line.count("catchup"), 0)
        # 反向确认这条线路确实在产物里、且地址与 ysp-live 的线路同 host（否则上面的断言是空的）
        self.assertIn(YSP_HOST, url)
        self.assertIn("CCTV1", line)

    def test_attributes_are_the_only_difference_for_other_sources(self):
        """开/关两版产物：除了 ysp-live 自己的 <EXTINF+地址> 对，其余行逐字节相同。"""
        on = self.render_m3u()
        off = self.render_m3u(export_catchup=False)
        self.assertNotEqual(on, off, "开启时至少 ysp-live 的行必须变化")

        ysp_urls = {f"http://{YSP_HOST}/cctv1.m3u8", f"http://{YSP_HOST}/cctv5.m3u8"}
        on_pairs = self._extinf_by_url(on)
        off_pairs = self._extinf_by_url(off)
        self.assertEqual(set(on_pairs), set(off_pairs), "线路集合本身不许变")
        # 非 ysp-live 线路：EXTINF 行与地址行都要逐字节相同
        self.assertEqual({u: line for u, line in on_pairs.items() if u not in ysp_urls},
                         {u: line for u, line in off_pairs.items() if u not in ysp_urls})
        # ysp-live 线路：只有它们变了（证明差异确实全部来自本特性）
        for url in ysp_urls:
            self.assertNotEqual(on_pairs[url], off_pairs[url], url)
        # 头部（#EXTM3U）与所有非 #EXTINF 行也一字未动
        self.assertEqual([l for l in on.splitlines() if not l.startswith("#EXTINF")],
                         [l for l in off.splitlines() if not l.startswith("#EXTINF")])

    # --- 开关与模板 ---

    def test_disabled_is_byte_identical_to_pre_change_output(self):
        """EXPORT_CATCHUP=0 → 产物里没有 catchup 字样，且与"模板为空"完全一致。"""
        off = self.render_m3u(export_catchup=False)
        self.assertNotIn("catchup", off)
        self.assertEqual(off, self.render_m3u(export_catchup_source=""))

    def test_full_url_template_switches_mode_to_default(self):
        """完整地址模板 → catchup="default"（Kodi 语义），且 {slug} 在导出时就地替换。"""
        tpl = f"http://{YSP_HOST}/{{slug}}.m3u8?start={{utc}}&end=${{end}}"
        m3u = self.render_m3u(export_catchup_source=tpl)
        extinf = self._extinf_by_url(m3u)
        line = extinf[f"http://{YSP_HOST}/cctv1.m3u8"]
        self.assertIn('catchup="default"', line)
        self.assertIn(f'catchup-source="http://{YSP_HOST}/cctv1.m3u8?start={{utc}}&end=${{end}}"', line)
        # 占位符必须原样留给播放器（导出时替换 = 把时间冻死在生成订阅的那一刻）
        self.assertNotIn("{slug}", line)

    def test_query_template_keeps_player_placeholders_verbatim(self):
        """查询串模板 → catchup="append"，播放器占位符一字不改。"""
        line = self._extinf_by_url(self.render_m3u())[f"http://{YSP_HOST}/cctv1.m3u8"]
        self.assertIn('catchup="append"', line)
        self.assertIn("${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}", line)

    def test_slug_template_without_derivable_slug_skips_attributes(self):
        """模板要 {slug}、但地址路径里推不出频道名 → 宁可不挂属性，也不生成取不到的回看地址。"""
        with _ISOLATED.write() as conn:
            conn.execute(
                "INSERT INTO source_entries(source_slug, entry_key, stream_fingerprint, channel_key, "
                "name, normalized_name, group_title, url, region, active, first_seen_at, last_seen_at) "
                "VALUES ('ysp-live', 'y9', 'fp-y9', 'y9', '怪地址台', '怪地址台', '其他', "
                "'http://host.example/?token=abc', 'domestic', 1, '2026-10-05T00:00:00+00:00', "
                "'2026-10-05T00:00:00+00:00')"
            )
        try:
            m3u = self.render_m3u(export_catchup_source=f"http://{YSP_HOST}/{{slug}}.m3u8")
            line = self._extinf_by_url(m3u)["http://host.example/?token=abc"]
            self.assertNotIn("catchup", line, line)
            # 同一份产物里，路径正常的 ysp-live 线路仍然挂上了属性（不是整体失效）
            self.assertIn("catchup=", self._extinf_by_url(m3u)[f"http://{YSP_HOST}/cctv1.m3u8"])
        finally:
            with _ISOLATED.write() as conn:
                conn.execute("DELETE FROM source_entries WHERE entry_key='y9'")

    def test_days_and_slug_list_are_configurable(self):
        m3u = self.render_m3u(export_catchup_days=3, export_catchup_source_slugs="other-local")
        extinf = self._extinf_by_url(m3u)
        self.assertIn('catchup-days="3"', extinf[f"http://{YSP_HOST}/cctv9.m3u8"])
        self.assertNotIn("catchup", extinf[f"http://{YSP_HOST}/cctv1.m3u8"])

    # --- 其它出口与解析器 ---

    def test_txt_has_no_catchup_and_is_byte_identical(self):
        """TXT 表达不了 catchup：开关开着也要与关闭时逐字节相同，且不含 catchup 字样。"""
        on = self.render_txt()
        self.assertNotIn("catchup", on)
        self.assertEqual(on, self.render_txt(export_catchup=False))

    def test_parser_reads_back_catchup_lines_unchanged(self):
        """本项目自己的解析器读回带属性的产物：频道/线路/tvg-id 与不带属性时完全一致。"""
        on = parsers.parse_m3u(self.render_m3u().encode("utf-8"))
        off = parsers.parse_m3u(self.render_m3u(export_catchup=False).encode("utf-8"))
        self.assertEqual(len(on), len(off))
        self.assertEqual([e.name for e in on], [e.name for e in off])
        self.assertEqual([e.url for e in on], [e.url for e in off])
        self.assertEqual([e.tvg_id for e in on], [e.tvg_id for e in off])
        self.assertEqual([e.group_title for e in on], [e.group_title for e in off])
        for entry in on:
            self.assertNotIn("catchup", entry.name)
            self.assertNotIn("catchup", entry.url)
            self.assertNotIn("catchup", entry.tvg_id)
            self.assertNotIn("catchup", entry.group_title)
        # 且属性确实进了解析出的频道名之外的地方：不污染、但也不该把标题吃掉
        self.assertIn("CCTV1", [e.name for e in on])

    def test_static_export_matches_http_export(self):
        """静态 data/live.m3u 与 HTTP 出口同一份产物（同样带属性）。"""
        out_dir = Path(_TMPDIR.name) / "static"
        out_dir.mkdir(exist_ok=True)
        cfg = dataclass_replace(_FIXED_SETTINGS, data_dir=out_dir,
                                main_exclude_group="", static_export=True)
        with mock.patch.object(static_export, "settings", cfg):
            result = static_export.write_static_exports()
            written = (out_dir / "live.m3u").read_text(encoding="utf-8")
        self.assertGreater(result.get("m3u", 0), 0)
        self.assertIn("catchup=", written)
        self.assertEqual(written, self.render_m3u())


@unittest.skipIf(TestClient is None, "宿主机没有 FastAPI（容器内会跑这一组）")
class CatchupCacheKeyTests(unittest.TestCase):
    """配置进导出缓存键：改模板后立刻拿到新产物，不会在 60 秒 TTL 里串味。"""

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

    def test_cache_key_changes_with_catchup_config(self):
        first = self.client.get("/iptv")
        self.assertEqual(first.status_code, 200)
        self.assertIn("catchup=", first.text)

        # 换个模板（同一进程、同一 TTL 窗口内）：缓存键必须跟着变，否则这里会拿到旧产物
        tpl = f"http://{YSP_HOST}/{{slug}}.m3u8?start={{utc}}&end=${{end}}"
        with mock.patch.object(main_module, "settings",
                               dataclass_replace(_FIXED_SETTINGS, export_catchup_source=tpl)):
            with mock.patch.object(static_export, "settings",
                                   dataclass_replace(_FIXED_SETTINGS, export_catchup_source=tpl)):
                second = self.client.get("/iptv")
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(first.text, second.text, "改模板后必须重新渲染（配置要进缓存键）")
        self.assertIn(f'catchup-source="http://{YSP_HOST}/cctv1.m3u8?start={{utc}}&end=${{end}}"',
                      second.text)
        self.assertIn("catchup=", second.text)

    def test_disabled_config_served_without_attributes(self):
        with mock.patch.object(main_module, "settings",
                               dataclass_replace(_FIXED_SETTINGS, export_catchup=False)):
            with mock.patch.object(static_export, "settings",
                                   dataclass_replace(_FIXED_SETTINGS, export_catchup=False)):
                response = self.client.get("/playlist.m3u")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("catchup", response.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
