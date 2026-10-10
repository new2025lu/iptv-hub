from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

ALLOWED_STREAM_SCHEMES = {"http", "https", "rtmp", "rtsp", "rtp", "udp", "mmsh", "srt"}
ATTRIBUTE_RE = re.compile(r'([\w-]+)="([^"]*)"')
# 畸形 #EXTINF 行的识别依据：切出来的「标题」里若还残留这些属性标记，说明切分点选错了
_ATTR_MARK_RE = re.compile(
    r'(?:tvg-id|tvg-name|tvg-logo|tvg-country|tvg-language|tvg-shift|group-title'
    r'|http-user-agent|http-referrer|http-referer)\s*=\s*"',
    re.I,
)
QUALITY_SUFFIX_RE = re.compile(r"\s*(?:\(|\[)?(?:4K|UHD|FHD|HD|SD|2160[pi]|1080[pi]|720p|576[pi]|540p|480p|360p|240p)(?:\)|\])?\s*$", re.I)
COUNTRY_RE = re.compile(r"\.([a-z]{2})(?:@|$)", re.I)
# 解析热路径上的正则一律预编译：用字符串模式调用每次都要走 re 的 512 条缓存查表，
# 实测 2.3 万条的源上这类查表有 9.4 万次（每条 4 次），纯属固定开销。
_DASH_NORMALIZE_RE = re.compile(r"[‐‑‒–—−]")
_WHITESPACE_RE = re.compile(r"\s+")
_TITLE_QUALITY_RE = re.compile(r"\(([^()]*(?:2160|1080|720|576|540|480|360|240)[pi])\)", re.I)
_TITLE_LABEL_RE = re.compile(r"\[([^\]]+)\]")


@dataclass(slots=True)
class ParsedEntry:
    entry_key: str
    stream_fingerprint: str
    channel_key: str
    name: str
    normalized_name: str
    tvg_id: str
    logo: str
    group_title: str
    country: str
    language: str
    quality: str
    url: str
    user_agent: str
    referrer: str
    labels: str = ""

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
CJK_PATTERN = re.compile(r"[\u4e00-\u9fff]")

# v55：tvgo-local 是本机自建中继（wjecee/tv-go），只播国内频道，但 CCTV 那 18 路的
# tvg-name 是纯 ASCII（"CCTV-1"）、group_title 又是 "CCTV"（不在 DOMESTIC_GROUPS 里），
# 兜底的 CJK 正则匹配不到 → 会被判成 international，再被 KEEP_REGION=domestic 整批丢掉
# （实测 items_seen=58 却只落库 40）。加进这个白名单 = 按源整体信任，与 jiangsu-local 同口径。
# v56：两条 nTv 适配器源都显式列入白名单（按源整体信任）。
#   * ntv-fm 是**必须**的：48 条里 46 条是 SomaFM —— group_title="SomaFM" 不在
#     DOMESTIC_GROUPS、频道名又是纯 ASCII（"Groove Salad"），兜底的 CJK 正则匹配不到
#     → 整批被判成 international，再被 KEEP_REGION=domestic 丢掉（与 tvgo-local 同形态：
#     items_seen 有值、落库为 0）。另外 2 条国内电台靠 "国内" 子串本来就能过。
#   * ntv-cctv **今天不加也能过**（group_title="央视频道" 命中了下面的 "央视" 子串判定），
#     显式列入是防御性的：适配器的兜底分组名一旦改动、或上游某条 EXTINF 少了 group-title，
#     不会整批静默消失 —— 这类「靠启发式恰好成立」的路径不值得依赖。
DOMESTIC_SOURCES = {"hacks-domestic", "zbds-domestic", "cctv-live", "guovin-live", "ifansclub-live", "ifansclub-local", "tvgo-local", "jiangsu-local", "spider-local", "aggr-local", "local-19105", "suxuang-live", "vbskycn-live", "yang-live", "fanmingming-v6", "ntv-cctv", "ntv-fm"}
DOMESTIC_COUNTRIES = {"cn", "hk", "mo", "tw", "zh"}
DOMESTIC_GROUPS = {"央视", "卫视", "国内", "中国大陆", "中国", "地方", "港澳台", "中文", "华语"}


