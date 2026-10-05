"""今日数据统计 API：供主页平台卡下方的「今日采集 / 今日投递」小统计使用。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter
from sqlalchemy import func, select

from backend.db.database import init_db, session_scope
from backend.db.models import DailyStat
from backend.db.models import Job as JobRow
from backend.platforms import registry

router = APIRouter(tags=["stats"])


@router.get("/api/stats/today")
def today_stats() -> dict:
    """今日各平台：采集数（当天入库）+ 投递数（当天成功投递）。"""
    init_db()
    today = date.today().isoformat()
    collected: dict[str, int] = {}
    applied: dict[str, int] = {}

    with session_scope() as s:
        for platform, cnt in s.execute(
            select(JobRow.platform, func.count())
            .where(func.date(JobRow.collected_at) == today)
            .group_by(JobRow.platform)
        ).all():
            collected[platform] = int(cnt)
        for platform, cnt in s.execute(
            select(DailyStat.platform, DailyStat.applied).where(DailyStat.date == today)
        ).all():
            applied[platform] = int(cnt or 0)

    return {
        "ok": True,
        "date": today,
        "platforms": {
            name: {"collected": collected.get(name, 0), "applied": applied.get(name, 0)}
            for name in registry.all_names()
        },
    }
