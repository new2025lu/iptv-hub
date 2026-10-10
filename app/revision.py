"""全局导出数据版本号。

任何导致导出内容变化的动作（同步成功、测速落库、剔除/恢复/复活）都会自增版本号，
订阅导出缓存据此生成 ETag，播放器可用条件请求拿到 304，避免每次全量重算。
"""

from __future__ import annotations

import threading

_export_revision = {"value": 0}
# 自增是「读-加-写」两步，bump 会从 FastAPI 线程池端点、同步 worker、测速落库线程
# 同时进来；不加锁可能丢一次自增 —— 版本号偏小意味着「内容变了版本没变」，
# 缓存会把旧数据当新版本命中（直到 TTL 兜底）。
_lock = threading.Lock()


def bump_revision() -> None:
    with _lock:
        _export_revision["value"] += 1


def current_revision() -> int:
    # 读单个 int 在 CPython 下本身原子，不需要锁
    return _export_revision["value"]
