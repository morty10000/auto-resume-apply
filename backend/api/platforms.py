"""平台列表与登录相关 API。"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from backend.db.database import session_scope
from backend.db.models import VerifyEvent
from backend.services import edge_login

router = APIRouter(prefix="/api/platforms", tags=["platforms"])


def _has_adapter(name: str) -> bool:
    from backend.platforms import registry

    try:
        registry.get(name)
        return True
    except ValueError:
        return False


@router.get("")
async def list_platforms() -> list[dict]:
    """全部平台的登录状态（先用浏览器实时 cookie 校准一遍）。"""
    await edge_login.probe_login_states()
    running = edge_login.edge_running()

    # 验证情况（近 24h 触发次数 + 最近一次时间/来源；平台卡片「验证情况」展示用）
    cutoff = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    verify_map: dict[str, dict] = {}
    try:
        with session_scope() as s:
            rows = s.execute(
                select(VerifyEvent)
                .where(VerifyEvent.created_at >= cutoff)
                .order_by(VerifyEvent.id.desc())
            ).scalars().all()
        for r in rows:
            info = verify_map.setdefault(
                r.platform, {"count_24h": 0, "last_at": None, "last_source": None, "last_detail": None}
            )
            info["count_24h"] += 1
            if info["last_at"] is None:
                info["last_at"] = r.created_at
                info["last_source"] = r.source
                info["last_detail"] = r.detail
    except Exception:  # noqa: BLE001  展示功能：任何异常不阻塞平台状态返回
        verify_map = {}

    items = []
    for name, meta in edge_login.PLATFORM_LOGIN.items():
        st = edge_login.get_login_status(name)
        items.append({
            "name": name,
            "display_name": meta["display_name"],
            "has_adapter": _has_adapter(name),
            "status": st.get("status", "unknown"),
            "message": st.get("message", ""),
            "source": st.get("source", ""),
            "updated_at": st.get("updated_at"),
            "checked_at": edge_login.get_checked_at(name),
            "edge_running": running,
            "verify": verify_map.get(name) or {
                "count_24h": 0, "last_at": None, "last_source": None, "last_detail": None,
            },
        })
    return items


@router.post("/{name}/login")
async def start_login(name: str, force: bool = False) -> dict:
    """发起登录：打开 Edge 登录窗口并开始等待扫码（force=1 强制重新登录）。"""
    if name not in edge_login.PLATFORM_LOGIN:
        raise HTTPException(status_code=404, detail=f"未知平台: {name}")

    # 先实时探测，避免「其实已登录但缓存是旧状态」导致的误判
    await edge_login.probe_login_states()
    current = edge_login.get_login_status(name)
    if not force and current.get("status") == "logged_in":
        return {"status": "logged_in", "message": "已处于登录状态"}

    # 会话复核（仅非强制）：cookie 检测失败，但服务端会话可能仍有效
    # （session 型登录 cookie 跨浏览器重启丢失）→ 打开会话页看站点还认不认；
    # 仍认 → 直接恢复登录态（记指纹），不让用户白跑一趟扫码重登
    if not force:
        verified = await edge_login.verify_live_session(name)
        if verified is True:
            await edge_login.mark_session_verified(name)
            return {"status": "logged_in", "message": "会话复核通过：平台仍认登录，已恢复登录状态"}

    ok, msg = await edge_login.launch_login_window(name)
    if not ok:
        raise HTTPException(status_code=500, detail=msg)

    # 清掉可能残留的旧登录 cookie：保证后续「登录成功」判定一定来自新登录
    await edge_login.clear_login_cookies(name)

    meta = edge_login.PLATFORM_LOGIN[name]
    if meta.get("auth_hints"):
        edge_login.ensure_watcher(name)
    else:
        edge_login.set_login_status(name, "waiting", "请在 Edge 中完成登录（该平台自动检测未接入）")
    return {"status": "waiting", "message": msg}


@router.get("/{name}/login/status")
def login_status(name: str) -> dict:
    if name not in edge_login.PLATFORM_LOGIN:
        raise HTTPException(status_code=404, detail=f"未知平台: {name}")
    return edge_login.get_login_status(name)
