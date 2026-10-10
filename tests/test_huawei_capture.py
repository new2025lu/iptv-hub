"""v61：华为 HWCTC / 抓包 JSP（属性式频道表）的解析。

背景：运营商抓包得到的文件（如 getchannellistHWCTC.jsp）里一行就是一条记录：
    <Channel ChannelName="CCTV-1 综合" ChannelURL="igmp://...|http://...?fmt=ts2hls&amp;rrsip=..." />

修复前两条链路都不认识它：
1. 按 URL 添加：detect_format() 认不出属性式内容，落到「名称,地址」判定后返回 m3u，
   parse_m3u() 解析出 0 条 → 报「解析不出任何有效条目」，源根本加不进来；
2. 内联粘贴（POST /api/sources/inline → parse_channel_list）：会被逐行的
   「找第一个 http 地址」兜底分支接住，实测拿到的名字是
   「<Channel ChannelName="CCTV-1 综合" Channel」这种 XML 残渣，地址里还留着 &amp; ——
   不报错、静默入库，播放器拿不到流。

本文件只钉这几件事：顺序无关配对、&amp; 还原、多地址拆分、类型属性当分组。不联网、不碰数据库。
"""

from __future__ import annotations

import unittest

from app.parsers import HUAWEI_DEFAULT_GROUP, parse_channel_list, parse_huawei_capture, region_of
from app.sync import detect_format

_CAPTURE = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<ChannelList>\n"
    '  <Channel ChannelName="CCTV-1 综合" ChannelURL="igmp://239.253.1.1:8000|'
    'http://a.test/cctv1/index.m3u8?fmt=ts2hls&amp;rrsip=cdn&amp;zoneoffset=480" ChannelGenre="央视" />\n'
    '  <Channel ChannelURL="http://a.test/cctv2/index.m3u8?fmt=ts2hls&amp;token=x" ChannelName="CCTV-2 财经" />\n'
    '  <Channel ChannelName="贵州卫视（高清）" ChannelURL="http://a.test/gzws/index.m3u8?fmt=ts2hls" />\n'
    '  <Channel ChannelName="" ChannelURL="" />\n'
    "</ChannelList>\n"
).encode()

_M3U = (
    '#EXTM3U\n#EXTINF:-1 tvg-id="CCTV-1" group-title="央视频道",CCTV1\n'
    "http://a.test/1.m3u8\n"
).encode()

_LIST = "CCTV1,http://a.test/1.m3u8\n湖南卫视,http://a.test/hn.m3u8\n".encode()


class DetectFormatTests(unittest.TestCase):
    def test_capture_is_detected_as_huawei(self):
        self.assertEqual(detect_format(_CAPTURE), "huawei")

    def test_gbk_capture_is_detected(self):
        gbk = _CAPTURE.decode("utf-8").encode("gb18030")
        self.assertEqual(detect_format(gbk), "huawei")

    def test_existing_formats_unaffected(self):
        self.assertEqual(detect_format(_M3U), "m3u")
        self.assertEqual(detect_format(_LIST), "list")


class ParseHuaweiCaptureTests(unittest.TestCase):
    def test_names_and_urls_are_clean(self):
        entries = parse_huawei_capture(_CAPTURE)
        names = [e.name for e in entries]
        self.assertEqual(names, ["CCTV-1 综合", "CCTV-2 财经", "贵州卫视（高清）"])
        for entry in entries:
            self.assertNotIn("<Channel", entry.name)
            self.assertNotIn("&amp;", entry.url)

    def test_attribute_order_does_not_matter(self):
        """地址写在名字前面的那条，名字必须挂对（这正是网上那版正则踩的坑）。"""
        entries = parse_huawei_capture(_CAPTURE)
        by_url = {e.url: e.name for e in entries}
        self.assertEqual(by_url["http://a.test/cctv2/index.m3u8?fmt=ts2hls&token=x"], "CCTV-2 财经")

    def test_html_entities_unescaped(self):
        entries = parse_huawei_capture(_CAPTURE)
        first = [e for e in entries if e.name == "CCTV-1 综合"][0]
        self.assertIn("&rrsip=cdn&zoneoffset=480", first.url)

    def test_multiple_addresses_are_split_and_unsupported_scheme_skipped(self):
        """一条记录里的 igmp 地址不在 ALLOWED_STREAM_SCHEMES 里，跳过它、保留 http。"""
        entries = parse_huawei_capture(_CAPTURE)
        urls = [e.url for e in entries if e.name == "CCTV-1 综合"]
        self.assertEqual(len(urls), 1)
        self.assertTrue(urls[0].startswith("http://"))

    def test_empty_record_is_skipped(self):
        entries = parse_huawei_capture(_CAPTURE)
        self.assertTrue(all(e.name.strip() for e in entries))
        self.assertEqual(len(entries), 3)

    def test_genre_attribute_becomes_group_title(self):
        entries = parse_huawei_capture(_CAPTURE)
        first = [e for e in entries if e.name == "CCTV-1 综合"][0]
        self.assertEqual(first.group_title, "央视")

    def test_entries_without_genre_get_default_group(self):
        entries = parse_huawei_capture(_CAPTURE)
        second = [e for e in entries if e.name == "CCTV-2 财经"][0]
        self.assertEqual(second.group_title, HUAWEI_DEFAULT_GROUP)

    def test_gbk_capture_parses_chinese_names(self):
        gbk = _CAPTURE.decode("utf-8").encode("gb18030")
        entries = parse_huawei_capture(gbk)
        self.assertIn("贵州卫视（高清）", [e.name for e in entries])

    def test_genre_keeps_ascii_names_domestic(self):
        """类型属性落到 group_title 后，纯 ASCII 的频道名也能被判成国内，不被 KEEP_REGION 丢掉。"""
        slug = "manual::deadbeef"
        entries = parse_huawei_capture(_CAPTURE)
        first = [e for e in entries if e.name == "CCTV-1 综合"][0]
        self.assertEqual(region_of(slug, first.country, first.group_title, first.name), "domestic")


class ParseChannelListIntegrationTests(unittest.TestCase):
    def test_inline_paste_path_recognizes_capture(self):
        """内联粘贴走的是 parse_channel_list，它必须自己认出抓包内容（main.py 直接调它）。"""
        entries = parse_channel_list(_CAPTURE)
        self.assertEqual([e.name for e in entries], ["CCTV-1 综合", "CCTV-2 财经", "贵州卫视（高清）"])

    def test_m3u_still_takes_the_m3u_branch(self):
        entries = parse_channel_list(_M3U)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, "CCTV1")


if __name__ == "__main__":
    unittest.main()
