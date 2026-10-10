# IPTV Hub

自建 IPTV 直播源聚合管理服务。定时抓取多个公开直播源，去重归类后提供 Web 控制台、M3U / TXT 导出与订阅地址。

> 合规声明：本项目**仅做索引与文本抓取**，不代理、不转播、不缓存任何媒体流，也不内置任何播放页与播放器。所有流地址归各自源站所有，请在当地法律与源站条款允许范围内使用。

## 数据源

### 手动添加数据源

「数据源」页面顶部有添加表单：填地址（可选备注名、可指定格式，留空自动识别 m3u / json / TVBox 配置）即可。
点「添加并检测」后服务端会：

1. **先真抓一次源并解析** —— 抓不到、或解析不出任何条目就直接拒绝，不会留下半个源；
2. 把这次抓取**实际访问成功过的主机**（含重定向跳转）写入放行白名单；
3. 写入源行，并排队一次「只同步这个源」的任务；
4. 同步完成后自动维护接着测速，测通的线路随即进入总源（订阅导出）。

所以加完源不用再手工点测速，等「同步记录」里这一轮跑完即可。对应接口：

| 操作 | 接口 |
| --- | --- |
| 添加源 | `POST /api/sources?url=<地址>&name=<备注>&format=m3u\|json\|tvbox\|list\|huawei`（`format` 留空则按内容自动识别） |
| 移除源 | `DELETE /api/sources/{slug}`（仅限手动添加的源；内置源删了也会随重启回来，故拒绝） |
| 查看放行主机 | `GET /api/allowed-hosts`（区分内置白名单与手动放行） |
| 撤销放行主机 | `DELETE /api/allowed-hosts/{host}`（只对手动放行的生效） |

**同一个地址第二次提交（v62）**：不会重复添加，也不会重复抓取 ——

- **前端本地查重**：提交前先用已加载的源列表比对（trim 后完全相等，与服务端 `sources.url` 的 UNIQUE 同口径），
  命中就直接提示「这个地址已经在数据源里了：<源名>（状态、条数）」并**滚动定位 + 短暂高亮**到那张卡片，
  **连这次请求都不发**（避免重复尝试）；
- **服务端兜底**：如果列表还没刷新到、或该地址的展示串被脱敏打码导致本地漏判，`POST /api/sources`
  会在**真抓取之前**返回 `409`，`detail` 是结构化对象
  （`{message, slug, name, status, item_count, enabled}`），前端据此给出同样的提示与定位；
  若那条源是「已停用」状态，提示里会说明可在卡片上启用；
- 两条路径都**不会再抓一次上游、也不会写库**；`format` 非法时仍是 `400`（校验在重复检查之前，不会被 409 抢答）。

### 粘贴导入频道清单

运营商内网源拿到的往往是一份 `名称,播放地址` 的逐条清单，没有可抓取的播放列表地址。
「数据源」页面的「或直接粘贴频道清单」折叠区可以把它整份粘进去：

- 每行 `名称,播放地址`，也兼容 Tab/空白分隔、全角逗号；直接粘 M3U 也行
- v60：`分组名,#genre#` 单独一行是**分组头**，作用到其后的条目（国内 txt 清单的通用写法）。
  联网源里的同款清单同样支持 —— 加了分组头，`group_title` 才不是空的，`region_of` 才不会
  把 `CCTV1`、`CGTN` 这类纯 ASCII 名判成国外而被 `KEEP_REGION=domestic` 丢掉
- v61：**华为 HWCTC / 抓包 JSP（属性式频道表）** 也能直接粘。一行一条记录，形如
  `<Channel ChannelName="CCTV-1 综合" ChannelURL="igmp://...|http://...?fmt=ts2hls&amp;rrsip=..." ChannelGenre="央视" />`。
  解析按**文档顺序**配对（地址写在名字前面也正确）、把 `&amp;` 还原成 `&`、竖线分隔的多地址逐条入库，
  并把 `ChannelGenre` / `Genre` 之类的类型属性落成 `group_title`。修复前这种文件会被逐行兜底分支
  接住：名字是 `<Channel ChannelName="…" Channel` 这种 XML 残渣、地址还留着 `&amp;`
  （不报错、静默入库，但播放器拿不到流）
- 内容存到 `data/inline/<slug>.m3u`，登记成 `format=inline` 的**本地源**
- 之后与联网源走**同一条流水线**：同步（读文件解析）→ 测速 → 剔除 → 并入订阅
- **用同一个名字再粘一次 = 更新这个源**（不会越建越多）；删源会一并删掉内容文件

    POST /api/sources/inline?name=<清单名>     # 请求体就是清单原文

> 粘贴清单没有 group-title / country，`region_of` 只能靠频道名猜，而它对纯 ASCII 名
> （`CCTV-1`、`CGTN`）会判成 `international`。实测一份 63 条的山东移动清单因此被
> `KEEP_REGION=domestic` 丢了 19 条央视线路。所以**粘贴源按当前 `KEEP_REGION` 归档、
> 不做启发式过滤** —— 用户主动粘贴本身就是「我要这些频道」的表达。

> **安全边界**：服务端只向白名单内的主机发起请求。内置白名单写死在
> `app/sync.py` 的 `ALLOWED_SOURCE_HOSTS_BUILTIN`；手动添加的源把主机写进
> `allowed_hosts` 表（放行范围恰好是「这次抓取真正访问成功过的那些主机」）。
> 环回 / 链路本地 / 组播 / 未指定地址一律拒绝；内网网段**允许** ——
> 自建的本地源（NAS 上的中继、运营商内网网关等）都属于这一类。


