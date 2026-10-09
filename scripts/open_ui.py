"""启动辅助：在系统专用 Edge 窗口中打开本项目的前端界面（start.bat 调用）。

- 等待后端就绪（轮询本机端口，不发平台请求）
- 专用窗口未开 → 启动（界面页为其标签）；已开 → 复用/切换到界面标签
- 全部通过调试端口 HTTP 完成，不建立 CDP 会话——不给平台页留下自动化痕迹

加固（2026-10-09，v1.0.5）：
- 全程日志落盘 data/startup.log（start.bat 以无窗口方式启动本脚本，print 不可见）
- 任一环节失败：弹系统消息框（ctypes 直调，零外部依赖）+ 用默认浏览器兜底打开界面
"""
from __future__ import annotations

import asyncio
import ctypes
import os
import socket
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

LOG_PATH = PROJECT_ROOT / "data" / "startup.log"


def _init_log() -> None:
    """把 stdout/stderr 重定向到日志文件（无控制台启动时 print 不可见）。"""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        f = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
        sys.stdout = f
        sys.stderr = f
    except OSError:
        pass


def _log(msg: str) -> None:
    try:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)
    except Exception:  # noqa: BLE001
        pass


def _message_box(text: str, title: str = "全自动投递简历系统") -> None:
    """系统消息框（ctypes 直调，不依赖 PowerShell / 第三方组件；失败静默）。"""
    try:
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)  # MB_ICONINFORMATION
    except Exception:  # noqa: BLE001
        pass


def _open_default_browser(url: str) -> bool:
    """用系统默认浏览器打开界面（兜底：即使专用 Edge 起不来，用户也能看到界面）。"""
    try:
        os.startfile(url)  # noqa: S606  # Windows 专用
        return True
    except Exception:  # noqa: BLE001
        return False


def _server_ready(timeout_s: float = 150.0) -> bool:
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


async def _open_ui() -> tuple[bool, str]:
    """冷启动 / 复用专用 Edge 并打开界面。返回 (成功?, 说明)。"""
    try:
        ok, msg = await browser.ensure_running_async(browser.SYSTEM_KEY, browser.UI_URL)
    except FileNotFoundError as e:
        return False, f"找不到 Microsoft Edge：{e}"
    except Exception as e:  # noqa: BLE001
        return False, f"启动 Edge 失败：{type(e).__name__}: {e}"
    if not ok:
        return False, f"Edge 调试端口未就绪：{msg}"

    t = None
    for _ in range(12):  # 等界面标签出现（冷启动需要几秒）
        t = _find_ui()
        if t is not None:
            break
        await asyncio.sleep(0.5)
    if t is None:  # 窗口在但没有界面标签 → 原生补开
        browser.forward_open(browser.SYSTEM_KEY, browser.UI_URL)
        for _ in range(12):
            t = _find_ui()
            if t is not None:
                break
            await asyncio.sleep(0.5)

    if t is not None:
        browser.activate_target(browser.SYSTEM_KEY, t.get("id", ""))
        return True, "界面已在专用 Edge 窗口中打开"
    return False, "Edge 已启动，但界面标签未出现"


def main() -> None:
    _log("=== open_ui 启动（start.bat 调用） ===")
    try:
        if not _server_ready():
            _log("后端未就绪（等待超时），放弃打开界面")
            _message_box(
                "后端服务启动超时。\n\n"
                "请查看 data\\server_console.log 与 data\\startup.log 中的错误信息。\n"
                "常见原因：8000 端口被其他程序占用、杀毒软件拦截。"
            )
            return
        ok, msg = asyncio.run(_open_ui())
    except Exception:  # noqa: BLE001
        _log("open_ui 异常：\n" + traceback.format_exc())
        _open_default_browser(browser.UI_URL)
        _message_box(
            "打开专用 Edge 窗口时出错（详情见 data\\startup.log）。\n\n"
            "已尝试用默认浏览器打开界面作为后备。"
        )
        return

    _log(msg)
    if ok:
        return
    # 失败：默认浏览器兜底 + 消息框说明（pythonw 无控制台，必须可见）
    _log("尝试用默认浏览器兜底打开界面 …")
    fallback = _open_default_browser(browser.UI_URL)
    if fallback:
        _message_box(
            f"{msg}\n\n"
            "已用默认浏览器打开界面作为后备；\n"
            "登录/自动化等功能需要 Microsoft Edge（详情见 data\\startup.log）。"
        )
    else:
        _message_box(
            f"{msg}\n\n请手动用浏览器访问 http://127.0.0.1:8000（详情见 data\\startup.log）。"
        )


if __name__ == "__main__":
    # 先初始化日志（无控制台时所有输出落盘），再导入重型依赖；导入失败也要可见
    _init_log()
    try:
        import backend.services.browser as browser  # noqa: E402
    except Exception:  # noqa: BLE001
        _log("导入 backend.services.browser 失败：\n" + traceback.format_exc())
        _message_box(
            "程序组件加载失败（详情见 data\\startup.log）。\n"
            "请重新完整解压安装包（勿从压缩包内直接运行）后再试。"
        )
        raise SystemExit(1)
    globals()["browser"] = browser
    main()
