"""订阅内容构建（模版分组 + 多线路）与静态文件导出。

- 模版（data/channel-template.txt）存在时：频道按模版分组与标准化（CCTV1综合/CCTV-1 → CCTV1），
  同一频道的多条线路按测速速度降序排列（最快 = 线路 1）；
- 模版不存在时：退回按归一化分组（央视/地方台/其他）每频道输出最快一条；
- 静态导出：write_static_exports() 把 M3U 与 TXT 写入 data/live.m3u、data/live.txt，
  供播放器直接读取，每日同步+测速完成后自动刷新（失效线路已剔除）。
"""

from __future__ import annotations

import os
import re
import urllib.parse
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

from . import template
from .config import settings
from .db import db


def _resolve_region(region: str) -> str:
    if not region and settings.keep_region in {"domestic", "international"}:
        return settings.keep_region
    return region


# 数据源分组名的长度上限。两处共用同一个数：main._normalize_group（用户显式传参，超长 → 400）
# 与下面的 existing_group()（配置值，超长 → 忽略）。
EXPORT_GROUP_MAXLEN = 64


def existing_group(name: str) -> str:
    """把候选分组名收敛成「库里确实存在的分组名」；空白 / 超长 / 不存在一律返回 ''。

    这是**配置值**（MAIN_EXCLUDE_GROUP）与静态导出路径共用的宽松口径：配置写错、或那个
    分组以后被删干净时降级为"不排除"，绝不让主订阅 400 或直接空掉 —— 一条配置错误不该
    让全家播放器同时看不到订阅。用户显式传参走 main._normalize_group 的严格口径
    （不存在 → 400，理由是键空间与缓存放大，见那里的说明），两者的判存 SQL 收敛在这里。
    """
    text = (name or "").strip()
    if not text or len(text) > EXPORT_GROUP_MAXLEN:
        return ""
    if not db.row("SELECT 1 AS ok FROM sources WHERE source_group=? LIMIT 1", (text,)):
        return ""
    return text


def main_exclude_group() -> str:
    """主订阅默认排除的分组（MAIN_EXCLUDE_GROUP 的**生效值**；没配/无效 = ''）。

    HTTP 默认导出（/iptv、/playlist.m3u、/txt）与静态导出（write_static_exports）都取它，
    两边因此永远同口径。每次调用查一次库（sources 表 50 行级，与 _normalize_source 的
    每次点查同量级）：配置改了、或分组被删掉，下一次导出就是新行为，不必重启。
    """
    return existing_group(settings.main_exclude_group)


