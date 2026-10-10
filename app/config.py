from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "on", "y"}


# v52 回放（catch-up）默认模板：与内置源 ysp-live 官方脚本自己 `/all.m3u` 里发布的那串
# **逐字符相同**（append 模式：只给查询串，播放器自行拼到频道地址后面）。上游作者就是拿
# 这串给同一个服务做回看的，比自己另编一套更可信；服务端实测也吃 `?playseek=`。
DEFAULT_CATCHUP_SOURCE = "?playseek=${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}"


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.getenv("DATA_DIR", "/data"))
    timezone: str = os.getenv("TZ", "Asia/Shanghai")
    sync_hour: int = _env_int("SYNC_HOUR", 4, 0, 23)
    sync_minute: int = _env_int("SYNC_MINUTE", 20, 0, 59)
    startup_sync_delay: int = _env_int("STARTUP_SYNC_DELAY", 8, 0, 300)
    # 两次「自动维护（整轮测速）」之间的最小间隔（秒）。单源同步不再触发整轮测速（见
    # sync._auto_maintenance），这个间隔是给全量同步兜底的：连续几轮全量也只测一次。
    auto_maintenance_min_interval: int = _env_int("AUTO_MAINTENANCE_MIN_INTERVAL", 1800, 60, 86400)
    # 「订阅地址发现」每天定时抓取的时间（默认 05:00，晚于 04:20 的同步/测速）。
    # 抓取只读索引页、只记候选；重复地址跳过，已入库/已处理的当天不再打扰。
    discover_hour: int = _env_int("DISCOVER_HOUR", 5, 0, 23)
    discover_minute: int = _env_int("DISCOVER_MINUTE", 0, 0, 59)
    request_timeout: int = _env_int("REQUEST_TIMEOUT", 45, 5, 180)
    # 同步时的并发源数上限。每个源的响应体会整段留在内存（上限 max_source_bytes），
    # 不限并发的话峰值内存 = N × 16MiB；NAS 上内存比带宽金贵。
    sync_concurrency: int = _env_int("SYNC_CONCURRENCY", 6, 1, 64)
    max_source_bytes: int = _env_int("MAX_SOURCE_BYTES", 16 * 1024 * 1024, 1024, 128 * 1024 * 1024)
    admin_token: str = os.getenv("IPTV_ADMIN_TOKEN", "").strip()
    app_name: str = os.getenv("APP_NAME", "IPTV Hub")
    # 应用版本号：**唯一来源**。页面标题（index.html 里的 __APP_VERSION__ 占位符，
    # 由 / 路由在返回时替换）与 /api/meta 的 version 字段都取这一个值。
    # 此前 FastAPI 写死 "1.1.0"、页面标题写死 "v4/v6"，两套编号互不相干。
    app_version: str = os.getenv("APP_VERSION", "v6")
    # 上限放到 128，便于一次性全量重测时临时调高（日常建议 64，源站吃紧时调回 8~16）
    speed_concurrency: int = _env_int("SPEED_CONCURRENCY", 8, 1, 128)
    speed_timeout: float = _env_float("SPEED_TIMEOUT", 8.0, 2.0, 30.0)
    # 测速采样：先丢弃 warmup 字节（CDN 边缘预缓冲），只用剩余部分算「持续吞吐」。
    # 实测某线路开头 512KB 是 2707KB/s、之后只有 450KB/s，不丢预热段就会把卡顿线路排到最前。
    speed_warmup_bytes: int = _env_int("SPEED_WARMUP_BYTES", 524288, 0, 4 * 1024 * 1024)
    speed_sample_bytes: int = _env_int("SPEED_SAMPLE_BYTES", 1048576, 65536, 8 * 1024 * 1024)
    speed_max_items: int = _env_int("SPEED_MAX_ITEMS", 5000, 20, 20000)
    # 自动维护：同步完成后依次执行「测速 -> 标记失效 -> 达标剔除」
    auto_speed_after_sync: bool = _env_bool("AUTO_SPEED_AFTER_SYNC", True)
    auto_speed_limit: int = _env_int("AUTO_SPEED_LIMIT", 1500, 20, 20000)
    # 自适应预算：固定预算必须 >= 存量按设计复检周期折算出的日需求量，否则
    # 「失效标记 -> 剔除 -> 复活」这条流水线永远走不完，可用线路也长期不复检
    # （一条已经死掉但状态仍是 ok 的线路会一直占着线路 1）。
    # 实测：可用 16327 条 / RETEST_OK_HOURS=72h ≈ 5442 条/天，
    #       失效  9735 条 / FAIL_RETEST_HOURS=20h ≈ 11682 条/天，
    # 而原固定预算只有 1500 条/天（差 11 倍）。打开后按实际存量算，上限 AUTO_SPEED_MAX。
    # 开销参考：1500 条约 76~120 秒，16000 条约 15~20 分钟，每天一次可以接受。
    auto_speed_adaptive: bool = _env_bool("AUTO_SPEED_ADAPTIVE", True)
    auto_speed_max: int = _env_int("AUTO_SPEED_MAX", 20000, 20, 100000)
    auto_speed_region: str = os.getenv("AUTO_SPEED_REGION", "").strip().lower()
    auto_prune_after_speed: bool = _env_bool("AUTO_PRUNE_AFTER_SPEED", True)
    # 区域保留策略：空=全部；domestic=仅保留国内（国外线路剔除）；international=仅保留国外
    # 默认 domestic（国内专属模式）；可用环境变量 KEEP_REGION 覆盖
    keep_region: str = os.getenv("KEEP_REGION", "domestic").strip().lower()
    # SQLite mmap 上限（0=禁用）。让 SQLite 直接走操作系统页缓存，省掉 read() 与页拷贝；
    # 扫描类查询实测收益 fast-CTE 2.1x、导出取数 1.7x、源聚合 1.9x。
    sqlite_mmap_bytes: int = _env_int("SQLITE_MMAP_BYTES", 268435456, 0, 2 * 1024 * 1024 * 1024)
    # 「读到一点点就卡住」的流：到期样本仍小于 speed_min_sample_bytes 时，
    # 把窗口延长到 speed_stall_timeout 再看一次（实测这类多是慢启动的可播线路，
    # 5 秒窗口会把它们算成 2~3 KB/s 并排到最后）
    speed_min_sample_bytes: int = _env_int("SPEED_MIN_SAMPLE_BYTES", 32768, 1024, 8 * 1024 * 1024)
    speed_stall_timeout: float = _env_float("SPEED_STALL_TIMEOUT", 12.0, 1.0, 60.0)
    # 测速目标地址守卫：频道地址来自第三方播放列表，测速会真的请求它，
    # 因此默认拦截内网/环回/链路本地/保留地址（盲 SSRF 防护）。
    # 这里列出的主机是例外（逗号分隔）——你自己的本地源本来就跑在内网。
    # 数据源白名单 ALLOWED_SOURCE_HOSTS 里的主机自动信任，无需在此重复。
    speed_allow_private_hosts: str = os.getenv("SPEED_ALLOW_PRIVATE_HOSTS", "").strip()
    # TVBox 配置地址（逗号分隔）。每个地址会成为一个 format=tvbox 的数据源：
    # 同步时抓取配置 → 取出 lives[].url → 子地址逐个过白名单后按普通源同步。
    # 配置里新增直播源会自动跟进；未在白名单里的子主机不会被抓取，只在「数据源」页面提示需放行。
    # 干净基线（2026-10-10）：默认留空，避免首次启动就自动种入一个 TVBox 数据源；
    # 需要时在 .env 里设置 TVBOX_CONFIGS（逗号分隔）即可：
    tvbox_configs: str = os.getenv("TVBOX_CONFIGS", "").strip()
    # v57：「按主机走代理」——只给列出的主机（含其子域）在**抓取订阅列表**时走代理，其余一律直连。
    # 用途：源站对本机出口做了 Cloudflare 拦截、而本机代理能通。实测 `yang-1989.xyz` 三个源：
    #   直连（2026-10-08 两代出口 IP 都试过）→ 403「Sorry, you have been blocked」；
    #   经本机 clash（境外节点）→ 200 / 119,742 B / 461 条。
    # 留空 = 全关，行为与改动前逐字节相同（proxy_for() 恒返回空串）。
    # ⚠️ 只影响**抓取订阅列表**这一条路径（sync._open_source / sync.probe_manual_source）；
    #    测速（speed.py）、EPG 回源（main.py）、订阅地址发现（discover.py）都**不**走这里 ——
    #    否则测速流量会被导到境外节点，整轮测速结果失真。
    source_proxy: str = os.getenv("SOURCE_PROXY", "").strip()
    # 允许走上面那个代理的主机后缀（逗号分隔）。匹配规则：主机名相等，或是它的子域。
    # 例：写 `yang-1989.xyz` 会命中 `iptv.yang-1989.xyz` / `migu.yang-1989.xyz`；
    # 不会命中 `yang-1989.xyz.evil.com`（后缀比较带前导点）。留空 = 全关。
    source_proxy_hosts: str = os.getenv("SOURCE_PROXY_HOSTS", "").strip()
    # EPG 节目单地址：配置后，导出的 M3U 会在文件头输出 x-tvg-url，播放器（TiviMate/DIYP 等）即可加载节目单。
    # 默认必须为空：带个人令牌的订阅地址写进源码默认值，会随克隆/备份/分享扩散，
    # 且每个导出的 M3U 头部都带着它（二次扩散）。需要时在 .env 里显式配置。
    epg_url: str = os.getenv("EPG_URL", "").strip()
    # 本机 EPG 代理：导出的 M3U 头部改成指向 `{base}/epg.xml`，上游地址（含令牌）不再外泄。
    # public_base_url 用于**没有请求上下文**的地方（静态文件 data/live.m3u）；
    # 留空时 HTTP 导出按请求的 Host 推断（每个客户端都拿到自己能访问的地址）。
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    # EPG 缓存有效期（小时）：TTL 内直接回缓存，过期才回源；回源失败回退旧缓存
    epg_proxy_ttl_hours: int = _env_int("EPG_PROXY_TTL_HOURS", 6, 1, 168)
    # EPG 单次下载上限（XMLTV 动辄几十 MB，比订阅列表大得多，单独设限）
    epg_proxy_max_bytes: int = _env_int("EPG_PROXY_MAX_BYTES", 64 * 1024 * 1024, 1024, 512 * 1024 * 1024)
    # 静态订阅：每次同步/测速完成后把结果写入 data/live.m3u、data/live.txt（失效线路已剔除）
    static_export: bool = _env_bool("STATIC_EXPORT", True)
    # 主订阅（/iptv、/playlist.m3u、/txt 与静态 data/live.m3u|txt）**默认排除**的数据源分组名。
    # 留空 = 与以前完全一致（主订阅包含全部线路）。配置后，不带分组参数的默认导出等价于
    # 自动加上 `?exclude_group=<该组>`：播放器地址一个字都不用改，就把这一组让给独立出口。
    # 显式带 group / exclude_group 的请求一律以参数为准 —— 包括 `?group=`、`?exclude_group=`
    # 这种空值（= 临时取回全量），这是不改配置就能复现旧行为的逃生口。
    # 取值必须真的存在（看 static_export.existing_group）：写错、或该分组以后被删干净时
    # **忽略该配置**（降级为不排除），不会因为一条配置错误让所有播放器订阅变成 400。
    main_exclude_group: str = os.getenv("MAIN_EXCLUDE_GROUP", "").strip()
    # 多线路导出时，每个频道按速度保留的线路条数（最快的 = 线路 1）
    max_lines_per_channel: int = _env_int("MAX_LINES_PER_CHANNEL", 5, 1, 20)
    # 央视系频道（央视频道/中数传媒/4K节目）线路多，保留更多备选
    max_lines_cctv: int = _env_int("MAX_LINES_CCTV", 10, 1, 30)
    # 失效标记：连续标记 prune_max_fail 次后，下一次直接剔除（不再测速）
    prune_max_fail: int = _env_int("PRUNE_MAX_FAIL", 3, 1, 10)
    # 导出时隐藏「判定失效」的线路（v42，**非破坏性**：库里一行不改、条目不 active=0，
    # 只是不让它进订阅产物）。AUTO_PRUNE_AFTER_SPEED=false 之后死线路留在库里继续复检，
    # 播放器却要为此背一份更大的订阅，所以把"别给播放器"和"别删数据"拆成两件事。
    export_hide_dead: bool = _env_bool("EXPORT_HIDE_DEAD", True)
    # 「判定失效」的连续失败阈值：连续失败达到这个次数才算死（默认与 PRUNE_MAX_FAIL 对齐）。
    # 依据：一次失败可能只是抖动，连续失败才是真死；fail_count 成功后即清零，
    # 所以它恒等于"当前连续失败次数"。注意生产实测（27,223 活跃条目）：状态级判死
    # （error/unsupported/purged 共 8,782 行）在导出查询里**早就**被排除了
    # （WHERE h.status IS NULL OR h.status='ok'），再叠加本阈值（>=3 与 >3 两种口径）
    # 都是 0 行 —— 这个开关是"状态判据被放宽 / 健康模型变化"时的兜底，今天不会让订阅变小。
    export_dead_fail_count: int = _env_int("EXPORT_DEAD_FAIL_COUNT", 3, 1, 100)
    # --- v52 回放（catch-up）---
    # 在 M3U 的 `#EXTINF` 上给「具备回看能力的数据源」的线路补 catchup / catchup-source /
    # catchup-days 三个属性，播放器（Kodi IPTV Simple、TiviMate 等）据此把 EPG 里的往期
    # 节目拼成回看地址。**默认开启**；`EXPORT_CATCHUP=0` 一键回到改动前（产物逐字节相同）。
    # 只影响订阅产物，不碰直播：属性是给播放器的附加信息，不支持的播放器会忽略；
    # 就算属性被用上而回看取流失败，ysp-live 侧也会自己回退到直播（见该脚本 do_GET）。
    export_catchup: bool = _env_bool("EXPORT_CATCHUP", True)
    # 允许挂回看属性的数据源 slug（逗号分隔）。**必须按 source_slug 精确判定，不按 URL 猜**：
    # 同一个 host:port 被别的源、别的 slug 收录时（或以后换了域名解析），按 URL 猜就会给
    # 没有回看能力的线路挂上假属性 —— 播放器会去请求一个取不到的回看地址。
    export_catchup_source_slugs: str = os.getenv("EXPORT_CATCHUP_SOURCE_SLUGS", "ysp-live").strip()
    # catchup-days：回看窗口天数（ysp-live 官方脚本声明 7 天，实测可取到 2 小时前的窗口）。
    export_catchup_days: int = _env_int("EXPORT_CATCHUP_DAYS", 7, 1, 30)
    # catchup-source 模板：
    #  * 以 `?` / `&` 开头 → 查询串，导出 `catchup="append"`（Kodi/IP TV Simple 的 append 语义：
    #    把它拼到频道地址后面）；
    #  * 以 http(s):// 开头 → 完整地址，导出 `catchup="default"`（Kodi 的 default 语义：
    #    整条回看地址由它给出，不再用频道地址）。
    # 模板里的 `{slug}` 在**导出时**就地替换成频道名（从该线路地址的最后一段推出），
    # 其余 `{utc}` / `{lutc}` / `${end}` / `${(b)yyyyMMddHHmmss}` 等占位符**原样留给播放器**
    # 替换 —— 导出时替换它们等于把时间冻死在生成订阅的那一刻。
    # 空值 = 用内置默认（与 ysp-live 自己发布的写法一致）。注意在 `.env`/compose 里写含 `$`
    # 的模板必须写成 `$$`（compose 会先做一次变量插值），用不含 `$` 的写法（如
    # `?utc={utc}&lutc={lutc}`）可以完全绕开这个坑。
    export_catchup_source: str = (
        os.getenv("EXPORT_CATCHUP_SOURCE", "") or DEFAULT_CATCHUP_SOURCE
    ).strip()
    # --- v53 偏好源（让有回看能力的频道优先走本源）---
    # 线路排序键的**第一优先级**：`source_slug` 在这个列表里的线路，排到其它源的线路之前；
    # 之后仍按原有键（已验证 → 快 → 稳 → 延迟低 → URL）排。默认 `ysp-live` —— 它是唯一
    # 能回看的源，只有它的线路当上「线路 1」，播放器里的回看入口才默认可用（v52 实测：
    # 6,159 条线路里只有 22 条来自它，多数频道的主用线路是别的源，回看点不动）。
    # **空值 = 行为与加这个键之前逐字节相同**（所有线路权重相同，排序退化为原键）。
    # 只改「同一频道内谁排前面」，不改每频道保留的线路条数（MAX_LINES_*）、不改取数范围，
    # 因此它不会让没有该源的频道发生任何变化。
    export_prefer_sources: str = os.getenv("EXPORT_PREFER_SOURCES", "ysp-live").strip()
    # 失效冷却：判定失效满 retest_after_days 天后复活重测，成功即恢复
    retest_after_days: int = _env_int("RETEST_AFTER_DAYS", 14, 1, 365)
    # 已标记失效（未剔除）的线路，每隔 fail_retest_hours 小时复测一次并累加标记
    fail_retest_hours: int = _env_int("FAIL_RETEST_HOURS", 20, 1, 720)
    # 季度清理：复活过、复活后仍然失效的线路永久停用（详见 speed.purge_revived_streams）。
    # PURGE_INTERVAL_DAYS 是两次清理的最小间隔；PURGE_MIN_REVIVE 是「被复活过几次」的阈值。
    purge_interval_days: int = _env_int("PURGE_INTERVAL_DAYS", 90, 1, 3650)
    purge_min_revive: int = _env_int("PURGE_MIN_REVIVE", 1, 1, 50)
    # 正常线路复检周期（小时）
    retest_ok_hours: int = _env_int("RETEST_OK_HOURS", 72, 1, 720)
    # 数据源级失效告警：某个源连续多少天没有一次成功同步（或状态为 error）就在控制台标红。
    # 上游悄悄挂掉（例如 116.ltd 那次）时，只看数据源页的 status 很容易漏掉。
    source_stale_days: int = _env_int("SOURCE_STALE_DAYS", 3, 1, 60)
    # 每周维护：同步记录只保留最近 N 条
    sync_runs_keep: int = _env_int("SYNC_RUNS_KEEP", 100, 10, 5000)
    # 每周维护：删除连续 N 天没再出现过的 inactive 历史条目（含其遗留健康记录）
    inactive_retention_days: int = _env_int("INACTIVE_RETENTION_DAYS", 30, 7, 365)
    # v48「长期失效」清理：连续失效且持续超过 N 天的线路，在**每周维护**里把它的
    # source_entries 置为 active=0（停用条目，不删数据，可用 /api/prune/restore 恢复）。
    # 0 = 关闭，此时行为与加这个特性之前逐字节一致（默认就是 0：是否开启由使用方拍板，
    # 不由代码替用户决定 —— 它会让订阅里"判死但还挂着"的线路消失）。
    # 为什么默认 30 天而不是跟 RETEST_AFTER_DAYS(14)：14 天正是"复活重测"的窗口，
    # 取同一个数会让一条线路在"刚要复活重测"的同一周被停用，两个机制互相打架。
    # 判据细节见 speed.cleanup_long_dead_streams()。
    auto_cleanup_dead_days: int = _env_int("AUTO_CLEANUP_DEAD_DAYS", 0, 0, 3650)
    # 频道详情里在主用线路之外额外展示的备选线路条数
    backup_lines_shown: int = _env_int("BACKUP_LINES_SHOWN", 5, 0, 50)

    def __post_init__(self) -> None:
        # 跨字段约束（各自独立钳制挡不住的组合错误）：
        # min_sample > sample 时采样循环在 total>=sample 就停，min_sample 永远达不到，
        # 「样本不足」必然触发 —— 每一条流都判 error，fail_count 逐轮累加后配合
        # AUTO_PRUNE_AFTER_SPEED 会把整个库逐步剔除。warmup > sample 则预热永不生效，
        # 预热段混回测速样本（正是 warmup 要消除的虚高）。clamp 到 sample 并告警。
        if self.speed_min_sample_bytes > self.speed_sample_bytes:
            logging.getLogger("iptv-hub.config").warning(
                "SPEED_MIN_SAMPLE_BYTES(%s) > SPEED_SAMPLE_BYTES(%s)，已钳制为后者",
                self.speed_min_sample_bytes, self.speed_sample_bytes,
            )
            object.__setattr__(self, "speed_min_sample_bytes", self.speed_sample_bytes)
        if self.speed_warmup_bytes > self.speed_sample_bytes:
            logging.getLogger("iptv-hub.config").warning(
                "SPEED_WARMUP_BYTES(%s) > SPEED_SAMPLE_BYTES(%s)，已钳制为后者",
                self.speed_warmup_bytes, self.speed_sample_bytes,
            )
            object.__setattr__(self, "speed_warmup_bytes", self.speed_sample_bytes)

    @property
    def database_path(self) -> Path:
        return self.data_dir / "iptv.sqlite3"

    @property
    def epg_cache_path(self) -> Path:
        """本机 EPG 代理的磁盘缓存路径（放 data/ 跟着备份走；不放内存是因为 XMLTV 动辄几十 MB）。"""
        return self.data_dir / "epg-cache.xml"

    @property
    def epg_proxy_url(self) -> str:
        """导出的 M3U 头部要写的地址；未配置 EPG_URL 时为空（此时不写 x-tvg-url）。"""
        if not self.epg_url:
            return ""
        return f"{self.public_base_url}/epg.xml" if self.public_base_url else ""

    def proxy_for(self, url: str) -> str:
        """该 URL 走哪个代理；未配置、或主机不在名单里时返回空串（v57）。

        * 名单匹配**按主机名后缀**：`yang-1989.xyz` 命中它自己和它的子域；
          比较时带前导点（`host.endswith("." + suffix)`），所以 `…yang-1989.xyz.evil.com` 不命中。
        * 只做字符串判断，不解析 DNS —— 具体连哪个 IP 由代理侧决定（见 sync._client_for 的说明）。
        """
        if not self.source_proxy or not self.source_proxy_hosts:
            return ""
        host = (urlsplit(url).hostname or "").lower()
        if not host:
            return ""
        for item in self.source_proxy_hosts.split(","):
            suffix = item.strip().lower().lstrip(".")
            if suffix and (host == suffix or host.endswith("." + suffix)):
                return self.source_proxy
        return ""


settings = Settings()
