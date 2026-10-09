"""全流程编排 API：一键「采集 → 匹配 → 投递」改为服务端自驱执行。

- POST /api/pipeline/run   提交任务后立即返回；三个阶段在服务端后台依次执行。

为什么放到服务端：
此前三个阶段由前端页面接力调用（页面卡住 / 刷新 / 关闭都会让流程断在中间）。
现在提交一次即可 —— 浏览器挂起、刷新、关闭都不影响流程推进；
「停止 / 暂停 / 继续」仍与单阶段任务完全一致（协作式取消 + 任务状态中心）。

锁的约定：提交时即占用 collect_lock 直至整个流程结束，
任何其他任务入口（含单阶段入口）在此期间一律 409。
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.api.apply import ApplyIn, _apply_impl
from backend.api.collect import CollectIn, _collect_impl
from backend.api.match import MatchIn, _match_body
from backend.core import task_control
from backend.core.task_locks import BUSY_DETAIL, apply_lock, collect_lock, match_lock
from backend.db.database import init_db
from backend.services import task_hub

router = APIRouter(tags=["pipeline"])
logger = logging.getLogger(__name__)

_VALID_STAGES = ("collect", "match", "apply")

# 后台编排任务句柄（防 GC；任务结束时自动清理）
_tasks: set[asyncio.Task] = set()


class PipelineIn(BaseModel):
    """一键全流程请求体：阶段列表 + 各阶段配置（与单阶段入口同构）。"""

    stages: list[str] = Field(default_factory=lambda: list(_VALID_STAGES))
    collect: CollectIn | None = None
    match: MatchIn | None = None
    apply: ApplyIn | None = None


@router.post("/api/pipeline/run")
async def run_pipeline(item: PipelineIn) -> dict:
    """启动服务端自驱的全流程任务：立即返回，后台依次执行各阶段。

    - 页面刷新 / 关闭 / 浏览器挂起都不再影响流程（阶段接力由服务端推进）；
    - 「停止 / 暂停 / 继续」与单阶段任务一致（协作式取消）；
    - 进度仍通过 /api/task/status 轮询展示。
    """
    stages = [s for s in _VALID_STAGES if s in item.stages]
    if not stages:
        raise HTTPException(status_code=400, detail="没有可执行的任务阶段")
    if "collect" in stages and item.collect is None:
        raise HTTPException(status_code=400, detail="缺少采集配置")
    if "match" in stages and item.match is None:
        raise HTTPException(status_code=400, detail="缺少匹配配置")
    if "apply" in stages and item.apply is None:
        raise HTTPException(status_code=400, detail="缺少投递配置")
    if collect_lock.locked() or apply_lock.locked() or match_lock.locked():
        raise HTTPException(status_code=409, detail=BUSY_DETAIL)

    # 启动即占锁：编排期间任何其他任务入口一律 409，避免任务中枢状态互相覆盖
    await collect_lock.acquire()
    try:
        task = asyncio.create_task(_pipeline_worker(item, stages))
    except BaseException:
        collect_lock.release()
        raise
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"ok": True, "started": True, "stages": stages}


async def _pipeline_worker(item: PipelineIn, stages: list[str]) -> None:
    """后台编排：依次执行 collect → match → apply；任一环节失败 / 被停止即终止后续。"""
    hub = task_hub.hub
    is_all = len(stages) > 1
    flow = "all" if is_all else stages[0]
    # 统一注入流程标记（单阶段入口保持原语义；全流程各环节共享同一任务上下文）
    if item.collect is not None:
        item.collect.flow = flow
    if item.match is not None:
        item.match.flow = flow
    if item.apply is not None:
        item.apply.flow = flow

    try:
        init_db()

        # ---- 采集 ----
        if "collect" in stages:
            try:
                res = await _collect_impl(item.collect)
            except task_control.TaskCancelled:
                _end_if_alive(hub, "已手动停止（采集环节已中断，保留已采数据）")
                return
            except Exception as e:  # noqa: BLE001
                logger.exception("全流程任务：采集环节异常")
                _end_if_alive(hub, f"任务异常（采集环节）：{type(e).__name__}: {e}")
                return
            if isinstance(res, dict) and res.get("stopped"):
                return
            if await _gap_check(hub):
                return

        # ---- 匹配（同步打分丢线程池，避免阻塞事件循环） ----
        if "match" in stages:
            try:
                res = await asyncio.to_thread(_match_body, item.match)
            except task_control.TaskCancelled:
                _end_if_alive(hub, "已手动停止（匹配环节已中断）")
                return
            except Exception as e:  # noqa: BLE001
                logger.exception("全流程任务：匹配环节异常")
                _end_if_alive(hub, f"任务异常（匹配环节）：{type(e).__name__}: {e}")
                return
            if isinstance(res, dict) and res.get("stopped"):
                return
            if await _gap_check(hub):
                return

        # ---- 投递 ----
        if "apply" in stages:
            try:
                res = await _apply_impl(item.apply)
            except task_control.TaskCancelled:
                _end_if_alive(hub, "已手动停止（投递环节已中断，已投数据保留）")
                return
            except Exception as e:  # noqa: BLE001
                logger.exception("全流程任务：投递环节异常")
                _end_if_alive(hub, f"任务异常（投递环节）：{type(e).__name__}: {e}")
                return
            if isinstance(res, dict) and res.get("stopped"):
                return

        # 全部阶段执行完毕：兜底收尾（含 apply 的正常路径已在 _apply_impl 内收尾；
        # 未包含 apply 的组合在这里收尾，避免任务状态中心滞留 active）
        if hub.state and hub.state.get("active"):
            stats = hub.state.get("stats") or {}
            hub.end(
                ok=True,
                summary=f"任务完成：采集 {stats.get('collected', 0)} 个"
                        f" · 匹配通过 {stats.get('matched', 0)} 个",
            )
    finally:
        collect_lock.release()


def _end_if_alive(hub: task_hub.TaskHub, summary: str) -> None:
    """兜底收尾：阶段异常时任务状态中心可能仍处于 active，统一收敛。"""
    if hub.state and hub.state.get("active"):
        hub.end(ok=False, summary=summary)


async def _gap_check(hub: task_hub.TaskHub) -> bool:
    """阶段之间的停顿点：处理暂停等待 + 停止请求；返回 True 表示应终止后续阶段。"""
    await task_control.wait_if_paused()
    if task_control.is_cancelled():
        _end_if_alive(hub, "已手动停止（流程中断，已完成的数据保留）")
        return True
    return False