def _fetch_lines(favorite: bool, source: str, region: str, group: str = "",
                 exclude_group: str = "") -> list[dict]:
    """取全部活跃线路（含测速速度）；失效/不支持的线路不参与输出。

    v43：新增 `group` / `exclude_group`（数据源分组维度）。取值的存在性由
    `main._normalize_group` 在 HTTP 层校验，这里只当查询条件用；两者都是**纯查询**、
    不写库，且与 source/region 一样只是往 WHERE 里加子句，可以任意叠加。默认都是空串，
    不带新参数时生成的 SQL 与改动前逐字节相同（主订阅输出不变的前提）。

    v42：多取一列 `is_dead` —— 它是**附加**判据（连续失败达到 EXPORT_DEAD_FAIL_COUNT），
    由 `_drop_dead_lines` 在**每个频道内部**决定是否丢弃：某频道若一条可用线路都不剩，
    就保底留下排序最优的那条（见该函数）。原来的 `(h.status IS NULL OR h.status='ok')`
    条件刻意保留不动：状态级判死的行（error/unsupported/purged）根本不进组织器，
    否则"整频道只有死线路"的频道会被保底规则重新拉回订阅 —— 生产实测那会多出
    2,221 个频道、+4,092 条线路，与"让订阅变小"的目标正好相反。
    """
    clauses = ["e.active=1"]
    params: list[object] = []
    # 这条 CASE 的参数在最前面（SQL 里它出现在 WHERE 之前），params 顺序必须与之一致
    select_params: list[object] = [settings.export_dead_fail_count]
    # sources 表在本查询里没有被引用，原 JOIN 只为 2 万行各做一次无用回表，已去掉；
    # favorites 只在「仅收藏」时才需要连接
    # v40：sources 又被引用了 —— 导出的「发现源」分组需要 e 行的 source_group，
    # 于是这个 JOIN 重新加回来。代价仍是一次主键点查（slug 是 PK），实测导出耗时无可见变化；
    # 用 LEFT JOIN + COALESCE 而不是 INNER JOIN，是为了在极端情况下（外键被绕过、
    # 条目行指向已删除的源）也不会把线路整条丢掉 —— 那种行按「未分组」处理即可。
    joins = ("LEFT JOIN stream_health h ON h.fingerprint=e.stream_fingerprint\n"
             "        LEFT JOIN sources s ON s.slug=e.source_slug")
    if favorite:
        clauses.append("f.channel_key IS NOT NULL")
        joins += "\n        LEFT JOIN favorites f ON f.channel_key=e.channel_key"
    if source:
        clauses.append("e.source_slug=?")
        params.append(source)
    if region in {"domestic", "international"}:
        clauses.append("e.region=?")
        params.append(region)
    # v43 分组维度（两个条件互相独立，与 source/region 天然叠加）：
    if group:
        # 「只要这个分组」：/iptv?group=发现源 与固定出口 /discovered.m3u 走的就是这一条。
        # group 非空（HTTP 层已挡掉空串），所以 LEFT JOIN 未命中的行不会混进来。
        clauses.append("COALESCE(s.source_group, '')=?")
        params.append(group)
    if exclude_group:
        # 「去掉这个分组」：给主订阅瘦身用。COALESCE 让 LEFT JOIN 未命中（条目指向已被删掉的
        # 源）的行按未分组 '' 参与比较 —— '' <> '发现源' 成立，这类行照旧保留，不会被误删。
        clauses.append("COALESCE(s.source_group, '')<>?")
        params.append(exclude_group)
    where = " AND ".join(clauses)
    # rows_raw（sqlite3.Row）而不是 rows（dict）：下游只用 row["列名"] 取值，
    # 把 1.7 万行转成 dict 实测要 0.167s CPU，占冷导出的 19.3%，纯属白花。
    return db.rows_raw(
        f"""
        SELECT e.name, e.group_title, e.url, e.referrer, e.user_agent, e.logo, e.tvg_id,
               e.stream_fingerprint,
               e.source_slug,
               COALESCE(s.source_group, '') AS source_group,
               h.speed_kbps, h.latency_ms, h.ok_total, h.fail_total,
               CASE WHEN COALESCE(h.fail_count, 0) >= ? THEN 1 ELSE 0 END AS is_dead
        FROM source_entries e
        {joins}
        WHERE {where} AND (h.status IS NULL OR h.status='ok')
        """,
        select_params + params,
    )


def _safe_field(value: str) -> str:
    """用于 M3U 逗号之后、TXT 的字段值：逗号必须换成全角，否则会截断字段。"""
    return str(value or "").replace("\x00", "").replace("\ufeff", "").replace("\r", " ").replace("\n", " ").replace(",", "，")


def _attr(value: str) -> str:
    """用于 M3U 的属性值（tvg-id/tvg-name/tvg-logo/group-title）。

    属性在双引号里，逗号是合法的，不能像 _safe_field 那样替换掉——台标 URL 带逗号会被改坏。
    """
    return str(value or "").replace("\x00", "").replace("\ufeff", "").replace("\r", " ").replace("\n", " ").replace('"', "'")


class Line(NamedTuple):
    """一条候选线路的原始指标。"""
    speed: float | None
    latency: int | None
    fail_total: int
    ok_total: int
    url: str
    referrer: str
    user_agent: str
    logo: str
    tvg_id: str
    fingerprint: str  # 流指纹：同一频道内据此去重，避免同一地址占多个线路名额
    # v42：该线路是否「判定失效」（连续失败达到 EXPORT_DEAD_FAIL_COUNT）。
    # 默认 False，这样外部/旧调用方直接构造 Line(...) 时行为不变。
    dead: bool = False
    # v52：线路所属数据源 slug。回看属性只按它判定（`settings.export_catchup_source_slugs`），
    # 不按 URL 猜 —— 同一个 host:port 可能被多个源收录。默认 ""（旧调用方构造时行为不变）。
    source_slug: str = ""