def region_of(source_slug: str, country: str, group_title: str = "", name: str = "") -> str:
    """判定频道区域：domestic（国内）/ international（国外）。"""
    slug = (source_slug or "").lower()
    if slug in DOMESTIC_SOURCES:
        return "domestic"
    code = (country or "").lower().strip()
    if code:
        return "domestic" if code in DOMESTIC_COUNTRIES else "international"
    group = (group_title or "").strip()
    if group in DOMESTIC_GROUPS or any(token in group for token in ("央视", "卫视", "国内", "中国")):
        return "domestic"
    if CJK_PATTERN.search(name or ""):
        return "domestic"
    return "international"


def normalize_group(name: str, group_title: str, region: str) -> str:
    """频道分组归一化：国内 → 央视 / 地方台 / 其他；国外 → 国外。

    央视：CCTV / 央视字样；
    地方台：具备电视台特征（卫视、地方、省市台、新闻综合、都市、民生等）；
    其他：轮播/点播类及无法归类的频道。
    """
    if region == "international":
        return "国外"
    text = f"{clean_text(name, 64)} {clean_text(group_title, 64)}"
    if "cctv" in (name or "").lower() or "央视" in text:
        return "央视"
    channel_tokens = (
        "卫视", "地方", "省台", "市台", "电视台", "新闻综合", "综合频道", "都市", "民生", "经济",
        "影视", "少儿", "体育", "科教", "公共", "生活", "娱乐", "旅游", "纪录", "戏曲",
        "农业", "军事", "高清", "电影", "电视剧", "文艺", "法治", "气象", "移动", "海洋",
        "音乐", "新闻频道", "交通",
    )
    if any(token in text for token in channel_tokens):
        return "地方台"
    return "其他"


def clean_text(value: object, limit: int = 512) -> str:
    text = "" if value is None else str(value)
    return text.replace("\x00", "").replace("\ufeff", "").replace("\r", " ").replace("\n", " ").strip()[:limit]


def normalize_name(name: str) -> str:
    value = unicodedata.normalize("NFKC", clean_text(name, 256)).casefold()
    value = _DASH_NORMALIZE_RE.sub("-", value)
    value = _WHITESPACE_RE.sub(" ", value).strip()
    previous = None
    while previous != value:
        previous = value
        value = QUALITY_SUFFIX_RE.sub("", value).strip()
    return value or "未命名频道"


def normalize_stream_url(url: str) -> str:
    raw = clean_text(url, 4096)
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_STREAM_SCHEMES:
        raise ValueError(f"unsupported stream scheme: {scheme or 'missing'}")
    host = (parts.hostname or "").lower()
    # 所有协议都要求 host：`rtmp://` / `udp://` 这类空 host 会被 urlunsplit 归一成
    # `rtmp:` 这种垃圾串，而它正是 stream_fingerprint 的唯一来源 —— 不同的坏地址会
    # 塌缩成同一指纹、共用一条健康记录，还可能以「未测」状态混进订阅。
    if not host:
        raise ValueError("stream URL has no host")
    # IPv6 字面量必须带方括号重建 netloc：parts.hostname 返回的是**不带括号**的形式，
    # 直接拼会得到 `http://2001:db8::1:8080/x` 这种非法 URL —— 更糟的是
    # `[2001:db8::1]:8080`（host+port）与 `[2001:db8::1:8080]`（纯 host）会归一成同一串，
    # 两条不同的流塌缩成同一 stream_fingerprint、共用一条健康记录互相污染。
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    netloc = host
    if parts.username:
        auth = parts.username
        if parts.password:
            auth += f":{parts.password}"
        netloc = f"{auth}@{netloc}"
    if port:
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc or parts.netloc.lower(), parts.path, parts.query, ""))


def entry_key_for(tvg_id: object, stream_fingerprint: str) -> str:
    """条目冲突键 = 「频道身份 + 流指纹」的 sha256。

    频道身份用 tvg-id 去掉 `@后缀` 再 casefold —— 与 channel_key 的 `id:{base_id}`
    同一套口径：entry_key 标识「某源里的某条线路」，channel_key 标识「哪个频道」。

    **这条公式只有这一处**：make_entry() 写新条目、db._migrate() 重算存量键都必须调它。
    曾经 db.py 内联了一份「带 split('@')/casefold」的版本而 make_entry 用原始 tvg_id，
    结果生产 62791 行里 28580 行存的是迁移键：这些源下次真正解析内容时，每条线路都会
    按另一套键再插一行（旧行留下 active=0），条目与 items_added 成倍虚增、first_seen_at
    被重置，而每次重启迁移又把重复合并回去 —— 库在两个口径之间来回震荡、永不收敛。
    """
    external_id = clean_text(tvg_id, 256)
    base_id = external_id.split("@", 1)[0].strip().casefold() if external_id else ""
    return hashlib.sha256(f"{base_id}\n{stream_fingerprint}".encode("utf-8")).hexdigest()


