"""随机化限速器：所有平台操作间隔统一从这里取，保持拟人节奏。

平台风控对固定间隔敏感——任何涉及平台页面的 sleep 都必须走这里，
禁止散落的固定 asyncio.sleep。

所有停顿均为「可取消」：用户点停止时最长 0.5 秒内提前返回，
由调用方的检查点完成退出（见 backend.core.task_control）。
"""
from __future__ import annotations

import math
import random

from backend.core import task_control


def jitter(lo: float, hi: float) -> float:
    """返回 [lo, hi] 区间随机秒数（截断对数正态：右偏分布，比均匀更接近人类操作间隔）。"""
    lo = max(0.05, float(lo))
    hi = max(lo + 0.05, float(hi))
    if hi - lo < 0.15:                      # 区间过窄时退化为均匀取数
        return random.uniform(lo, hi)
    mu = math.log(lo + (hi - lo) * 0.4)     # 中位数落在区间前 40% 处，长尾拖向 hi
    sigma = 0.55
    for _ in range(10):                     # 截断采样：落在 [lo, hi] 内即取
        v = math.exp(random.gauss(mu, sigma))
        if lo <= v <= hi:
            return v
    return random.uniform(lo, hi)


async def random_delay(lo: float = 0.8, hi: float = 2.5) -> None:
    """通用操作间随机停顿（秒）。"""
    await task_control.cancellable_sleep(jitter(lo, hi))


async def page_delay(lo: float = 2.0, hi: float = 4.5) -> None:
    """翻页 / 页面跳转之间的停顿（可配置区间）。"""
    await task_control.cancellable_sleep(jitter(lo, hi))


async def keyword_delay(lo: float = 8.0, hi: float = 20.0) -> None:
    """两个关键词之间的停顿（可配置区间，拟人化防风控）。"""
    await task_control.cancellable_sleep(jitter(lo, hi))
