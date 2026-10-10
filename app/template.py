"""DIYP 频道匹配模版：解析与应用。

模版格式（每行）：
    分组,#genre#
    标准频道名：别名1#别名2#别名3
    标准频道名,别名1#别名2#别名3

用途：
    1. 频道名标准化——把 CCTV1综合 / CCTV-1 / CCTV1HD 等各种叫法归一到标准名；
    2. 分组归类——按模版分组（央视频道/卫视频道/各省节目/4K节目/港台节目...）输出；
    3. 未匹配的频道归入「其他频道」组，置于列表末尾。

模版文件默认位于数据目录（data/channel-template.txt），修改后自动重载。
"""

from __future__ import annotations

import re
import time
from functools import lru_cache
from pathlib import Path

from .config import settings
from .parsers import normalize_name

_state: dict = {"path": None, "mtime": None, "groups": [], "match": {}, "checked_at": float("-inf")}
FALLBACK_GROUP = "其他频道"
# 一次订阅导出会对 2 万行反复调用 match_channel，节流模板文件的 stat 检查；
# 模板改动最多延迟 1 秒生效，可接受
_CHECK_INTERVAL = 1.0


@lru_cache(maxsize=65536)
def _key(name: str) -> str:
    """匹配键：归一化（去清晰度后缀）后，再忽略空格/横杠/间隔点/零宽 BOM 差异。

    纯函数，且 2 万行线路的频道名去重后只有几千个，缓存后全量匹配 2.3s -> 约 0.1s。
    """
    return re.sub(r"[\s\-·・\ufeff]+", "", normalize_name(name))


def template_path() -> Path:
    return Path(settings.data_dir) / "channel-template.txt"


def _parse(content: str) -> tuple[list[tuple[str, list[str]]], dict[str, tuple[str, str]]]:
    groups: list[tuple[str, list[str]]] = []
    match: dict[str, tuple[str, str]] = {}
    pure_keys: set[str] = set()
    alias_entries: list[tuple[str, str, str]] = []
    current_group = FALLBACK_GROUP

    def register(group: str, canonical: str, aliases: list[str]) -> None:
        if not canonical:
            return
        if groups and groups[-1][0] == group:
            groups[-1][1].append(canonical)
        else:
            groups.append((group, [canonical]))
        for alias in [canonical, *aliases]:
            if alias.strip():
                alias_entries.append((alias.strip(), canonical, group))

    for raw in content.splitlines():
        line = raw.replace("\ufeff", "").strip()
        if not line:
            continue
        if line.endswith(",#genre#"):
            current_group = line[: -len(",#genre#")].strip() or FALLBACK_GROUP
            continue
        if "：" in line:
            canonical, _, aliases = line.partition("：")
            register(current_group, canonical.strip(), [a.strip() for a in aliases.split("#") if a.strip()])
        elif "," in line:
            canonical, _, aliases = line.partition(",")
            register(current_group, canonical.strip(), [a.strip() for a in aliases.split("#") if a.strip()])

    # 两轮注册：先普通 setdefault，再让「纯净别名」（未因 4K/HD 后缀剥离而变形的）覆盖，
    # 避免「湖南卫视4K」抢走「湖南卫视」的匹配。
    for alias, canonical, group in alias_entries:
        key = _key(alias)
        if key:
            match.setdefault(key, (canonical, group))
    for alias, canonical, group in alias_entries:
        key = _key(alias)
        if key and normalize_name(alias) == alias.casefold() and key not in pure_keys:
            match[key] = (canonical, group)
            pure_keys.add(key)

    return groups, match


def _ensure_loaded() -> bool:
    now = time.monotonic()
    # 1 秒内直接复用上次结果，避免导出时上万次 stat 系统调用
    if now - _state["checked_at"] < _CHECK_INTERVAL:
        return bool(_state["groups"])
    path = template_path()
    _state["checked_at"] = now
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _state.update({"path": None, "mtime": None, "groups": [], "match": {}})
        return False
    if _state["path"] == str(path) and _state["mtime"] == mtime:
        return bool(_state["groups"])
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    groups, match = _parse(content)
    _state.update({"path": str(path), "mtime": mtime, "groups": groups, "match": match})
    return bool(groups)


def match_channel(name: str) -> tuple[str, str] | None:
    """按频道名匹配模版，返回 (标准频道名, 分组)；未匹配返回 None。"""
    if not _ensure_loaded():
        return None
    return _state["match"].get(_key(name))


def match_key(name: str) -> str:
    """公开 _key：供持有一致性快照的调用方自行查表（见 snapshot）。"""
    return _key(name)


def snapshot() -> tuple[list[tuple[str, list[str]]], dict[str, tuple[str, str]]] | None:
    """一次渲染用的一致性快照 (groups, match)；模版不可用返回 None。

    一次订阅导出要对 2 万行反复匹配，期间模版文件若被替换（_CHECK_INTERVAL=1s 节流
    下完全可能跨过一次重载），逐行调 match_channel() 会前半用旧模版、后半用新模版，
    而 groups() 迭代的又是另一版 —— 对不上的频道既不进 matched 也不进 unmatched，
    静默从订阅里消失。整次渲染只取一次快照即可避免。
    """
    if not _ensure_loaded():
        return None
    return _state["groups"], _state["match"]
