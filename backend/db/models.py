"""数据层 ORM 模型（SQLAlchemy 2.0）。

表清单：
    jobs          采集到的岗位（含匹配结果与投递状态）
    applications  每次投递的流水记录
    configs       用户配置键值对
    daily_stats   每日各平台投递计数（限额用）
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, REAL, Text, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def local_now() -> str:
    """本地时间字符串，与 SQLite datetime('now','localtime') 格式一致。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Base(DeclarativeBase):
    """所有模型的基类。"""


class JobStatus:
    """jobs.status 合法取值。"""

    COLLECTED = "collected"   # 已采集
    MATCHED = "matched"       # 匹配通过，待投递
    REJECTED = "rejected"     # 匹配未过
    APPLIED = "applied"       # 已投递
    FAILED = "failed"         # 投递失败
    SKIPPED = "skipped"       # 手动跳过


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("platform", "platform_job_id", name="uq_jobs_platform_job"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(Text, nullable=False)          # boss / zhilian / ...
    platform_job_id: Mapped[str] = mapped_column(Text, nullable=False)   # 平台内唯一 ID（去重键）
    title: Mapped[str | None] = mapped_column(Text)
    company: Mapped[str | None] = mapped_column(Text)
    salary: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[str | None] = mapped_column(Text)          # JSON：HR 活跃度等平台特有字段
    match_score: Mapped[float | None] = mapped_column(REAL)
    match_detail: Mapped[str | None] = mapped_column(Text)   # JSON：各维度得分明细
    matched_at: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Text, default=JobStatus.COLLECTED, server_default=text("'collected'")
    )
    collected_at: Mapped[str] = mapped_column(
        Text, default=local_now, server_default=text("(datetime('now','localtime'))")
    )
    applied_at: Mapped[str | None] = mapped_column(Text)

    applications: Mapped[list[Application]] = relationship(back_populates="job")


class Application(Base):
    """一次投递/沟通的流水记录。"""

    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    result: Mapped[str | None] = mapped_column(Text)     # success / fail
    message: Mapped[str | None] = mapped_column(Text)    # 成功提示 / 失败原因
    created_at: Mapped[str] = mapped_column(
        Text, default=local_now, server_default=text("(datetime('now','localtime'))")
    )

    job: Mapped[Job] = relationship(back_populates="applications")


class AppConfig(Base):
    """用户配置：key-value，value 为 JSON 字符串。"""

    __tablename__ = "configs"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)


class DailyStat(Base):
    """每日各平台投递计数，主键 (date, platform)。"""

    __tablename__ = "daily_stats"

    date: Mapped[str] = mapped_column(Text, primary_key=True)       # '2026-10-01'
    platform: Mapped[str] = mapped_column(Text, primary_key=True)
    applied: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
