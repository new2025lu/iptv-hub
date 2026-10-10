"""可选联网自检：SomaFM 这类源「只认正常 UA」—— 默认 skip，手动才跑。

```bash
python3 -m unittest tests.test_soma_ua_live -v                            # 跳过（不联网）
IPTV_LIVE_UA_CHECK=1   python3 -m unittest tests.test_soma_ua_live -v     # 抽 3 条
IPTV_LIVE_UA_CHECK=all python3 -m unittest tests.test_soma_ua_live -v     # 适配器清单里的 SomaFM 全量
```

为什么是独立文件、而且要联网：`tests/test_speed_default_ua.py` 是**离线护栏**，
只能保证「发出去的 UA 不是空串 / curl / 库名」这类已知坏值；
**「上游今天还认这个 UA 吗」只能真发一次请求才知道**。这里就是那层「上游策略变化」的探测，
所以：默认 skip（CI / 容器里跑测试不该依赖外网），抽样 3 条，全量才加 `=all`。

**被测的是 app 自己的请求头** —— `speed._headers("", "")`，也就是库里 `user_agent` 为空的
绝大多数条目实际会发出去的那一份；这里不另抄一个 UA 常量，否则护栏又变成摆设。

目标清单来源优先级：`$NTV_FM_M3U` → `data/ntv-fm.m3u`（仓库相对） → 内置 3 条样本。
换机器 / 在容器里跑时清单不存在会**自动退回内置样本**，不会因路径缺失而假失败。

背景实测（2026-10-08）：空 UA 与 `curl/*` → 403；
浏览器 / VLC / ExoPlayer / Lavf(ffmpeg) / Kodi UA → 206。所以「空 UA 的对照组」如果哪天也通了，
说明上游不再区分 UA，本文件就可以退休。
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

import httpx

from app.speed import _headers

_MODE = os.getenv("IPTV_LIVE_UA_CHECK", "").strip().lower()
_ENABLED = bool(_MODE)
_SAMPLE_SIZE = 3
_RANGE = "bytes=0-65535"
_TIMEOUT = float(os.getenv("IPTV_LIVE_UA_TIMEOUT", "10"))

# SomaFM 的公开 128k mp3 直连；实测需要正常 UA（空 UA / curl 一律 403）
_BUILTIN_SAMPLES = (
    "https://ice5.somafm.com/groovesalad-128-mp3",
    "https://ice5.somafm.com/defcon-128-mp3",
    "https://ice5.somafm.com/dronezone-128-mp3",
)

_M3U_CANDIDATES = (
    os.getenv("NTV_FM_M3U", "").strip(),
    "data/ntv-fm.m3u",
)


def _urls_from_m3u() -> list[str]:
    for raw in _M3U_CANDIDATES:
        if not raw:
            continue
        path = Path(raw)
        if not path.is_file():
            continue
        urls = [
            line.strip()
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip().startswith(("http://", "https://"))
        ]
        if urls:
            return urls
    return []


def _targets() -> list[str]:
    urls = [u for u in _urls_from_m3u() if "somafm.com" in u] or list(_BUILTIN_SAMPLES)
    return urls if _MODE == "all" else urls[:_SAMPLE_SIZE]


@unittest.skipUnless(
    _ENABLED,
    "需要联网：设 IPTV_LIVE_UA_CHECK=1（抽 3 条）或 =all（全量）才跑",
)
class SomaFmUaLiveTests(unittest.TestCase):
    def test_hub_default_ua_is_accepted(self):
        """hub 实际发出去的请求头（DEFAULT_UA）必须被上游接受。"""
        headers = dict(_headers("", ""))  # ← 被测对象
        headers["Range"] = _RANGE
        targets = _targets()
        self.assertTrue(targets, "没有可测目标：清单为空且内置样本也没了")

        failures: list[str] = []
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, trust_env=False) as client:
            for url in targets:
                try:
                    resp = client.get(url, headers=headers)
                    ok = resp.status_code in (200, 206) and len(resp.content) > 0
                    detail = f"{resp.status_code} / {len(resp.content)}B"
                except Exception as exc:  # noqa: BLE001 - 自检要把它当失败收集，不能中断整轮
                    ok, detail = False, f"{type(exc).__name__}: {exc}"
                if not ok:
                    failures.append(f"{url} -> {detail}")

        self.assertEqual(
            failures,
            [],
            "hub 默认 UA 被上游拒了（先看离线护栏 tests/test_speed_default_ua.py）：\n"
            + "\n".join(failures),
        )

    def test_empty_ua_control(self):
        """对照组：不带 UA 应当被拒；若也通了，说明上游不再区分 UA，本文件可退休。

        「被拒」有两种形态，都算对照组通过：一种是 403（带响应体），
        另一种是**直接把连接掐断**（httpx.RemoteProtocolError）—— 实测空 UA 两种都会遇到。
        """
        url = _targets()[0]
        status: int | None = None
        detail = ""
        try:
            with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, trust_env=False) as client:
                resp = client.get(url, headers={"Range": _RANGE})
            status, detail = resp.status_code, f"{resp.status_code} / {len(resp.content)}B"
        except Exception as exc:  # noqa: BLE001 - 连接被掐断同样属于「被拒」
            detail = f"{type(exc).__name__}: {exc}"

        if status in (200, 206):
            self.skipTest(f"上游已不区分 UA（空 UA 也返回 {detail}）—— 本护栏可以退休了")
        if status is None:
            return  # 连接层被拒，对照组符合预期
        self.assertGreaterEqual(status, 400, f"空 UA 对照组返回了意外状态码 {status}")



if __name__ == "__main__":
    unittest.main()
