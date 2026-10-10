# 变更记录

## 2026-10-10 —— 干净无源基线

本版本交付的是**不内置任何数据源**的形态：部署后由使用者在「数据源」页面自行添加。
业务功能（同步 / 测速 / 发现 / EPG 代理 / M3U·TXT 导出 / 回看属性）与带内置源的版本一致。

### 相对带内置源的版本做了什么

- `app/db.py`：`SOURCE_SEEDS` 置为空元组、`SOURCE_SEED_GROUPS` 置为空字典；
  写入路径 `db._seed_sources` / `db._seed_source_groups` 与其语义**完全保留**。
- `app/config.py`：`tvbox_configs` 的代码默认值由 `https://9280.kstore.vip/newwex.json` 改为空串 ——
  否则即使 `.env` 不写这一项，首次启动也会自动种入一个 TVBox 源。
- `docker-compose.yml`：`TVBOX_CONFIGS` 由 `"${TVBOX_CONFIGS:-https://9280.kstore.vip/newwex.json}"`
  改为 `"${TVBOX_CONFIGS:-}"`。Compose 的 `${VAR:-default}` 语义下**空值会回退成默认值**，
  所以只清 `.env` 不够 —— 漏掉这一处时容器仍会种入 1 个 TVBox 源。
- `.env.example`：`TVBOX_CONFIGS` 同步置空（原值保留在上一行的注释里）。
- 删除运行期残留：`data/inline/*.m3u`、`data/.maintain_marker`、`data/.purge_marker`、
  `data/.region_policy`、`__pycache__`、`*.pyc`。仓库内不含任何数据库文件，首次启动自动建空库。

### 部署后应为空（本次实测）

- `GET /api/sources` → `[]`
- `GET /healthz` → `{"status":"ok","sources":0}`
- `GET /api/stats` → `raw_entries: 0`、`channels: 0`、`streams: 0`

### 开源整理

- 移除全部 `*.bak-*` 快照（其中含已删除内置源的完整定义，以及私有订阅地址）。
- 移除内部协作文档，以及写死个人环境的备份 / 还原脚本。
- 修正 `README.md` 中与「零内置源」矛盾的段落：原「内置 20 个种子源」表格、
  `TVBOX_CONFIGS` 默认值、个人部署路径与局域网地址示例。
- 把源码注释与测试里用到的真实订阅令牌样例替换为合成值。

### 发布前脱敏

第一轮移除凭据类内容：`.env`、`*.bak-*` 快照、私有订阅地址、私有运维路径。

第二轮清掉「环境指纹」，只动注释 / docstring / 文档 / 测试夹具数据：

- 内网 IP `192.168.31.x` → RFC 5737 文档保留地址 `192.0.2.x`；
- 注释与 docstring 里的私有 NAS 路径、私有源名移除；
- `tests/test_soma_ua_live.py` 的适配器清单默认路径改为仓库相对 `data/ntv-fm.m3u`。

第三轮清掉其余可识别信息，**其中一项是行为改动**：

- 抓取订阅列表时发出的 `User-Agent` 去掉了个人代号与部署形态描述，改为中性的
  `IPTV-Hub/1.0 (metadata sync)`；同时把两处内联复制品改为引用 `SOURCE_USER_AGENT`
  常量，避免再次漂移。**这会改变应用对上游源站的自我介绍**，若你的上游按 UA 过滤请自行调整；
- 测试注释里的真实出口 IPv4 与真实源地址移除；
- `docker-compose.yml` 的本地镜像名去掉个人前缀；
- README 里的具体 NAS 平台名改为通用表述。

**可执行代码的改动只有上述 `app/sync.py` 的 3 行**（用 AST 剥掉 docstring 后逐文件比对得出：
`app/` 下 13 个模块中 12 个语法树完全一致）。

仍保留的是**功能性取值**，改动会改变运行时行为或破坏回归护栏，因此有意留下：
`app/sync.py` 的 `ALLOWED_SOURCE_HOSTS_BUILTIN`、`app/parsers.py` 的 `DOMESTIC_SOURCES`，
以及守护前者的 `tests/test_ntv_sources.py` 断言。
## 已知限制

- `tests/` 里有多处 `tempfile.TemporaryDirectory(dir="/tmp")` 硬编码了 POSIX 临时目录，
  在 Windows 上会因路径不可写而报错。本项目面向 Linux / Docker 部署，请在容器或 WSL 内跑测试。
- 部分用例需要 FastAPI / httpx 才能导入，未安装时自动 skip；
  `tests/test_soma_ua_live.py` 的联网自检默认跳过（置 `IPTV_LIVE_UA_CHECK=1` 才跑）。
