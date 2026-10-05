"""投递 API：对「匹配通过」的岗位发起真实沟通（立即沟通 + 可选自定义打招呼语）。

- POST /api/apply/run     按分数从高到低投递；每日限额 / 间隔 / 长休息 / 验证熔断
- GET  /api/applications  投递记录（尝试流水 + 待投递队列）
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from backend.api.match import load_resume
from backend.core import task_control
from backend.core.pace import pace_pair
from backend.core.task_locks import BUSY_DETAIL, collect_lock, match_lock
from backend.core.task_locks import apply_lock as _lock
from backend.db.database import session_scope
from backend.db.models import Application as AppRow
from backend.db.models import DailyStat
from backend.db.models import Job as JobRow
from backend.db.models import JobStatus
from backend.platforms import get as get_platform
from backend.platforms.base import ApplyResult, Job
from backend.services import task_hub

router = APIRouter(tags=["apply"])

PLATFORM = "boss"


class ApplyIn(BaseModel):
    """一次投递的请求体。"""

    limit: int | None = Field(default=None, ge=1, le=100)     # 本轮上限（None = 全部/受每日限额约束）
    job_ids: list[int] | None = None                          # 指定岗位（重试单个用）
    daily_limit: int = Field(default=30, ge=1, le=500)
    delay_min: float = Field(default=5.0, ge=1, le=600)       # 岗位间隔
    delay_max: float = Field(default=12.0, ge=1, le=600)
    rest_every: int = Field(default=10, ge=0, le=100)         # 每 N 个长休息
    rest_min: float = Field(default=30.0, ge=5, le=600)
    rest_max: float = Field(default=60.0, ge=5, le=600)
    greeting: str | None = None                               # 自定义打招呼语模板
    daily_limit_by_platform: dict[str, int] | None = None     # 每个平台各自的每日上限（优先生效）
    pace_by_platform: dict[str, dict] | None = None           # 每平台节奏方案：{platform: {"apply_delay": [lo, hi]}}
    flow: str = "apply"            # apply=单独投递；all=一键全流程里的投递环节
    flow_label: str = ""           # 任务来源，监控页展示用


def _today() -> str:
    return date.today().isoformat()


def _daily_used(platform: str = PLATFORM) -> int:
    with session_scope() as s:
        row = s.execute(
            select(DailyStat).where(DailyStat.date == _today(), DailyStat.platform == platform)
        ).scalar_one_or_none()
        return int(row.applied or 0) if row else 0


def _row_payload(r: JobRow) -> dict:
    try:
        extra = json.loads(r.extra) if r.extra else {}
    except ValueError:
        extra = {}
    return {
        "platform": r.platform, "platform_job_id": r.platform_job_id,
        "title": r.title, "company": r.company, "salary": r.salary,
        "city": r.city, "url": r.url, "extra": extra,
    }


def _fill_greeting(tpl: str | None, job: Job, skills: list[str]) -> str | None:
    """打招呼语模板变量替换：{job_title} {company} {skills}。"""
    if not tpl or not tpl.strip():
        return None
    # 挑用于句子的技能：短词、无空格、去互为子串的重复（嵌入式 vs 嵌入式软件）
    cand = [s for s in (skills or []) if " " not in s and len(s) <= 10]
    nice = [s for s in cand if not any(s != o and s in o for o in cand)][:4]
    text = tpl.strip()
    text = text.replace("{job_title}", job.title or "")
    text = text.replace("{company}", job.company or "")
    text = text.replace("{skills}", "、".join(nice) if nice else "相关技能")
    return text[:200]


@router.post("/api/apply/run")
async def run_apply(item: ApplyIn) -> dict:
    """执行一轮投递（同步；按平台分组，内部含节奏间隔与每日限额）。"""
    if _lock.locked() or collect_lock.locked() or match_lock.locked():
        raise HTTPException(status_code=409, detail=BUSY_DETAIL)

    resume = load_resume() or {}
    skills = list(resume.get("skills") or [])

    async with _lock:
        hub = task_hub.hub
        is_all = item.flow == "all"
        st = hub.state
        reuse = bool(is_all and st and st.get("active") and st.get("kind") == "all")
        if not reuse:
            hub.begin(
                kind="all" if is_all else "apply",
                stages=["collect", "match", "apply"] if is_all else ["apply"],
                label=item.flow_label or "",
            )
            task_control.clear()   # 新任务开始：复位上一轮的停止请求
        hub.enter_stage("apply", "投递沟通", "整理待投递队列…")
        hub.update(percent=62 if is_all else 4)

        with session_scope() as s:
            q = select(JobRow).where(JobRow.status == JobStatus.MATCHED)
            if item.job_ids:
                q = q.where(JobRow.id.in_(item.job_ids))
            q = q.order_by(JobRow.match_score.desc(), JobRow.id.asc())
            rows = s.execute(q).scalars().all()
            queue = [(r.id, _row_payload(r), r.match_score) for r in rows]

        # 按平台分组（保持分数序），平台之间按各自最高分排序
        groups: dict[str, list] = {}
        for row_id, payload, score in queue:
            groups.setdefault(payload["platform"], []).append((row_id, payload, score))
        platform_order = sorted(
            groups, key=lambda p: groups[p][0][2] if groups[p][0][2] is not None else 0, reverse=True
        )
        used_all = sum(_daily_used(p) for p in groups)
        base = {
            "ok": True, "queued": len(queue), "processed": 0, "applied": 0, "failed": 0,
            "daily_used": used_all, "daily_limit": item.daily_limit,
            "by_platform": {},
            "need_verify": False, "results": [],
        }
        if not queue:
            base["message"] = "没有待投递的岗位（先跑一次匹配，或已全部投递）"
            hub.log("WARN", "没有待投递的岗位（先跑一次匹配，或已全部投递）")
            hub.end(ok=True, summary="投递阶段：没有待投递的岗位")
            return base

        cap = item.limit or len(queue)
        total = max(1, min(cap, len(queue)))
        queue_desc = "、".join(f"{p}×{len(groups[p])}" for p in platform_order)
        limits_desc = "、".join(
            f"{p}×{int((item.daily_limit_by_platform or {}).get(p, item.daily_limit) or item.daily_limit)}"
            for p in platform_order
        )
        hub.log(
            "INFO",
            f"投递队列 {len(queue)} 个（{queue_desc}）｜ 间隔 {item.delay_min:.0f}-{item.delay_max:.0f} 秒"
            f" ｜ 各平台每日上限 {limits_desc}",
        )
        results: list[dict] = []
        applied = failed = 0
        need_verify = False
        started = time.time()
        processed = 0
        stop = False
        by_platform: dict[str, dict] = {}

        def _stopped_response() -> dict:
            """手动停止：保留已完成的投递记录后返回。"""
            hub.update(stats={"applied": applied, "failed": failed})
            hub.log("WARN", f"投递已手动停止：成功 {applied} 个 · 失败 {failed} 个（已投数据已入库）")
            hub.end(ok=False, summary=f"投递已手动停止（成功 {applied} · 失败 {failed}）")
            base.update({
                "processed": len(results), "applied": applied, "failed": failed,
                "need_verify": need_verify, "results": results, "stopped": True,
                "daily_used": sum(_daily_used(p) for p in groups),
                "by_platform": by_platform,
                "elapsed": round(time.time() - started, 1),
            })
            return base

        async def _apply_platform(platform: str) -> None:
            nonlocal applied, failed, processed, need_verify
            if stop or processed >= cap:
                return
            if task_control.is_cancelled():
                return
            await task_control.wait_if_paused()
            stat = {"ok": True, "applied": 0, "failed": 0, "skipped": 0, "reason": ""}
            by_platform[platform] = stat
            try:
                adapter = get_platform(platform)
            except ValueError:
                stat.update({"ok": False, "reason": "适配器未实现", "skipped": len(groups[platform])})
                hub.log("WARN", f"{platform}：适配器未实现，跳过 {len(groups[platform])} 个岗位")
                return
            display = getattr(adapter, "display_name", platform) or platform
            if not await adapter.check_login():
                stat.update({"ok": False, "reason": "未登录", "skipped": len(groups[platform])})
                for row_id, payload, score in groups[platform]:
                    results.append({
                        "job_id": row_id, "title": payload.get("title"), "company": payload.get("company"),
                        "salary": payload.get("salary"), "score": score, "platform": platform,
                        "success": False, "message": "平台未登录，已跳过",
                    })
                hub.log("WARN", f"{display}：登录态失效，跳过 {len(groups[platform])} 个岗位")
                return
            # 该平台自己的投递节奏（防風控方案；缺省回退全局 delay_min/max）
            p_lo, p_hi = pace_pair(
                item.pace_by_platform, platform, "apply_delay",
                (float(item.delay_min), float(item.delay_max)),
            )
            # 该平台自己的每日上限（by_platform 优先，缺省回退全局 daily_limit）
            limit_for = (item.daily_limit_by_platform or {}).get(platform, item.daily_limit)
            limit_for = max(1, min(500, int(limit_for or item.daily_limit)))
            remaining = max(0, limit_for - _daily_used(platform))
            hub.update(
                detail=f"【{display}】开始投递（{len(groups[platform])} 个候选，今日剩余名额 {remaining}"
                       f" · 节奏 {p_lo:.0f}-{p_hi:.0f}s）…"
            )
            hub.log("INFO", f"{display}：本平台投递节奏 {p_lo:.0f}-{p_hi:.0f} 秒")

            platform_total = len(groups[platform])
            platform_done = 0
            p_step = 0
            for row_id, payload, score in groups[platform]:
                if stop or processed >= cap or remaining <= 0:
                    break
                if task_control.is_cancelled():
                    return
                await task_control.wait_if_paused()
                idx = processed + 1
                if p_step > 0:
                    lo, hi = p_lo, p_hi
                    wait = random.uniform(lo, hi)
                    is_rest = bool(item.rest_every and p_step % item.rest_every == 0)
                    if is_rest:
                        lo2, hi2 = sorted((float(item.rest_min), float(item.rest_max)))
                        wait = random.uniform(lo2, hi2)
                    hub.update(detail=(
                        f"长休息 {wait:.0f} 秒（模拟人工节奏，防触发风控）…" if is_rest
                        else f"投递间隔等待 {wait:.0f} 秒…"
                    ))
                    await task_control.cancellable_sleep(wait)
                    if task_control.is_cancelled():
                        return
                p_step += 1

                job = Job(**payload)
                greeting = _fill_greeting(item.greeting, job, skills)
                hub.update(detail=f"（{idx}/{total}）正在投递【{display}】{job.title} @ {job.company} …")
                try:
                    r = await adapter.apply(job, greeting)
                except task_control.TaskCancelled:
                    return
                except Exception as e:  # noqa: BLE001
                    r = ApplyResult(success=False, message=f"投递异常（{type(e).__name__}）：{e}")
                now = time.strftime("%Y-%m-%d %H:%M:%S")

                with session_scope() as s:
                    s.add(AppRow(job_id=row_id, result="success" if r.success else "fail", message=r.message))
                    if r.success:
                        jr = s.get(JobRow, row_id)
                        jr.status = JobStatus.APPLIED
                        jr.applied_at = now
                        ds = s.execute(
                            select(DailyStat).where(
                                DailyStat.date == _today(), DailyStat.platform == platform
                            )
                        ).scalar_one_or_none()
                        if ds is None:
                            ds = DailyStat(date=_today(), platform=platform, applied=0)
                            s.add(ds)
                        ds.applied = int(ds.applied or 0) + 1

                processed += 1
                platform_done += 1
                if r.success:
                    applied += 1
                    stat["applied"] += 1
                    remaining -= 1
                    hub.log("OK", f"（{processed}/{total}）{display} {job.title} 投递成功：{r.message}")
                else:
                    failed += 1
                    stat["failed"] += 1
                    hub.log("ERR", f"（{processed}/{total}）{display} {job.title} 投递失败：{r.message}")
                hub.update(
                    detail=f"已处理 {processed}/{total}：成功 {applied} · 失败 {failed}",
                    percent=(62 if is_all else 4) + (97 - (62 if is_all else 4)) * processed / total,
                    stats={"applied": applied, "failed": failed},
                )
                results.append({
                    "job_id": row_id, "title": job.title, "company": job.company,
                    "salary": job.salary, "score": score, "platform": platform,
                    "success": r.success, "message": r.message,
                })
                if r.need_verify:
                    need_verify = True
                    remaining_n = max(0, platform_total - platform_done)
                    hub.log(
                        "WARN",
                        f"{display}：检测到安全验证（验证码），已跳过该平台"
                        f"（剩余 {remaining_n} 个岗位未处理，处理验证后可续投）",
                    )
                    break

        # ---- 四平台并行投递：每个平台独立协程，各自节奏互不阻塞 ----
        _results = await asyncio.gather(
            *[_apply_platform(p) for p in platform_order],
            return_exceptions=True,
        )
        for _r in _results:
            if isinstance(_r, BaseException) and not isinstance(_r, task_control.TaskCancelled):
                hub.log("ERR", f"平台投递任务意外异常：{_r!r}")
        if task_control.is_cancelled():
            return _stopped_response()

        hub.update(stats={"applied": applied, "failed": failed})
        hub.update(percent=100)
        verify_note = "（有平台遇验证码已跳过，处理后可续投）" if need_verify else ""
        if is_all:
            hub.end(ok=True, summary=f"全流程完成：投递成功 {applied} 个 · 失败 {failed} 个{verify_note}")
        else:
            hub.end(ok=True, summary=f"投递完成：成功 {applied} 个 · 失败 {failed} 个{verify_note}")

        base.update({
            "processed": len(results), "applied": applied, "failed": failed,
            "need_verify": need_verify, "results": results,
            "daily_used": sum(_daily_used(p) for p in groups),
            "by_platform": by_platform,
            "elapsed": round(time.time() - started, 1),
        })
        return base


@router.get("/api/applications")
def list_applications(limit: int = 300) -> dict:
    """投递记录：尝试流水（成功/失败）+ 待投递队列。"""
    limit = min(max(limit, 1), 1000)
    out: list[dict] = []
    with session_scope() as s:
        apps = s.execute(select(AppRow).order_by(AppRow.id.desc()).limit(limit)).scalars().all()
        ids = {a.job_id for a in apps}
        jobs: dict[int, JobRow] = {}
        if ids:
            for r in s.execute(select(JobRow).where(JobRow.id.in_(ids))).scalars():
                jobs[r.id] = r
        for a in apps:
            j = jobs.get(a.job_id)
            if j is None:
                continue
            out.append({
                "kind": "attempt", "id": a.id, "job_id": a.job_id,
                "platform": j.platform, "title": j.title, "company": j.company,
                "salary": j.salary, "city": j.city, "score": j.match_score,
                "status": "applied" if a.result == "success" else "failed",
                "message": a.message, "time": a.created_at,
            })
        pending = s.execute(
            select(JobRow).where(JobRow.status == JobStatus.MATCHED)
            .order_by(JobRow.match_score.desc(), JobRow.id.asc()).limit(limit)
        ).scalars().all()
        for j in pending:
            out.append({
                "kind": "pending", "id": f"j{j.id}", "job_id": j.id,
                "platform": j.platform, "title": j.title, "company": j.company,
                "salary": j.salary, "city": j.city, "score": j.match_score,
                "status": "matched", "message": "待投递",
                "time": j.matched_at or j.collected_at,
            })
    return {"ok": True, "rows": out}
