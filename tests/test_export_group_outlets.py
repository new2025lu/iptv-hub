"""v43 独立订阅出口（/discovered.m3u、/discovered.txt）与 group/exclude_group 的 HTTP 层测试。

**需要 FastAPI**（导入 app.main）：容器内 `python -m unittest discover -s tests` 会跑全部用例；
宿主机没有装 FastAPI 时这一组整体 skip（`app.main` 导入失败），其余测试不受影响。

隔离硬约束：只在 `/tmp` 下建库（先 assert 再动 SQL），并把 `app.main` / `app.static_export`
里的 `db` 全局换成它；TestClient **不进入 lifespan**，所以不会触发任何启动期写库动作，
生产库 `/data/iptv.sqlite3` 与 `data/live.m3u|txt` 完全不被触碰。

覆盖点：
  * `group=` / `exclude_group=` 的取值校验（不存在 → 400、超长 → 400、strip）；
  * 固定出口 `/discovered.m3u|txt` 与 `/iptv|txt?group=发现源` **逐字节相同**；
  * 新参数**进导出缓存键**：两种口径内容不同且各自命中自己的缓存（不会串味）；
  * 这些出口**不需要令牌**（同一进程内把 admin_token 设上，/api/sources 401、订阅仍 200）。
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace as dataclass_replace
from pathlib import Path
from unittest import mock

from app import db as db_module
from app import static_export
from app.config import settings as real_settings

try:  # 宿主机没有 FastAPI 时只跳过这一组（其余用例与容器无关）
    from fastapi.testclient import TestClient

    from app import main as main_module
except ModuleNotFoundError:  # pragma: no cover - 宿主机环境
    TestClient = None
    main_module = None


# 隔离硬约束：先在 /tmp 下建库并断言路径，再执行任何 SQL
_TMPDIR = tempfile.TemporaryDirectory(prefix="iptv-hub-outlet-", dir="/tmp")
_DB_PATH = Path(_TMPDIR.name) / "outlet.sqlite3"
assert str(_DB_PATH).startswith("/tmp/"), _DB_PATH
_ISOLATED = db_module.Database(_DB_PATH)
_ISOLATED.initialize()

# 固定渲染参数 + 关掉 EPG（x-tvg-url）：产物只由 fixture 决定，断言才可能与字节挂钩。
# `main_exclude_group` 必须显式钉成空串：容器 .env 里配了 `MAIN_EXCLUDE_GROUP=发现源`，
# 不钉住的话"默认导出 = 全量"这组断言在容器里会随配置漂移（专门的用例在下面单独覆盖配置生效）。
_FIXED_SETTINGS = dataclass_replace(
    real_settings, keep_region="", export_hide_dead=True, export_dead_fail_count=3,
    max_lines_per_channel=5, max_lines_cctv=10, epg_url="", public_base_url="",
    main_exclude_group="",
)

DISCOVERED = "发现源"
OTHER = "别的组"
UNKNOWN = "不存在的组"


def _add_source(slug: str, source_group: str) -> None:
    with _ISOLATED.write() as conn:
        conn.execute(
            "INSERT INTO sources(slug, name, url, format, priority, source_group) "
            "VALUES (?, ?, ?, 'auto', 1, ?) ON CONFLICT(slug) DO UPDATE SET source_group=excluded.source_group",
            (slug, slug, f"http://{slug}.example/x.m3u", source_group),
        )


def _add_entry(slug: str, key: str, name: str, url: str, *, group_title: str = "央视",
               region: str = "domestic") -> None:
    with _ISOLATED.write() as conn:
        conn.execute(
            "INSERT INTO source_entries(source_slug, entry_key, stream_fingerprint, channel_key, "
            "name, normalized_name, group_title, url, region, active, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, '2026-10-05T00:00:00+00:00', "
            "'2026-10-05T00:00:00+00:00') "
            "ON CONFLICT(source_slug, entry_key) DO UPDATE SET url=excluded.url",
            (slug, key, f"fp-{key}", key, name, name, group_title, url, region),
        )


class OutletTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with _ISOLATED.write() as conn:
            conn.execute("DELETE FROM source_entries")
            conn.execute("DELETE FROM stream_health")
            conn.execute("DELETE FROM sources")
        _add_source("g-disc", DISCOVERED)
        _add_source("g-misc", OTHER)
        _add_source("g-plain", "")
        _add_entry("g-disc", "d1", "CCTV1", "http://d/1")
        _add_entry("g-disc", "d2", "CCTV2", "http://d/2")
        _add_entry("g-misc", "m1", "CCTV1", "http://m/1")
        _add_entry("g-plain", "p1", "CCTV1", "http://p/1")

    def setUp(self):
        self._patches = [
            mock.patch.object(main_module, "db", _ISOLATED),
            mock.patch.object(main_module, "settings", _FIXED_SETTINGS),
            mock.patch.object(static_export, "db", _ISOLATED),
            mock.patch.object(static_export, "settings", _FIXED_SETTINGS),
            mock.patch.object(static_export.template, "snapshot", lambda: None),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)
        # 导出缓存是模块级全局：每个用例从干净状态开始，避免上一条用例的键干扰计数
        main_module._EXPORT_CACHE.clear()
        self.addCleanup(main_module._EXPORT_CACHE.clear)
        # 不进入 lifespan：启动期会 db.initialize()/排队同步，测试只走 ASGI 请求
        self.client = TestClient(main_module.app)

    @staticmethod
    def urls(text: str) -> list[str]:
        return [line for line in text.splitlines() if line and not line.startswith("#")]

    @staticmethod
    def txt_urls(text: str) -> list[str]:
        return [line.split(",", 1)[1] for line in text.splitlines()
                if line and not line.endswith(",#genre#")]

    @staticmethod
    def md5(text: bytes) -> str:
        return hashlib.md5(text).hexdigest()


@unittest.skipIf(TestClient is None, "宿主机没有 FastAPI：这一组在容器内运行")
class GroupParamHttpTests(OutletTestCase):
    def test_group_param_keeps_only_that_group(self):
        r = self.client.get("/iptv", params={"group": DISCOVERED})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(sorted(self.urls(r.text)), ["http://d/1", "http://d/2"])

    def test_exclude_group_param_drops_that_group(self):
        r = self.client.get("/iptv", params={"exclude_group": DISCOVERED})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(sorted(self.urls(r.text)), ["http://m/1", "http://p/1"])

    def test_group_and_exclude_group_stack(self):
        r = self.client.get("/iptv", params={"group": DISCOVERED, "exclude_group": OTHER})
        self.assertEqual(sorted(self.urls(r.text)), ["http://d/1", "http://d/2"])
        r2 = self.client.get("/iptv", params={"group": DISCOVERED, "exclude_group": DISCOVERED})
        self.assertEqual(self.urls(r2.text), [])

    def test_playlist_alias_supports_the_same_params(self):
        a = self.client.get("/playlist.m3u", params={"exclude_group": DISCOVERED})
        b = self.client.get("/iptv", params={"exclude_group": DISCOVERED})
        self.assertEqual(a.status_code, 200)
        self.assertEqual(self.md5(a.content), self.md5(b.content))

    def test_txt_supports_the_same_params(self):
        r = self.client.get("/txt", params={"group": DISCOVERED})
        self.assertEqual(sorted(self.txt_urls(r.text)), ["http://d/1", "http://d/2"])
        r2 = self.client.get("/txt", params={"exclude_group": DISCOVERED})
        self.assertEqual(sorted(self.txt_urls(r2.text)), ["http://m/1", "http://p/1"])

    def test_group_value_is_stripped(self):
        r = self.client.get("/iptv", params={"group": f"  {DISCOVERED}  "})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(sorted(self.urls(r.text)), ["http://d/1", "http://d/2"])

    def test_unknown_group_is_400(self):
        for field in ("group", "exclude_group"):
            r = self.client.get("/iptv", params={field: UNKNOWN})
            self.assertEqual(r.status_code, 400, f"{field}= 不存在的分组必须 400")
            self.assertIn("不存在", r.json()["detail"])

    def test_too_long_group_is_400(self):
        for field in ("group", "exclude_group"):
            r = self.client.get("/iptv", params={field: "x" * 65})
            self.assertEqual(r.status_code, 400)
            self.assertIn("64", r.json()["detail"])
        # 恰好 64 字符：长度过关，但库里不存在 → 仍是 400（键空间收敛到真实分组）
        self.assertEqual(self.client.get("/iptv", params={"group": "x" * 64}).status_code, 400)

    def test_headers_expose_effective_group_params(self):
        r = self.client.get("/iptv", params={"group": DISCOVERED, "exclude_group": OTHER})
        # 中文只能按 URL 编码进响应头（否则 Starlette 编码 latin-1 时会 500）
        self.assertEqual(r.headers["x-export-group"], "%E5%8F%91%E7%8E%B0%E6%BA%90")
        self.assertEqual(r.headers["x-export-exclude-group"], "%E5%88%AB%E7%9A%84%E7%BB%84")
        # 不带参数时是空串，一眼能看出"这次没按分组过滤"
        plain = self.client.get("/iptv")
        self.assertEqual(plain.headers["x-export-group"], "")
        self.assertEqual(plain.headers["x-export-exclude-group"], "")

    def test_default_request_has_no_group_key_components(self):
        """不传参数时缓存键里的分组位置是空串：键结构与改动前等价，不产生额外键。"""
        self.client.get("/iptv")
        expected = ("m3u", "multi", False, "", "", "", "", main_module._export_dead_key(),
                    main_module._export_catchup_key(),  # v52：M3U 键多出回看配置这一段
                    main_module._export_prefer_key())  # v53：再多一段偏好源配置
        self.assertIn(expected, main_module._EXPORT_CACHE)

    def test_group_and_exclude_group_are_separate_cache_entries(self):
        """新参数进缓存键的三条证据：内容不同 / 键不同 / 60s 内各自命中缓存。"""
        with mock.patch.object(static_export, "build_txt", wraps=static_export.build_txt) as spy:
            only = self.client.get("/txt", params={"group": DISCOVERED})
            only_again = self.client.get("/txt", params={"group": DISCOVERED})
            self.assertEqual(spy.call_count, 1, "同参数第二次请求必须命中缓存，不得再次全量渲染")
            self.assertEqual(self.md5(only.content), self.md5(only_again.content))

            without = self.client.get("/txt", params={"exclude_group": DISCOVERED})
            self.assertEqual(spy.call_count, 2, "换了分组参数 = 新缓存键，必须重新渲染")
            self.assertEqual(self.md5(without.content), self.md5(
                self.client.get("/txt", params={"exclude_group": DISCOVERED}).content))
            self.assertEqual(spy.call_count, 2, "新键也要能命中自己的缓存")

        self.assertNotEqual(self.md5(only.content), self.md5(without.content), "两种口径的内容不得相同")
        keys = [k for k in main_module._EXPORT_CACHE if k[0] == "txt"]
        self.assertEqual(len(keys), 2, f"两种口径必须是两条独立缓存：{keys}")
        # v53：TXT 键也多出偏好源配置（偏好改的是"选中哪几条线路"，TXT 输出的正是这些地址）
        self.assertIn(("txt", "multi", False, "", "", DISCOVERED, "",
                       main_module._export_dead_key(), main_module._export_prefer_key()), keys)
        self.assertIn(("txt", "multi", False, "", "", "", DISCOVERED,
                       main_module._export_dead_key(), main_module._export_prefer_key()), keys)

    def test_etag_differs_between_group_and_exclude(self):
        a = self.client.get("/iptv", params={"group": DISCOVERED})
        b = self.client.get("/iptv", params={"exclude_group": DISCOVERED})
        self.assertNotEqual(a.headers["etag"], b.headers["etag"])
        # 同参数的 If-None-Match 依旧 304（缓存/条件请求没被新参数破坏）
        again = self.client.get("/iptv", params={"group": DISCOVERED},
                                headers={"If-None-Match": a.headers["etag"]})
        self.assertEqual(again.status_code, 304)


@unittest.skipIf(TestClient is None, "宿主机没有 FastAPI：这一组在容器内运行")
class DiscoveredOutletTests(OutletTestCase):
    def test_discovered_m3u_equals_group_param_byte_for_byte(self):
        outlet = self.client.get("/discovered.m3u")
        explicit = self.client.get("/iptv", params={"group": DISCOVERED})
        self.assertEqual(outlet.status_code, 200)
        self.assertEqual(self.md5(outlet.content), self.md5(explicit.content))
        self.assertEqual(sorted(self.urls(outlet.text)), ["http://d/1", "http://d/2"])
        self.assertEqual(outlet.headers["x-export-group"], "%E5%8F%91%E7%8E%B0%E6%BA%90")

    def test_discovered_txt_equals_group_param_byte_for_byte(self):
        outlet = self.client.get("/discovered.txt")
        explicit = self.client.get("/txt", params={"group": DISCOVERED})
        self.assertEqual(outlet.status_code, 200)
        self.assertEqual(self.md5(outlet.content), self.md5(explicit.content))
        self.assertEqual(sorted(self.txt_urls(outlet.text)), ["http://d/1", "http://d/2"])

    def test_discovered_outlet_shares_cache_entry_with_explicit_param(self):
        """固定出口与显式参数命中同一条缓存（键里的分组值都是 '发现源'）。"""
        with mock.patch.object(static_export, "build_m3u", wraps=static_export.build_m3u) as spy:
            self.client.get("/discovered.m3u")
            self.client.get("/iptv", params={"group": DISCOVERED})
            self.assertEqual(spy.call_count, 1, "两次请求应复用同一份渲染结果")

    def test_discovered_outlet_declares_only_its_own_group(self):
        """只输出发现源：'别的组' 与未分组源的线路一条都不能出现。"""
        text = self.client.get("/discovered.m3u").text
        self.assertIn("http://d/1", text)
        self.assertNotIn("http://m/1", text)
        self.assertNotIn("http://p/1", text)


@unittest.skipIf(TestClient is None, "宿主机没有 FastAPI：这一组在容器内运行")
class EmptyDiscoverGroupTests(unittest.TestCase):
    """设计取舍的证据：分组名被删干净后，固定出口退化成空订阅，而不是 400。"""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(prefix="iptv-hub-outlet-empty-", dir="/tmp")
        cls._path = Path(cls._tmpdir.name) / "empty.sqlite3"
        assert str(cls._path).startswith("/tmp/"), cls._path
        cls._db = db_module.Database(cls._path)
        cls._db.initialize()
        with cls._db.write() as conn:
            conn.execute("DELETE FROM source_entries")
            conn.execute("DELETE FROM sources")
        _add_source_in(cls._db, "g-plain", "")
        _add_entry_in(cls._db, "g-plain", "p1", "CCTV1", "http://p/1")

    def setUp(self):
        self._patches = [
            mock.patch.object(main_module, "db", self._db),
            mock.patch.object(main_module, "settings", _FIXED_SETTINGS),
            mock.patch.object(static_export, "db", self._db),
            mock.patch.object(static_export, "settings", _FIXED_SETTINGS),
            mock.patch.object(static_export.template, "snapshot", lambda: None),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)
        main_module._EXPORT_CACHE.clear()
        self.addCleanup(main_module._EXPORT_CACHE.clear)
        self.client = TestClient(main_module.app)

    def test_fixed_outlet_degrades_to_empty_while_param_returns_400(self):
        outlet = self.client.get("/discovered.m3u")
        self.assertEqual(outlet.status_code, 200, "固定出口在分组消失后应退化成空订阅")
        self.assertEqual([line for line in outlet.text.splitlines() if not line.startswith("#")], [])
        explicit = self.client.get("/iptv", params={"group": DISCOVERED})
        self.assertEqual(explicit.status_code, 400, "显式参数传不存在的分组仍然 400")


@unittest.skipIf(TestClient is None, "宿主机没有 FastAPI：这一组在容器内运行")
class ExportOutletAuthTests(OutletTestCase):
    """订阅出口是给播放器的：**必须免令牌**（与 /iptv 改动前一致）。"""

    def setUp(self):
        super().setUp()
        # 本组用例把管理令牌设上，模拟"已启用鉴权"的部署
        self._token_patch = mock.patch.object(
            main_module, "settings",
            dataclass_replace(_FIXED_SETTINGS, admin_token="unit-test-token"),
        )
        self._token_patch.start()
        self.addCleanup(self._token_patch.stop)

    def test_subscription_outlets_are_reachable_without_token(self):
        for path, params in (("/iptv", None), ("/playlist.m3u", None), ("/txt", None),
                             ("/discovered.m3u", None), ("/discovered.txt", None),
                             ("/iptv", {"group": DISCOVERED}),
                             ("/iptv", {"exclude_group": DISCOVERED}),
                             ("/txt", {"exclude_group": DISCOVERED})):
            r = self.client.get(path, params=params)
            self.assertEqual(r.status_code, 200, f"{path}{params or ''} 必须免令牌可读")

    def test_admin_endpoints_still_require_the_token(self):
        self.assertEqual(self.client.get("/api/sources").status_code, 401)
        ok = self.client.get("/api/sources", headers={"Authorization": "Bearer unit-test-token"})
        self.assertEqual(ok.status_code, 200)


@unittest.skipIf(TestClient is None, "宿主机没有 FastAPI：这一组在容器内运行")
class MainExcludeGroupTests(OutletTestCase):
    """MAIN_EXCLUDE_GROUP：默认导出自动排除该组，显式参数覆盖，固定出口不受影响。"""

    def setUp(self):
        super().setUp()
        configured = dataclass_replace(_FIXED_SETTINGS, main_exclude_group=DISCOVERED)
        # main 读 settings.main_exclude_group（/api/meta 与配置来源），static_export 读它做过滤
        for target in (main_module, static_export):
            patch = mock.patch.object(target, "settings", configured)
            patch.start()
            self.addCleanup(patch.stop)

    def test_default_export_excludes_configured_group(self):
        default = self.client.get("/iptv")
        explicit = self.client.get("/iptv", params={"exclude_group": DISCOVERED})
        self.assertEqual(sorted(self.urls(default.text)), ["http://m/1", "http://p/1"])
        self.assertEqual(self.md5(default.content), self.md5(explicit.content),
                         "默认导出必须与显式 ?exclude_group= 逐字节相同")
        self.assertEqual(default.headers["x-export-exclude-group"], "%E5%8F%91%E7%8E%B0%E6%BA%90")

    def test_playlist_and_txt_use_the_same_default(self):
        alias = self.client.get("/playlist.m3u")
        self.assertEqual(sorted(self.urls(alias.text)), ["http://m/1", "http://p/1"])
        txt = self.client.get("/txt")
        self.assertEqual(sorted(self.txt_urls(txt.text)), ["http://m/1", "http://p/1"])

    def test_discovered_outlets_ignore_the_config(self):
        """固定出口的分组是服务端常量，配置不该把它排除掉（否则出口就成空的了）。"""
        m3u = self.client.get("/discovered.m3u")
        self.assertEqual(sorted(self.urls(m3u.text)), ["http://d/1", "http://d/2"])
        self.assertEqual(m3u.headers["x-export-group"], "%E5%8F%91%E7%8E%B0%E6%BA%90")
        self.assertEqual(m3u.headers["x-export-exclude-group"], "")
        self.assertEqual(sorted(self.txt_urls(self.client.get("/discovered.txt").text)),
                         ["http://d/1", "http://d/2"])

    def test_explicit_empty_params_win_and_restore_full_list(self):
        full = self.client.get("/iptv", params={"group": ""})
        self.assertEqual(sorted(self.urls(full.text)),
                         ["http://d/1", "http://d/2", "http://m/1", "http://p/1"])
        empty_exclude = self.client.get("/iptv", params={"exclude_group": ""})
        self.assertEqual(sorted(self.urls(empty_exclude.text)),
                         ["http://d/1", "http://d/2", "http://m/1", "http://p/1"])
        self.assertEqual(empty_exclude.headers["x-export-exclude-group"], "")

    def test_explicit_param_replaces_the_configured_group(self):
        """显式传别的分组 = 换一组排除，配置的「发现源」不再排除。"""
        other = self.client.get("/iptv", params={"exclude_group": OTHER})
        self.assertEqual(sorted(self.urls(other.text)), ["http://d/1", "http://d/2", "http://p/1"])
        only = self.client.get("/iptv", params={"group": OTHER})
        self.assertEqual(sorted(self.urls(only.text)), ["http://m/1"])

    def test_cache_key_carries_the_effective_exclude_group(self):
        """配置生效的默认导出与「显式空参数」必须落在两条不同的缓存键上（否则 60s 内串味）。"""
        self.client.get("/iptv")
        self.client.get("/iptv", params={"group": ""})
        keys = [k for k in main_module._EXPORT_CACHE if k[0] == "m3u"]
        self.assertEqual(len(keys), 2, f"应当恰好两条键：{keys}")
        self.assertIn(("m3u", "multi", False, "", "", "", DISCOVERED,
                       main_module._export_dead_key(),
                       main_module._export_catchup_key(),  # v52：M3U 键多出回看配置
                       main_module._export_prefer_key()), keys)  # v53：再多一段偏好源配置
        self.assertIn(("m3u", "multi", False, "", "", "", "",
                       main_module._export_dead_key(),
                       main_module._export_catchup_key(),
                       main_module._export_prefer_key()), keys)

    def test_meta_exposes_the_config(self):
        meta = self.client.get("/api/meta").json()
        self.assertEqual(meta["main_exclude_group"], DISCOVERED)
        self.assertEqual(meta["main_exclude_group_configured"], DISCOVERED)


@unittest.skipIf(TestClient is None, "宿主机没有 FastAPI：这一组在容器内运行")
class MainExcludeGroupInvalidTests(OutletTestCase):
    """配置写错（分组不存在）：忽略配置，主订阅退化成不过滤，**绝不 400**。"""

    def setUp(self):
        super().setUp()
        configured = dataclass_replace(_FIXED_SETTINGS, main_exclude_group=UNKNOWN)
        for target in (main_module, static_export):
            patch = mock.patch.object(target, "settings", configured)
            patch.start()
            self.addCleanup(patch.stop)

    def test_invalid_configured_group_is_ignored(self):
        r = self.client.get("/iptv")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(sorted(self.urls(r.text)),
                         ["http://d/1", "http://d/2", "http://m/1", "http://p/1"])
        self.assertEqual(r.headers["x-export-exclude-group"], "")

    def test_meta_shows_configured_but_not_effective(self):
        meta = self.client.get("/api/meta").json()
        self.assertEqual(meta["main_exclude_group"], "", "无效配置的生效值必须是空")
        self.assertEqual(meta["main_exclude_group_configured"], UNKNOWN,
                         "原文要照实回显，用户才能看出'配了但没生效'")


def _add_source_in(db: db_module.Database, slug: str, source_group: str) -> None:
    with db.write() as conn:
        conn.execute(
            "INSERT INTO sources(slug, name, url, format, priority, source_group) "
            "VALUES (?, ?, ?, 'auto', 1, ?)",
            (slug, slug, f"http://{slug}.example/x.m3u", source_group),
        )


def _add_entry_in(db: db_module.Database, slug: str, key: str, name: str, url: str) -> None:
    with db.write() as conn:
        conn.execute(
            "INSERT INTO source_entries(source_slug, entry_key, stream_fingerprint, channel_key, "
            "name, normalized_name, group_title, url, region, active, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, '央视', ?, 'domestic', 1, '2026-10-05T00:00:00+00:00', "
            "'2026-10-05T00:00:00+00:00')",
            (slug, key, f"fp-{key}", key, name, name, url),
        )


if __name__ == "__main__":
    unittest.main()
