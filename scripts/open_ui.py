"""启动辅助：在系统专用 Edge 窗口中打开本项目的前端界面（start.bat 调用）。

- 等待后端就绪（轮询本机端口，不发平台请求）
- 专用窗口未开 → 启动（界面页为其标签）
- 专用窗口已开 → 复用/切换到界面标签（无则原生转发补开并切到前台）

全部通过调试端口 HTTP 完成，不建立 CDP 会话——不给平台页留下自动化痕迹。
这样「去登录」打开的登录页会始终和界面页共处同一个窗口。
"""
from __future__ import annotations

import asyncio
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.services import browser


def _server_ready(timeout_s: float = 120.0) -> bool:
    """等待后端 HTTP 端口可连接。"""
    parsed = urlparse(browser.UI_URL)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.5)
    return False


def _find_ui() -> dict | None:
    for t in browser.list_targets(browser.SYSTEM_KEY):
        if t.get("type") == "page" and browser.is_ui_url(t.get("url") or ""):
            return t
    return None


async def main() -> None:
    ok, msg = await browser.ensure_running_async(browser.SYSTEM_KEY, browser.UI_URL)
    if not ok:
        print(f"打开界面失败：{msg}")
        return

    t = None
    for _ in range(10):  # 等界面标签出现（冷启动需要几秒）
        t = _find_ui()
        if t is not None:
            break
        await asyncio.sleep(0.5)
    if t is None:  # 窗口在但没有界面标签 → 原生补开
        browser.forward_open(browser.SYSTEM_KEY, browser.UI_URL)
        for _ in range(10):
            t = _find_ui()
            if t is not None:
                break
            await asyncio.sleep(0.5)

    if t is not None:
        browser.activate_target(browser.SYSTEM_KEY, t.get("id", ""))
        print("界面已在专用 Edge 窗口中打开")
    else:
        print("打开界面失败：未找到界面标签")


if __name__ == "__main__":
    if not _server_ready():
        print("后端未就绪，跳过打开界面")
        raise SystemExit(0)
    asyncio.run(main())