def make_entry(
    *,
    name: object,
    url: object,
    tvg_id: object = "",
    logo: object = "",
    group_title: object = "",
    country: object = "",
    language: object = "",
    quality: object = "",
    user_agent: object = "",
    referrer: object = "",
    labels: object = "",
) -> ParsedEntry:
    display_name = clean_text(name, 256) or clean_text(tvg_id, 256) or "未命名频道"
    raw_url = clean_text(url, 4096)
    normalized_url = normalize_stream_url(raw_url)
    external_id = clean_text(tvg_id, 256)
    base_id = external_id.split("@", 1)[0].strip().casefold() if external_id else ""
    inferred_country = clean_text(country, 8).lower()
    if not inferred_country and external_id:
        match = COUNTRY_RE.search(external_id)
        if match:
            inferred_country = match.group(1).lower()
    normalized = normalize_name(display_name)
    group = clean_text(group_title, 128)
    agent = clean_text(user_agent, 1024)
    referer = clean_text(referrer, 2048)
    if base_id:
        channel_key = f"id:{base_id}"
    else:
        context = f"{normalized}|{inferred_country}|{group.casefold()}"
        channel_key = "name:" + hashlib.sha256(context.encode("utf-8")).hexdigest()[:24]
    stream_seed = f"{normalized_url}\n{referer}\n{agent}"
    stream_fingerprint = hashlib.sha256(stream_seed.encode("utf-8")).hexdigest()
    # entry_key 里**不放 display_name**：它是 upsert 的冲突键，一旦带上显示名，
    # 同一线路只要改名（节目名当频道名的那种源每次同步都变）就变成一条新条目 ——
    # first_seen_at 与收藏（channel_key）随之丢失、added/removed 虚高、库持续膨胀。
    # 用「频道身份（tvg-id 去掉 @ 后缀、casefold）+ 流指纹」，两者都只跟线路本身有关。
    # 公式必须与 db.py 的存量迁移共用 entry_key_for()，否则两边算出不同的键：
    # 迁移每次启动把行改成迁移键、解析写回时又按另一套键插入，条目成倍虚增且永不收敛。
    entry_key = entry_key_for(external_id, stream_fingerprint)
    return ParsedEntry(
        entry_key=entry_key,
        stream_fingerprint=stream_fingerprint,
        channel_key=channel_key,
        name=display_name,
        normalized_name=normalized,
        tvg_id=external_id,
        logo=clean_text(logo, 2048),
        group_title=group or "未分类",
        country=inferred_country,
        language=clean_text(language, 32),
        quality=clean_text(quality, 32),
        url=raw_url,
        user_agent=agent,
        referrer=referer,
        labels=clean_text(labels, 128),
    )


def _split_extinf(line: str) -> tuple[str, str]:
    """把 #EXTINF 行切成 (元数据, 标题)。

    标准格式是 `#EXTINF:-1 <属性...>,标题`。但线上确实存在畸形行，只取「第一个引号外逗号」会切错：

    1. `#EXTINF:-1,<属性串>,标题` —— 时长后面被多写了一个逗号（suxuang 源的 4K 频道行）；
    2. 属性值（URL）未加引号或整行被源站写重复，引号配对错乱，第一个「引号外逗号」
       落在 URL 中间（local-19105 源，实测该行 11 个引号且属性块重复了两遍）。

    这两类的共同特征是：**切出来的「标题」里仍然含有 `tvg-xxx="` 这类属性标记**。
    因此这里不只取第一个引号外逗号，而是跳过所有会让标题残留属性标记的候选切分点，
    继续往后找；正常标题里自带的逗号不会被误伤（它们不会引入属性标记）。
    """
    # 快路径：整行没有任何引号属性时，第一个逗号就是切分点。
    # 原实现对每个字符做一次 Python 级循环（一条 200+ 字符的 EXTINF 行就是 200 次迭代，
    # 实测占解析自身耗时的 1.4s/2.3 万条）。这里与下面的慢路径**语义完全等价**：
    # 无引号 ⇒ 标题里不可能出现 `tvg-id="` 这类属性标记（该正则要求双引号），
    # 所以「跳过会让标题残留属性标记的切分点」这条规则在无引号时永不触发。
    if '"' not in line:
        index = line.find(",")
        if index < 0:
            return line, ""
        return line[:index], line[index + 1:]
    in_quotes = False
    for index, char in enumerate(line):
        if char == '"':
            in_quotes = not in_quotes
        elif char == "," and not in_quotes:
            title = line[index + 1 :]
            if _ATTR_MARK_RE.search(title[:512]):
                continue
            return line[:index], title
    return line, ""


