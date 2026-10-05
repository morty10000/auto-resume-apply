"""浏览器管理（全系统仅使用 Edge）。

- 每个平台/工具一个独立 Edge profile（data/browser_data/edge_{key}）+ 本地调试端口
- 登录窗口 = 真实 Edge（用户手动扫码）；自动化 = 通过 CDP 挂接同一实例（登录态天然共享）
- CDP 连接只在执行操作期间保持，用完立即断开（减少被风控识别的窗口期）
"""
from __future__ import annotations

import asyncio
import json
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
]

# 系统专用窗口：所有平台共用同一个 Edge 实例，平台登录页为其标签页
SYSTEM_KEY = "system"

# 项目前端界面地址（start.bat 启动的本地服务；界面页与登录页同窗口承载）
UI_URL = "http://127.0.0.1:8000"
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
    for p in EDGE_CANDIDATES:
        if Path(p).exists():
            return p
    raise FileNotFoundError("找不到 msedge.exe，请确认已安装 Microsoft Edge")


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
        if is_running(key):
            edge = find_edge()
            profile = BROWSER_DATA_DIR / f"edge_{key}"
            subprocess.Popen([edge, f"--user-data-dir={profile}", url], close_fds=True)
            return True
        _spawn(key, [UI_URL, url])
        return True
    except Exception:  # noqa: BLE001
        return False


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
