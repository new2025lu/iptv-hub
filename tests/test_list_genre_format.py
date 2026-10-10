"""v60：`分组名,#genre#` 清单（txt 逐条清单）的两处修复。

触发场景是用户给的 `https://live.zbds.top/tv/iptv4.txt` —— 标准 txt 清单，
第一行就是 `央视频道,#genre#` 分组头。修复前两条链路都错：

1. `detect_format()` 只看第一条有效行：`央视频道,#genre#` 既不含 `#EXTINF`、也不是
   「名称,http://…」→ 直接 `break` 返回 `"m3u"` → `parse_m3u()` 返回 0 条 →
   `POST /api/sources` 报 400「解析不出任何有效条目」，源根本加不进来；
2. `parse_channel_list()` 把分组头当坏行丢掉 → 这一组条目的 `group_title` 为空 →
   `region_of()` 只能靠频道名猜，纯 ASCII 名（CCTV1 / CGTN）不命中 CJK 兜底 →
   判成 international → 被 `KEEP_REGION=domestic` 整组丢弃（实测 685 条丢 40 条）。

本文件只钉这两件事：不联网、不碰数据库。
"""

from __future__ import annotations

import unittest

from app.parsers import genre_header, parse_channel_list, region_of
from app.sync import detect_format

_LIST_WITH_GENRE = (
    "央视频道,#genre#\n"
    "CCTV1,http://a.test/1.m3u8\n"
    "CCTV2,http://a.test/2.m3u8\n"
    "卫视频道,#genre#\n"
    "湖南卫视,http://a.test/hn.m3u8\n"
).encode()

_LIST_PLAIN = "CCTV1,http://a.test/1.m3u8\n湖南卫视,http://a.test/hn.m3u8\n".encode()

_M3U = (
    '#EXTM3U\n#EXTINF:-1 tvg-id="CCTV-1" group-title="央视频道",CCTV1\n'
    "http://a.test/1.m3u8\n"
).encode()


class GenreHeaderTests(unittest.TestCase):
    def test_recognizes_halfwidth_and_fullwidth_separator(self):
        self.assertEqual(genre_header("央视频道,#genre#"), "央视频道")
        self.assertEqual(genre_header("央视频道，#genre#"), "央视频道")
        self.assertEqual(genre_header('"央视频道",#genre#'), "央视频道")

    def test_ignores_normal_entry_lines_and_bare_marker(self):
        self.assertEqual(genre_header("CCTV1,http://a.test/1.m3u8"), "")
        self.assertEqual(genre_header("#genre#"), "")          # 没有左半部分
        self.assertEqual(genre_header("央视频道,#Genre#"), "央视频道")   # 大小写不敏感


class DetectFormatTests(unittest.TestCase):
    def test_genre_header_list_is_detected_as_list(self):
        self.assertEqual(detect_format(_LIST_WITH_GENRE), "list")

    def test_plain_list_still_detected(self):
        self.assertEqual(detect_format(_LIST_PLAIN), "list")

    def test_m3u_still_detected(self):
        self.assertEqual(detect_format(_M3U), "m3u")


class ParseChannelListGenreTests(unittest.TestCase):
    def test_group_header_applies_to_following_entries(self):
        entries = parse_channel_list(_LIST_WITH_GENRE)
        self.assertEqual([e.group_title for e in entries], ["央视频道", "央视频道", "卫视频道"])

    def test_list_without_header_keeps_default_group(self):
        entries = parse_channel_list(_LIST_PLAIN)
        self.assertEqual([e.group_title for e in entries], ["未分类", "未分类"])

    def test_ascii_names_survive_keep_region_domestic(self):
        """手动源的 slug 不在 DOMESTIC_SOURCES 白名单里，靠分组头才不会被判成国外。"""
        slug = "manual::deadbeef"
        entries = parse_channel_list(_LIST_WITH_GENRE)
        regions = [region_of(slug, e.country, e.group_title, e.name) for e in entries]
        self.assertEqual(regions, ["domestic", "domestic", "domestic"])

    def test_header_does_not_leak_across_m3u(self):
        """内容是 M3U 时仍走 parse_m3u：分组头逻辑只在逐条清单里生效。"""
        entries = parse_channel_list(_M3U)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].group_title, "央视频道")


if __name__ == "__main__":
    unittest.main()
