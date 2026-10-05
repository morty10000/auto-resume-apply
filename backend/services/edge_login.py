"""Edge 登录流程：打开登录窗口 / 轮询登录状态 / 保存登录态。

全系统仅使用 Edge：
- 登录窗口与自动化共用同一个 Edge 实例（独立 profile + 本地调试端口）
- 登录页标签一律用「Edge 单实例原生转发」打开（等同手动点「+」），
  绝不用自动化工具创建——Boss 等平台会检测自动化标签并让页面自我清屏
- 登录检测只读浏览器调试端点（HTTP + 原始 CDP 读 cookie）：不挂接页面、
  不加载平台页面、不产生平台请求；出现登录特征 cookie 即判定登录成功
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from backend.services import browser

logger = logging.getLogger(__name__)

PLATFORM_LOGIN: dict[str, dict] = {
    "boss": {
        "display_name": "Boss直聘",
        "login_url": "https://www.zhipin.com/web/user/?ka=header-login",
        "cookie_domain": "zhipin.com",
        # 登录后才会出现的 cookie（判定登录成功的唯一依据）
        # 注意：__zp_stoken__ 是反爬设备指纹，匿名访问也会种上，绝不能算登录特征
        "auth_hints": ("wt2", "bst", "zp_at", "geek_zp_token", "zp_token"),
    },
    "zhilian": {
        "display_name": "智联招聘",
        "login_url": "https://passport.zhaopin.com/login",
        "cookie_domain": "zhaopin.com",
        # 实测：匿名访问只有设备类 cookie（x-zp-client-id / smidV2 等）；
        # at / rt（访问令牌 + 刷新令牌）仅登录后出现 → 登录特征
        "auth_hints": ("at", "rt"),
    },
    "job51": {
        "display_name": "51job",
        "login_url": "https://login.51job.com/login.php",
        "cookie_domain": "51job.com",
        # 2026-10-02 对照实验校准：全新匿名 profile 无 _c_WBKFRo（旧「51job」cookie 已被站点弃用）；
        # _c_WBKFRo 仅登录后出现（承载登录会话）→ 登录特征
        "auth_hints": ("_c_WBKFRo",),
    },
    "liepin": {
        "display_name": "猎聘",
        "login_url": "https://www.liepin.com/login/",
        "cookie_domain": "liepin.com",
        # 实测：匿名只有 XSRF-TOKEN / __uuid / __gc_id 等会话类 cookie；
        # lt_auth（登录令牌）与 user_name（用户名）仅登录后出现 → 登录特征
        "auth_hints": ("lt_auth", "user_name"),
    },
}


# ---------------------------------------------------------------- 登录窗口

def _find_login_target(meta: dict, targets: list[dict] | None = None) -> dict | None:
    """在目标列表中查找该平台的登录页标签。"""
    if targets is None:
        targets = browser.list_targets(browser.SYSTEM_KEY)
    for t in targets:
        if t.get("type") != "page":
            continue
        u = (t.get("url") or "").lower()
        if meta["cookie_domain"] in u and ("login" in u or "/web/user/" in u):
            return t
    return None


async def launch_login_window(platform: str) -> tuple[bool, str]:
    """在系统专用 Edge 窗口中打开/切换该平台的登录页标签（等同浏览器「+」新标签页）。

    全系统只有一个专用 Edge 窗口（界面页 + 各平台登录页都是它的标签）：
    - 窗口未开 → 启动窗口，同时带「界面页 + 登录页」两个标签
    - 窗口已开 → 已有该平台登录页则切过去；否则在界面标签旁新开一个（绝不新开窗口）

    注意：登录页标签必须用「单实例原生转发」打开——自动化工具创建的标签会被
    Boss 等平台检测并自我清屏，导致无法登录。所有 HTTP 探测均无 CDP 会话，页面无感。
    """
    meta = PLATFORM_LOGIN[platform]

    if not browser.is_running(browser.SYSTEM_KEY):
        ok, msg = await browser.ensure_running_async(
            browser.SYSTEM_KEY, [browser.UI_URL, meta["login_url"]]
        )
        if not ok:
            return False, f"专用 Edge 窗口启动失败：{msg}"
        for _ in range(15):
            await asyncio.sleep(0.6)
            t = _find_login_target(meta)
            if t is not None:
                browser.activate_target(browser.SYSTEM_KEY, t.get("id", ""))
                break
        return True, "已启动专用 Edge 窗口，并在界面标签旁打开登录页"

    # 已有该平台登录页 → 直接把那个标签切到前台
    t = _find_login_target(meta)
    if t is not None:
        browser.activate_target(browser.SYSTEM_KEY, t.get("id", ""))
        return True, "已切换到窗口中的登录页标签"

    # 保证界面标签存在（登录页要开在界面旁边）
    targets = browser.list_targets(browser.SYSTEM_KEY)
    if not any(browser.is_ui_url(t2.get("url") or "") for t2 in targets):
        browser.forward_open(browser.SYSTEM_KEY, browser.UI_URL)
        await asyncio.sleep(1.2)

    # 原生转发新开登录页标签（等同点击 +，无自动化痕迹）
    if not browser.forward_open(browser.SYSTEM_KEY, meta["login_url"]):
        return False, "登录页打开失败，请重试"
    for _ in range(15):
        await asyncio.sleep(0.6)
        t = _find_login_target(meta)
        if t is not None:
            browser.activate_target(browser.SYSTEM_KEY, t.get("id", ""))
            return True, "已在窗口界面标签旁新开登录页标签"
    return False, "登录页打开失败，请重试"


# ---------------------------------------------------------------- 登录状态存储

_status_cache: dict[str, dict] = {}


def _load_status(platform: str) -> dict | None:
    from backend.db.database import session_scope
    from backend.db.models import AppConfig

    with session_scope() as s:
        row = s.get(AppConfig, f"login_{platform}")
        if row and row.value:
            try:
                return json.loads(row.value)
            except ValueError:
                return None
    return None


def _save_status(platform: str, data: dict) -> None:
    from backend.db.database import session_scope
    from backend.db.models import AppConfig

    payload = json.dumps(data, ensure_ascii=False)
    with session_scope() as s:
        row = s.get(AppConfig, f"login_{platform}")
        if row:
            row.value = payload
        else:
            s.add(AppConfig(key=f"login_{platform}", value=payload))


def get_login_status(platform: str) -> dict:
    if platform not in _status_cache:
        _status_cache[platform] = _load_status(platform) or {
            "status": "unknown", "message": "", "updated_at": None,
        }
    return _status_cache[platform]


def set_login_status(platform: str, status: str, message: str = "") -> None:
    data = {
        "status": status,
        "message": message,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _status_cache[platform] = data
    _save_status(platform, data)


async def probe_login_states() -> None:
    """实时探测各平台登录态（只读 cookie，零页面接触），并刷新状态缓存。

    用于打开页面 / 点击登录时即时反映真实状态：
    - 检测到特征 cookie 且缓存非 logged_in → 更新为已登录
    - 特征 cookie 消失且缓存为 logged_in → 更新为登录态已失效
    - Edge 未运行 / 读取失败 → 不动缓存（保持原状态）
    """
    if not browser.is_running(browser.SYSTEM_KEY):
        return
    cookies = await browser.read_cookies(browser.SYSTEM_KEY, None)
    if cookies is None:
        return
    for name, meta in PLATFORM_LOGIN.items():
        hints = meta.get("auth_hints") or ()
        if not hints:
            continue
        domain = meta["cookie_domain"]
        names = {c.get("name") for c in cookies if domain in (c.get("domain") or "")}
        hits = names & set(hints)
        cur = get_login_status(name)
        if hits and cur.get("status") != "logged_in":
            set_login_status(
                name,
                "logged_in",
                f"登录成功（特征: {', '.join(sorted(hits))}），登录状态已保存",
            )
        elif not hits and cur.get("status") == "logged_in":
            set_login_status(name, "not_logged", "登录态已失效，请重新登录")


# ---------------------------------------------------------------- 登录等待任务

_watchers: dict[str, asyncio.Task] = {}


def ensure_watcher(platform: str, timeout_s: int = 1800) -> None:
    task = _watchers.get(platform)
    if task and not task.done():
        return
    _watchers[platform] = asyncio.get_running_loop().create_task(_watch(platform, timeout_s))


async def _watch(platform: str, timeout_s: int) -> None:
    """轮询登录状态：只读浏览器调试端点，绝不挂接页面。

    - 存活检测：本机端口 socket（无副作用）
    - 登录证据：从浏览器端点直读 cookie（原始 CDP），出现登录特征 cookie 即判定成功
    - 关键点：不做任何页面级 attach——Playwright 的页面挂接会让 Boss 登录页自我清屏
    """
    meta = PLATFORM_LOGIN[platform]
    hints = meta.get("auth_hints") or ()
    if not hints:
        set_login_status(platform, "waiting", "请在 Edge 中完成登录（该平台自动检测未接入）")
        return

    set_login_status(platform, "waiting", "等待在 Edge 中完成登录…")
    deadline = time.time() + timeout_s
    closed_streak = 0
    seen_names: set[str] = set()

    while time.time() < deadline:
        await asyncio.sleep(4)
        if not browser.is_running(browser.SYSTEM_KEY):
            closed_streak += 1
            if closed_streak >= 5:
                set_login_status(platform, "not_logged", "登录窗口已关闭，可重新发起")
                return
            continue
        closed_streak = 0

        cookies = await browser.read_cookies(browser.SYSTEM_KEY, meta["cookie_domain"])
        if cookies is None:
            continue
        names = {c.get("name") for c in cookies}
        if names - seen_names:
            seen_names |= names
            logger.info("[%s] cookie 变化: %s", platform, sorted(names))
        hits = names & set(hints)
        if hits:
            logger.info("[%s] 检测到登录特征 cookie: %s", platform, sorted(hits))
            set_login_status(
                platform,
                "logged_in",
                f"登录成功（特征: {', '.join(sorted(hits))}），登录状态已保存",
            )
            return

    set_login_status(platform, "not_logged", "未检测到登录（超时），可重新发起")


async def resume_pending_logins() -> None:
    """服务重启后，若 Edge 仍在运行且有等待中的登录，恢复轮询任务。"""
    for name, _meta in PLATFORM_LOGIN.items():
        if get_login_status(name).get("status") == "waiting" and browser.is_running(browser.SYSTEM_KEY):
            ensure_watcher(name)