class ChannelLine(NamedTuple):
    """导出用的一条线路。"""
    label: str        # 带编号的显示名（001 CCTV1），播放器按标题排序即呈现分组顺序
    name: str         # 标准频道名（CCTV1），用于 EPG 匹配
    url: str
    referrer: str
    user_agent: str
    logo: str
    tvg_id: str
    # v52：数据源 slug（回看属性判定用），见 Line.source_slug
    source_slug: str = ""


def _line_item(row: dict) -> Line:
    # is_dead 是 v42 新增的列；老调用方（测试、外部脚本）传进来的 row 可能没有它，
    # sqlite3.Row 缺列抛 IndexError、dict 缺键抛 KeyError —— 都按"未判定失效"处理。
    try:
        dead = bool(row["is_dead"])
    except (KeyError, IndexError):
        dead = False
    # v52 同理：source_slug 是新增列，老调用方没有它时按"未知源"处理（= 不挂回看属性）
    try:
        source_slug = row["source_slug"] or ""
    except (KeyError, IndexError):
        source_slug = ""
    return Line(
        speed=row["speed_kbps"],
        latency=row["latency_ms"],
        fail_total=row["fail_total"] or 0,
        ok_total=row["ok_total"] or 0,
        url=row["url"],
        referrer=row["referrer"] or "",
        user_agent=row["user_agent"] or "",
        logo=row["logo"] or "",
        tvg_id=row["tvg_id"] or "",
        fingerprint=row["stream_fingerprint"] or "",
        dead=dead,
        source_slug=source_slug,
    )


@lru_cache(maxsize=8)
def _slug_set(raw: str) -> frozenset[str]:
    """把逗号分隔的 slug 配置解析成集合（按原始配置串缓存）。

    `_line_order` 会对**每一条**线路求一次键（生产 2 万+ 行），所以这里不能每次
    重新 split；用不可变的原始配置串当缓存键，配置变了自然换一个新条目。
    """
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def _prefer_slugs() -> frozenset[str]:
    """偏好源 slug 集合（读设置；测试里可 monkeypatch 这个函数）。

    空集合 = 所有线路权重相同 = 排序与加这个键之前逐字节相同（"清空即回退"的实现保证）。
    """
    return _slug_set(settings.export_prefer_sources or "")


def _line_order(item: Line):
    """线路排序（线路 1 在最前）：偏好源 → 已验证 → 快 → 稳 → 延迟低 → URL。

    v53：最前面多一个「偏好源权重」。回看（catch-up）只有 ysp-live 一个源具备，而回看
    入口在播放器里通常只挂在**线路 1** 上（TiviMate/Kodi 按频道取第一条线路）；如果
    偏好源的线路排在别的源后面、甚至被 MAX_LINES_* 截掉，那个频道就等于没有回看。
    实测（v52 产物）：6,159 条线路里只有 22 条来自 ysp-live，绝大多数频道的线路 1 是别的源。

    这个键只做「同一频道内的排序偏置」，**不改**取数范围、不改每频道条数上限、
    不改分组归属：偏好源没有覆盖的频道里所有线路权重相同，排序结果与原来完全一致
    （Python 的 sort 是稳定的，且其余键逐项相同）—— 这正是"没有该源的频道逐字节不变"的
    实现依据。`EXPORT_PREFER_SOURCES` 清空时集合为空，全部权重相同，行为回到改动前。

    速度之所以仍排在权重之后：偏好源是**回放能力**的唯一来源，而速度只在"同一能力档位内"
    才有可比性 —— 拿一个不可回看的快线路顶掉唯一可回看的线路，丢的是功能而不是体验。
    """
    prefer = _prefer_slugs()
    return (
        0 if (item.source_slug or "") in prefer else 1,
        item.speed is None,
        -(item.speed or 0),
        item.fail_total,
        -item.ok_total,
        item.latency if item.latency is not None else 10 ** 9,
        item.url,
    )


def _dedupe_lines(lines: list[Line]) -> list[Line]:
    """同一频道内按流指纹去重（调用前须已排序，保留排名最优的那条）。

    同一路流会被多个源收录、或在同源内因 tvg_id 大小写等差异留下多行；导出若按
    「条目行」而不是「流指纹」截断，同一个地址就会占掉多个线路名额。实测四川科教
    limit=5 导出的 5 条里有 4 条是同一 URL，第 3 条真正可用的线路被挤出订阅，
    而 /api/channels 的 usable_streams 只算 1 条 —— 播放器则显示「线路 1..4」全是同一直播源。
    """
    seen: set[str] = set()
    out: list[Line] = []
    for line in lines:
        key = line.fingerprint or line.url
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return out


