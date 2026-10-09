"""平台列表与登录相关 API。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

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
