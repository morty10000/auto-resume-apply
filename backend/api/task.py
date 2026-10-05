"""任务实时状态 API。

- GET /api/task/status   当前任务的阶段 / 动作 / 统计 / 事件日志 快照

供前端监控页轮询：运行中每 1.5 秒拉一次；页面刷新 / 重开后拉一次即可恢复
「正在做什么、做到哪一步」，并与服务端任务锁保持一致，避免重复启动困惑。
"""
from __future__ import annotations

from fastapi import APIRouter

from backend.core import task_control
from backend.core.task_locks import any_busy, apply_lock, collect_lock
from backend.services import task_hub
from backend.services import verify as verify_watch

router = APIRouter(tags=["task"])


@router.get("/api/task/status")
def task_status() -> dict:
    """当前任务状态快照 + 服务端任务锁的真实占用情况。"""
    task_hub.hub.sweep_stale()
    snap = task_hub.hub.snapshot()
    snap["locks"] = {
        "collect": collect_lock.locked(),
        "apply": apply_lock.locked(),
    }
    # 状态中心与实际锁不一致时，以锁为准提示「有任务在运行」（含匹配任务）
    snap["busy"] = bool(snap.get("active") or any_busy())
    snap["paused"] = task_control.is_paused()
    return snap


@router.post("/api/task/stop")
async def task_stop() -> dict:
    """请求停止当前任务（协作式：在下一个检查点安全退出，已处理和已采集的数据保留）。"""
    task_control.request()
    task_control.clear_pause()
    task_hub.hub.log("WARN", "收到停止请求：当前操作完成后立即停止（已处理的数据会保留）")
    # 无实际任务在跑但状态中心残留 active（如全流程半途中断）→ 直接复位，避免界面悬置
    hub = task_hub.hub
    if hub.state and hub.state.get("active") and not any_busy():
        hub.end(ok=False, summary="已手动停止（当时无进行中的任务，状态已复位）")
    return {"ok": True, "message": "停止请求已发送"}


@router.post("/api/task/pause")
async def task_pause() -> dict:
    """请求暂停当前任务：在下一个停顿点冻结（不再向平台发出操作），点「继续」恢复。"""
    task_control.request_pause()
    task_hub.hub.log("WARN", "收到暂停请求：当前操作完成后冻结（点「继续」恢复）")
    return {"ok": True, "message": "暂停请求已发送"}


@router.post("/api/task/resume")
async def task_resume() -> dict:
    """恢复已暂停的任务。"""
    task_control.clear_pause()
    task_hub.hub.log("INFO", "收到继续请求：任务恢复运行")
    return {"ok": True, "message": "已恢复运行"}


@router.post("/api/task/verify-resolved")
def verify_resolved() -> dict:
    """用户在界面确认「验证已处理」：立即放行正在等待安全验证的流程。"""
    verify_watch.mark_resolved()
    task_hub.hub.log("INFO", "收到人工确认：验证已处理，任务继续")
    return {"ok": True}