**本仓库不声明任何内置源**：`app/db.py` 的 `SOURCE_SEEDS` 是空元组、`SOURCE_SEED_GROUPS`
是空字典，`TVBOX_CONFIGS` 的代码默认值与 compose 默认值都是空串。因此首次启动建的是
**空库**（0 源 / 0 条目 / 0 候选 / 0 放行主机），`GET /api/sources` 返回 `[]`、
`GET /healthz` 返回 `{"status":"ok","sources":0}`。所有数据源都由使用者在「数据源」页面
**手动添加**（联网列表、粘贴频道清单皆可），添加时服务端会真抓一次并自动放行访问成功的主机。

`SOURCE_SEEDS` 的语义与写入路径（`db._seed_sources`）完全保留。想把某批源做成内置，
按 5 元组 `(slug, 显示名, url, format, priority)` 自己写进 `SOURCE_SEEDS` 即可，重启后自动写入。

- 每日一次同步（`SYNC_HOUR` / `SYNC_MINUTE`，代码默认 04:20；随附 `.env.example` 设为 03:00，容器本地时区）。
- 全部使用条件请求（保存 `ETag` / `Last-Modified`），未变更不重复下载。
- 全部数据源**并发抓取**，最坏耗时从「串行之和」降为「最慢单个」。
- 单个源失败不影响其他源；保留上一次可用数据。

### 启用 / 停用数据源（v51 + v58 + v59）

停用（界面数据源页的开关；等价于 `POST /api/sources/<slug>/enable?value=0`）是**持久的用户意图**：

- 种子的 upsert **刻意不更新 `enabled`** → 容器重启、内置源种子重写都不会把它拉回启用；
- 停用后该源**不再同步、也不再测速**（`sync.select_sync_sources` 与 `speed._pending_sql` 都要求 `enabled=1`），
  条目与健康记录一行不删，重新启用即刻恢复原语义；
- **v58「停用即静默」**：`/api/stats` 的源级失效告警（首页红字）与「待首次同步」计数都只看**启用中**的源。
  此前一个被主动停用的故障源会永远顶着红字，看起来像"停用没生效"（实测：停用一个故障源后
  `source_alerts.count` 仍是 1）。
- **v59**：自动复活（`speed.revive_expired`，每轮全量同步后的自动维护都会跑）也只看**启用中**的源 ——
  停用源里因失效被剔除的条目不会被系统悄悄放回来（否则会抵消下面这句手工清理，并把 `revive_count` +1）。
  同一流指纹同时挂在启用源与停用源上时，只激活启用源那一侧。

> ⚠️ 停用**不会**把它的线路移出订阅：导出只看条目 `active=1`，而停用源**再也不会被测速**，
> 所以它的死线不会自动被判定失效。要把死线从订阅里去掉，得把那些条目置为 `active=0`
> （界面里的「剔除」语义，可 `POST /api/prune/restore?source=<slug>` 或整库恢复）。

### nTv 源适配器（`ntv-cctv` / `ntv-fm`，v56）