def parse_m3u(content: bytes) -> list[ParsedEntry]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("gb18030", errors="replace")
    entries: list[ParsedEntry] = []
    pending: dict[str, str] | None = None
    ext_group = ""
    for raw_line in text.splitlines():
        if len(raw_line) > 16384:
            continue
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            metadata, title = _split_extinf(line)
            attrs = {key.lower(): value for key, value in ATTRIBUTE_RE.findall(metadata)}
            pending = {
                "name": title.strip() or attrs.get("tvg-name", ""),
                "tvg_id": attrs.get("tvg-id", ""),
                "logo": attrs.get("tvg-logo", ""),
                "group_title": attrs.get("group-title", "") or ext_group,
                "country": attrs.get("tvg-country", ""),
                "language": attrs.get("tvg-language", ""),
                "user_agent": attrs.get("http-user-agent", ""),
                "referrer": attrs.get("http-referrer", ""),
                "quality": "",
                "labels": "",
            }
            quality = _TITLE_QUALITY_RE.search(title)
            if quality:
                pending["quality"] = quality.group(1)
            labels = _TITLE_LABEL_RE.findall(title)
            pending["labels"] = ", ".join(labels)
            continue
        if line.startswith("#EXTGRP:"):
            ext_group = clean_text(line.partition(":")[2], 128)
            if pending:
                pending["group_title"] = pending.get("group_title") or ext_group
            continue
        if line.startswith("#EXTVLCOPT:") and pending:
            option = line[len("#EXTVLCOPT:") :]
            key, _, value = option.partition("=")
            if key == "http-user-agent":
                pending["user_agent"] = value
            elif key in {"http-referrer", "http-referer"}:
                pending["referrer"] = value
            continue
        if line.startswith("#"):
            continue
        if pending:
            try:
                entries.append(make_entry(url=line, **pending))
            except (ValueError, UnicodeError):
                pass
            pending = None
    return _dedupe_entries(entries)


def genre_header(line: str) -> str:
    """`分组名,#genre#` 分组头 → 分组名；不是分组头返回空串。

    v60：这类 txt 清单用单独一行 `分组名,#genre#` 来分组（国内 IPTV 清单的通用写法）。
    以前它既不匹配「名称,http://…」也不含 URL，被当坏行丢掉 —— 于是这一组频道的
    `group_title` 全空（落库成 `未分类`），`region_of()` 只能拿频道名去猜：纯 ASCII 名
    （CCTV1、CGTN）不命中 CJK 兜底 → 判成 international → 被 `KEEP_REGION=domestic`
    整组丢弃（实测 `live.zbds.top/tv/iptv4.txt` 685 条里丢 40 条）。
    """

    for sep in (",", "\uff0c", "\t"):
        idx = line.find(sep)
        if idx <= 0:
            continue
        if line[idx + 1:].strip().strip('"').casefold() == "#genre#":
            return line[:idx].strip().strip('"').strip()
    return ""


# v61：运营商抓包文件（华为 HWCTC / JSP）是**属性式**记录，不是「名称,地址」逐条清单。
# 典型一行：<Channel ChannelName="CCTV-1 综合" ChannelURL="igmp://...|http://...?fmt=ts2hls&amp;rrsip=..." />
# 两个坑：① 属性顺序不固定（ChannelURL 可能在 ChannelName 前面）；② 值里的 &amp; 是 HTML 实体，
# 不还原就会把「&amp;」当成参数名的一部分，播放器直接拿不到流。
_HUAWEI_HINT_RE = re.compile(r'ChannelName\s*=\s*["\']|<\s*Channel\b', re.I)
_HUAWEI_ATTR_RE = re.compile(
    r'(?P<key>ChannelName|ChannelURL)\s*=\s*(?P<quote>["\'])(?P<value>[^"\']*)(?P=quote)', re.I
)
_HUAWEI_ELEMENT_RE = re.compile(r"<Channel\b[^>]*>", re.I)
_HUAWEI_URL_ATTR_RE = re.compile(r'ChannelURL\s*=\s*["\']([^"\']*)["\']', re.I)
# 抓包文件里表示「类型」的属性名各家不同，能认到就拿来当分组（它直接影响 region_of 的判定）
_HUAWEI_GENRE_ATTR_RE = re.compile(r'(?:ChannelGenre|Genre|GroupName|ChannelGroup)\s*=\s*["\']([^"\']*)["\']', re.I)
HUAWEI_DEFAULT_GROUP = "抓包导入"


