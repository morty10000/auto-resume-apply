"""岗位匹配 API：用简历档案给数据库里的岗位打分并落库。

- POST /api/match/run   执行一次全量打分（更新 match_score / match_detail / matched_at / status）
- GET  /api/matches     匹配记录（按分数倒序，供「匹配记录」页展示）
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from backend.core import task_control
from backend.core.paths import RESUMES_DIR
from backend.core.task_locks import BUSY_DETAIL, apply_lock, collect_lock, match_lock
from backend.db.database import session_scope
from backend.db.models import Job as JobRow
from backend.db.models import JobStatus
from backend.services.matcher import score_job
from backend.services import task_hub

router = APIRouter(tags=["match"])

PROFILE_PATH = RESUMES_DIR / "profile.json"

# 参与打分的状态（已投递 / 投递失败的岗位不再重新匹配）
_MATCHABLE = (JobStatus.COLLECTED, JobStatus.MATCHED, JobStatus.REJECTED, JobStatus.SKIPPED)


class MatchIn(BaseModel):
    """一次匹配的请求体（来自「匹配设置」+ 采集配置里的城市 / 薪资）。"""

    threshold: int = Field(default=60, ge=0, le=100)
    salary_min: int | None = Field(default=None, ge=0, le=1000)
    salary_max: int | None = Field(default=None, ge=0, le=1000)
    cities: list[str] | None = None
    blacklist: list[str] | None = None
    flow: str = "match"              # match=单独匹配；all=一键全流程里的匹配环节
    flow_label: str = ""             # 任务来源，监控页展示用


def load_resume() -> dict | None:
    """读取简历档案的 parsed 部分；未上传返回 None。"""
    if not PROFILE_PATH.exists():
        return None
    try:
        payload = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    parsed = payload.get("parsed")
    return parsed if isinstance(parsed, dict) else None


@router.post("/api/match/run")
def run_match(item: MatchIn) -> dict:
    """执行匹配打分：简历 × 数据库全部（未投递）岗位。"""
    # 跨任务互斥：任何其他任务在运行都拒绝（防止任务中枢状态互相覆盖）
    if collect_lock.locked() or apply_lock.locked():
        raise HTTPException(status_code=409, detail=BUSY_DETAIL)
    if not match_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail=BUSY_DETAIL)
    try:
        return _match_body(item)
    finally:
        match_lock.release()


def _match_body(item: MatchIn) -> dict:
    resume = load_resume()
    if not resume:
        raise HTTPException(status_code=400, detail="还没有简历档案，请先在「简历」页上传简历")

    hub = task_hub.hub
    is_all = item.flow == "all"
    st = hub.state
    reuse = bool(is_all and st and st.get("active") and st.get("kind") == "all")
    if not reuse:
        hub.begin(
            kind="all" if is_all else "match",
            stages=["collect", "match", "apply"] if is_all else ["match"],
            label=item.flow_label or "",
        )
        task_control.clear()   # 新任务开始：复位上一轮的停止请求
    hub.enter_stage("match", "匹配打分", "读取简历档案与岗位库…")
    hub.update(percent=42 if is_all else 4)

    cfg = item.model_dump(exclude={"flow", "flow_label"})
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    matched_rows: list[dict] = []
    scanned = rejected = 0
    lo, hi = (42, 60) if is_all else (4, 97)
    hub.log("INFO", f"开始匹配打分：阈值 {cfg.get('threshold')} 分 ｜ 逐条比对本轮全部未投递岗位…")

    try:
        with session_scope() as s:
            rows = s.execute(
                select(JobRow).where(JobRow.status.in_(_MATCHABLE)).order_by(JobRow.id)
            ).scalars().all()
            total = max(1, len(rows))
            hub.update(detail=f"共 {len(rows)} 个岗位参与打分…", percent=lo)
            for idx, row in enumerate(rows, start=1):
                try:
                    extra = json.loads(row.extra) if row.extra else {}
                except ValueError:
                    extra = {}
                job = {
                    "title": row.title, "company": row.company, "salary": row.salary,
                    "city": row.city, "extra": extra, "description": row.description,
                }
                result = score_job(job, resume, cfg)
                row.match_score = result["score"]
                row.match_detail = json.dumps(result["detail"], ensure_ascii=False)
                row.matched_at = now
                row.status = result["status"]
                scanned += 1
                if result["status"] == JobStatus.MATCHED:
                    matched_rows.append({
                        "id": row.id, "platform": row.platform, "title": row.title,
                        "company": row.company, "salary": row.salary, "city": row.city,
                        "score": result["score"],
                    })
                else:
                    rejected += 1
                if task_control.is_cancelled():
                    hub.update(stats={"matched": len(matched_rows)}, percent=hi)
                    hub.end(ok=False, summary=f"匹配已手动停止（已完成 {scanned} 个）")
                    matched_rows.sort(key=lambda r: r["score"], reverse=True)
                    return {
                        "ok": True, "stopped": True,
                        "scanned": scanned, "matched": len(matched_rows), "rejected": rejected,
                        "threshold": cfg["threshold"], "top": matched_rows[:10],
                    }
                if idx % 50 == 0 or idx == len(rows):
                    hub.update(
                        detail=f"正在打分（{idx}/{len(rows)}）｜已通过 {len(matched_rows)} 个…",
                        percent=lo + (hi - lo) * idx / total,
                    )
    except Exception as e:  # noqa: BLE001
        hub.end(ok=False, summary=f"匹配失败：{e}")
        raise

    hub.update(stats={"matched": len(matched_rows)}, percent=hi)
    hub.log("OK", f"匹配完成：扫描 {scanned} 个 ｜ 通过 {len(matched_rows)} 个 ｜ 未通过 {rejected} 个")
    if is_all:
        hub.update(detail=f"匹配完成：{len(matched_rows)} 个岗位进入投递队列，准备投递…")
    else:
        hub.end(ok=True, summary=f"匹配完成：通过 {len(matched_rows)} 个 · 未通过 {rejected} 个")

    matched_rows.sort(key=lambda r: r["score"], reverse=True)
    return {
        "ok": True,
        "scanned": scanned,
        "matched": len(matched_rows),
        "rejected": rejected,
        "threshold": cfg["threshold"],
        "top": matched_rows[:10],
    }


@router.get("/api/matches")
def list_matches(limit: int = 200, filter: str = "all") -> dict:
    """匹配记录（仅含已打分的岗位），按分数倒序。"""
    limit = min(max(limit, 1), 1000)
    with session_scope() as s:
        query = select(JobRow).where(JobRow.match_score.is_not(None))
        if filter == "matched":
            query = query.where(JobRow.status == JobStatus.MATCHED)
        elif filter == "rejected":
            query = query.where(JobRow.status == JobStatus.REJECTED)
        query = query.order_by(JobRow.match_score.desc(), JobRow.id.desc()).limit(limit)
        rows = s.execute(query).scalars().all()
        out = []
        for r in rows:
            try:
                detail = json.loads(r.match_detail) if r.match_detail else {}
            except ValueError:
                detail = {}
            out.append({
                "id": r.id,
                "platform": r.platform,
                "title": r.title,
                "company": r.company,
                "salary": r.salary,
                "city": r.city,
                "url": r.url,
                "status": r.status,
                "match_score": r.match_score,
                "matched_at": r.matched_at,
                "collected_at": r.collected_at,
                "detail": detail,
            })
    return {"ok": True, "rows": out}