上游 [buhanzhe/NativeWasmTv](https://github.com/buhanzhe/NativeWasmTv)（开源网页电视 nTv）
生态的源分两类，**只有一类能进本项目**：

| 上游 | 是什么 | 能否进 hub |
| --- | --- | --- |
| `builtin_channels.txt`（主仓 git） | 20 条央视直连 HLS（`ld*cctvwbcd*` CDN） | ✅ → `ntv-cctv` |
| `fm.txt`（[webSourceM3U8](https://github.com/buhanzhe/webSourceM3U8) Releases） | 48 条电台流（46 SomaFM + 2 国内电台） | ✅ → `ntv-fm` |
| `webview.txt` | 482 行 / 29 组：央视、卫视 + 28 个省级地方台，全是 `webview://<网页>` | ❌ 需 nTv 壳解析 |
| [TvWasm/cjs](https://github.com/TvWasm/cjs) 的 `.cjs` 地址 | 壳内解析的地址（`cmg.cjs?id=…`） | ❌ 需 nTv 壳解析 |

后两类必须先由 nTv 壳（WebView + 站点原生 `.so` + QuickJS）解析成 m3u8，而本项目只索引
**流地址**（白名单只抓白名单主机；TVBox 通道也只跟进 `lives[].url`、**不执行** `sites[]` 里的 spider）。
所以地方台页面**不能**直接加进来 —— 要覆盖它得另建无头解析层，属于独立议题。

接入方式：把归一化后的 M3U 地址当作**普通数据源**，在「数据源」页面手动添加（或自己写进
`SOURCE_SEEDS`）。本仓库**不内置**这两条源 —— 它们指向部署者自己的适配器实例，写死在这里
对别人没有意义。适配器只负责把 `webview://` 条目与清单式写法归一化成标准 M3U，
hub 只吃 m3u/txt/json；上游 `sites[]` 里的 spider **不会被执行**。

## 快速部署（NAS / Docker Compose）

```bash
mkdir -p /opt/iptv-hub
cd /opt/iptv-hub
# 放入本项目文件后：
cp .env.example .env
docker compose up -d --build
```

访问 `http://<NAS_IP>:18787`。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `IPTV_PORT` | `18787` | 宿主机映射端口 |
| `PUID` / `PGID` | `1000` / `1001` | 运行用户（NAS 上的常见默认值） |
| `TZ` | `Asia/Shanghai` | 时区，决定每日同步时间 |
| `SYNC_HOUR` / `SYNC_MINUTE` | `4` / `20` | 每日同步时刻 |
| `STARTUP_SYNC_DELAY` | `8` | 启动后延迟秒数再首次同步 |
| `REQUEST_TIMEOUT` | `45` | 抓取超时秒数 |
| `IPTV_ADMIN_TOKEN` | 空 | 设置后，写操作需 `Authorization: Bearer <token>` |
| `APP_NAME` | `IPTV Hub` | 界面标题 |
| `APP_VERSION` | `v6` | 应用版本号（唯一来源）：页面标题与 `/api/meta` 的 `version` 都取它 |
| `KEEP_REGION` | `domestic` | 区域保留策略：`domestic` 仅保留国内（国外剔除）、`international` 仅保留国外、留空=全部 |
| `MAIN_EXCLUDE_GROUP` | 空 | 主订阅默认排除的数据源分组：留空=主订阅含全部线路；设为某分组名（如「发现源」）后，不带分组参数的 `/iptv`、`/playlist.m3u`、`/txt` 与静态 `data/live.m3u|txt` 都排除该组（详见「分组订阅出口」） |

## 数据与持久化

- SQLite 与状态全部在 `./data`（卷 `./data:/data`）。
- 容器以只读根文件系统运行，`cap_drop: ALL`，无新特权；仅 `/tmp` 为 tmpfs。
- 日志自动轮转（单文件上限 10MB，保留 3 份），长年运行不会撑爆磁盘。
- **每周自动维护**：每日同步流水线末尾检查标记文件，满 7 天自动执行 `VACUUM` + WAL checkpoint，防止频繁增删产生的碎片与 WAL 膨胀。
- 备份：直接打包 `data/` 目录即可。

## 订阅导出缓存

`/iptv` 与 `/txt` 使用**内存 TTL 缓存（60 秒）+ ETag 条件请求**：

- 播放器/客户端带 `If-None-Match` 访问时，内容未变直接返回 **304**，几乎零开销；
- 同步、测速落库、剔除/恢复/复活等任何数据变化都会自增全局版本号并改变 ETag，客户端可即时拿到新内容；
- 首次生成后 60 秒内同参数请求直接复用，避免大库下反复全量计算。

### 回放 / 时移（catch-up，v52 + v53）

导出的 M3U 会给「具备回看能力的数据源」的线路补上三个属性（位置在 `#EXTINF` 的逗号之前）：

```
#EXTINF:-1 tvg-id="CCTV1" tvg-name="001 CCTV1" group-title="央视" catchup="append" catchup-days="7" catchup-source="?utc={utc}&lutc={lutc}",001 CCTV1
http://<你的源站地址>/cctv1.m3u8
```

| 配置 | 默认 | 说明 |
|---|---|---|
| `EXPORT_CATCHUP` | `true` | 总开关。设 `0` 一键回到改动前（产物逐字节相同，直播不受影响） |
| `EXPORT_CATCHUP_SOURCE_SLUGS` | `ysp-live` | 允许挂属性的**数据源 slug**（逗号分隔）。按 `source_entries.source_slug` 判定，**不按 URL 猜** |
| `EXPORT_CATCHUP_DAYS` | `7` | 写进 `catchup-days`（回看窗口天数，**就是源站上限**，见下） |
| `EXPORT_CATCHUP_SOURCE` | 代码内置 `?playseek=${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}` | `catchup-source` 模板，见下。**本机 `.env`（v53）已显式设为 TiviMate 风格 `?utc={utc}&lutc={lutc}`** |
| `EXPORT_PREFER_SOURCES` | `ysp-live` | **偏好源**（v53，逗号分隔的 slug）：这些源的线路在**同一频道内**排到最前（其后仍按速度/稳定性等原键排序），让有回看能力的源拿到「线路 1」。留空 = 排序与 v53 之前逐字节相同 |

**为什么只给 `ysp-live`**：回看要求**源站自己**能按时间窗取回历史流。`ysp-live`
（官方 7.3 脚本的本地中继）原生支持，它的 `/all.m3u` 自己就发布了同一串 `catchup-source`；
其它数据源没有这个能力，给它们加属性只会让播放器去请求一个取不到的回看地址。

**为什么还需要 `EXPORT_PREFER_SOURCES`**：播放器的回看入口通常只挂在**线路 1** 上，
而按速度排序时 `ysp-live`（本机中继，链路是「NAS → 源站 CDN」）经常排不进每个频道的前几名。
v52 实测：6,159 条线路里只有 22 条来自它、只有 22 个频道带得上回看属性。加上偏好源之后
（v53 实测，同一份库）：`ysp-live` 线路 22 → **61** 条、覆盖频道 22 → **60** 个，
**每个偏好频道的线路 1 都是它**；没有该源覆盖的频道**逐字节不变**，每频道线路条数也不变。

**模板与模式**（模式由模板形状决定，不需要也不允许另填）：

- 以 `?` / `&` 开头 → `catchup="append"`：整串拼到频道地址后面（两种内置写法都是这个形状）；
- 以 `http://` / `https://` 开头 → `catchup="default"`：整条回看地址由 `catchup-source` 给出；
- `{slug}` 在**导出时**替换成频道名（取自线路地址最后一段，推不出来就不挂属性）；
- `{utc}` `{lutc}` `${end}` `${(b)yyyyMMddHHmmss}` 等占位符**原样留给播放器**替换 ——
  导出时替换等于把时间冻死在生成订阅的那一刻。

**两种写法在源站都实测可用**（同一时间窗、同一个频道）：

| `catchup-source` | 面向的播放器 | 实测结果 |
|---|---|---|
| `?utc={utc}&lutc={lutc}`（v53 默认，TiviMate / SIPTV 系） | TiviMate / OTT Navigator | `GET /cctv1.m3u8?utc=1791260615&lutc=1791262415` → **200**，`#EXT-X-PLAYLIST-TYPE:VOD`，157,481 B |
| `?playseek=${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}`（v52 默认，Kodi 系） | Kodi PVR IPTV Simple | `GET /cctv1.m3u8?playseek=20261006122335-20261006125335` → **200**，同为 VOD，157,481 B |

区别只在**播放器认哪种占位符**，服务端两种都吃。**要改回 Kodi / 时移风格**：

```bash
# .env（含 $ 的模板必须把 $ 写成 $$，compose 会先做一次变量插值）
EXPORT_CATCHUP_SOURCE=?playseek=$${(b)yyyyMMddHHmmss}-$${(e)yyyyMMddHHmmss}
docker compose up -d --force-recreate      # env 在容器创建时固化，restart 不生效
```

**回看窗口就是 7 天，超了看不到**（v53 实测，`?utc=` 形态逐步逼近边界）：

| 回看起点 | 结果 |
|---|---|
| 1 小时前 / 6 天 23 小时前 / **整 7 天前** | **200** + `#EXT-X-PLAYLIST-TYPE:VOD`（真正的回看流） |
| 7 天 1 小时前 / 8 天前 | 仍是 200，但返回的是**直播**播放列表（约 1.5 KB、无 `VOD` 标记、`EXT-X-MEDIA-SEQUENCE` 是当前时刻） |

也就是说：超过 7 天的回看点不到，而且**播放器不会报错** —— 它会静默播成直播。
所以 `EXPORT_CATCHUP_DAYS` 保持 `7`（与 `ysp-live` 自己 `/all.m3u` 里声明的
`catchup-days="7"` 一致）；调大它只会让播放器多显示几天点不动的节目单。

**播放器兼容性**：`catchup` / `catchup-source` / `catchup-days` 是 Kodi PVR IPTV Simple 的
标准 M3U 标签（[官方 README](https://github.com/kodi-pvr/pvr.iptvsimple#catchup)），
`append` 模式要求值是查询串、`default` 模式要求是完整地址；Kodi 支持的占位符包括
`{utc}`（节目开始）、`{lutc}`（当前时间）、`${start}`、`${end}`、`{duration}` 等。
TiviMate / OTT Navigator 这类播放器用同一套 `catchup` 属性，但各自认的占位符集合不同
（TiviMate 系常见 `{utc}` / `{lutc}`），所以模板做成了可配的。
**不支持的播放器**（PotPlayer、VLC、mpv、DIYP 等纯播放器/无回看 UI 的客户端）：这些属性在
`#EXTINF` 的属性区、逗号之前，未知属性一律被忽略，**不影响直播播放**；回看入口本来也需要
播放器侧的 EPG 回看 UI。

**三个出口一致**：`/iptv`、`/playlist.m3u`、静态 `data/live.m3u` 是同一份产物（带属性）；
`/txt` 的格式表达不了属性，**保持原样**（不含任何 catchup 字样）；`/discovered.m3u` 是发现源的
线路，不在 slug 列表里，自然不带。配置值（开关/模板/天数/slug 列表/偏好源）都进导出缓存键，
改配置不会在 60 秒 TTL 内拿到旧产物。

> ⚠ `.env` / `docker-compose.yml` 里写含 `$` 的模板必须把 `$` 转义成 `$$`
> （compose 会先做一次变量插值）。用 `?utc={utc}&lutc={lutc}` 这类不含 `$` 的写法可以完全绕开。

### EPG 节目单（本机代理，v53）

导出的 M3U 头部写的是**本机** `/epg.xml`，不是上游地址 —— 上游带个人令牌，写进订阅会随分享扩散。

- `/epg.xml`（同 `/epg`）由 `EPG_URL` 回源，缓存落盘 `data/epg-cache.xml`；
  `EPG_PROXY_TTL_HOURS`（默认 6 小时）内直接回缓存、过期才回源，**回源失败回退旧缓存**；
- 单文件上限 `EPG_PROXY_MAX_BYTES`（默认 64 MiB）；**≤ 8 MiB 时整份读进内存下发**
  （一个原子快照，避免 `FileResponse` 先 stat 再 open 的截断窗口），超过就走流式；
- 该接口**不接受任何请求参数**（不存在 SSRF 面），只能抓运维配置的那一个地址。

**当前源（`.env` 的 `EPG_URL`）= `https://raw.githubusercontent.com/fanmingming/live/main/e.xml`**
（fanmingming 的 e.xml，GitHub Action 约每 4 小时更新一次，比 6 小时的缓存 TTL 更勤，
所以 TTL 保持默认即可）。2026-10-06 实测对照：

| 候选源 | 体积 | programme 覆盖 | channel | 与导出 tvg-id 的交集 |
|---|---|---|---|---|
| 旧：`m3u.ibert.me/epg/112114_xyz.xml` | 2.86 MiB | **仅 2026-10-06 一天**（19,064 条） | 540 | 257 |
| 新：`raw.githubusercontent.com/…/e.xml` | 7.31 MiB | **09-30 ~ 10-12（13 个日历日）**，其中 ≤ 今天 7 天（34,705 条） | 123 | 97 |
| `m3u.ibert.me/epg/51zmt.xml` | 1.05 MiB | 10-06 ~ 10-07（2 天） | 101 | 19 |
| `gitee.com/gsls200808/xmltvepg/raw/master/e7.xml.gz` | 0.99 MiB(gz) / 13.20 MiB | 09-30 ~ 10-06（7 天，覆盖最好） | 1454 | 4（频道 id 是数字，与本项目 tvg-id 对不上，**不可用**） |
| `epg.pw/xmltv/epg_CN.xml` | >2.1 MiB（未下完即超时） | 只有未来 6 天，**没有过去** | 690+ | — |
| `epg.112114.xyz`（112114 官方） | 不可达 | DNS 被污染到 Facebook IP，容器内 `Network is unreachable` | — | — |

**代价**：新源的频道数比旧的少（540 → 123），与导出的 tvg-id 交集 257 → 97；对最重要的
`ysp-live` 64 路则是 **55/64**（旧源 59/64）。这是「能不能回看」与「多少频道有节目单」之间的
取舍 —— 旧源虽然覆盖频道多，但只有**当天**节目，播放器里根本点不到回看。
两者兼得只能做**多源合并**（尚未实现）。

**换 `EPG_URL` 必须做两件事**：

```bash
# 1) 删掉旧缓存：代理只看缓存文件的 mtime，不看它来自哪个 URL，否则会继续下发上一家的节目单
rm -f data/epg-cache.xml
# 2) 重建容器（env 在容器创建时固化）
docker compose up -d --force-recreate
```

首次访问 `/epg.xml` 会先返回 503（触发后台回源），几秒~一两分钟后即可拿到新节目单；
日志里能看到 `EPG 缓存已更新：<字节数> -> epg-cache.xml`。

**回退**：把 `EPG_URL` 改回 `https://m3u.ibert.me/epg/112114_xyz.xml`、删缓存、重建容器即可
（切换前的旧缓存备份在 `/tmp/v53/epg-cache.112114.xml`）。GitHub raw 被墙时可换
`https://cdn.jsdelivr.net/gh/fanmingming/live@main/e.xml`（同源，jsDelivr 侧最长缓存 12 小时）
或 `https://gh-proxy.com/https://raw.githubusercontent.com/fanmingming/live/main/e.xml`。

## 频道分区

每个频道按区域标记为 **国内 / 国外**，判定顺序：

1. 来自国内源（`hacks-domestic`、`zbds-domestic`）→ 国内；
2. 有国家代码且属于 `cn/hk/mo/tw/zh` → 国内，其他 → 国外；
3. 无国家代码时按分组名与频道名是否含中文推断。

订阅地址可按区域导出：

- 国内：`/iptv?region=domestic`
- 国外：`/iptv?region=international`
- 全部：`/iptv`

### 分组订阅出口（v43）

数据源分组（`sources.source_group`，目前只有「发现」页采用候选时会写入「发现源」）可以单独成出口：

| 地址 | 内容 |
|---|---|
| `/discovered.m3u`、`/discovered.txt` | 只有「发现源」这一组（等价 `/iptv?group=发现源`、`/txt?group=发现源`），播放器可直接粘贴 |
| `/iptv?group=<分组名>`、`/txt?group=<分组名>` | 只输出该分组（任意分组名，必须真实存在，否则 400） |
| `/iptv?exclude_group=<分组名>`、`/txt?exclude_group=<分组名>` | 去掉该分组（两个参数可叠加；同值 = 空集） |

过滤按**线路所属数据源的分组**生效，与 `source`、`region` 参数可任意叠加，纯查询不写库。

**让主订阅默认就干净**：在 `.env` 里设置 `MAIN_EXCLUDE_GROUP=发现源` 后，**不带分组参数**的
`/iptv`、`/playlist.m3u`、`/txt` 与静态 `data/live.m3u|txt` 都自动排除该组 —— 播放器里的地址
一个字都不用改。语义：

- 请求里出现 `group` 或 `exclude_group`（**包括空值**）= 以参数为准，配置不介入；
  所以 `?exclude_group=`、`?group=` 是不改配置就能临时取回全量订阅的逃生口；
- 固定出口 `/discovered.m3u|txt` 不受该配置影响（它们的分组是服务端常量）；
- 取值必须是真实存在的分组名；写错或该分组以后被删干净时，该配置**自动失效**（降级为不排除），
  不会让播放器拿到 400 或空订阅；
- 生效值见 `/api/meta` 的 `main_exclude_group`（`main_exclude_group_configured` 是 `.env` 原文，
  两者不一致就说明配置没生效）；本次生效的分组也会回显在导出响应头
  `X-Export-Group` / `X-Export-Exclude-Group`（URL 编码，`%E5%8F%91%E7%8E%B0%E6%BA%90` = 发现源）；
- 分组参数进导出缓存键，改配置后不会出现 60 秒内新旧内容混发。

### 区域保留策略（KEEP_REGION）

设置 `KEEP_REGION=domestic` 可进入**国内专属模式**：

- 启动时自动把国外线路（`region=international`）置为不活跃，从频道列表与订阅中剔除（记录保留，可随时恢复）；
- 每次同步只激活国内条目，国外条目不再入库激活；
- 订阅导出（`/iptv`、`/txt`）未显式指定 `region` 时默认只输出国内。

恢复国外只需把 `KEEP_REGION` 留空（或设为 `international`）后重启，被剔除的国外线路会随下次同步重新激活。

> 说明：剔除采用与失效剔除一致的 `active=0` 语义，并非物理删除；订阅与列表即时生效，数据可逆。

### TVBox 配置源（自动跟进）

`TVBOX_CONFIGS` 里声明的地址会成为一个 `format=tvbox` 的数据源。同步时：

1. 抓取 TVBox 配置 JSON（结构为 `{"sites":[...], "lives":[{"name":..,"url":..}]}`）；
2. 从 `lives[].url` 取出真正的直播列表地址；
3. **每个子地址的主机都必须在 `ALLOWED_SOURCE_HOSTS` 里**，放行的按普通源同步，
   并自动创建一个子源（slug 由 URL 哈希得出，因此改显示名不会产生重复源，
   同一列表被多个配置引用也只同步一次）；
4. 配置里新增直播列表会**自动跟进**，无需改代码。

**未放行的主机不会被抓取**，只会把主机名写进该源的 `error_message`，「数据源」页面直接显示：

```
需放行主机：cdn.example.com（未放行已跳过，放行需加入 ALLOWED_SOURCE_HOSTS）
```

> 这是**只读提示**：页面上没有「放行」按钮，放行只能由有服务器权限的人改 `ALLOWED_SOURCE_HOSTS` /
> `TVBOX_CONFIGS` 并重启。这样「谁能扩大白名单」这件事始终锁在服务器权限上，
> 而不是暴露成一个网页按钮（网页按钮会要求先做鉴权改造，否则等于把白名单变成人人可加）。

## 每日自动维护流水线

每天 `SYNC_HOUR:SYNC_MINUTE`（代码默认 04:20；随附 `.env.example` 为 03:00）触发一次，串成一条流水线，**长期循环执行**：

```
同步源（并发） → 复活冷却期满的线路 → 抽样测速 → 批量落库 → 达标剔除 → 订阅按速度排序 →（每周）VACUUM
```

失效线路的生命周期（状态机）：

| 阶段 | 行为 |
| --- | --- |
| 第 1 次测不通 | 标记 `1/3`。**立即移出订阅**（导出只认「测通」与「未测」线路），频道详情里标为「失效」——播放器永远拿到当前可用的线路，靠多线路自动替补实现无缝切换 |
| 第 2 次仍不通 | 标记 `2/3`（间隔 `FAIL_RETEST_HOURS`，默认 20 小时，即每日一轮） |
| 第 3 次仍不通 | 标记 `3/3`，最后一次机会 |
| 第 4 次仍不通 | **从数据库剔除且不再测速**（`fail_count > PRUNE_MAX_FAIL`，`active=0`，记录保留） |
| 中途测通 | 标记立即清零，自动回到订阅（按速度重新排序） |
| 被剔除满 `RETEST_AFTER_DAYS`（默认 14 天） | **自动复活**：重新纳入检测、标记归零，再次走完整流程 |
| 复活后又持续失败 | 由**季度清理**（每 `PURGE_INTERVAL_DAYS` 天）永久停用：`status='purged'`，不再测速、不进订阅、同步再抓到同一地址也不复活 |

> 注意：「标记 1/3 ~ 3/3」是**数据库剔除**的计数器，不影响订阅资格——订阅资格只看
> 最近一次测速结果（通/不通）。这样一条线路抖动一次就会暂时让位给备选，
> 恢复后又凭速度排回去；只有持续不通满 4 轮才会被真正剔除。

因此不存在「一次性清理」：失效的会周期性复活重试，持续不通的会在 3 次标记后被移除，长期循环。

> **季度清理**是这条循环的终点：线路每被复活一次，`stream_health.revive_count` 就 +1；
> 复活之后又失败过（`fail_count >= 1`，复活会把它清零，所以它天然表示「复活后」）
> 且当前仍是 `error`/`unsupported` 的，会在下一次季度清理时永久停用。
> 停用是**墓碑语义**而不是物理删除 —— 健康行留在库里当标记，所以下次同步即使又抓到
> 同一个地址，它也不会复活、不会被测速、不会进订阅；想反悔用
> `POST /api/purged-streams/restore` 一键放回。相关接口：
> `POST /api/prune/purge-revived`（`apply=false` 先预览）、`GET /api/purged-streams`。

## 线路测速与失效剔除

> 合规边界：测速**只读取有限字节**（默认 1MB）并在超时后立即断开，绝不下完整流、不代理、不转播。并发由 `SPEED_CONCURRENCY` 控制（.env 当前 64），可随时取消。

> **测速目标地址守卫（SSRF 防护）**：频道地址来自第三方播放列表，而测速会**真的对它发起 HTTP 请求**。
> 不校验的话，列表里塞一个 `http://127.0.0.1/…` 或 `http://<内网IP>:<管理端口>/…`，就能让 NAS 去请求自己的内网服务（盲 SSRF）。
> 因此测速发起请求前会解析主机并拦截 **内网 / 环回 / 链路本地 / 保留 / 组播** 地址；**信任主机**直接放行：
> 数据源白名单 `ALLOWED_SOURCE_HOSTS`（内置白名单 + 手动放行）里的主机自动信任，另有 `SPEED_ALLOW_PRIVATE_HOSTS` 可追加。
> 被拦截的线路返回 `skipped`、**不落库**（既不记成功也不记失败），所以不会误伤本地源、也不会污染失效计数。
> 同时**已关闭 httpx 的自动跳转**，改为逐跳校验后再跟随 —— 否则一个公网地址 302 到内网即可绕过全部校验。

> **速率为什么不算整段平均**：很多国内 HLS 源在 CDN 边缘有 ~0.5MB 预缓冲，开头瞬间吐出很高速率再断崖回落。
> 实测同一条线路开头 512KB 是 2707KB/s、之后只有 450KB/s（相差 6 倍），而另一条全程平稳的线路稳定在 ~530KB/s。
> 只用开头一小段、或对整段求平均，都会把**开局快、随后卡顿**的线路排到真正流畅的线路前面。
> 现在先丢弃 `SPEED_WARMUP_BYTES`（默认 512KB）再用剩余部分计算速率，HLS 还会跨最多 3 个连续分片累计读取。

### 测量方法版本与自愈

`stream_health.method` 记录每条记录是用哪个版本的算法测出来的（当前 `METHOD_VERSION`）。
**改动测速算法时必须递增该常量**：`_pending_sql` 会把 `method` 与当前版本不符的记录视为「待重测」，
于是算法升级后旧数据会自动重新测量、排序自动纠正，不需要人工清库或写迁移脚本。

`/api/stats` 的 `speed_backlog` 会给出欠账情况，便于判断预算是否够用：

```json
"speed_backlog": { "method": "3-steady-state", "pending": 27031, "outdated_method": 27031,
                   "never_tested": 0, "daily_budget": 17439, "days_to_clear": 2 }
```

`daily_budget` 即自动维护每轮的测速预算：开启 `AUTO_SPEED_ADAPTIVE`（默认）时按
「未测全量 + 可用存量/复检周期 + 失效存量/复测间隔」折算日需求量（下限 `AUTO_SPEED_LIMIT`，
上限 `AUTO_SPEED_MAX`）。**不建议关掉自适应改回固定小预算**：失效线路数量通常是可用线路的
数倍且每天都到期，预算不够会被它们整轮吃光 —— 可用线路长期不复检（死线路挂着 ok 占住线路 1），
`fail_count` 涨得太慢也永远走不到剔除阈值，整条「标记 → 剔除 → 复活」流水线实际冻结。

| 操作 | 接口 | 说明 |
| --- | --- | --- |
| 启动测速 | `POST /api/speed-test?scope=favorites\|all\|channel&region=&limit=300&refresh=false` | 收藏 / 全部 / 指定频道；`region=domestic\|international` 只测国内或国外 |
| 查看进度 | `GET /api/speed-test` | 完成数、正常数、异常数、本轮剔除数 |
| 取消 | `POST /api/speed-test/cancel` | 立即停止 |
| 预演剔除 | `POST /api/prune` | 只统计，不落库 |
| 执行剔除 | `POST /api/prune?apply=true&min_fail=4` | 默认阈值 = `PRUNE_MAX_FAIL + 1` |
| 恢复 | `POST /api/prune/restore` | 恢复全部被剔除的线路 |
| 复活冷却期满 | `POST /api/prune/revive` | 复活失效满 `RETEST_AFTER_DAYS` 天的线路，标记归零 |

- 测速结果写入 `stream_health`：延迟、吞吐（KB/s）、HTTP 状态、连续失败次数、最近失败时间，以及历史累计成功/失败次数（`ok_total` / `fail_total`）。
- 线路排序统一为**「快 + 稳」**（`static_export._line_order`）：
  已验证（有实测速度）优先 → **速度降序** → 历史失败次数升序 → 历史成功次数降序 → 延迟升序 → URL 兜底（保证同一份数据每次导出顺序一致）。
  注意 `fail_count` 是「连续失败」计数、成功后即清零，只服务于剔除规则，不能当稳定性用，因此另加 `ok_total` / `fail_total`。
- 频道详情与订阅导出共用同一套排序和条数口径：央视系频道 `MAX_LINES_CCTV` 条（默认 10），其它频道 `MAX_LINES_PER_CHANNEL` 条（默认 5）。
  频道详情前 5 条标记「推荐」，「流变体」处显示「显示前 N / 共 M 条」。
- 央视系判定与导出分组一致：模版分组 `央视频道` / `中数传媒` / `4K节目` 归为央视；无模版时看 `group_title == '央视'`。
- 订阅导出：`mode=multi` 每频道输出多条（按上面的条数）；其它 mode 每频道只输出最快 1 条。
  `/iptv`、`/txt` 默认 `multi`；静态文件 `data/live.txt` 用 `multi`，`data/live.m3u` 目前仍是每频道 1 条。
- `top` 参数目前**未生效**（仅为兼容保留；旧文档写的 `mode=best&top=5` 从未实现）。
- 被剔除的线路不出现在频道列表与任何订阅中；冷却期满后会自动复活重试。

### 主用 / 备选 / 失效标记与自动替补

频道详情里每条线路都带 `role`：

| role | 含义 |
| --- | --- |
| `primary` | 前 `MAX_LINES_CCTV` / `MAX_LINES_PER_CHANNEL` 条可用线路，就是当前订阅实际使用的那几条 |
| `backup` | 其余可用线路，排在主用之后 |
| `dead` | 已判定不通（`error` / `unsupported`，或连续失败达 `PRUNE_MAX_FAIL`），不会出现在订阅里 |

**自动替补无需额外配置**：每次生成订阅（`/iptv`、`/txt`、静态文件）都会重新按当前健康度排序并取前 N 条，
`dead` 线路被直接排除，其后的备选自动顶上。频道详情同时返回 `primary_count` / `backup_count` /
`dead_streams` / `usable_streams`；`/api/sources` 也带上了每个源的 `playable_streams` / `dead_streams`。

### 每周自动维护

同步任务末尾按 7 天节流（marker 文件 `data/.maintain_marker`）自动执行，也可 `POST /api/maintenance` 手动触发：

1. `sync_runs` 只保留最近 `SYNC_RUNS_KEEP` 条（默认 100）；
2. 删除连续 `INACTIVE_RETENTION_DAYS` 天（默认 30）没再出现过的 `active=0` 条目——源站换地址后旧行只会被置为 inactive，长期会越积越多；
3. 删除已无对应条目的孤儿 `stream_health` 记录；
4. `PRAGMA wal_checkpoint(TRUNCATE)` + `VACUUM` 收缩文件。

> 维护整段是同步阻塞操作（VACUUM 可能数秒），运行在线程池里，不会卡住 Web 请求。

相关环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SPEED_CONCURRENCY` | `8` | 测速并发数（代码默认 8；本部署 .env 设为 64，全量重测时可临时调高） |
| `SPEED_TIMEOUT` | `5` | 单条超时秒数（失效线路占比高，超时是它们的主要耗时） |
| `SPEED_SAMPLE_BYTES` | `1048576` | 单条最多读取字节（读够才能测出持续吞吐，见下方说明） |
| `SPEED_WARMUP_BYTES` | `524288` | 计算速率时丢弃的开头字节（CDN 边缘预缓冲），见下方说明 |
| `SPEED_MAX_ITEMS` | `5000` | 单次手动测速请求的条数上限 |
| `SPEED_ALLOW_PRIVATE_HOSTS` | 空 | 测速目标地址守卫的**例外主机**（逗号分隔）。默认拦截内网/环回/链路本地/保留地址 |
| `TVBOX_CONFIGS` | 空 | TVBox 配置地址（逗号分隔），同步时自动提取 `lives[].url` 并跟进。留空 = 不生成任何 TVBox 源 |
| `AUTO_SPEED_AFTER_SYNC` | `true` | 同步后自动测速 |
| `AUTO_SPEED_LIMIT` | `1500` | 自动测速每轮条数下限（自适应预算的地板值） |
| `AUTO_SPEED_ADAPTIVE` | `true` | 自适应预算：按存量与复检周期折算日需求量，需求量更高时自动上调本轮预算 |
| `AUTO_SPEED_MAX` | `20000` | 自适应预算上限（同时作为自动维护单轮条数的硬上限） |
| `AUTO_SPEED_REGION` | 空 | 限定自动测速区域：`domestic` / `international` / 空=全部 |
| `AUTO_PRUNE_AFTER_SPEED` | `true` | 测速后自动剔除达标线路 |
| `PRUNE_MAX_FAIL` | `3` | 标记几次后下一次直接剔除 |
| `PURGE_INTERVAL_DAYS` | `90` | 季度清理间隔（天） |
| `PURGE_MIN_REVIVE` | `1` | 复活几次后才进入季度清理候选 |
| `FAIL_RETEST_HOURS` | `20` | 已标记失效线路的复测间隔 |
| `RETEST_AFTER_DAYS` | `14` | 被剔除线路的复活等待天数 |
| `RETEST_OK_HOURS` | `72` | 正常线路的例行复检周期 |
| `SYNC_RUNS_KEEP` | `100` | 每周维护时保留的同步记录条数 |
| `INACTIVE_RETENTION_DAYS` | `30` | 每周维护时删除「连续这么多天没再出现过」的 inactive 历史条目 |
| `BACKUP_LINES_SHOWN` | `5` | 频道详情里在主用线路之外额外展示的备选线路条数 |
| `MAX_LINES_PER_CHANNEL` | `5` | 多线路模式下，非央视频道每频道保留的线路条数 |
| `MAX_LINES_CCTV` | `10` | 多线路模式下，央视系频道（央视频道/中数传媒/4K节目）每频道保留的线路条数 |

## 界面与接口

Web 控制台：源状态、统计、搜索/筛选、频道列表与详情、收藏、手动同步、同步日志、主题切换、响应式布局。

主要接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 健康检查 |
| GET | `/api/meta` | 同步状态 |
| GET | `/api/stats` | 统计 |
| GET | `/api/sources` | 各源状态 |
| POST | `/api/sync` | 手动触发同步（令牌开启时需鉴权） |
| GET | `/api/sync-runs?limit=N` | 同步历史 |
| GET | `/api/channels?...` | 频道检索（关键词、分组、源、收藏、区域、`sort=speed\|name\|default`、分页） |
| GET | `/api/channels/{key}` | 频道详情与其流地址 |
| PUT / DELETE | `/api/favorites/{key}` | 收藏 / 取消收藏 |
| POST | `/api/maintenance` | 手动触发每周维护（清理 + VACUUM） |
| POST | `/api/apply-region-policy` | 手动应用一次 KEEP_REGION 区域保留策略 |
| POST | `/api/sources` | 手动添加数据源（先抓取校验，再排队同步与测速） |
| DELETE | `/api/sources/{slug}` | 移除手动添加的数据源 |
| GET / DELETE | `/api/allowed-hosts` | 查看 / 撤销手动放行的数据源主机 |
| GET | `/iptv`、`/playlist.m3u` | M3U 订阅输出（支持 `group` / `exclude_group`；默认套用 `MAIN_EXCLUDE_GROUP`） |
| GET | `/txt` | TXT 分组输出（参数同 `/iptv`） |
| GET | `/discovered.m3u`、`/discovered.txt` | 「发现源」分组的独立订阅出口（等价 `?group=发现源`，不受 `MAIN_EXCLUDE_GROUP` 影响） |
| GET | `/epg.xml`（同 `/epg`） | 本机 EPG 代理：回源 `EPG_URL`、缓存 `data/epg-cache.xml`，播放器只访问这里（上游令牌不外泄） |

## 测试

```bash
pip install -r requirements.txt
PYTHONPATH=. python -m unittest discover -s tests -v
```

## 运维

```bash
cd /opt/iptv-hub
docker compose logs -f --tail=100
docker compose restart
docker compose pull && docker compose up -d   # 仅当使用远端镜像时
```

更新代码后需重建：

```bash
docker compose up -d --build
```

## 贡献

- 提交前先跑一遍上面「测试」一节的命令；
- 改动 `app/` 下的业务代码时，同步更新 `docker-compose.yml` 与 `.env.example` 里的参数说明 ——
  两处都写同一件事，改了不跟就会口径漂移；
- 手工用 `curl` 验证流地址时**必须带 `-A`**（正常浏览器 UA）：空 UA 与 `curl/8.x` 会拿到假 403。

