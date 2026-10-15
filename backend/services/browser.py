"""浏览器管理（全系统仅使用 Edge）。

- 每个平台/工具一个独立 Edge profile（data/browser_data/edge_{key}）+ 本地调试端口
- 登录窗口 = 真实 Edge（用户手动扫码）；自动化 = 通过 CDP 挂接同一实例（登录态天然共享）
- CDP 连接只在执行操作期间保持，用完立即断开（减少被风控识别的窗口期）
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlparse

import websockets

from playwright.async_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    async_playwright,
)

from backend.core.paths import BROWSER_DATA_DIR

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
]


def _edge_from_registry() -> str | None:
    """从注册表 App Paths 查 Edge（覆盖非标准安装位置 / 被精简的系统盘布局）。"""
    try:
        import winreg  # Windows 专用；非 Windows 环境直接跳过
    except ImportError:
        return None
    keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
    ]
    for hive, path in keys:
        try:
            with winreg.OpenKey(hive, path) as k:
                val, _ = winreg.QueryValueEx(k, None)
        except OSError:
            continue
        if val and Path(str(val)).exists():
            return str(val)
    return None

# 系统专用窗口：所有平台共用同一个 Edge 实例，平台登录页为其标签页
SYSTEM_KEY = "system"

def _api_port() -> int:
    """本地服务端口；测试 / 多实例场景可通过环境变量覆盖。"""
    raw = os.environ.get("AUTO_RESUME_API_PORT", "8000").strip() or "8000"
    try:
        port = int(raw)
    except ValueError:
        port = 8000
    return min(max(port, 1), 65535)


API_PORT = _api_port()

# 项目前端界面地址（start.bat 启动的本地服务；界面页与登录页同窗口承载）
UI_URL = f"http://127.0.0.1:{API_PORT}"
_UI_NETLOC = urlparse(UI_URL).netloc                      # 127.0.0.1:8000
_UI_LOCAL = f"localhost:{urlparse(UI_URL).port or 80}"    # localhost:8000

# key → 本地调试端口（固定映射，避免冲突）
EDGE_PORTS: dict[str, int] = {
    SYSTEM_KEY: 9223,
    "devshot": 9391,
    "_selftest": 9392,
}
_FALLBACK_PORT = 9390


def find_edge() -> str:
    """定位 msedge.exe：常见安装路径 → 注册表 → PATH，全部落空才抛错。"""
    for p in EDGE_CANDIDATES:
        if p and Path(p).exists():
            return p
    reg = _edge_from_registry()
    if reg:
        return reg
    which = shutil.which("msedge")
    if which:
        return which
    raise FileNotFoundError(
        "找不到 Microsoft Edge（msedge.exe）。"
        "请确认系统已安装 Edge（https://www.microsoft.com/edge 可免费安装）后重试。"
    )


def port_for(key: str) -> int:
    return EDGE_PORTS.get(key, _FALLBACK_PORT)


def is_running(key: str) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port_for(key))) == 0


def _spawn(key: str, urls: list[str]) -> None:
    """启动该 key 的 Edge 实例（独立 profile + 调试端口；多个 URL = 同一窗口的多个标签页）。"""
    edge = find_edge()
    profile = BROWSER_DATA_DIR / f"edge_{key}"
    profile.mkdir(parents=True, exist_ok=True)
    args = [
        edge,
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={port_for(key)}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1440,900",
        # 防节流 / 防「窗口遮挡误判」（远程桌面、虚拟机、多屏环境下
        # Chromium 会把可见窗口误判为被遮挡 → 页面冻结、滚轮失效、定时器延迟）：
        "--disable-features=CalculateNativeWinOcclusion",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        *urls,
    ]
    subprocess.Popen(args, close_fds=True)


def _as_urls(url) -> list[str]:
    """把 str / list[str] / None 统一成 URL 列表。"""
    if not url:
        return []
    if isinstance(url, str):
        return [url]
    return [u for u in url if u]


def ensure_running(key: str, url=None) -> tuple[bool, str]:
    """确保该 key 的 Edge 实例在运行（只负责启动，不等待端口就绪）。"""
    if is_running(key):
        return True, "Edge 实例已在运行"
    _spawn(key, _as_urls(url))
    return True, "已启动"


async def ensure_running_async(
    key: str,
    url=None,
    timeout_s: float = 25.0,
    attempts: int = 2,
) -> tuple[bool, str]:
    """确保 Edge 在运行且调试端口已就绪；启动失败自动重试，避免「假成功」。"""
    if is_running(key):
        return True, "Edge 实例已在运行"
    last = "未知错误"
    for i in range(attempts):
        _spawn(key, _as_urls(url))
        per = timeout_s / attempts
        waited = 0.0
        while waited < per:
            if is_running(key):
                return True, "已启动"
            await asyncio.sleep(0.3)
            waited += 0.3
        last = f"等待调试端口就绪超时（第 {i + 1} 次尝试）"
        await asyncio.sleep(1.5)
    return False, last


def list_targets(key: str) -> list[dict]:
    """通过调试端口 HTTP 列出所有标签页（不建立 CDP 会话，页面无感知）。"""
    if not is_running(key):
        return []
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port_for(key)}/json/list", timeout=5
        ) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def activate_target(key: str, target_id: str) -> bool:
    """请求浏览器把对应标签页/窗口切到前台（HTTP，无 CDP 会话）。"""
    if not target_id:
        return False
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port_for(key)}/json/activate/{target_id}", timeout=5
        ) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def forward_open(key: str, url: str) -> bool:
    """单实例转发：在已有窗口里像「+」一样新开一个原生标签页（无自动化痕迹）。

    若实例已不在运行：改为「带调试端口」启动（否则新实例无法被 CDP 连接），
    并同时带上界面页标签，保持窗口结构一致。
    """
    try:
        # 说明：登录场景需要「可见」标签（用户要手动扫码），故不加前台守卫；
        # 但兜底开页时若窗口不在前台，仍尽量保留用户当前焦点。
        if is_running(key):
            edge = find_edge()
            profile = BROWSER_DATA_DIR / f"edge_{key}"
            with _foreground_guard():
                subprocess.Popen([edge, f"--user-data-dir={profile}", url], close_fds=True)
            return True
        _spawn(key, [UI_URL, url])
        return True
    except Exception:  # noqa: BLE001
        return False


async def open_background_tab(key: str, url: str, timeout_s: float = 15.0) -> bool:
    """在后台新建原生标签页，不抢当前页面焦点（不建立页面级 CDP 会话）。

    采集 / 正文 / 投递页统一走这里，用户前台可一直停留在项目监控页；
    登录与验证码仍由各自逻辑显式切到前台。
    """
    try:
        if not is_running(key):
            ok, _ = await ensure_running_async(key, UI_URL)
            if not ok:
                return False
        ws_url = _browser_ws_url(key)
        if not ws_url:
            return False
        with _foreground_guard():
            async with websockets.connect(ws_url, max_size=16 * 1024 * 1024) as ws:
                await ws.send(json.dumps({
                    "id": 1,
                    "method": "Target.createTarget",
                    "params": {"url": url, "background": True, "forTab": True},
                }))
                deadline = time.time() + timeout_s
                while time.time() < deadline:
                    wait_s = max(0.5, deadline - time.time())
                    try:
                        reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=wait_s))
                    except asyncio.TimeoutError:
                        break
                    if reply.get("id") != 1:
                        continue
                    if reply.get("error"):
                        return False
                    return bool((reply.get("result") or {}).get("targetId"))
    except Exception:  # noqa: BLE001
        return False
    return False


# ---------------------------------------------------------------- 前台守卫（Windows）
#
# 背景：Chromium 新建 target 时会把浏览器窗口顶到前台。若用户此时在看别的应用，
# 就会被打断。守卫会在操作前后比对前台窗口，被抢走则立刻还回去。
try:
    import ctypes

    _u32 = ctypes.windll.user32

    def _fg_hwnd() -> int:
        try:
            return int(_u32.GetForegroundWindow())
        except Exception:  # noqa: BLE001
            return 0

    def _restore_fg(hwnd: int, tries: int = 6) -> None:
        """把前台窗口还给 hwnd（跨进程安全版本，带重试）。

        Windows 前台锁规则下，后台进程直接 SetForegroundWindow 通常会被短暂忽略；
        且 Chromium 新建标签会把窗口顶到前台。这里用 AttachThreadInput 挂接线程、
        必要时用一次 0 键事件「吃掉」前台锁，并在 50ms 间隔内重试若干次，确保用户
        视角不被后台建页打断。
        """
        if not hwnd:
            return
        try:
            if not (_u32.IsWindow(hwnd) and _u32.IsWindowVisible(hwnd)):
                return
            if _u32.GetForegroundWindow() == hwnd:
                return
            cur_tid = ctypes.c_ulong()
            tgt_tid = ctypes.c_ulong()
            _u32.GetWindowThreadProcessId(_u32.GetForegroundWindow(), ctypes.byref(cur_tid))
            _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(tgt_tid))
            attached = False
            if cur_tid.value and tgt_tid.value and cur_tid.value != tgt_tid.value:
                try:
                    attached = bool(_u32.AttachThreadInput(cur_tid.value, tgt_tid.value, True))
                except Exception:  # noqa: BLE001
                    attached = False
            try:
                for _ in range(max(1, tries)):
                    _u32.ShowWindow(hwnd, 9)            # SW_RESTORE：确保窗口未最小化
                    _u32.SetForegroundWindow(hwnd)
                    if _u32.GetForegroundWindow() == hwnd:
                        break
                    # 释放前台锁：发一个空键事件，让系统认为本线程刚有输入
                    try:
                        _u32.keybd_event(0, 0, 0, 0)
                        _u32.keybd_event(0, 0, 0x2, 0)
                    except Exception:  # noqa: BLE001
                        pass
                    _u32.SetForegroundWindow(hwnd)
                    if _u32.GetForegroundWindow() == hwnd:
                        break
                    _u32.Sleep(50)
            finally:
                if attached:
                    try:
                        _u32.AttachThreadInput(cur_tid.value, tgt_tid.value, False)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            pass

except ImportError:  # 非 Windows：不做焦点处理
    def _fg_hwnd() -> int:
        return 0

    def _restore_fg(hwnd: int) -> None:
        return


class _foreground_guard:
    """操作期间保持原前台窗口不变（仅 Windows 生效）。"""

    def __enter__(self):
        self.prev = _fg_hwnd()
        return self

    def __exit__(self, *exc):
        try:
            if self.prev and _fg_hwnd() != self.prev:
                _restore_fg(self.prev)
        except Exception:  # noqa: BLE001
            pass
        return False


# ---------------------------------------------------------------- 机器人专属标签页
#
# 需求：采集 / 正文 / 投递一律在「后台专属标签页」里完成，用户的可见标签页
# （界面页、手动打开的岗位页、登录页）绝不被导航、绝不被抢焦点。
#
# 实现：后台新建的标签页记到内存表 _owned_tabs（{target_id: domain}），
# 并对页面打一个 window.__autoResumeBot 标记 —— 进程重启后内存表丢失时，
# 仍可凭页面内标记认领，不会退化成「随便找一个同域名标签」而导走用户页面。
_owned_tabs: dict[str, str] = {}          # target_id -> 域名片段
_OWN_MARK_JS = "(() => { try { window.name = window.name || 'auto-resume-bot';"
_OWN_MARK_JS += " window.__autoResumeBot = 1; return 'ok'; } catch (e) { return 'err'; } })()"
_OWN_PROBE_JS = "(() => { try { return (window.__autoResumeBot === 1 || window.name === 'auto-resume-bot') ? 'bot' : 'user'; } catch (e) { return 'err'; } })()"


def remember_tab(target_id: str, domain: str) -> None:
    """登记一个由本项目创建、可安全复用的后台标签页。"""
    if target_id:
        _owned_tabs[target_id] = domain or ""


def forget_tab(target_id: str) -> None:
    if target_id:
        _owned_tabs.pop(target_id, None)


def is_owned(target_id: str) -> bool:
    return target_id in _owned_tabs


async def mark_owned(key: str, target_id: str) -> None:
    """给标签页打上「本项目专属」标记（重启后仍可凭标记认领）。"""
    await raw_evaluate(key, target_id, _OWN_MARK_JS, timeout_s=8)


async def open_owned_tab(key: str, domain: str, url: str, timeout_s: float = 15.0) -> dict | None:
    """后台新建一个「本项目专属」标签页（不抢焦点），返回该 target。

    与 open_background_tab 的区别：这里会记住 target_id 并打页面标记，
    供后续 find_owned_tab 精确复用，避免导走用户自己打开的页面；
    并且全程有「前台守卫」——新建标签若把 Edge 窗口顶到前台，立刻把焦点还回去。
    """
    try:
        if not is_running(key):
            ok, _ = await ensure_running_async(key, UI_URL)
            if not ok:
                return None
        ws_url = _browser_ws_url(key)
        if not ws_url:
            return None
        target_id = ""
        with _foreground_guard():
            async with websockets.connect(ws_url, max_size=16 * 1024 * 1024) as ws:
                await ws.send(json.dumps({
                    "id": 1,
                    "method": "Target.createTarget",
                    # background=True：新标签不抢占当前可见标签页，用户视角无任何切换
                    "params": {"url": url, "background": True, "forTab": True},
                }))
                deadline = time.time() + timeout_s
                while time.time() < deadline:
                    wait_s = max(0.5, deadline - time.time())
                    try:
                        reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=wait_s))
                    except asyncio.TimeoutError:
                        break
                    if reply.get("id") != 1:
                        continue
                    if reply.get("error"):
                        return None
                    target_id = (reply.get("result") or {}).get("targetId") or ""
                    break
        if not target_id:
            return None
        # 新标签可能稍晚才出现在 /json/list：边等边轮询，避免「建成功却找不到」
        tab = None
        for _ in range(int(timeout_s / 0.5) + 1):
            tab = find_target_by_id(key, target_id)
            if tab is not None:
                break
            await asyncio.sleep(0.5)
        if tab is None:
            # 建了却查不到：直接关掉，防止留下没人认领的孤儿标签越积越多
            close_target(key, target_id)
            return None
        await asyncio.sleep(1.0)          # 等页面上下文建立，再打标记
        await mark_owned(key, target_id)
        remember_tab(target_id, domain)
        return find_target_by_id(key, target_id)
    except Exception:  # noqa: BLE001
        return None


def find_target_by_id(key: str, target_id: str) -> dict | None:
    """按 id 取标签页信息（含最新 URL）。"""
    if not target_id:
        return None
    for t in list_targets(key):
        if t.get("id") == target_id:
            return t
    return None


async def find_owned_tab(key: str, domain: str) -> dict | None:
    """找一个本项目专属的、匹配 domain 的后台标签页。

    优先级：① 内存已登记的（最快）→ ② 页面内带 bot 标记的（进程重启后仍可认领）。
    都不存在则返回 None（调用方会新建一个专属标签页，绝不占用用户标签）。
    """
    for tid, dom in list(_owned_tabs.items()):
        if domain and dom and domain not in dom and dom not in domain:
            continue
        t = find_target_by_id(key, tid)
        if t is not None and t.get("type") == "page":
            return t
        forget_tab(tid)                    # 标签已关闭，清掉登记
    for t in list_targets(key):
        if t.get("type") != "page":
            continue
        u = t.get("url") or ""
        if domain and domain not in u:
            continue
        if is_ui_url(u):
            continue
        if await raw_evaluate(key, t.get("id", ""), _OWN_PROBE_JS, timeout_s=6) == "bot":
            remember_tab(t.get("id", ""), domain)
            return t
    return None


async def ensure_owned_tab(key: str, domain: str, first_url: str, timeout_s: float = 20.0) -> dict | None:
    """取一个本项目专属的后台标签页：有就复用，没有才新建（全程不切前台）。

    建页偶发失败（Edge 调试端点瞬时繁忙）时重试 2 次；仍失败则退回原生转发，
    并把新开的标签也登记为专属（避免下次再去动用户自己的页面）。
    """
    with _foreground_guard():
        tab = await find_owned_tab(key, domain)
        if tab is not None:
            return tab
    for attempt in range(3):
        # 先查再建：建页偶发「成功但列表未刷新」时，重查能直接复用，
        # 避免反复重试开出好几个同域标签（实测猎聘/智联曾一次开 4 个）。
        with _foreground_guard():
            tab = await find_owned_tab(key, domain)
            if tab is not None:
                return tab
            tab = await open_owned_tab(key, domain, first_url, timeout_s=15.0)
        if tab is not None:
            return tab
        if attempt < 2:
            await asyncio.sleep(1.2)
    with _foreground_guard():
        return await _fallback_owned_tab(key, domain, first_url, timeout_s)
    return None


async def _fallback_owned_tab(key: str, domain: str, first_url: str, timeout_s: float) -> dict | None:
    """[兜底] CDP 建页不可用时的认领流程（调用方需自行加前台守卫）。"""
    if open_background_tab_sync_fallback(key, first_url):
        for _ in range(int(timeout_s / 0.8)):
            await asyncio.sleep(0.8)
            for t in list_targets(key):
                if t.get("type") != "page" or domain not in (t.get("url") or ""):
                    continue
                # 只认领「刚刚由本项目打开」的标签：用页面标记校验，
                # 绝不能把用户自己开的同域名页面据为己有（会导走他的页面）。
                if is_owned(t.get("id", "")):
                    return t
                await mark_owned(key, t.get("id", ""))
                if await raw_evaluate(key, t.get("id", ""), _OWN_PROBE_JS, timeout_s=6) == "bot":
                    remember_tab(t.get("id", ""), domain)
                    return t
    return None


async def ensure_apply_tab_background(key: str, domain: str, url: str) -> dict | None:
    """投递专用：取一个专属后台标签，并确保项目看板仍在用户眼前。

    步骤：① 取/建专属后台标签 → ② 若该标签此刻正被用户看到，先把界面页顶回前台 →
    ③ 返回标签。这样后续对它的导航与点击都发生在后台，用户视角一直停在看板。
    """
    with _foreground_guard():
        tab = await ensure_owned_tab(key, domain, url)
        if tab is None:
            return None
        ensure_tab_background(key, tab.get("id", ""))
        return tab


def open_background_tab_sync_fallback(key: str, url: str) -> bool:
    """[兜底] 同步版后台开标签（仅在 CDP 建页不可用时使用）。"""
    return forward_open(key, url)


def focus_ui_tab(key: str) -> bool:
    """把项目界面标签页切到前台（仅用于「运行中把用户拉回看板」）。

    只在界面页确实存在时执行；找不到界面页返回 False（不做任何焦点操作，
    避免误把某个平台页顶到前台）。
    """
    for t in list_targets(key):
        if t.get("type") == "page" and is_ui_url(t.get("url") or ""):
            return activate_target(key, t.get("id", ""))
    return False


def ensure_tab_background(key: str, target_id: str) -> bool:
    """确保指定标签页处于后台（不被用户看见）：若它当前是活动标签，先把界面页顶到前台。

    这样后续对该标签的导航/点击不会出现在用户眼前——用户视角始终停在项目看板。
    """
    if not target_id:
        return False
    active = None
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port_for(key)}/json/list", timeout=5
        ) as r:
            targets = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return False
    for t in targets or []:
        if t.get("type") == "page" and not is_ui_url(t.get("url") or ""):
            active = t
            break
    if active is not None and active.get("id") == target_id:
        # 守卫：切标签会把 Edge 窗口顶到系统前台，操作结束后把焦点还给用户原本的应用
        with _foreground_guard():
            return focus_ui_tab(key)
    return True


def is_ui_url(url: str) -> bool:
    """判断 URL 是否为本项目前端界面。"""
    u = url or ""
    return _UI_NETLOC in u or _UI_LOCAL in u


def _browser_ws_url(key: str) -> str | None:
    """取该实例的浏览器级 WebSocket 调试地址。"""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port_for(key)}/json/version", timeout=5
        ) as r:
            return json.loads(r.read().decode("utf-8")).get("webSocketDebuggerUrl")
    except Exception:  # noqa: BLE001
        return None


async def read_cookies(key: str, domain_filter: str | None = None) -> list[dict] | None:
    """浏览器端点直读 cookie（原始 CDP，不挂接任何页面，平台页面无感）。

    与 Playwright 的 snapshot 不同：不建立页面级 CDP 会话，不会干扰 Boss 等
    带反自动化检测的页面。失败返回 None（调用方保持等待即可）。
    """
    ws_url = _browser_ws_url(key)
    if not ws_url:
        return None
    try:
        async with websockets.connect(ws_url, max_size=16 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
            for _ in range(20):
                reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
                if reply.get("id") == 1:
                    cookies = (reply.get("result") or {}).get("cookies") or []
                    if domain_filter:
                        cookies = [
                            c
                            for c in cookies
                            if domain_filter in (c.get("domain") or "")
                        ]
                    return cookies
    except Exception:  # noqa: BLE001
        return None
    return None


async def delete_cookies(key: str, items: list[dict]) -> int:
    """按 (name, domain, path) 精确删除 cookie。

    用于「重新登录」前置：清掉可能残留的旧登录 cookie，
    让登录等待逻辑以「新出现的登录 cookie」为准，避免旧 cookie 造成假登录。

    实现注记（2026-10-09 实测）：浏览器级 CDP 没有 cookie 删除命令
    （Storage.deleteCookies 不存在；Network.deleteCookies 只在页面会话可用）。
    因此通过「页面 target 的独立 CDP 会话」调用 Network.deleteCookies：
    优先选界面页（127.0.0.1 本机页，无平台感知），次选任意 http 页；
    绝不挂接平台页面。返回成功删除的数量；失败返回已删除数（不抛错）。
    """
    if not items:
        return 0
    page = None
    targets = list_targets(key)
    for t in targets:
        if t.get("type") == "page" and is_ui_url(t.get("url") or ""):
            page = t
            break
    if page is None:
        for t in targets:
            if t.get("type") == "page" and (t.get("url") or "").startswith("http"):
                page = t
                break
    ws_url = (page or {}).get("webSocketDebuggerUrl")
    if not ws_url:
        return 0
    deleted = 0
    try:
        async with websockets.connect(ws_url, max_size=16 * 1024 * 1024) as ws:
            mid = 0
            for it in items:
                mid += 1
                params: dict = {"name": it.get("name") or ""}
                if it.get("domain"):
                    params["domain"] = it["domain"]
                if it.get("path") is not None:
                    params["path"] = it.get("path") or "/"
                await ws.send(json.dumps({
                    "id": mid, "method": "Network.deleteCookies", "params": params,
                }))
                for _ in range(20):
                    reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
                    if reply.get("id") == mid:
                        if not reply.get("error"):
                            deleted += 1
                        break
    except Exception:  # noqa: BLE001
        return deleted
    return deleted


def find_target(key: str, substring: str, target_type: str = "page") -> dict | None:
    """在实例中查找 URL 含 substring 的目标（标签页）。"""
    for t in list_targets(key):
        if t.get("type") == target_type and substring in (t.get("url") or ""):
            return t
    return None


async def page_network_alive(key: str, target_id: str) -> bool | None:
    """探测标签页的网络是否仍然工作（后台停留过久的标签页会「网络僵尸化」）。

    在页面内对同源静态资源发一次带超时的 fetch：
    - True：能收到响应（网络正常）
    - False：请求异常 / 评估超时（僵尸化，需要关掉重开）
    - None：无法判断（如页面正在导航中）
    """
    js = (
        "(async () => {"
        "  try {"
        "    const c = new AbortController();"
        "    const t = setTimeout(() => c.abort(), 5000);"
        "    await fetch('/favicon.ico?_=' + Date.now(), {cache: 'no-store', signal: c.signal});"
        "    clearTimeout(t);"
        "    return 'alive';"
        "  } catch (e) { return 'dead'; }"
        "})()"
    )
    raw = await raw_evaluate(key, target_id, js, await_promise=True, timeout_s=12)
    if raw is None:
        return False
    return raw == "alive"


def close_target(key: str, target_id: str) -> bool:
    """关闭一个标签页（调试端点 HTTP，无 CDP 挂接）。"""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port_for(key)}/json/close/{target_id}", timeout=5
        ):
            pass
        return True
    except Exception:  # noqa: BLE001
        return False


async def replace_tab(key: str, target_id: str, reopen_url: str) -> bool:
    """关闭僵尸标签页，并用原生转发重开一个（用于网络僵尸化的自愈）。"""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port_for(key)}/json/close/{target_id}", timeout=5
        ):
            pass
    except Exception:  # noqa: BLE001
        pass
    await asyncio.sleep(1.2)
    return forward_open(key, reopen_url)


async def replace_tab_background(key: str, domain: str, target_id: str, reopen_url: str) -> dict | None:
    """重建一个专属后台标签页：关掉僵尸标签 → 后台另开一个（前台页面保持不动）。

    旧实现 replace_tab 走 forward_open（原生转发），会新开一个「可见」标签页，
    把用户正在看的页面顶掉；这里改为后台重建，用户视角完全无感。
    """
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port_for(key)}/json/close/{target_id}", timeout=5
        ):
            pass
    except Exception:  # noqa: BLE001
        pass
    forget_tab(target_id)
    await asyncio.sleep(1.2)
    return await open_owned_tab(key, domain, reopen_url)


async def raw_evaluate(
    key: str,
    target_id: str,
    expression: str,
    *,
    await_promise: bool = False,
    timeout_s: float = 30.0,
) -> str | None:
    """原始 CDP 在指定标签页上执行 JS（不启用任何域，页面无感）。

    约定表达式返回字符串；成功返回该字符串，失败返回 None。
    """
    ws_url = None
    for t in list_targets(key):
        if t.get("id") == target_id:
            ws_url = t.get("webSocketDebuggerUrl")
            break
    if not ws_url:
        return None
    try:
        async with websockets.connect(ws_url, max_size=16 * 1024 * 1024) as ws:
            await ws.send(json.dumps({
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": expression,
                    "returnByValue": True,
                    "awaitPromise": await_promise,
                },
            }))
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout_s))
                if reply.get("id") == 1:
                    result = reply.get("result") or {}
                    if result.get("exceptionDetails"):
                        return None
                    return (result.get("result") or {}).get("value")
    except Exception:  # noqa: BLE001
        return None
    return None


async def raw_navigate(key: str, target_id: str, url: str) -> bool:
    """原始 CDP 导航指定标签页到目标 URL（等同用户在该标签页打开页面）。"""
    ws_url = None
    for t in list_targets(key):
        if t.get("id") == target_id:
            ws_url = t.get("webSocketDebuggerUrl")
            break
    if not ws_url:
        return False
    try:
        async with websockets.connect(ws_url, max_size=4 * 1024 * 1024) as ws:
            await ws.send(json.dumps({
                "id": 1,
                "method": "Page.navigate",
                "params": {"url": url},
            }))
            deadline = time.time() + 15
            while time.time() < deadline:
                reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if reply.get("id") == 1:
                    return "error" not in reply
    except Exception:  # noqa: BLE001
        return False
    return False


@asynccontextmanager
async def session(key: str, url: str | None = None) -> AsyncIterator[BrowserContext]:
    """[遗留调试辅助] 挂接 Edge 实例的通用上下文；主流程禁用（页面级挂接会触发平台反自动化检测）。"""
    ok, msg = await ensure_running_async(key, url)
    if not ok:
        raise PlaywrightError(f"Edge 实例未就绪: {msg}")
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{port_for(key)}", timeout=8000
        )
        if browser.contexts:
            ctx = browser.contexts[0]
        else:
            ctx = await browser.new_context()
        yield ctx
    finally:
        await pw.stop()


@asynccontextmanager
async def page_scope(key: str, url: str | None = None) -> AsyncIterator[Page]:
    """[遗留调试辅助] 一次操作级页面作用域（挂接→新标签→用完关闭）；主流程禁用。"""
    async with session(key) as ctx:
        page = await ctx.new_page()
        try:
            if url:
                await page.goto(url, wait_until="domcontentloaded")
            yield page
        finally:
            try:
                await page.close()
            except PlaywrightError:
                pass


def find_page(ctx: BrowserContext, needle: str) -> Page | None:
    """[遗留调试辅助] 在实例中查找 URL 含 needle 的标签页（配合会话使用）。"""
    for p in ctx.pages:
        try:
            if needle in (p.url or ""):
                return p
        except PlaywrightError:
            continue
    return None


async def snapshot(key: str, cookie_domain: str | None = None) -> dict | None:
    """[遗留调试辅助] Playwright 版 cookie/URL 快照；现行登录检测请用 read_cookies()（原始 CDP、零挂接）。"""
    if not is_running(key):
        return None
    try:
        async with session(key) as ctx:
            cookies = await ctx.cookies()
            urls = [p.url for p in ctx.pages]
    except PlaywrightError:
        return None
    if cookie_domain:
        cookies = [c for c in cookies if cookie_domain in (c.get("domain") or "")]
    return {"cookies": cookies, "urls": urls}


# ---------------------------------------------------------------- 通用工具

VERIFY_SELECTORS: tuple[str, ...] = (
    "#nc_1_wrapper", ".nc-container", ".nc_scale",       # 阿里云盾滑块（Boss 常见）
    ".geetest_holder", ".geetest_panel",                  # 极验
    "#captcha", ".captcha-container", ".verify-wrap",
    "iframe[src*='captcha']", "iframe[src*='verify']",
)

VERIFY_TEXT_NEEDLES: tuple[str, ...] = (
    "请完成安全验证",
    "拖动滑块",
    "拖动下方滑块",
    "按住滑块",
)


async def detect_verify(page: Page) -> bool:
    """[遗留] 旧版验证码检测（Playwright 版）；现行机制见 backend/services/verify.py。"""
    for sel in VERIFY_SELECTORS:
        try:
            elements = await page.query_selector_all(sel)
        except PlaywrightError:
            continue
        for el in elements:
            try:
                if await el.is_visible():
                    return True
            except PlaywrightError:
                continue
    try:
        text = await page.inner_text("body", timeout=2000)
    except PlaywrightError:
        return False
    return any(needle in text for needle in VERIFY_TEXT_NEEDLES)