def _hide_dead_enabled() -> bool:
    """导出时是否隐藏判死线路（读设置；测试里可 monkeypatch 这个函数）。"""
    return bool(settings.export_hide_dead)


def _drop_dead_lines(lines: list[Line], stats: dict | None = None) -> list[Line]:
    """丢掉「判定失效」的线路，但**每个频道至少保底留 1 条**（v42）。

    为什么需要保底：过滤按的是"连续失败次数"，而一个频道可能所有线路都刚好被判死；
    若直接全丢，用户看到的是"这个频道不见了"——那是比"线路画质差"更难理解的故障。
    保底取排序后的第一条（`_line_order`：已验证 → 快 → 稳 → 延迟低），也就是这批里
    相对最可能可用的那条。

    调用前必须已经 sort + dedupe（保底取的就是 lst[0]）。
    开关关掉时（EXPORT_HIDE_DEAD=false）原样返回，行为与改动前完全一致。
    """
    if not _hide_dead_enabled():
        return lines
    live = [line for line in lines if not line.dead]
    if live:
        if stats is not None:
            stats["filtered_dead"] = stats.get("filtered_dead", 0) + (len(lines) - len(live))
        return live
    # 全死：保底留一条。stats 里单独计数，方便核对"有多少频道是靠保底活着的"
    if stats is not None:
        stats["kept_alive_channels"] = stats.get("kept_alive_channels", 0) + 1
    return lines[:1]


def _pick_channel_meta(lines: list[Line]) -> tuple[str, str]:
    """从同一频道的多条线路里挑一个可用的台标与 EPG id（优先排名靠前的线路）。

    台标是频道级信息：某条线路没带 logo 不代表这个频道没台标，所以取第一条非空的即可，
    这样能把台标覆盖率从「线路级」提升到「频道级」。
    """
    logo = next((line.logo for line in lines if line.logo), "")
    tvg_id = next((line.tvg_id for line in lines if line.tvg_id), "")
    return logo, tvg_id


def _single_group_channels(channel_groups: dict[str, set[str]]) -> dict[str, str]:
    """算出「整频道只由一个分组源提供」的频道 → 该分组名（v40 的导出分组覆盖表）。

    判定口径（纯查询结果推导，不写库）：
      * 输入是「导出频道名 → 该频道**全部可导出线路**所属的 source_group 集合」；
      * 集合里**有且只有一个非空**分组时才覆盖 —— 例如三条线路都来自「发现源」的频道，
        整频道改挂到「发现源」，在 M3U/TXT 里单独成组；
      * 只要还有任意一条线路来自未分组源（source_group=''），或线路横跨两个不同分组，
        就**保持原有分组**（央视/地方台/其他）—— 否则同一频道会同时出现在两个分组里。
    为什么按「可导出线路」而不是库里全部线路判定：已失效（h.status='error'/'unsupported'）
    的线路根本不会进导出，不可能造成重复；为它们拖住整个频道的分组没有意义。
    """
    overrides: dict[str, str] = {}
    for name, groups in channel_groups.items():
        if len(groups) != 1:
            continue
        only = next(iter(groups))
        if only:
            overrides[name] = only
    return overrides


def _apply_group_overrides(
    groups: list[tuple[str, list["ChannelLine"]]], overrides: dict[str, str]
) -> list[tuple[str, list["ChannelLine"]]]:
    """把「整频道由某个分组源提供」的频道从标准分组搬到该分组（返回新的分组列表）。

    只搬运、不复制：频道在原分组里被移除，因此不会出现「同一频道在两个组里各一份」。
    新分组一律**追加在最后**（组内顺序保持不变），保证同一份数据每次导出的分组顺序
    完全一致 —— ETag 才有意义，播放器里的分组也不会每次刷新就换位置。
    """
    if not overrides:
        return groups
    kept: list[tuple[str, list[ChannelLine]]] = []
    moved: dict[str, list[ChannelLine]] = {}
    for group, entries in groups:
        stay: list[ChannelLine] = []
        for entry in entries:
            target = overrides.get(entry.name)
            if target and target != group:
                moved.setdefault(target, []).append(entry)
            else:
                stay.append(entry)
        if stay:
            kept.append((group, stay))
    for target in sorted(moved):
        # 目标分组恰好已经存在（例如无模版兜底路径里 group_title 本来就叫「发现源」）时
        # 合并进去，不新建重复分组
        existing = next((i for i, (name, _) in enumerate(kept) if name == target), None)
        if existing is None:
            kept.append((target, moved[target]))
        else:
            kept[existing] = (target, kept[existing][1] + moved[target])
    return kept


