"""测速请求的 User-Agent 护栏（SomaFM 这类源「只认正常 UA」）。

背景（2026-10-08 线上实测，nTv 适配器 → `ntv-fm` 的 46 条 SomaFM 电台）：

| 请求头 | 结果 |
| --- | --- |
| 不带 `-A`（空 UA）/ `curl/8.5.0` | **403**（46 条全灭） |
| 浏览器 UA、`VLC/3.0.20`、`ExoPlayerLib/2.19.1`、`Lavf/60.16.100`（ffmpeg）、`Kodi/20.5`、`IPTVHub`、hub 自己的 UA | **200/206**（48/48 全通） |
| `python-httpx/0.28.1` | 连接直接失败（000） |

也就是说：**测速默认 UA 一旦变成空串、或换成 curl/库名，这 46 条电台会在整轮测速里
被静默判死**，而报错文案还是「HTTP 403 / 连接失败」，看起来像线路坏了 ——
正是 2026-10-08 两个会话交接时特意提醒的那个坑（见仓库根 `COORDINATION.md`）。

这里把它钉住（不联网、不碰数据库，只断言请求头本身）：

1. `DEFAULT_UA` 非空，且不是已知会被拒的「脚本/库」UA；
2. 条目没带 UA 时，`_headers()` 必须真的回落到 `DEFAULT_UA`（不能回落成空串）；
3. 条目自带 UA 时必须原样使用（第三方源明确要求 UA 的场景）。
"""

from __future__ import annotations

import unittest

from app.speed import DEFAULT_UA, _headers

# 已实测会被 SomaFM 拒掉（或连不上）的 UA 前缀。清单只放**有证据**的几个，
# 不要凭感觉扩大：列表越长越容易误伤后来引入的合法 UA。
_KNOWN_BAD_UA_PREFIXES = (
    "curl/",
    "python-",
    "python/",
    "httpx",
    "wget",
    "go-http-client",
    "libwww",
    "java/",
)


class SpeedDefaultUaTests(unittest.TestCase):
    def test_default_ua_is_not_script_like(self):
        ua = (DEFAULT_UA or "").strip()
        self.assertTrue(ua, "DEFAULT_UA 不能为空：空 UA 会被 SomaFM 一类源整批 403")
        lowered = ua.lower()
        for prefix in _KNOWN_BAD_UA_PREFIXES:
            self.assertFalse(
                lowered.startswith(prefix),
                f"DEFAULT_UA 命中已知会被拒的脚本/库 UA 前缀 {prefix!r}：{ua!r}",
            )

    def test_headers_falls_back_to_default_ua(self):
        """条目没带 UA（库里绝大多数行都是空串）时必须用 DEFAULT_UA。"""
        headers = _headers("", "")
        self.assertEqual(headers["User-Agent"], DEFAULT_UA)
        self.assertTrue(headers["User-Agent"].strip())

    def test_headers_keeps_entry_ua(self):
        """条目自带 UA（如第三方列表里的 http-user-agent=）必须原样透传。"""
        headers = _headers("SomePlayer/1.0", "https://example.com/")
        self.assertEqual(headers["User-Agent"], "SomePlayer/1.0")
        self.assertEqual(headers["Referer"], "https://example.com/")

    def test_headers_does_not_send_referer_when_empty(self):
        self.assertNotIn("Referer", _headers("", ""))


if __name__ == "__main__":
    unittest.main()
