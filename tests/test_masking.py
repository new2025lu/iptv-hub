"""v42 审计 P1-5：展示用脱敏规则的回归测试。

两处修复：
  * `mask_hostname` 原先要求主机名至少三段才打码，「令牌当顶级域」（`<24位令牌>.com`）
    这类两段写法会整串原样露出 —— 现在只按最左标签长度判定；
  * `mask_source_url` 的 userinfo（`user:pass@host`）必须整体丢弃。

这些函数原先只长在 `app/main.py`（会连带导入 FastAPI，宿主机测不了），
v42 搬到 `app/masking.py` 之后可以在不装 Web 框架的环境直接验证。
"""

from __future__ import annotations

import unittest

from app.masking import mask_hostname, mask_source_url


class MaskHostnameTests(unittest.TestCase):
    def test_two_label_token_is_masked(self):
        """审计场景：<24位令牌>.com —— 两段也必须打码（原先 len(labels) > 2 漏掉）。"""
        token = "a1b2c3d4e5f6g7h8i9j0k1l2"        # 24 位，和上游私有令牌同量级
        self.assertEqual(mask_hostname(f"{token}.com"), "***.com")

    def test_three_label_token_still_masked(self):
        self.assertEqual(mask_hostname("a1b2c3d4e5f6g7h8i9j0k1l2.example.com"), "***.example.com")

    def test_normal_hosts_are_untouched(self):
        for host in ("cdn.example.com", "raw.githubusercontent.com", "live.hacks.tools",
                     "192.0.2.10", "m3u.ibert.me"):
            self.assertEqual(mask_hostname(host), host)

    def test_leftmost_label_just_below_threshold_is_kept(self):
        """15 位（阈值 16 之下）仍照常显示：`testingcf.`、`live.` 这类前缀不受影响。"""
        self.assertEqual(mask_hostname("abcdefghijklmno.example.com"), "abcdefghijklmno.example.com")


class MaskSourceUrlTests(unittest.TestCase):
    def test_userinfo_credentials_are_dropped(self):
        self.assertEqual(mask_source_url("http://alice:s3cr3t@host.example/x.m3u"),
                         "http://host.example/x.m3u")
        self.assertEqual(mask_source_url("https://token@host.example:8443/x.m3u"),
                         "https://host.example:8443/x.m3u")

    def test_token_path_segment_and_query_are_masked(self):
        self.assertEqual(mask_source_url("http://h.example/u/0123456789abcdef/m3u"),
                         "http://h.example/u/***/m3u")
        self.assertEqual(mask_source_url("http://h.example/sub?Ex4mPl3T0k=m3u"),
                         "http://h.example/sub?***")

    def test_structural_names_survive(self):
        self.assertEqual(mask_source_url("http://h.example/live.m3u"), "http://h.example/live.m3u")
        self.assertEqual(mask_source_url("https://h.example/index.m3u"), "https://h.example/index.m3u")

    def test_garbage_never_falls_back_to_raw(self):
        self.assertEqual(mask_source_url("not a url"), "***")
        self.assertEqual(mask_source_url(""), "***")


if __name__ == "__main__":
    unittest.main()