def _group_bucket(group: str) -> str:
    """模版细分组 → 三大分组：央视 / 地方台 / 其他。"""
    if group in {"央视频道", "中数传媒", "4K节目"}:
        return "央视"
    if group == template.FALLBACK_GROUP:
        return "其他"
    return "地方台"


def _is_cctv(name: str, group_title: str = "") -> bool:
    """是否央视系频道：口径与导出分组一致（模版优先，其次 group_title）。"""
    matched = template.match_channel(name)
    if matched:
        return _group_bucket(matched[1]) == "央视"
    return (group_title or "").strip() == "央视"


def _limit_for_bucket(bucket: str, multiline: bool) -> int:
    if not multiline:
        return 1
    return settings.max_lines_cctv if bucket == "央视" else settings.max_lines_per_channel


def channel_line_limit(name: str, group_title: str = "") -> int:
    """某频道保留的线路条数：央视系用 MAX_LINES_CCTV，其余用 MAX_LINES_PER_CHANNEL。

    这是「多线路」模式下的上限，也是/频道详情/页展示的备选条数依据；单线路模式的上限由
    `_limit_for_bucket(..., multiline=False)` 在导出内部处理，不经过这里（原先这里留了个
    `multiline` 参数，但没有任何调用点传过它，纯属误导）。
    """
    return _limit_for_bucket("央视" if _is_cctv(name, group_title) else "地方台", True)


def _organize(rows: list[dict], multiline: bool, groups_tpl: list, match_map: dict,
              stats: dict | None = None) -> list[tuple[str, list[tuple[str, str, str, str]]]]:
    """按模版匹配 + 三大分组（央视 → 地方台 → 其他）组织线路。

    匹配模版的频道加连续编号前缀（001 CCTV1...），任何播放器按标题排序即呈现分组顺序；
    同一频道的多条线路共用同一编号名（播放器识别为线路 1/2/3）；
    未匹配的轮播/杂项使用 9001 起的大编号沉底。
    线路按「快 + 稳」排序，央视系频道保留 MAX_LINES_CCTV 条备选。
    """
    matched: dict[tuple[str, str], list[Line]] = {}
    unmatched: dict[str, list[Line]] = {}
    # v40：导出频道名 → 该频道线路来源的数据源分组集合，供 _single_group_channels 判定
    # 「整频道是否只由一个分组源提供」。键用的是**导出后的频道名**（模版命中用规范名，
    # 未命中用 _safe_field(原始名)），与下面 buckets 里 ChannelLine.name 的口径一致。
    channel_groups: dict[str, set[str]] = {}
    for row in rows:
        m = match_map.get(template.match_key(row["name"]))
        item = _line_item(row)
        if m:
            canonical, group = m
            matched.setdefault((group, canonical), []).append(item)
            channel_name = canonical
        else:
            channel_name = _safe_field(row["name"])
            unmatched.setdefault(channel_name, []).append(item)
        channel_groups.setdefault(channel_name, set()).add((row["source_group"] or "").strip())

    buckets: dict[str, list[ChannelLine]] = {"央视": [], "地方台": [], "其他": []}
    # 编号必须定宽：模板频道数一旦超过 999，`:03d` 会让 "1000" 按字符串排在 "101" 前面、
    # 而 "999" 又排在 "9001" 之后（未匹配块专门做过定宽防护，这里之前漏了）。
    total_matched = sum(len(names) for _, names in groups_tpl)
    width = max(3, len(str(total_matched)))
    counter = 1
    for group, canonicals in groups_tpl:
        bucket = _group_bucket(group)
        limit = _limit_for_bucket(bucket, multiline)
        for canonical in canonicals:
            lst = matched.pop((group, canonical), None)
            if not lst:
                continue
            label = f"{counter:0{width}d} {canonical}"
            counter += 1
            lst.sort(key=_line_order)
            lst = _dedupe_lines(lst)
            # v42：判死线路在这里丢（每频道保底 1 条，见 _drop_dead_lines）
            lst = _drop_dead_lines(lst, stats)
            logo, tvg_id = _pick_channel_meta(lst)
            for line in lst[:limit]:
                buckets[bucket].append(ChannelLine(label, canonical, line.url, line.referrer,
                                                   line.user_agent, logo, tvg_id, line.source_slug))

    if unmatched:
        # 编号必须「定宽」且「首位保持 9」，两个条件缺一不可：
        #  - 不定宽：5 位数的 "10000" 会按字符串比较排到 4 位数的 "9001" 前面；
        #  - 补零到 "09001"：首位变 0，会被 3 位数的 "420" 反超（'4' > '0'）。
        # 因此按条数选宽度，并让基址始终是 9000…01 这种同位数的高位值。
        width = 4
        while (10 ** width - 1) - (9 * 10 ** (width - 1) + 1) + 1 < len(unmatched):
            width += 1
        un_counter = 9 * 10 ** (width - 1) + 1
        limit = _limit_for_bucket("其他", multiline)
        for name, lst in sorted(unmatched.items()):
            lst.sort(key=_line_order)
            lst = _dedupe_lines(lst)
            lst = _drop_dead_lines(lst, stats)
            logo, tvg_id = _pick_channel_meta(lst)
            label = f"{un_counter:0{width}d} {name}"
            for line in lst[:limit]:
                buckets["其他"].append(ChannelLine(label, name, line.url, line.referrer,
                                                   line.user_agent, logo, tvg_id, line.source_slug))
            un_counter += 1

    # 最后再搬分组：编号、线路排序与截断都已按原分组做完，搬运只改频道挂在哪个组，
    # 不影响「线路 1 是最快的那条」这类组内语义。
    return _apply_group_overrides(
        [(k, v) for k, v in buckets.items() if v], _single_group_channels(channel_groups)
    )


