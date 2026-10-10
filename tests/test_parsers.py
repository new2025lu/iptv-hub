from __future__ import annotations

import json
import unittest

from app.parsers import (
    entry_key_for,
    make_entry,
    normalize_name,
    normalize_stream_url,
    parse_channel_list,
    parse_iptv_org_json,
    parse_m3u,
    parse_tvbox_lives,
    region_of,
)


class ParserTests(unittest.TestCase):
    def test_m3u_parses_attributes_and_vlc_options(self):
        payload = b'''#EXTM3U\n#EXTINF:-1 tvg-id="CCTV-1" tvg-name="CCTV1" tvg-logo="https://img.test/c1.png" group-title="\xe5\xa4\xae\xe8\xa7\x86\xe9\xa2\x91\xe9\x81\x93",CCTV1 HD (1080p)\n#EXTVLCOPT:http-referrer=https://portal.test/\n#EXTVLCOPT:http-user-agent=Test Agent\nhttps://stream.test/live.m3u8#fragment\n'''
        entries = parse_m3u(payload)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.tvg_id, "CCTV-1")
        self.assertEqual(entry.group_title, "央视频道")
        self.assertEqual(entry.quality.lower(), "1080p")
        self.assertEqual(entry.referrer, "https://portal.test/")
        self.assertEqual(entry.user_agent, "Test Agent")
        self.assertEqual(entry.url, "https://stream.test/live.m3u8#fragment")

    def test_iptv_org_json_is_deduplicated(self):
        payload = json.dumps([
            {"channel": "A.tv@SD", "title": "A TV (720p)", "url": "https://example.test/a.m3u8", "quality": "720p", "labels": []},
            {"channel": "A.tv@SD", "title": "A TV (720p)", "url": "https://example.test/a.m3u8", "quality": "720p", "labels": []},
        ]).encode()
        entries = parse_iptv_org_json(payload)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].channel_key, "id:a.tv")

    def test_url_normalization_keeps_query_order_and_removes_fragment(self):
        value = normalize_stream_url("HTTPS://EXAMPLE.TEST:443/path?b=2&a=1#x")
        self.assertEqual(value, "https://example.test/path?b=2&a=1")

    def test_disallowed_scheme_is_rejected(self):
        with self.assertRaises(ValueError):
            make_entry(name="bad", url="file:///etc/passwd")

    def test_quality_suffix_does_not_define_channel_identity(self):
        self.assertEqual(normalize_name("CCTV 1 HD"), normalize_name("CCTV 1"))

    # ---- v20/v21 修复点的回归钉死 ----

    def test_entry_key_formula_ignores_at_suffix_and_case(self):
        """entry_key 口径：tvg-id 去 @ 后缀 + casefold，与 db 迁移共用同一公式。"""
        a = entry_key_for("CCTV@HD", "fp1")
        b = entry_key_for("cctv@sd", "fp1")
        c = entry_key_for("cctv", "fp1")
        self.assertEqual(a, b)
        self.assertEqual(b, c)
        self.assertNotEqual(a, entry_key_for("cctv", "fp2"))

    def test_ipv6_url_normalization_keeps_brackets_and_no_collision(self):
        """IPv6 字面量重建 netloc 必须带方括号；host+port 与纯 host 不得塌缩成同一串。"""
        with_port = normalize_stream_url("http://[2001:db8::1]:8080/x")
        bare = normalize_stream_url("http://[2001:db8::1:8080]/x")
        self.assertEqual(with_port, "http://[2001:db8::1]:8080/x")
        self.assertEqual(bare, "http://[2001:db8::1:8080]/x")
        self.assertNotEqual(with_port, bare)

    def test_parse_channel_list_fullwidth_comma_and_tab(self):
        payload = "CCTV-1，http://a.test/1.m3u8\n湖南卫视\thttp://b.test/2.m3u8\n# 注释\n\n".encode()
        entries = parse_channel_list(payload)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].name, "CCTV-1")
        self.assertEqual(entries[1].url, "http://b.test/2.m3u8")

    def test_parse_channel_list_delegates_m3u_content(self):
        payload = b'#EXTM3U\n#EXTINF:-1 tvg-id="A",Channel A\nhttp://a.test/x.m3u8\n'
        entries = parse_channel_list(payload)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].tvg_id, "A")

    def test_parse_tvbox_lives_filters_and_dedupes(self):
        payload = json.dumps({
            "sites": [{"name": "点播"}],
            "lives": [
                {"name": "直播A", "url": "http://a.test/live.m3u"},
                {"name": "直播A2", "url": "http://a.test/live.m3u"},
                {"name": "坏地址", "url": "udp://239.0.0.1:1234"},
                "not-a-dict",
            ],
        }).encode()
        lives = parse_tvbox_lives(payload)
        self.assertEqual(lives, [("直播A", "http://a.test/live.m3u")])

    def test_region_of_basic(self):
        self.assertEqual(region_of("cctv-live", "", "", ""), "domestic")
        self.assertEqual(region_of("other", "us", "", ""), "international")
        self.assertEqual(region_of("other", "", "央视频道", ""), "domestic")
        self.assertEqual(region_of("other", "", "", "湖南卫视"), "domestic")
        self.assertEqual(region_of("other", "", "", "CNN"), "international")


if __name__ == "__main__":
    unittest.main()
