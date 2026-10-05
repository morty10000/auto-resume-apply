"""平台适配器注册表（独立模块，避免与平台模块循环 import）。"""
from __future__ import annotations

from .base import BasePlatform

_registry: dict[str, BasePlatform] = {}


def register(platform: BasePlatform) -> BasePlatform:
    """注册（或覆盖注册）一个平台适配器实例。"""
    if not platform.name:
        raise ValueError(f"{type(platform).__name__}.name 不能为空")
    _registry[platform.name] = platform
    return platform


def unregister(name: str) -> None:
    """注销平台（测试与热重载用）。"""
    _registry.pop(name, None)


def get(name: str) -> BasePlatform:
    """取平台适配器实例；未注册时报 ValueError。"""
    try:
        return _registry[name]
    except KeyError:
        raise ValueError(f"未注册的平台: {name!r}，已注册: {sorted(_registry)}") from None


def all_names() -> list[str]:
    """全部已注册平台标识（排序）。"""
    return sorted(_registry)


def list_all() -> list[BasePlatform]:
    """全部已注册平台实例。"""
    return [_registry[n] for n in all_names()]