def _fallback_organize(rows: list[dict], multiline: bool,
                       stats: dict | None = None) -> list[tuple[str, list[ChannelLine]]]:
    """无模版时的兜底：按归一化分组（group_title）输出。"""
    buckets: dict[str, dict[str, list[Line]]] = {}
    # v40：与模版路径同一套分组覆盖判定（口径见 _single_group_channels）
    channel_groups: dict[str, set[str]] = {}
    for row in rows:
        group = (row["group_title"] or "直播频道").strip() or "直播频道"
        name = _safe_field(row["name"])
        buckets.setdefault(group, {}).setdefault(name, []).append(_line_item(row))
        channel_groups.setdefault(name, set()).add((row["source_group"] or "").strip())
    result: list[tuple[str, list[ChannelLine]]] = []
    for group, names in buckets.items():
        limit = _limit_for_bucket("央视" if group == "央视" else "地方台", multiline)
        entries: list[ChannelLine] = []
        for name, lst in names.items():
            lst.sort(key=_line_order)
            # 模版路径在 _organize 里做了指纹去重，兜底路径漏了：模版文件缺失时
            # 同一 URL 照样会占满线路名额（四川科教 5 条 4 条同 URL 那类事故）
            lst = _dedupe_lines(lst)
            lst = _drop_dead_lines(lst, stats)
            logo, tvg_id = _pick_channel_meta(lst)
            for line in lst[:limit]:
                entries.append(ChannelLine(name, name, line.url, line.referrer,
                                           line.user_agent, logo, tvg_id, line.source_slug))
        entries.sort(key=lambda e: e.label)
        result.append((group, entries))
    # 与模版路径同样在最后搬运分组（口径见 _single_group_channels）
    return _apply_group_overrides(result, _single_group_channels(channel_groups))


def _organize_rows(rows: list[dict], mode: str, stats: dict | None = None):
    """按模版（存在时）或归一化分组组织线路；multi 模式保留每频道多条线路。

    stats（可选，v42）会写入本次渲染的过滤统计：`filtered_dead`（被隐藏的判死线路数）、
    `kept_alive_channels`（靠"至少留 1 条"保底才没消失的频道数）。调用方据此在
    响应头 / 日志里给出可核对的数字，不必再算一遍。
    """
    multiline = mode == "multi"
    # 整次渲染用同一份模版快照：渲染中途模版热重载会让部分行按旧版匹配、
    # 分组按新版迭代，对不上的频道静默消失
    snap = template.snapshot()
    if snap is not None:
        return _organize(rows, multiline, *snap, stats=stats)
    # 无模版时也要遵循 multi：此前恒传 1，导致 /iptv?mode=multi 与 /txt?mode=multi 线路数不一致
    return _fallback_organize(rows, multiline, stats=stats)


