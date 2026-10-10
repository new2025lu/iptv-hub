"""v50：`POST /api/prune/restore` 定向恢复的回归测试。

背景（真实缺陷）：v50 之前这个接口只有"全恢复"一种语义 ——
`UPDATE source_entries SET active=1 WHERE active=0`。线上 43380 条 inactive 里
绝大多数是「源站已不再列出」或「KEEP_REGION 区域策略剔除」的行，按一次就把它们
全部拉回订阅与测速队列；清理类操作（v48 长期失效清理、v50 重复死频道清理）想只
回退自己刚清的那几十条，除了整包还原没有别的办法。

v50 增加三个**可选**过滤器 `fingerprint=` / `channel_key=` / `source=`：
  * 可重复出现（`?fingerprint=a&fingerprint=b`），也可逗号分隔（`?fingerprint=a,b`）；
  * 同一维度内是 OR，跨维度是 AND；
  * **一个参数都不传时，语义与响应键必须与 v50 之前完全一致**（恢复全部 inactive，
    仍遵守 KEEP_REGION）；
  * 空 token / 整体未匹配到任何条目 → 400（静默 0 会让人以为"已经恢复过了"）；
  * 合法但当前都是 active=1（没东西可恢复）→ 200 + restored_entries=0。

需要 FastAPI（导入 app.main）：容器内 `PYTHONPATH=. python -m unittest discover -s tests`
跑全部用例；宿主机没装 FastAPI 时这一组整体 skip，其余测试不受影响。

隔离硬约束：只在 `/tmp` 下建库（先 assert 再动任何 SQL），并把 `app.main` 里的 `db`
全局换成它；TestClient **不进入 lifespan**，不会触发启动期写库。生产库
`data/iptv.sqlite3` 与本组测试无关。
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace as dataclass_replace
from pathlib import Path
from unittest import mock

from app import db as db_module
from app.config import settings as real_settings
from app.revision import current_revision

try:  # 宿主机没有 FastAPI 时只跳过这一组（其余用例与容器无关）
    from fastapi.testclient import TestClient

    from app import main as main_module
except ModuleNotFoundError:  # pragma: no cover - 宿主机环境
    TestClient = None
    main_module = None


# 隔离硬约束：先在 /tmp 下建库并断言路径，再执行任何 SQL
_TMPDIR = tempfile.TemporaryDirectory(prefix="iptv-hub-restore-", dir="/tmp")
_DB_PATH = Path(_TMPDIR.name) / "restore.sqlite3"
assert str(_DB_PATH).startswith("/tmp/"), _DB_PATH
assert "/data/iptv.sqlite3" not in str(_DB_PATH), _DB_PATH
_ISOLATED = db_module.Database(_DB_PATH)
_ISOLATED.initialize()

ADMIN_TOKEN = "test-admin-token-v50"
# 断言具体恢复条数的用例一律显式钉死 keep_region：容器里的 KEEP_REGION=domestic，
# 跟着真实配置走会让「不传参数恢复全部」这类用例随部署环境变红。
_AUTH_SETTINGS = dataclass_replace(real_settings, admin_token=ADMIN_TOKEN, keep_region="")
_OPEN_SETTINGS = dataclass_replace(real_settings, admin_token="", keep_region="")
# 不带区域策略 / 带区域策略两套（线上 KEEP_REGION=domestic，所以两条路都必须测）
_NO_REGION = dataclass_replace(real_settings, admin_token="", keep_region="")
_KEEP_DOMESTIC = dataclass_replace(real_settings, admin_token="", keep_region="domestic")

TS = "2026-10-05T02:00:00+00:00"

# (source_slug, entry_key, channel_key, region, active) —— 指纹固定为 fp-<entry_key>
SEED = (
    ("s1", "e1", "ck-a", "domestic", 0),
    ("s2", "e2", "ck-a", "domestic", 0),
    ("s3", "e3", "ck-b", "domestic", 0),
    ("s1", "e4", "ck-b", "international", 0),
    ("s2", "e5", "ck-b", "domestic", 1),
    ("s3", "e6", "ck-c", "domestic", 0),
)


def _seed() -> None:
    with _ISOLATED.write() as conn:
        conn.execute("DELETE FROM source_entries")
        conn.execute("DELETE FROM stream_health")
        conn.execute("DELETE FROM sources")
        for slug, name, priority in (("s1", "源一", 1), ("s2", "源二", 2), ("s3", "源三", 3)):
            conn.execute(
                "INSERT INTO sources(slug, name, url, format, priority, source_group) "
                "VALUES (?, ?, ?, 'auto', ?, '')",
                (slug, name, f"http://{slug}.example/x.m3u", priority),
            )
        for slug, entry_key, channel_key, region, active in SEED:
            conn.execute(
                "INSERT INTO source_entries(source_slug, entry_key, stream_fingerprint, channel_key, "
                "name, normalized_name, group_title, url, region, active, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, '测试', ?, ?, ?, ?, ?)",
                (slug, entry_key, f"fp-{entry_key}", channel_key, f"频道{entry_key}",
                 f"频道{entry_key}", f"http://{slug}.example/{entry_key}", region, active, TS, TS),
            )


def _active(fingerprint: str) -> int:
    row = _ISOLATED.row("SELECT active FROM source_entries WHERE stream_fingerprint=?", (fingerprint,))
    return int(row["active"])


def _inactive_count() -> int:
    return int(_ISOLATED.row("SELECT COUNT(*) AS n FROM source_entries WHERE active=0")["n"])


class RestoreTestCase(unittest.TestCase):
    """每条用例前重置种子数据 —— 恢复是写操作，用例之间不能共享状态。"""

    def setUp(self):
        _seed()
        self._patches = [mock.patch.object(main_module, "db", _ISOLATED)]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    def patch_settings(self, value):
        patch = mock.patch.object(main_module, "settings", value)
        patch.start()
        self.addCleanup(patch.stop)

    def call(self, **kwargs):
        """直接调用端点函数（不经 HTTP）：缺省参数与 FastAPI 的 None 默认值一致。"""
        params = {"fingerprint": None, "channel_key": None, "source": None}
        params.update(kwargs)
        return main_module.restore_streams(**params)


@unittest.skipIf(TestClient is None, "宿主机没有 FastAPI：这一组在容器内运行")
class RestoreTargetedTests(RestoreTestCase):
    """定向恢复：只动点名的行。"""

    def test_by_fingerprint_only_touches_named_row(self):
        self.patch_settings(_NO_REGION)
        before = _inactive_count()
        result = self.call(fingerprint=["fp-e1"])
        self.assertEqual(result["restored_entries"], 1)
        self.assertEqual(result["matched_entries"], 1)
        self.assertEqual(result["filters"], {"fingerprint": ["fp-e1"]})
        self.assertEqual(_active("fp-e1"), 1)
        # 其余 4 条 inactive 一行都不许动
        self.assertEqual(_inactive_count(), before - 1)
        for fp in ("fp-e2", "fp-e3", "fp-e4", "fp-e6"):
            self.assertEqual(_active(fp), 0, fp)

    def test_by_channel_key_restores_that_channel(self):
        self.patch_settings(_NO_REGION)
        result = self.call(channel_key=["ck-a"])
        self.assertEqual(result["restored_entries"], 2)
        self.assertEqual((_active("fp-e1"), _active("fp-e2")), (1, 1))
        self.assertEqual(_active("fp-e3"), 0)

    def test_by_source_restores_that_source(self):
        self.patch_settings(_NO_REGION)
        result = self.call(source=["s3"])
        self.assertEqual(result["restored_entries"], 2)   # e3 与 e6
        self.assertEqual((_active("fp-e3"), _active("fp-e6")), (1, 1))
        self.assertEqual(_active("fp-e1"), 0)

    def test_repeated_and_comma_separated_are_equivalent(self):
        self.patch_settings(_NO_REGION)
        repeated = self.call(fingerprint=["fp-e1", "fp-e2"])
        self.assertEqual(repeated["restored_entries"], 2)
        _seed()   # 复位后用逗号写法再跑一遍，结果必须一致
        comma = self.call(fingerprint=["fp-e1,fp-e2"])
        self.assertEqual(comma["restored_entries"], 2)
        self.assertEqual(comma["filters"], {"fingerprint": ["fp-e1", "fp-e2"]})

    def test_duplicate_tokens_are_deduplicated(self):
        self.patch_settings(_NO_REGION)
        result = self.call(fingerprint=["fp-e1", "fp-e1,fp-e1"])
        self.assertEqual(result["filters"]["fingerprint"], ["fp-e1"])
        self.assertEqual(result["restored_entries"], 1)

    def test_dimensions_are_anded_within_dimension_ored(self):
        self.patch_settings(_NO_REGION)
        # fingerprint 命中 e1/e2，source 命中 s2 → 只有 e2
        result = self.call(fingerprint=["fp-e1", "fp-e2"], source=["s2"])
        self.assertEqual(result["restored_entries"], 1)
        self.assertEqual(_active("fp-e2"), 1)
        self.assertEqual(_active("fp-e1"), 0)

    def test_already_active_target_is_not_an_error(self):
        self.patch_settings(_NO_REGION)
        result = self.call(fingerprint=["fp-e5"])   # 唯一一条本来就 active 的
        self.assertEqual(result["restored_entries"], 0)
        self.assertEqual(result["matched_entries"], 1)


@unittest.skipIf(TestClient is None, "宿主机没有 FastAPI：这一组在容器内运行")
class RestoreBackwardCompatTests(RestoreTestCase):
    """不传参数 = v50 之前的旧行为，逐字节保持。"""

    def test_no_params_restores_everything_inactive(self):
        self.patch_settings(_NO_REGION)
        result = self.call()
        self.assertEqual(result, {"restored_entries": 5})
        self.assertEqual(_inactive_count(), 0)
        self.assertEqual(_active("fp-e5"), 1)

    def test_no_params_response_keys_unchanged(self):
        self.patch_settings(_NO_REGION)
        self.assertEqual(set(self.call()), {"restored_entries"})

    def test_no_params_respects_keep_region(self):
        self.patch_settings(_KEEP_DOMESTIC)
        result = self.call()
        # 4 条 domestic inactive 恢复，international 的 e4 留下
        self.assertEqual(result["restored_entries"], 4)
        self.assertEqual(result["kept_inactive_by_region_policy"], 1)
        self.assertEqual(result["keep_region"], "domestic")
        self.assertEqual(_active("fp-e4"), 0)

    def test_targeted_restore_also_respects_keep_region(self):
        self.patch_settings(_KEEP_DOMESTIC)
        result = self.call(channel_key=["ck-b"])   # e3(domestic) / e4(international) / e5(已 active)
        self.assertEqual(result["restored_entries"], 1)
        self.assertEqual(result["kept_inactive_by_region_policy"], 1)
        self.assertEqual(result["keep_region"], "domestic")
        self.assertEqual(_active("fp-e3"), 1)
        self.assertEqual(_active("fp-e4"), 0)


@unittest.skipIf(TestClient is None, "宿主机没有 FastAPI：这一组在容器内运行")
class RestoreValidationTests(RestoreTestCase):
    """非法参数必须 400，且不能写库。"""

    def test_empty_token_is_400(self):
        self.patch_settings(_OPEN_SETTINGS)
        for kwargs in ({"fingerprint": [""]}, {"fingerprint": ["fp-e1,,fp-e2"]}, {"source": [" "]},
                       {"channel_key": ["ck-a", ""]}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(main_module.HTTPException) as ctx:
                    self.call(**kwargs)
                self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(_inactive_count(), 5, "400 不该写库")

    def test_unknown_value_is_400(self):
        self.patch_settings(_OPEN_SETTINGS)
        for kwargs in ({"fingerprint": ["fp-nope"]}, {"channel_key": ["ck-nope"]}, {"source": ["nope"]},
                       {"fingerprint": ["fp-e1"], "source": ["nope"]}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(main_module.HTTPException) as ctx:
                    self.call(**kwargs)
                self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(_inactive_count(), 5)

    def test_too_many_tokens_is_400(self):
        self.patch_settings(_OPEN_SETTINGS)
        with self.assertRaises(main_module.HTTPException) as ctx:
            self.call(fingerprint=[f"fp-{index}" for index in range(main_module._RESTORE_FILTER_MAX + 1)])
        self.assertEqual(ctx.exception.status_code, 400)

    def test_token_cap_boundary_is_accepted(self):
        """上限本身不算越界：恰好 _RESTORE_FILTER_MAX 项时正常走校验与恢复。

        构造：1 个真指纹 + 499 个不存在的，合计正好 500 —— 必须**不**报"超限"，
        而是正常恢复命中的那 1 条（超限只由 501 项触发，见上一个用例）。
        """
        self.patch_settings(_OPEN_SETTINGS)
        tokens = ["fp-e1"] + [f"fp-x{index}" for index in range(main_module._RESTORE_FILTER_MAX - 1)]
        self.assertEqual(len(tokens), main_module._RESTORE_FILTER_MAX)
        result = self.call(fingerprint=tokens)
        self.assertEqual(result["restored_entries"], 1)
        self.assertEqual(_active("fp-e1"), 1)


@unittest.skipIf(TestClient is None, "宿主机没有 FastAPI：这一组在容器内运行")
class RestoreRevisionTests(RestoreTestCase):
    """导出缓存靠 revision 失效：真的改了行必须自增，没改不许自增。"""

    def test_revision_bumps_only_when_rows_change(self):
        self.patch_settings(_NO_REGION)
        start = current_revision()
        self.call(fingerprint=["fp-e1"])
        self.assertEqual(current_revision(), start + 1)
        # 同一条再来一次：0 行，版本号不动
        self.call(fingerprint=["fp-e1"])
        self.assertEqual(current_revision(), start + 1)
        # 未匹配到任何条目 → 400，同样不许动版本号
        with self.assertRaises(main_module.HTTPException):
            self.call(fingerprint=["fp-nope"])
        self.assertEqual(current_revision(), start + 1)


@unittest.skipIf(TestClient is None, "宿主机没有 FastAPI：这一组在容器内运行")
class RestoreHttpTests(RestoreTestCase):
    """HTTP 层：参数写法、鉴权（与 v49 的只读接口同口径，语义未改）。"""

    def setUp(self):
        super().setUp()
        # 不用 with：TestClient 只有在 with 里才跑 lifespan，这里必须避开启动期写库/起调度线程
        self.client = TestClient(main_module.app)

    def test_query_string_forms(self):
        self.patch_settings(_AUTH_SETTINGS)
        headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        response = self.client.post("/api/prune/restore?fingerprint=fp-e1&fingerprint=fp-e2", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["restored_entries"], 2)
        _seed()
        response = self.client.post("/api/prune/restore?channel_key=ck-a", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["restored_entries"], 2)
        _seed()
        response = self.client.post("/api/prune/restore?fingerprint=fp-e3,fp-e6", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["restored_entries"], 2)

    def test_no_params_via_http_restores_all(self):
        self.patch_settings(_AUTH_SETTINGS)
        response = self.client.post("/api/prune/restore", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"restored_entries": 5})

    def test_invalid_filter_returns_400_over_http(self):
        self.patch_settings(_AUTH_SETTINGS)
        headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        for query in ("fingerprint=", "fingerprint=fp-nope", "source=", "channel_key=ck-a,,ck-b"):
            with self.subTest(query=query):
                response = self.client.post(f"/api/prune/restore?{query}", headers=headers)
                self.assertEqual(response.status_code, 400, response.text)

    def test_requires_admin_token(self):
        self.patch_settings(_AUTH_SETTINGS)
        self.assertEqual(self.client.post("/api/prune/restore").status_code, 401)
        self.assertEqual(self.client.post("/api/prune/restore?fingerprint=fp-e1").status_code, 401)
        self.assertEqual(
            self.client.post("/api/prune/restore?fingerprint=fp-e1",
                             headers={"Authorization": "Bearer wrong"}).status_code, 401)
        self.assertEqual(_inactive_count(), 5, "401 不该写库")

    def test_open_when_token_not_configured(self):
        """令牌为空时与其它接口一致：不校验（v50 未改鉴权语义）。"""
        self.patch_settings(_OPEN_SETTINGS)
        response = self.client.post("/api/prune/restore?fingerprint=fp-e1")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["restored_entries"], 1)


if __name__ == "__main__":   # pragma: no cover
    unittest.main()
