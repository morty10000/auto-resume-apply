"""跨任务互斥锁与忙提示（全服务共享，单实例）。

设计（全系统同一时间只允许一个任务在跑：采集 / 匹配 / 投递 任选其一）：
· collect / apply 在异步端点运行 —— asyncio.Lock
· match 在同步端点（线程池）运行 —— threading.Lock
· 任一任务运行时，其他任务入口一律 409（防止任务中枢状态互相覆盖，
  也防止两个任务争用同一个浏览器标签页）。
"""
from __future__ import annotations

import asyncio
import threading

collect_lock = asyncio.Lock()
apply_lock = asyncio.Lock()
match_lock = threading.Lock()

BUSY_DETAIL = "已有任务在运行，请到「运行监控」页查看进度"


def any_busy() -> bool:
    """是否有任一任务在运行（供状态接口 / 前端预检使用）。"""
    return bool(collect_lock.locked() or apply_lock.locked() or match_lock.locked())
