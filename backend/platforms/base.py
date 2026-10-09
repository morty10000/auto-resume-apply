"""平台适配器抽象基类与通用数据模型。

所有招聘平台（Boss直聘 / 智联 / 51job / 猎聘 ...）统一实现 BasePlatform 接口，
由 registry 注册后供调度器调用，新增平台不改动其他任何代码。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, Literal

from pydantic import BaseModel, Field


class JobQuery(BaseModel):
    """岗位搜索条件（前端配置页产出）。"""

    keywords: list[str] = Field(min_length=1)          # 岗位关键词，如 ["Python 后端"]
    cities: list[str] = Field(min_length=1)            # 城市名，如 ["上海", "杭州"]
    salary_min: int | None = None                       # 月薪下限（K/月，如 4 = 4K）
    salary_max: int | None = None                       # 月薪上限（K/月）
    experience: list[str] | None = None                 # 经验要求（可多选），如 ["1-3年", "3-5年"]；空 = 不限
    education: list[str] | None = None                  # 学历要求（可多选），如 ["本科", "硕士"]；空 = 不限
    max_pages: int = Field(default=5, ge=1, le=50)      # 每个关键词最大翻页数
    max_jobs: int = Field(default=100, ge=1, le=1000)   # 本轮采集数量上限（达到后停止）

    # ---- 搜索方式 ----
    # each = 逐个关键词搜索；combined = 多个关键词合并为一次搜索
    keyword_mode: Literal["each", "combined"] = "each"

    # ---- 搜索节奏（模拟人工，防触发风控） ----
    kw_delay_min: float = Field(default=8.0, ge=1, le=600)    # 关键词间隔下限（秒）
    kw_delay_max: float = Field(default=20.0, ge=1, le=600)   # 关键词间隔上限（秒）
    page_delay_min: float = Field(default=3.0, ge=1, le=600)  # 翻页间隔下限（秒）
    page_delay_max: float = Field(default=8.0, ge=1, le=600)  # 翻页间隔上限（秒）
    shuffle_keywords: bool = True                             # 关键词随机顺序
    humanize_scroll: bool = True                              # 模拟滚动等浏览动作

    # ---- 正文校验（可选）----
    # True 时：通过列表筛选的岗位会抓详情页正文；正文中不含当前关键词的岗位被丢弃。
    # 抓到的正文同时入库，供简历匹配打分使用（技能识别更准）。
    verify_body: bool = False

    # ---- HR 活跃度过滤（可选）----
    # 约 X 天内有活跃信号才保留（None = 不限）；平台未提供活跃信息时不过滤。
    hr_active_days: int | None = None

    # ---- 已入库岗位集合（采集配额用）----
    # 这些岗位不占用 max_jobs 配额：配额只统计「新岗位」，
    # 已入库的岗位仍会返回（用于补录正文），但不会让采集提前停止。
    # None = 不做区分（兼容旧调用方）。
    known_ids: set[str] | None = None


class Job(BaseModel):
    """标准化岗位。"""

    platform: str
    platform_job_id: str            # 平台内唯一 ID，与 platform 组成去重键
    title: str
    company: str
    salary: str | None = None       # 保留原始文本，如 "15-25K·14薪"
    city: str | None = None
    url: str
    description: str | None = None
    extra: dict = Field(default_factory=dict)   # 平台特有字段：HR 活跃度、融资阶段等


class ApplyResult(BaseModel):
    """单个岗位投递结果。"""

    success: bool
    message: str = ""
    need_verify: bool = False       # 命中验证码/滑块，需人工处理后再继续


class BasePlatform(ABC):
    """平台适配器接口。子类在模块底部 registry.register() 注册实例。

    登录不由适配器负责：全系统仅使用 Edge，登录 = 打开 Edge 窗口手动扫码，
    登录态由 backend.services.edge_login 检测并保存，适配器直接复用同一实例。
    """

    name: ClassVar[str] = ""            # 唯一标识：boss / zhilian / job51 / liepin
    display_name: ClassVar[str] = ""    # 界面展示名

    # ---- 进度上报（采集时由调度层挂上回调，向任务状态中心报明细）----

    def set_progress_handler(self, handler) -> None:
        """挂载进度回调（handler(text: str)）；传 None 清除。"""
        self._progress_handler = handler

    def note(self, text: str) -> None:
        """向任务状态中心上报一行进度（上报失败绝不影响主流程）。"""
        cb = getattr(self, "_progress_handler", None)
        if cb is None:
            return
        try:
            cb(str(text))
        except Exception:  # noqa: BLE001
            pass

    @abstractmethod
    async def check_login(self) -> bool:
        """检查登录态是否有效（只做只读判断，不弹窗）。"""

    @abstractmethod
    async def search_jobs(self, query: JobQuery) -> list[Job]:
        """按条件采集岗位列表，内部自行处理翻页与限速。"""

    @abstractmethod
    async def apply(self, job: Job, greeting: str | None) -> ApplyResult:
        """投递 / 沟通单个岗位。greeting 为 None 时使用平台默认招呼语。"""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"
