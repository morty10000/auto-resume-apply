"""按平台节奏参数（防风控差异化方案）的解析工具。

前端「防风控方案」栏产出的结构：
    pace_by_platform = {
        "boss":     {"page_delay": [6, 12], "apply_delay": [20, 45]},
        "zhilian":  {"page_delay": [5, 10], "apply_delay": [15, 35]},
        ...
    }
采集（翻页延迟）与投递（岗位间隔）分别取 page_delay / apply_delay；
缺失或非法时回退到全局默认，并统一钳制到 [1, 600] 秒。
"""
from __future__ import annotations

from typing import Any

CLAMP_LO, CLAMP_HI = 1.0, 600.0


def pace_pair(
    pace_by_platform: dict[str, Any] | None,
    platform: str,
    key: str,
    default: tuple[float, float],
) -> tuple[float, float]:
    """解析一对 (lo, hi) 秒数；非法配置回退 default（先排序、再钳制）。"""
    try:
        pace = (pace_by_platform or {}).get(platform) or {}
        pair = pace.get(key) if isinstance(pace, dict) else None
        if isinstance(pair, (list, tuple)) and len(pair) >= 2:
            lo, hi = float(pair[0]), float(pair[1])
            if hi < lo:
                lo, hi = hi, lo
            lo = min(max(lo, CLAMP_LO), CLAMP_HI)
            hi = min(max(hi, CLAMP_LO), CLAMP_HI)
            return lo, hi
    except (TypeError, ValueError):
        pass
    d_lo, d_hi = float(default[0]), float(default[1])
    if d_hi < d_lo:
        d_lo, d_hi = d_hi, d_lo
    return d_lo, d_hi
