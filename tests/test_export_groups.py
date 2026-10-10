"""v40 导出分组（数据源分组 → M3U/TXT group-title）的回归测试。

只测纯函数：`_fetch_lines` 之外的判定与搬运逻辑不碰数据库，所以这些用例可以在宿主机
直接跑（导入 app.static_export 会连带导入 app.db，但本文件不调用任何 DB 方法）。
"""

from __future__ import annotations

import unittest

from app.static_export import (
    _apply_group_overrides,
    _fallback_organize,
    _organize,
    _single_group_channels,
    ChannelLine,
)
from app import template as template_mod


def line(label: str, name: str, url: str) -> ChannelLine:
    return ChannelLine(label, name, url, "", "", "", "")


def row(name: str, group_title: str, url: str, source_group: str = "", speed: float = 100.0) -> dict:
    """构造一行 _fetch_lines 形状的记录（只填判定会用到的列）。"""
    return {
        "name": name,
        "group_title": group_title,
        "url": url,
        "referrer": "",
        "user_agent": "",
        "logo": "",
        "tvg_id": "",
        "stream_fingerprint": url,
        "source_group": source_group,
        "speed_kbps": speed,
        "latency_ms": 10,
        "ok_total": 1,
        "fail_total": 0,
    }


class SingleGroupChannelTests(unittest.TestCase):
    def test_all_lines_from_one_group_is_overridden(self):
        """三条线路都来自「发现源」→ 整频道改挂该分组。"""
        table = {"湖南卫视": {"发现源"}, "CCTV1": {"发现源"}}
        self.assertEqual(_single_group_channels(table), {"湖南卫视": "发现源", "CCTV1": "发现源"})

    def test_one_ungrouped_line_blocks_the_override(self):
        """只要还有一条线路来自非分组源，就保持原分组（避免同一频道出现在两个组里）。"""
        self.assertEqual(_single_group_channels({"CCTV1": {"发现源", ""}}), {})

    def test_two_different_groups_block_the_override(self):
        """跨两个分组源时口径不明确，同样保持原分组。"""
        self.assertEqual(_single_group_channels({"CCTV1": {"发现源", "别的组"}}), {})

    def test_ungrouped_only_is_not_an_override(self):
        """全部未分组（空串）不是覆盖，行为与改动前一致。"""
        self.assertEqual(_single_group_channels({"CCTV1": {""}}), {})


class ApplyOverrideTests(unittest.TestCase):
    def test_channel_is_moved_not_copied(self):
        """搬运后原分组里不再有该频道：同频道不会重复出现在两个分组。"""
        groups = [("央视", [line("001 CCTV1", "CCTV1", "u1")]), ("其他", [line("9001 某某", "某某", "u2")])]
        out = _apply_group_overrides(groups, {"CCTV1": "发现源"})
        self.assertEqual([name for name, _ in out], ["其他", "发现源"])
        self.assertEqual([e.name for e in out[1][1]], ["CCTV1"])
        self.assertEqual(len([e for _, entries in out for e in entries]), 2, "条目总数不得变化")

    def test_existing_target_group_is_merged(self):
        """目标分组已存在时合并进去，不新建重复分组。"""
        groups = [("央视", [line("001 CCTV1", "CCTV1", "u1")]), ("发现源", [line("9001 某某", "某某", "u2")])]
        out = _apply_group_overrides(groups, {"CCTV1": "发现源"})
        self.assertEqual([name for name, _ in out], ["发现源"])
        self.assertEqual(sorted(e.name for e in out[0][1]), ["CCTV1", "某某"])

    def test_empty_groups_are_dropped_and_order_is_stable(self):
        """搬空的组不再输出；新分组排在最后，保证同一份数据每次导出顺序一致。"""
        groups = [("央视", [line("001 CCTV1", "CCTV1", "u1")]), ("其他", [line("9001 A", "A", "u2")])]
        out = _apply_group_overrides(groups, {"CCTV1": "发现源", "A": "发现源"})
        self.assertEqual([name for name, _ in out], ["发现源"])
        self.assertEqual([e.name for e in out[0][1]], ["CCTV1", "A"])


class OrganizeTests(unittest.TestCase):
    def test_template_path_moves_whole_channel(self):
        """模版路径：模版命中的频道若整条只由分组源提供，编号保留但整组搬走。"""
        rows = [row("CCTV1", "央视", "http://a/1", source_group="发现源")]
        groups_tpl = [("央视频道", ["CCTV1"])]
        # match_map 的键是 template.match_key（归一化后的名字），与 _organize 的查表口径一致
        match_map = {template_mod.match_key("CCTV1"): ("CCTV1", "央视频道")}
        out = _organize(rows, True, groups_tpl, match_map)
        self.assertEqual([name for name, _ in out], ["发现源"])
        self.assertEqual(out[0][1][0].label, "001 CCTV1")

    def test_template_path_keeps_mixed_channel_in_place(self):
        """同一频道另有一条未分组线路 → 留在央视，不产生第二个分组。"""
        rows = [
            row("CCTV1", "央视", "http://a/1", source_group="发现源", speed=300),
            row("CCTV1", "央视", "http://b/2", source_group="", speed=200),
        ]
        match_map = {template_mod.match_key("CCTV1"): ("CCTV1", "央视频道")}
        out = _organize(rows, True, [("央视频道", ["CCTV1"])], match_map)
        self.assertEqual([name for name, _ in out], ["央视"])
        self.assertEqual(len(out[0][1]), 2, "多线路模式两条线路都要保留")

    def test_fallback_path_groups_by_source_group(self):
        """无模版兜底路径走同一套口径。"""
        rows = [row("某某台", "其他", "http://a/1", source_group="发现源")]
        out = _fallback_organize(rows, True)
        self.assertEqual([name for name, _ in out], ["发现源"])
        self.assertEqual(out[0][1][0].name, "某某台")


if __name__ == "__main__":
    unittest.main()
