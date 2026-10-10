"""真实采集 API：执行 Boss 岗位采集并写入数据库。

采集走 Boss 适配器的真实管线（原生标签 + 页面内 API 请求，防风控），
同一时间只允许一个采集任务在运行。
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from backend.db.database import init_db, session_scope
from backend.db.models import Job as JobRow
from backend.db.models import JobStatus
from backend.core import task_control
from backend.core.pace import pace_pair
from backend.core.task_locks import BUSY_DETAIL, apply_lock, collect_lock as _lock, match_lock
from backend.platforms import get as get_platform
from backend.platforms.base import JobQuery
from backend.services import task_hub
from backend.services.verify import VerifyRequired, VerifyTimeout, note_verify_event

router = APIRouter(tags=["collect"])


def _persist_jobs(all_jobs: list) -> int:
    """把采集到的岗位写入数据库（platform + job_id 去重），返回新入库数量。"""
    new_rows = 0
    with session_scope() as s:
        for j in all_jobs:
            exists = s.execute(
                select(JobRow).where(
                    JobRow.platform == j.platform,
                    JobRow.platform_job_id == j.platform_job_id,
                )
            ).scalar_one_or_none()
            if exists:
                if j.description and not exists.description:
                    exists.description = j.description   # 老岗位补录正文
                continue
            row = JobRow(
                platform=j.platform,
                platform_job_id=j.platform_job_id,
                title=j.title,
                company=j.company,
                salary=j.salary,
                city=j.city,
                url=j.url,
                description=(j.description or None),
                extra=json.dumps(j.extra, ensure_ascii=False),
                status=JobStatus.COLLECTED,
            )
            s.add(row)
            new_rows += 1
    return new_rows


class CollectIn(BaseModel):
    """一次真实采集的请求体（前端配置字段子集）。"""

    platforms: list[str] = Field(default_factory=lambda: ["boss"], min_length=1)   # 要采集的平台
    keywords: list[str] = Field(min_length=1)
    cities: list[str] = Field(min_length=1)
    max_jobs: int = Field(default=100, ge=1, le=1000)
    max_pages: int = Field(default=5, ge=1, le=50)
    keyword_mode: str = "each"
    salary_min: int | None = Field(default=None, ge=0, le=1000)   # K/月
    salary_max: int | None = Field(default=None, ge=0, le=1000)   # K/月
    experience: list[str] | None = None                            # 经验要求（空=不限）
    education: list[str] | None = None                             # 学历要求（空=不限）
    kw_delay_min: float = Field(default=8.0, ge=1, le=600)
    kw_delay_max: float = Field(default=20.0, ge=1, le=600)
    page_delay_min: float = Field(default=3.0, ge=1, le=600)
    page_delay_max: float = Field(default=8.0, ge=1, le=600)
    shuffle_keywords: bool = True
    humanize_scroll: bool = True
    max_jobs_by_platform: dict[str, int] | None = None   # 每个平台各自的采集上限（优先生效）
    max_pages_by_platform: dict[str, int] | None = None  # 每个平台各自的页数上限（优先生效）
    pace_by_platform: dict[str, dict] | None = None      # 每平台节奏方案：{platform: {"page_delay": [lo, hi]}}
    verify_body_by_platform: dict[str, bool] | None = None   # 每平台正文校验（True=抓详情正文并按关键词过滤）
    hr_active_days: int | None = Field(default=None, ge=0, le=3650)   # HR活跃度过滤：X 天内有活跃信号才保留（None=不限）
    flow: str = "collect"            # collect=单独采集；all=一键全流程里的采集环节
    flow_label: str = ""             # 任务来源（配置名 / 当前表单），监控页展示用


@router.post("/api/collect/run")
async def run_collect(item: CollectIn) -> dict:
    """执行一次真实采集（可多平台）；结果写入数据库（platform + job_id 去重）。

    进度通过 task_hub 实时上报，供「运行监控」页轮询展示（含每关键词/每页明细）。
    """
    if _lock.locked() or apply_lock.locked() or match_lock.locked():
        raise HTTPException(status_code=409, detail=BUSY_DETAIL)
    async with _lock:
        return await _collect_impl(item)


async def _collect_impl(item: CollectIn) -> dict:
    """采集执行体（不含锁管理）：单阶段入口与全流程编排器共用。"""
    init_db()
    platforms = list(dict.fromkeys(item.platforms))   # 去重：防止同平台被并行启动两次
    hub = task_hub.hub
    is_all = item.flow == "all"
    hub.begin(
        kind="all" if is_all else "collect",
        stages=["collect", "match", "apply"] if is_all else ["collect"],
        label=item.flow_label or "",
    )
    task_control.clear()   # 新任务开始：复位上一轮的停止请求
    hub.enter_stage("collect", "采集岗位", "检查各平台登录状态…")
    hub.log(
        "INFO",
        f"开始采集：平台 {len(platforms)} 个 ｜ 关键词 {'、'.join(item.keywords)}"
        f" ｜ 城市 {'、'.join(item.cities)} ｜ 各平台上限单独设置",
    )

    try:
        query = JobQuery(**item.model_dump(exclude={"flow", "flow_label", "max_jobs_by_platform"}))
        all_jobs = []
        by_platform: dict[str, dict] = {}
        last_scanned = last_filtered = 0
        n = max(1, len(platforms))
        lo, hi = (2, 40) if is_all else (2, 97)   # 采集在整条流水线里的进度区间
        done_cnt = {"n": 0}
        saved_cnt = {"n": 0}            # 已增量入库的新行数（中途退出/断电不丢已采数据）
        resume_list: list = []          # 验证跳过平台的续跑信息（供前端显示「继续」按钮）

        def _save_now(items: list) -> None:
            """增量入库：某平台一完成立即写库；失败不影响本轮（结束时还会统一兜底入库）。"""
            try:
                saved_cnt["n"] += _persist_jobs(items)
            except Exception as _pe:  # noqa: BLE001
                hub.log("WARN", f"即时入库失败（{type(_pe).__name__}），将在本轮结束时统一入库")

        async def _run_platform(i: int, name: str) -> None:
            nonlocal last_scanned, last_filtered
            platform = None
            display = name
            try:
                platform = get_platform(name)
                display = getattr(platform, "display_name", name) or name
            except ValueError:
                by_platform[name] = {"ok": False, "reason": "适配器未实现"}
                hub.log("WARN", f"{display}：适配器未实现，已跳过")
                return
            try:
                hub.update(detail=f"{display}：检查登录状态…")
                if not await platform.check_login():
                    by_platform[name] = {"ok": False, "reason": "未登录"}
                    hub.log("WARN", f"{display}：未登录，已跳过（到主页平台卡片点「去登录」）")
                    return

                # 该平台自己的采集上限（by_platform 优先，缺省回退全局 max_jobs）
                p_limit = (item.max_jobs_by_platform or {}).get(name, item.max_jobs)
                p_limit = max(1, min(1000, int(p_limit or item.max_jobs)))
                # 该平台自己的页数上限（防深翻页；缺省回退全局 max_pages）
                p_pages = (item.max_pages_by_platform or {}).get(name, item.max_pages)
                p_pages = max(1, min(50, int(p_pages or item.max_pages)))
                # 已入库岗位集合：不占采集配额 —— 配额只统计「新岗位」，
                # 老岗位仍会返回（补录正文用），但不会让采集提前停止
                with session_scope() as _ks:
                    _known_ids = set(_ks.execute(
                        select(JobRow.platform_job_id).where(JobRow.platform == name)
                    ).scalars().all())
                # 该平台自己的翻页节奏（防風控方案；缺省回退全局设置）
                pd_lo, pd_hi = pace_pair(
                    item.pace_by_platform, name, "page_delay",
                    (query.page_delay_min, query.page_delay_max),
                )
                p_query = query.model_copy(update={
                    "max_jobs": p_limit,
                    "max_pages": p_pages,
                    "page_delay_min": pd_lo,
                    "page_delay_max": pd_hi,
                    "verify_body": bool((item.verify_body_by_platform or {}).get(name, False)),
                    "known_ids": _known_ids,
                })

                hub.update(detail=f"{display}：开始搜索岗位"
                                  f"（新增上限 {p_limit} 个 · 每词≤{p_pages}页 · 翻页节奏 {pd_lo:.0f}-{pd_hi:.0f}s）…")
                hub.log("INFO", f"{display}：开始搜索岗位（每词≤{p_pages} 页 · 翻页节奏 {pd_lo:.0f}-{pd_hi:.0f} 秒）")

                def _on_progress(text: str, d: str = display) -> None:
                    hub.update(detail=f"【{d}】{text}")
                    if "正在搜索" in text:      # 关键词级事件写日志；每页明细只更新横幅
                        hub.log("INFO", f"{d}：{text}")

                platform.set_progress_handler(_on_progress)
                try:
                    jobs = await platform.search_jobs(p_query)
                except (VerifyTimeout, VerifyRequired) as _verr:
                    # 安全验证：跳过该平台（保留已采部分），其余平台继续
                    partial = list(getattr(platform, "last_partial_jobs", []) or [])
                    all_jobs.extend(partial)
                    _save_now(partial)
                    reason_txt = (
                        "需要安全验证（验证码），已跳过该平台"
                        if isinstance(_verr, VerifyRequired)
                        else "等待人工验证超时，已中断"
                    )
                    note_verify_event(name, "collect", f"{reason_txt}（已采 {len(partial)} 个）")
                    by_platform[name] = {
                        "ok": False, "reason": reason_txt,
                        "collected": len(partial), "scanned": 0, "filtered": 0,
                        "capped": False, "limit": p_limit, "samples": [],
                    }
                    if isinstance(_verr, VerifyRequired):
                        # 续跑断点：记录该平台剩余（未完成）的关键词，处理验证后可一键继续
                        # 按出现次数扣减：重复关键词也能正确算出剩余
                        consumed: dict[str, int] = {}
                        for _dk in (getattr(platform, "last_done_keywords", []) or []):
                            consumed[_dk] = consumed.get(_dk, 0) + 1
                        remaining_kws = []
                        for _k in query.keywords:
                            if consumed.get(_k, 0) > 0:
                                consumed[_k] -= 1
                            else:
                                remaining_kws.append(_k)
                        if remaining_kws:
                            resume_list.append({
                                "platform": name,
                                "display": display,
                                "remaining_keywords": remaining_kws,
                                "cities": list(query.cities),
                            })
                    hub.log("WARN", f"{display}：{reason_txt}（保留已采 {len(partial)} 个）")
                    return
                except task_control.TaskCancelled:
                    raise
                except Exception as e:  # noqa: BLE001
                    # 单平台异常不拖垮整轮：记录后其余平台继续（保留已采部分）
                    partial = list(getattr(platform, "last_partial_jobs", []) or [])
                    all_jobs.extend(partial)
                    _save_now(partial)
                    by_platform[name] = {
                        "ok": False, "reason": f"平台异常：{type(e).__name__}: {e}",
                        "collected": len(partial), "scanned": 0, "filtered": 0,
                        "capped": False, "limit": p_limit, "samples": [],
                    }
                    hub.log(
                        "ERR",
                        f"{display}：采集异常（{type(e).__name__}），已跳过该平台"
                        f"（保留已采 {len(partial)} 个）",
                    )
                    return
                finally:
                    platform.set_progress_handler(None)

                scanned = int(getattr(platform, "last_scanned", 0) or 0)
                filtered = int(getattr(platform, "last_filtered", 0) or 0)
                body_dropped = int(getattr(platform, "last_body_dropped", 0) or 0)
                body_rescued = int(getattr(platform, "last_body_rescued", 0) or 0)
                last_scanned += scanned
                last_filtered += filtered
                all_jobs.extend(jobs)
                _before_saved = saved_cnt["n"]
                _save_now(jobs)   # 增量入库：该平台完成即写库
                _new_cnt = saved_cnt["n"] - _before_saved
                _seen_cnt = max(0, len(jobs) - _new_cnt)
                by_platform[name] = {
                    "ok": True, "collected": len(jobs),
                    "new": _new_cnt, "seen": _seen_cnt,
                    "scanned": scanned, "filtered": filtered,
                    "body_dropped": body_dropped,
                    "body_rescued": body_rescued,
                    "capped": _new_cnt >= p_limit,
                    "limit": p_limit,
                    "samples": [
                        {"title": j.title, "company": j.company, "salary": j.salary, "city": j.city}
                        for j in jobs[:5]
                    ],
                }
                _bd_txt = ""
                if body_dropped:
                    _bd_txt += f" · 正文弃 {body_dropped}"
                if body_rescued:
                    _bd_txt += f" · 正文救回 {body_rescued}"
                hub.log(
                    "OK",
                    f"{display}：采集完成，本轮 {len(jobs)} 个"
                    f"（新增 {_new_cnt} · 已见 {_seen_cnt} · 扫描 {scanned} · 过滤 {filtered}{_bd_txt}）",
                )
            except task_control.TaskCancelled:
                raise
            except Exception as e:  # noqa: BLE001
                by_platform.setdefault(name, {"ok": False, "reason": f"平台异常：{type(e).__name__}: {e}"})
                hub.log("ERR", f"{display}：平台任务异常（{type(e).__name__}），已跳过")

        async def _wrapped(i: int, name: str) -> None:
            task_control.raise_if_cancelled()
            await task_control.wait_if_paused()
            try:
                await _run_platform(i, name)
            finally:
                done_cnt["n"] += 1
                hub.update(
                    percent=lo + (hi - lo) * done_cnt["n"] / n,
                    stats={"collected": len(all_jobs)},
                )

        # ---- 四平台并行：每个平台独立协程，各自节奏互不阻塞 ----
        _results = await asyncio.gather(
            *[_wrapped(i, name) for i, name in enumerate(platforms)],
            return_exceptions=True,
        )
        for _r in _results:
            if isinstance(_r, BaseException) and not isinstance(_r, task_control.TaskCancelled):
                hub.log("ERR", f"平台任务意外异常：{_r!r}")
        # 先登记续跑断点（随后即使收到停止请求也不丢失），再检查取消
        for _info in resume_list:
            hub.add_resume(_info)
        if task_control.is_cancelled():
            raise task_control.TaskCancelled("任务已被手动停止")

        new_rows = saved_cnt["n"] + _persist_jobs(all_jobs)

        total = len(all_jobs)
        hub.update(stats={"collected": total})
        if is_all:
            hub.update(detail=f"采集完成：共 {total} 个（新入库 {new_rows}），准备匹配…", percent=hi)
            hub.log("OK", f"采集完成：共 {total} 个（新入库 {new_rows}），进入匹配阶段")
        else:
            hub.end(ok=True, summary=f"采集完成：共 {total} 个（新入库 {new_rows}）")

        samples = [
            {"title": j.title, "company": j.company, "salary": j.salary, "city": j.city}
            for j in all_jobs[:10]
        ]
        return {
            "ok": True,
            "collected": total,
            "new": new_rows,
            "filtered": last_filtered,
            "scanned": last_scanned,
            "capped": any(v.get("capped") for v in by_platform.values()),
            "by_platform": by_platform,
            "resume": resume_list,
            "samples": samples,
        }
    except task_control.TaskCancelled:
        # 手动停止（并行中断）：合并所有平台已采到的部分数据后保存
        seen = {(j.platform, j.platform_job_id) for j in all_jobs}
        for name in platforms:
            if name in by_platform:
                continue          # 已记账的平台（成功 / 已跳过 / 异常）无需重复
            try:
                p = get_platform(name)
            except ValueError:
                continue
            part = list(getattr(p, "last_partial_jobs", []) or [])
            for j in part:
                key = (j.platform, j.platform_job_id)
                if key not in seen:
                    all_jobs.append(j)
                    seen.add(key)
            if part:
                by_platform[name] = {
                    "ok": False, "reason": "已手动停止", "collected": len(part),
                    "scanned": 0, "filtered": 0, "capped": False, "samples": [],
                }
        new_rows = saved_cnt["n"] + _persist_jobs(all_jobs)
        hub.update(stats={"collected": len(all_jobs)})
        hub.log("WARN", f"任务已手动停止：保留已采到的 {len(all_jobs)} 个岗位（新入库 {new_rows} 个）")
        hub.end(ok=False, summary=f"已手动停止（保留已采到 {len(all_jobs)} 个 · 新入库 {new_rows} 个）")
        return {
            "ok": True, "stopped": True,
            "collected": len(all_jobs), "new": new_rows,
            "filtered": last_filtered, "scanned": last_scanned,
            "capped": False, "by_platform": by_platform, "samples": [],
            "resume": resume_list,
        }
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        hub.end(ok=False, summary=f"采集失败：{e}")
        raise


@router.get("/api/jobs")
def list_jobs(limit: int = 100) -> list[dict]:
    """最近的采集岗位（倒序）。"""
    init_db()
    with session_scope() as s:
        rows = s.execute(
            select(JobRow).order_by(JobRow.id.desc()).limit(min(max(limit, 1), 500))
        ).scalars().all()
        out = []
        for r in rows:
            try:
                ex = json.loads(r.extra) if r.extra else {}
            except ValueError:
                ex = {}
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
                "collected_at": r.collected_at,
                "hr_active_desc": ex.get("hr_active_desc"),
                "hr_active_days": ex.get("hr_active_days"),
            })
        return out