def looks_like_huawei_capture(content: bytes) -> bool:
    """是不是属性式抓包文件。

    只在文件开头找特征，并用 errors=replace 解码：GBK 抓包按 utf-8 解会出替换符，
    但 ChannelName=" 这些 ASCII 特征照样能匹配到。
    """
    head = content.lstrip()[:8192]
    return bool(_HUAWEI_HINT_RE.search(head.decode("utf-8", "replace")))


def _capture_urls(value: str) -> list[str]:
    """一条记录里的多个地址：抓包文件用竖线或分号分隔（也可能夹空白）。"""
    return [part.strip().strip('"') for part in re.split(r"[|;\s]+", value or "") if part.strip()]


def _append_capture(entries: list[ParsedEntry], name: str, url_value: str, genre_by_url: dict[str, str]) -> None:
    for url in _capture_urls(url_value):
        try:
            entries.append(make_entry(name=name, url=url, group_title=genre_by_url.get(url) or HUAWEI_DEFAULT_GROUP))
        except (ValueError, UnicodeError):
            continue        # igmp 之类的组播地址不在 ALLOWED_STREAM_SCHEMES 里，跳过这一条


def parse_huawei_capture(content: bytes) -> list[ParsedEntry]:
    """解析华为 HWCTC / 抓包 JSP 的属性式频道表。

    与网上流传的正则（假设 ChannelName 在前、ChannelURL 在后，且中间没有别的属性）不同，
    这里按**文档顺序**配对：先读到谁就先存着，另一个出现时结算。因此地址写在名字前面
    （ChannelURL=... ChannelName=...）也能正确挂上名字。值统一做 html.unescape。
    """
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("gb18030", errors="replace")

    genre_by_url: dict[str, str] = {}
    for element in _HUAWEI_ELEMENT_RE.finditer(text):
        block = element.group(0)
        url_attr = _HUAWEI_URL_ATTR_RE.search(block)
        genre_attr = _HUAWEI_GENRE_ATTR_RE.search(block)
        if not (url_attr and genre_attr):
            continue
        genre = html.unescape(genre_attr.group(1)).strip()
        if not genre:
            continue
        for url in _capture_urls(html.unescape(url_attr.group(1))):
            genre_by_url[url] = genre

    entries: list[ParsedEntry] = []
    pending_name: str | None = None
    pending_url: str | None = None
    for match in _HUAWEI_ATTR_RE.finditer(text):
        value = html.unescape(match.group("value"))
        if match.group("key").lower() == "channelname":
            if pending_url is not None:
                _append_capture(entries, value, pending_url, genre_by_url)
                pending_url = None
                pending_name = None
            else:
                pending_name = value
        else:
            if pending_name is not None:
                _append_capture(entries, pending_name, value, genre_by_url)
                pending_name = None
            else:
                pending_url = value
    return _dedupe_entries(entries)


