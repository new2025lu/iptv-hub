"""v62：手动添加源遇到「地址已在库中」时的 409 契约。

**为什么有这组用例**：用户报告在控制台「手动添加数据源」填一个地址加不进去。
真实原因是该地址**早已入库**（1220 条、status=ok），服务端正确地返回了 409；但 detail 只有一句
「这个地址已经添加过了」，前端只能显示「添加失败：…」，用户既不知道它已经存在、
也没法在几十个源里找到它 —— 观感就是「无法添加」。

v62 把 409 的 detail 改成**结构化对象**（带上已有源的 slug/name/status/item_count/enabled），
前端据此显示中性提示并滚动定位到那张卡片。这组用例锁住该契约：字段结构、类型、
**不产生任何写入**、以及格式校验仍在重复检查之前（避免误报 409）。

**需要 FastAPI**（导入 `app.main`）：容器内 `python -m unittest discover -s tests` 会跑这一组；
宿主机没有 FastAPI 时整组 skip（其余用例不受影响）。

隔离硬约束：只在 `/tmp` 下建库（先 assert 再动 SQL），并把 `app.main` 的 `db` 全局换成它；
TestClient **不进入 lifespan**（不触发启动期写库），生产库 `/data/iptv.sqlite3` 与
`data/live.m3u|txt` 完全不被触碰。
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace as dataclass_replace
from pathlib import Path
from unittest import mock

from app import db as db_module
from app.config import settings as real_settings

try:  # 宿主机没有 FastAPI 时只跳过这一组
    from fastapi.testclient import TestClient

    from app import main as main_module
except ModuleNotFoundError:  # pragma: no cover - 宿主机环境
    TestClient = None
    main_module = None


# 隔离硬约束：先在 /tmp 下建库并断言路径，再执行任何 SQL
_TMPDIR = tempfile.TemporaryDirectory(prefix="iptv-hub-dup409-", dir="/tmp")
_DB_PATH = Path(_TMPDIR.name) / "dup409.sqlite3"
assert str(_DB_PATH).startswith("/tmp/"), _DB_PATH
_ISOLATED = db_module.Database(_DB_PATH)
_ISOLATED.initialize()

# 这一组只关心「重复地址」的判定与响应，与导出/EPG 无关：把无关配置钉死，断言不随 .env 漂移。
_BASE_SETTINGS = dataclass_replace(
    real_settings, admin_token="", keep_region="", epg_url="", public_base_url="",
    export_hide_dead=True, main_exclude_group="",
)

EXISTING_URL = "http://live.example.test/live/live9/dgtv.txt"
EXISTING_SLUG = "manual::testdup01"
EXISTING_NAME = "dgtv 综合源（咪咕+地方台）"


def _reset_sources() -> None:
    with _ISOLATED.write() as conn:
        conn.execute("DELETE FROM source_entries")
        conn.execute("DELETE FROM stream_health")
        conn.execute("DELETE FROM sources")


def _insert_source(*, enabled: int = 1, item_count: int = 1220, status: str = "ok") -> None:
    with _ISOLATED.write() as conn:
        conn.execute(
            "INSERT INTO sources(slug, name, url, format, priority, item_count, status, enabled) "
            "VALUES (?, ?, ?, 'list', 60, ?, ?, ?)",
            (EXISTING_SLUG, EXISTING_NAME, EXISTING_URL, item_count, status, enabled),
        )


def _counts() -> tuple[int, int]:
    return (
        _ISOLATED.row("SELECT COUNT(*) AS n FROM sources")["n"],
        _ISOLATED.row("SELECT COUNT(*) AS n FROM source_entries")["n"],
    )


@unittest.skipIf(TestClient is None, "宿主机没有 FastAPI：这一组在容器内运行")
class DuplicateSourceConflictTests(unittest.TestCase):
    def setUp(self):
        _reset_sources()
        self._patches = [
            mock.patch.object(main_module, "db", _ISOLATED),
            mock.patch.object(main_module, "settings", _BASE_SETTINGS),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)
        # 不进入 lifespan：启动期会 initialize()/排队同步，测试只走 ASGI 请求
        self.client = TestClient(main_module.app)

    def test_duplicate_returns_structured_409(self):
        _insert_source()
        r = self.client.post("/api/sources", params={"url": EXISTING_URL})
        self.assertEqual(r.status_code, 409, r.text)
        detail = r.json()["detail"]
        # 结构：不再是裸字符串，而是带已有源信息的对象（前端据此提示 + 定位卡片）
        self.assertIsInstance(detail, dict)
        self.assertEqual(detail["message"], "这个地址已经在数据源里了")
        self.assertEqual(detail["slug"], EXISTING_SLUG)
        self.assertEqual(detail["name"], EXISTING_NAME)
        self.assertEqual(detail["status"], "ok")
        self.assertEqual(detail["item_count"], 1220)
        self.assertEqual(detail["enabled"], 1)
        for key in ("slug", "name", "status"):
            self.assertIsInstance(detail[key], str)
        for key in ("item_count", "enabled"):
            self.assertIsInstance(detail[key], int)

    def test_disabled_existing_source_reports_enabled_zero(self):
        # 停用态的源同样算「已在库中」；前端靠 enabled=0 提示「当前是已停用状态，
        # 可在卡片上启用」，所以这个字段必须如实透出（而不是一律 1）。
        _insert_source(enabled=0, status="not_modified", item_count=27)
        r = self.client.post("/api/sources", params={"url": EXISTING_URL})
        self.assertEqual(r.status_code, 409, r.text)
        detail = r.json()["detail"]
        self.assertEqual(detail["enabled"], 0)
        self.assertEqual(detail["status"], "not_modified")
        self.assertEqual(detail["item_count"], 27)

    def test_duplicate_check_writes_nothing(self):
        _insert_source()
        before_rows, before_entries = _counts()
        before = dict(_ISOLATED.row("SELECT * FROM sources WHERE slug=?", (EXISTING_SLUG,)))
        for _ in range(3):  # 幂等：重复请求也不该产生任何副作用
            r = self.client.post("/api/sources", params={"url": EXISTING_URL})
            self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual((before_rows, before_entries), _counts())
        self.assertEqual(dict(_ISOLATED.row("SELECT * FROM sources WHERE slug=?", (EXISTING_SLUG,))), before)

    def test_invalid_format_still_400_before_duplicate_check(self):
        # 格式校验在重复检查**之前**：地址重复但 format 非法时应当是 400（输入错），
        # 不能被 409 抢答 —— 否则用户会以为"只是重复"，实际是参数写错了。
        _insert_source()
        r = self.client.post("/api/sources", params={"url": EXISTING_URL, "format": "bogus"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("format", r.json()["detail"])

    def test_duplicate_requires_admin_when_token_configured(self):
        # 配了令牌时，重复判定同样要走鉴权：不能变成"未授权也能探测某个地址在不在库里"。
        _insert_source()
        secured = dataclass_replace(_BASE_SETTINGS, admin_token="test-token-v62")
        with mock.patch.object(main_module, "settings", secured):
            unauth = self.client.post("/api/sources", params={"url": EXISTING_URL})
            self.assertEqual(unauth.status_code, 401, unauth.text)
            ok = self.client.post(
                "/api/sources",
                params={"url": EXISTING_URL},
                headers={"Authorization": "Bearer test-token-v62"},
            )
            self.assertEqual(ok.status_code, 409, ok.text)
            detail = ok.json()["detail"]
            self.assertEqual(detail["slug"], EXISTING_SLUG)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
