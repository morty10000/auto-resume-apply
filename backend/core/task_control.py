"""任务启停控制（协作式取消 + 暂停/恢复）。

全系统同一时间只跑一个任务。用户点「停止」时：
- request() 置位取消标志
- 采集 / 匹配 / 投递的循环在检查点调用 raise_if_cancelled()（或检查 is_cancelled()）
  → 抛出 TaskCancelled / 提前返回 → 调用方保存好已完成的数据后退出
- 各段延时统一走 cancellable_sleep()，被取消时最长 0.5 秒内提前返回

暂停/恢复：request_pause() 在停顿点冻结（暂停期间不向平台发出操作、不消耗延迟时长），
clear_pause() 恢复继续；优先级：停止 > 暂停（暂停等待中收到停止 → 立即退出）。
新任务开始时（collect / match / apply 的 begin 分支）调用 clear() 复位两个标志。
"""
from __future__ import annotations

import asyncio
import threading


class TaskCancelled(Exception):
    """任务被手动停止（调用方应保存已完成数据后退出）。"""


_cancel = threading.Event()
_pause = threading.Event()


def request() -> None:
    """请求停止当前任务（幂等）。"""
    _cancel.set()


def clear() -> None:
    """复位取消与暂停标志（新任务开始时调用）。"""
    _cancel.clear()
    _pause.clear()


def request_pause() -> None:
    """请求暂停当前任务（在下一个停顿点冻结）。"""
    _pause.set()


def clear_pause() -> None:
    """恢复已暂停的任务。"""
    _pause.clear()


def is_cancelled() -> bool:
    return _cancel.is_set()


def is_paused() -> bool:
    return _pause.is_set()


async def wait_if_paused(step: float = 0.5) -> None:
    """暂停中则挂起（轮询至恢复或取消）；供循环检查点调用。"""
    while _pause.is_set():
        if _cancel.is_set():
            return
        await asyncio.sleep(step)


def raise_if_cancelled() -> None:
    """检查点：任务已被停止则抛出 TaskCancelled。"""
    if _cancel.is_set():
        raise TaskCancelled("任务已被手动停止")


async def cancellable_sleep(seconds: float, step: float = 0.5) -> None:
    """分片等待 + 暂停感知：取消立即返回；暂停时冻结（不消耗剩余时长）。"""
    remain = max(0.0, float(seconds))
    while True:
        if _cancel.is_set():
            return
        if _pause.is_set():
            await asyncio.sleep(step)
            continue
        if remain <= 0:
            return
        chunk = min(step, remain)
        await asyncio.sleep(chunk)
        remain -= chunk
