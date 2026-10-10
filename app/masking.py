"""展示用脱敏：数据源/候选地址里常带上游凭据，界面与日志不该回显原文。

**为什么单独成模块**（v42，审计 P1-5）：这两段逻辑原先只长在 `main.py` 里，
而「发现」模块（`discover.discovered_summary`）同样需要给候选地址打码 ——
`discover` 被 `main` 导入，反向导入会成环；而且 `main` 会连带导入 FastAPI，
测试里想单独验证脱敏规则就必须把整个 Web 框架拉起来。
搬到这里之后，两侧共用同一份规则，`tests/` 里也能直接 import 验证。
"""

from __future__ import annotations

from urllib.parse import urlsplit

# 路径段长度达到这个值就按"令牌"处理。上游私有路径令牌实测 24 位，但运营商的短令牌
# （`Ex4mPl3T0k` 之类 10 位上下）也存在，所以阈值取 10：`live.m3u`/`index.m3u`（8~9 位）
# 这类结构名照常显示，稍长的文件名会被打码 —— 反正识别一个源靠的是主机名。
_SECRET_SEGMENT_MIN = 10
# 主机名最左标签达到这个长度就按"令牌当了子域"处理（如 <24位令牌>.example.com）
_SECRET_LABEL_MIN = 16


def mask_hostname(hostname: str) -> str:
    """主机名脱敏：最左标签过长（像令牌）时打码，其余保留。

    有些订阅把令牌放在子域上（`https://<token>.example.com/…`），只遮路径挡不住。
    普通子域（`cdn.`、`raw.`、`testingcf.`）都短于阈值，不受影响。

    **不再要求"至少三段"**（v42，审计 P1-5）：原先 `len(labels) > 2` 才打码，
    于是「令牌当顶级域」的两段写法（`https://<24位令牌>.com/x.m3u`）整串原样露出 ——
    令牌恰好是长标签时，段数多少并不改变"它是令牌"这一事实，判定只该看长度。
    """
    labels = (hostname or "").split(".")
    if labels and len(labels[0]) >= _SECRET_LABEL_MIN:
        labels[0] = "***"
    return ".".join(labels)


def mask_source_url(url: str) -> str:
    """数据源地址的**展示用脱敏**：只保留 `scheme://host[:port]` 与结构化路径段。

    为什么需要：`/api/sources` 是 GET，不在 `require_admin` 范围内（局域网任何设备可读），
    而源地址里常带**上游的长期凭据** —— 例如 `http://116.ltd:1905/u/<24位令牌>/m3u`、
    `http://<NAS-IP>:50089/sub?Ex4mPl3T0k=m3u`。拿到这种地址可以随时重新拉一份新列表，
    比播放列表快照（一次性）更值钱。

    脱敏规则（保守但可辨认，前端只用它当一个文本显示）：
      * 保留 `scheme://host[:port]` —— 界面上靠它区分不同的源；
      * userinfo（`user:pass@`）一律不显示；
      * 路径段长度 ≥ 10 视为令牌 → `***`（`/sub`、`live.m3u`、`newwex.json` 这类照常显示）；
      * 查询串**整段**打码成 `?***`：键名与值都不保留。原先只打码值、保留键名，
        方向正好反了 —— 运营商内网源常见形态是 `?<随机令牌>=m3u`，秘密在**参数名**上
        （生产实测运营商内网源的 `?Ex4mPl3T0k=m3u` 因此被原样暴露）；fragment 去掉。
    解析异常时整体返回 `***`，绝不回退成原文。完整地址仍在 `.env` 与数据库里。

    v47 修正了两处 docstring/实现漂移，两处都在本段：
      * 路径段阈值原写 16、实现是 `_SECRET_SEGMENT_MIN = 10`（10 才挡得住 10 位短令牌）；
      * 查询串原写"只留键名，值一律 `***`（最多 4 个键）"，实现是整段 `?***`
        —— 既不留键名，也没有 4 键上限（`parts.query` 只是布尔判断）。
    """
    try:
        parts = urlsplit(url or "")
        if not parts.scheme or not parts.hostname:
            return "***"
        netloc = mask_hostname(parts.hostname)
        try:
            if parts.port:
                netloc = f"{netloc}:{parts.port}"
        except ValueError:      # 端口非法（例如 http://h:99999/）时只留主机名
            pass
        segs = ["***" if len(seg) >= _SECRET_SEGMENT_MIN else seg
                for seg in parts.path.split("/") if seg]
        tail = "/" + "/".join(segs) if segs else "/"
        if parts.query:
            # **整个查询串打码**（原先保留键名、只打码值，方向正好反了）：
            # 运营商内网源常见形态是 `?<随机令牌>=m3u` —— 秘密在**参数名**上，
            # 值只是格式选择。实测生产里这类源的 `?Ex4mPl3T0k=m3u` 因此被原样暴露。
            tail += "?***"
        return f"{parts.scheme}://{netloc}{tail}"
    except Exception:  # noqa: BLE001 - 脱敏失败就整体打码，不能漏原文
        return "***"
