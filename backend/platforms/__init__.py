"""平台适配器包。

新增平台：在 platforms/ 下新建模块，实现 BasePlatform 子类并在模块底部
register(实例) 注册，然后在本文件 import 该模块触发注册。
约定：一个平台模块只放选择器常量 + 一个适配器类，选择器集中在文件顶部便于改版维护。
"""
from .base import ApplyResult, BasePlatform, Job, JobQuery
from .registry import all_names, get, list_all, register, unregister

# 平台模块（import 即注册到 registry）
from . import boss  # noqa: F401
from . import zhilian  # noqa: F401
from . import job51  # noqa: F401
from . import liepin  # noqa: F401

__all__ = [
    "ApplyResult",
    "BasePlatform",
    "Job",
    "JobQuery",
    "all_names",
    "get",
    "list_all",
    "register",
    "unregister",
]