def parse_channel_list(content: bytes) -> list[ParsedEntry]:
    """解析「名称,播放地址」逐条清单（运营商内网源常见的就是这种格式）。

    每行按这个顺序找切点，保证不因为「URL 里有逗号」「名字里有空格」而整行丢掉：

      1. 在 `,` / `，` / Tab 里找**第一个能让右半部分成为 http(s) 地址**的位置；
      2. 找不到就取行里第一段 `http(s)://…` 当地址，它前面的文字当名称
         （所以「名字带空格 + 空格分隔」也支持）；
      3. 整行就是个地址时，名称取 URL 路径倒数第二段（纯 URL 列表也支持）。

    空行与 `#` 注释忽略；不是 http(s) 的行跳过（不因一行坏数据废掉整份清单）。
    `分组名,#genre#` 单独一行时是**分组头**，作用到其后的条目（v60，见 `genre_header`）。
    内容其实是 M3U 时（含 #EXTINF）直接转给 parse_m3u。
    """
    head = content.lstrip()[:65536]
    if head[:7].upper() == b"#EXTM3U" or b"#EXTINF" in head:
        return parse_m3u(content)
    # v61：抓包文件先走属性式解析，否则会落到下面的兜底分支 —— 名字变成
    # 「<Channel ChannelName="CCTV-1 综合" Channel」这种 XML 残渣，地址还留着 &amp;。
    if looks_like_huawei_capture(content):
        return parse_huawei_capture(content)
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("gb18030", errors="replace")
    entries: list[ParsedEntry] = []
    genre = ""
    for raw_line in text.splitlines():
        if len(raw_line) > 8192:
            continue
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # v60：分组头单独占一行，作用到其后的条目，直到下一个分组头
        header = genre_header(line)
        if header:
            genre = header
            continue
        name = url = ""
        for sep in (",", "\uff0c", "\t"):
            idx = line.find(sep)
            if idx <= 0:
                continue
            right = line[idx + 1:].strip().strip('"')
            if right.lower().startswith(("http://", "https://")):
                name, url = line[:idx], right
                break
        if not url:
            match = _URL_RE.search(line)
            if not match:
                continue
            url = match.group(0)
            name = line[: match.start()].rstrip(" \t,\uff0c:;|").strip()
        name = name.strip().strip('"').strip()
        url = url.strip().strip('"').strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        if not name:
            # 纯 URL 列表：用路径倒数第二段当名字（末段常是统一的 index.m3u8）
            parts = [part for part in urlsplit(url).path.split("/") if part]
            if len(parts) >= 2:
                name = parts[-2]
            elif parts:
                name = parts[-1]
            else:
                name = urlsplit(url).hostname or ""
        try:
            entries.append(make_entry(name=name, url=url, group_title=genre))
        except ValueError:
            continue                              # 不支持的协议等，跳过该行
    return _dedupe_entries(entries)


def parse_iptv_org_json(content: bytes) -> list[ParsedEntry]:
    payload = json.loads(content.decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError("IPTV.org payload must be an array")
    entries: list[ParsedEntry] = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        labels = item.get("labels") or []
        if not isinstance(labels, list):
            labels = [labels]
        try:
            entries.append(
                make_entry(
                    name=item.get("title") or item.get("channel"),
                    url=item.get("url"),
                    tvg_id=item.get("channel") or "",
                    group_title="IPTV.org",
                    quality=item.get("quality") or "",
                    user_agent=item.get("user_agent") or "",
                    referrer=item.get("referrer") or "",
                    labels=", ".join(clean_text(label, 64) for label in labels),
                )
            )
        except (ValueError, UnicodeError):
            continue
    return _dedupe_entries(entries)


def parse_tvbox_lives(content: bytes) -> list[tuple[str, str]]:
    """从 TVBox 配置里取出直播列表，返回 [(名称, 播放列表URL), ...]。

    TVBox 配置是 `{"sites":[...], "parses":[...], "lives":[{"name":..,"url":..}, ...]}` 结构，
    其中 **`lives[].url` 才是真正的直播列表**（m3u/txt），`sites` 是点播站点、与本项目无关。
    注意这里只做「提取地址」，不负责抓取——子地址的主机校验由同步流程逐个执行。
    """
    payload = json.loads(content.decode("utf-8", "replace"))
    if not isinstance(payload, dict):
        raise ValueError("TVBox 配置必须是 JSON 对象")
    lives = payload.get("lives")
    if not isinstance(lives, list):
        raise ValueError("TVBox 配置里没有 lives 列表")
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in lives:
        if not isinstance(item, dict):
            continue
        url = clean_text(item.get("url"), 2048)
        # TVBox 的 lives 里可能混入内嵌 m3u 内容或多播地址，本项目只接受 http(s) 播放列表地址
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        name = clean_text(item.get("name"), 64) or (urlsplit(url).hostname or "直播源")
        result.append((name, url))
    return result


def _dedupe_entries(entries: list[ParsedEntry]) -> list[ParsedEntry]:
    unique: dict[str, ParsedEntry] = {}
    for entry in entries:
        unique.setdefault(entry.entry_key, entry)
    return list(unique.values())