def _catchup_enabled() -> bool:
    """回看属性总开关（读设置；测试里可 monkeypatch 这个函数）。"""
    return bool(settings.export_catchup)


def _catchup_slugs() -> set[str]:
    """允许挂回看属性的数据源 slug 集合（逗号分隔配置，默认只有内置源 ysp-live）。"""
    return {s.strip() for s in (settings.export_catchup_source_slugs or "").split(",") if s.strip()}


# 频道名只取 URL 最后一段的「安全子集」：这段会被拼进 catchup-source 模板（一个带引号的
# 属性值），带空格/引号/斜杠等字符会让属性串变形，宁可退回不带 `{slug}` 的模板形态。
_CATCHUP_SLUG_RE = re.compile(r"^[A-Za-z0-9._~-]{1,64}$")


def _catchup_slug(url: str) -> str:
    """从线路地址推出频道名（`http://h:8767/cctv1.m3u8` → `cctv1`）。

    只看 URL 的**路径最后一段**（不看主机名：`http://host.example/?token=x` 这种地址里
    主机名不是频道名，拿它当 slug 会生成一个取不到的回看地址）。只有模板里出现 `{slug}`
    时才会用到；推不出来就返回空串，调用方据此**放弃挂属性**。
    """
    path = urllib.parse.urlsplit(str(url or "")).path
    tail = path.rstrip("/").rsplit("/", 1)[-1]
    for suffix in (".m3u8", ".m3u", ".ts"):
        if tail.lower().endswith(suffix):
            tail = tail[: -len(suffix)]
            break
    return tail if _CATCHUP_SLUG_RE.match(tail) else ""


def _catchup_attrs(entry: "ChannelLine") -> str:
    """给「具备回看能力的数据源」的线路生成 EXTINF 上的 catchup 属性串（v52）。

    判据是 `entry.source_slug`（精确到源），**不是 URL**：同一个 host:port 被别的源
    收录时不该跟着沾光。不属于允许列表 / 开关关闭 / 模板为空 → 返回空串，
    产物与改动前逐字节相同（这是"开关一关即回到原样"的实现保证）。

    catchup 模式按模板形状选，与 Kodi 的两种语义严格对应：
      * 查询串（`?…` / `&…`）→ `append`：拼到频道地址后面；
      * 完整地址（`http(s)://…`）→ `default`：整条回看地址由 catchup-source 给出。
    把完整地址配成 `append` 是自相矛盾的写法（播放器会把整条 URL 再拼到频道地址后面），
    所以这里不接受"模板形状"与"模式"由用户各填一半，只由形状决定模式。
    """
    if not _catchup_enabled():
        return ""
    if (entry.source_slug or "") not in _catchup_slugs():
        return ""
    tmpl = (settings.export_catchup_source or "").strip()
    if not tmpl:
        return ""
    if "{slug}" in tmpl:
        slug = _catchup_slug(entry.url)
        if not slug:
            return ""
        tmpl = tmpl.replace("{slug}", slug)
    mode = "default" if tmpl.lower().startswith(("http://", "https://")) else "append"
    days = int(settings.export_catchup_days)
    return f' catchup="{mode}" catchup-days="{days}" catchup-source="{_attr(tmpl)}"'


def _render_m3u(rows: list[dict], mode: str, tvg_url: str = "", stats: dict | None = None) -> str:
    # tvg_url 为**本机 EPG 代理**地址（`/epg.xml`）。以前这里直接写 settings.epg_url，
    # 于是播放器拿到的节目单地址里带着上游令牌，随播放列表一起扩散
    # （谁拿到订阅就等于拿到那个长期有效的 EPG 令牌）。现在由调用方传入本机地址；
    # 传空（未配置 EPG_URL、或无法推断本机地址）时不写 x-tvg-url。
    head = f'#EXTM3U x-tvg-url="{_attr(tvg_url)}"' if tvg_url else "#EXTM3U"
    out = [head]
    for group, entries in _organize_rows(rows, mode, stats):
        for entry in entries:
            # 台标：之前完全没输出 tvg-logo，播放器只能显示占位图
            logo = f'tvg-logo="{_attr(entry.logo)}" ' if entry.logo else ""
            # tvg-id 用真实 EPG 标识（原来填的是带编号的显示名，EPG 永远匹配不上）
            epg_id = _attr(entry.tvg_id) or _attr(entry.name)
            # tvg-name 必须保留编号前缀：很多播放器按 tvg-name 排序/显示，
            # 去掉编号就会丢掉「央视 → 地方台 → 其他」的顺序（按字母序把 [超清] 之类排到中文前面）
            out.append(
                f'#EXTINF:-1 {logo}tvg-id="{epg_id}" tvg-name="{_attr(entry.label)}" '
                f'group-title="{_attr(group)}"{_catchup_attrs(entry)},{_safe_field(entry.label)}'
            )
            if entry.referrer:
                out.append(f"#EXTVLCOPT:http-referrer={entry.referrer}")
            if entry.user_agent:
                out.append(f"#EXTVLCOPT:http-user-agent={entry.user_agent}")
            out.append(entry.url)
    return "\n".join(out) + "\n"


def build_m3u(mode: str = "multi", favorite: bool = False, source: str = "", region: str = "",
              tvg_url: str = "", stats: dict | None = None,
              group: str = "", exclude_group: str = "") -> str:
    return _render_m3u(
        _fetch_lines(favorite, source, _resolve_region(region), group, exclude_group),
        mode, tvg_url, stats,
    )


def _render_txt(rows: list[dict], mode: str, stats: dict | None = None) -> str:
    out: list[str] = []
    for group, entries in _organize_rows(rows, mode, stats):
        out.append(f"{_safe_field(group)},#genre#")
        out.extend(f"{entry.label},{entry.url}" for entry in entries)
    return "\n".join(out) + "\n"


def build_txt(mode: str = "multi", favorite: bool = False, source: str = "", region: str = "",
              stats: dict | None = None,
              group: str = "", exclude_group: str = "") -> str:
    return _render_txt(
        _fetch_lines(favorite, source, _resolve_region(region), group, exclude_group),
        mode, stats,
    )


def _atomic_write(path: Path, content: str) -> None:
    """原子写：先写临时文件再同卷 rename。

    播放器（DIYP/TiviMate）是定时拉取的，直接覆盖写会让它读到截断文件；
    进程中途被杀更会留下半个订阅文件、被当作「最新有效订阅」一直服务到下次导出。
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def write_static_exports() -> dict[str, int]:
    """把当前订阅结果生成静态文件到 data 目录（失效线路已剔除、按速度排序）。

    v42：返回值里多带 `filtered_dead` / `kept_alive_channels` —— m3u 与 txt 用的是同一份
    行数据、同一套过滤，所以两者的数字天然一致，这里取 m3u 那次渲染的统计（调用方打日志）。

    v43：静态文件与 HTTP 默认出口同口径 —— 同样套用 MAIN_EXCLUDE_GROUP（main_exclude_group()）。
    静态文件是「不带任何参数的主订阅」，配置了默认排除却只改 HTTP 出口、不改静态文件，
    两边行数就会对不上（用户拿 data/live.m3u 直接喂播放器时最容易踩到）。
    """
    result: dict[str, int] = {}
    # m3u 与 txt 复用同一份线路数据，避免把 2 万行查两遍
    try:
        rows = _fetch_lines(False, "", _resolve_region(""), "", main_exclude_group())
    except Exception:
        return {"m3u": -1, "txt": -1}
    m3u_stats: dict = {}
    try:
        m3u = _render_m3u(rows, "multi", settings.epg_proxy_url, m3u_stats)
        _atomic_write(Path(settings.data_dir) / "live.m3u", m3u)
        # 不能只数 startswith("http")：rtmp/rtsp/udp 等协议线路也会正常导出
        result["m3u"] = sum(1 for line in m3u.splitlines() if line and not line.startswith("#"))
    except Exception:
        result["m3u"] = -1
    try:
        txt = _render_txt(rows, "multi", {})
        _atomic_write(Path(settings.data_dir) / "live.txt", txt)
        result["txt"] = sum(1 for line in txt.splitlines() if line and not line.endswith(",#genre#"))
    except Exception:
        result["txt"] = -1
    result["filtered_dead"] = int(m3u_stats.get("filtered_dead", 0))
    result["kept_alive_channels"] = int(m3u_stats.get("kept_alive_channels", 0))
    return result
