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
import re
import time

from backend.services import browser

logger = logging.getLogger(__name__)


def _hint_hits(meta: dict, cookies: list[dict] | None) -> set[str]:
    """从 cookie 列表中筛出「未过期的登录特征 cookie 名」。

    关键点（2026-10-09 修复假绿灯）：
    - 特征 cookie 必须未过期（会话 cookie / expires<=0 / 未来时间才算有效）
    - 只看名字命中会把「已失效但长期留存」的 cookie（如 51job 有效期一年的
      _c_WBKFRo、智联的 rt）误判为已登录 → 必须带过期校验
    """
    domain = meta["cookie_domain"]
    hints = set(meta.get("auth_hints") or ())
    now = time.time()
    hits: set[str] = set()
    for c in cookies or []:
        name = c.get("name")
        if name not in hints or domain not in (c.get("domain") or ""):
            continue
        exp = c.get("expires")
        if exp is None or exp <= 0 or exp > now:
            hits.add(name)
    return hits


def _persist_value(meta: dict, cookies: list[dict] | None) -> tuple[str, str] | None:
    """按 persist_hint 取当前「持久指纹」cookie 的 (name, value)；未配置/不存在返回 None。"""
    p = meta.get("persist_hint")
    if not p:
        return None
    for c in cookies or []:
        if c.get("name") == p["name"] and p["domain"] in (c.get("domain") or ""):
            return (p["name"], str(c.get("value") or ""))
    return None


def _persist_match(meta: dict, cookies: list[dict] | None, cur: dict) -> bool:
    """持久指纹是否匹配（会话特征丢失后用于「登录态跨重启保留」判定）。"""
    fp = _persist_value(meta, cookies)
    if not fp:
        return False
    saved = (cur.get("persist") or {}).get(fp[0])
    return bool(saved) and saved == fp[1]


def _refresh_persist(platform: str, meta: dict, cookies: list[dict] | None, hits: set[str]) -> None:
    """登录证据（hits）出现时刷新持久指纹：记录当前 persist_hint cookie 值。"""
    if not hits:
        return
    fp = _persist_value(meta, cookies)
    if fp:
        set_persist_fingerprint(platform, *fp)


# 平台响应里出现这些文案 = 会话已被平台判定失效（运行期比 cookie 更可信的证据）
_LOGIN_LOST_RE = re.compile(
    r"请先登录|请登录|登录已失效|登录失效|登录状态.*(失效|异常)|重新登录|未登录|"
    r"not logged|login required|unauthorized",
    re.IGNORECASE,
)


def is_login_lost_text(text: str) -> bool:
    """判断一段接口响应/页面文本是否在提示「需要登录」。"""
    return bool(_LOGIN_LOST_RE.search(text or ""))

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
        # 2026-10-09 复审（站点机制变更）：
        # · _c_WBKFRo 已停用——重新登录后不再出现，且有效期一年的残留会致「假绿灯」，移除；
       # · 对照实验（全新匿名 profile 访问 www + we.51job.com 均无「51job」cookie）：
        #   重新登录流程中出现 51job=<cuid%3D…>（承载登录会话，session 级）→ 新登录特征
        "auth_hints": ("51job",),
        # 跨会话兜底（2026-10-09 实测）：session cookie 浏览器一重启就丢，但站点仍认登录
        # （登录态打开 we.51job.com 会直接进「我的职位」个人页；匿名则被弹回登录页）。
        # 登录证据出现时记录此持久 cookie（uid，28 天）的值作为指纹；
        # 重启后指纹匹配 → 判「登录态跨会话保留」，不再误报未登录。
        # （对照实验：JSESSIONID 匿名访问搜索页也会种上，不能作为判定依据）
        "persist_hint": {"name": "uid", "domain": "www.51job.com"},
        # 「去登录」触发时的会话复核：原生打开该页，按最终 URL 判断——
        # 登录态停留业务域（/pc/my/*）；未登录会被弹去 login 域（对照实验口径）
        "session_check": {"url": "https://we.51job.com/", "login_marker": "login.51job.com"},
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


# ---------------------------------------------------------------- 会话复核（跨重启假红自愈）

_session_checked_at: dict[str, float] = {}   # 复核冷却时间戳（防连点）
_SESSION_CHECK_COOLDOWN = 60.0               # 同一平台两次复核的最小间隔（秒）


async def verify_live_session(platform: str) -> bool | None:
    """「会话复核」：原生打开一次平台会话页，凭最终 URL 判断登录是否仍被站点承认。

    背景：session 型登录 cookie 在浏览器重启后丢失，但服务端会话可能仍有效
    （51job 实测：登录态打开 we 域直接进个人页；匿名则被弹回登录页）。
    cookie 检测失败时调用本函数复核，避免误报「未登录」。

    返回 True=站点仍认登录 / False=确实未登录 / None=无法判定（未配置/超时/冷却中）。
    全程零 CDP 挂接：原生转发打开 + 仅读标签 URL（调试端口 HTTP）。
    """
    meta = PLATFORM_LOGIN[platform]
    cfg = meta.get("session_check")
    if not cfg:
        return None
    now = time.time()
    if now - _session_checked_at.get(platform, 0.0) < _SESSION_CHECK_COOLDOWN:
        return None
    if not browser.is_running(browser.SYSTEM_KEY):
        return None
    _session_checked_at[platform] = now

    before = {t.get("id") for t in browser.list_targets(browser.SYSTEM_KEY)}
    if not browser.forward_open(browser.SYSTEM_KEY, cfg["url"]):
        return None

    # 等新标签出现
    new_id: str | None = None
    for _ in range(24):
        await asyncio.sleep(0.5)
        for t in browser.list_targets(browser.SYSTEM_KEY):
            if (
                t.get("id") not in before
                and t.get("type") == "page"
                and meta["cookie_domain"] in (t.get("url") or "")
            ):
                new_id = t.get("id")
                break
        if new_id:
            break
    if not new_id:
        return None

    # 等 URL 稳定（连续 2 秒不变视为跳转完成）
    prev: str | None = None
    stable = 0
    for _ in range(30):
        await asyncio.sleep(1.0)
        cur = None
        for t in browser.list_targets(browser.SYSTEM_KEY):
            if t.get("id") == new_id:
                cur = t.get("url") or ""
                break
        if cur and cur == prev:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
            prev = cur
    final_url = (prev or "").lower()
    if not final_url:
        return None
    if cfg["login_marker"] in final_url:
        logger.info("[%s] 会话复核：站点要求登录（%s）", platform, final_url[:90])
        return False
    if meta["cookie_domain"] in final_url:
        logger.info("[%s] 会话复核：站点仍认登录（%s）", platform, final_url[:90])
        return True
    return None


async def mark_session_verified(platform: str) -> None:
    """会话复核通过：记录当前持久指纹并置为已登录（跨重启后凭指纹快速判定）。"""
    meta = PLATFORM_LOGIN[platform]
    cookies = await browser.read_cookies(browser.SYSTEM_KEY, meta["cookie_domain"]) or []
    fp = _persist_value(meta, cookies)
    if fp:
        set_persist_fingerprint(platform, *fp)
    set_login_status(
        platform, "logged_in",
        "会话复核通过：平台仍认登录，状态已恢复",
        source="cookie",
    )


# ---------------------------------------------------------------- 登录状态存储

_status_cache: dict[str, dict] = {}
_checked_at: dict[str, str] = {}   # 每个平台「最近一次实际检测」的时间（内存态，不落库）


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


def get_checked_at(platform: str) -> str | None:
    """最近一次实际检测的时间（HH:MM:SS）；从未检测过返回 None。"""
    return _checked_at.get(platform)


def edge_running() -> bool:
    """专用 Edge 是否在运行（未运行时状态无法实时校验）。"""
    return browser.is_running(browser.SYSTEM_KEY)


def set_login_status(platform: str, status: str, message: str = "", source: str = "") -> None:
    """写入登录状态。source 记录证据来源：
    cookie = 本地 cookie 探测 / watch = 登录等待任务 / run = 运行期平台响应（最可信）
    """
    cur = get_login_status(platform)
    data = {
        "status": status,
        "message": message,
        "source": source or cur.get("source", ""),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    # 持久指纹与状态解耦：状态变更不清指纹（清除走 clear_persist_fingerprint 显式入口）
    if cur.get("persist"):
        data["persist"] = cur["persist"]
    _status_cache[platform] = data
    _save_status(platform, data)


def set_persist_fingerprint(platform: str, name: str, value: str) -> None:
    """登录证据出现时记录「持久指纹」：persist_hint cookie 的当前值。

    该指纹用于浏览器重启后（session 会话 cookie 丢失）的登录判定：
    指纹仍在且匹配 → 站点仍认登录（51job 实测），不再误报未登录。
    """
    if not name or not value:
        return
    cur = get_login_status(platform)
    persist = dict(cur.get("persist") or {})
    if persist.get(name) == value:
        return
    persist[name] = value
    data = dict(cur)
    data["persist"] = persist
    _status_cache[platform] = data
    _save_status(platform, data)


def clear_persist_fingerprint(platform: str) -> None:
    """作废持久指纹（「重新登录」前置 / 运行期确认失效时调用）。"""
    cur = get_login_status(platform)
    if not cur.get("persist"):
        return
    data = dict(cur)
    data.pop("persist", None)
    _status_cache[platform] = data
    _save_status(platform, data)


def mark_login_lost(platform: str, evidence: str) -> None:
    """运行期证据确认平台要求登录（服务端会话失效）→ 立即置为未登录。

    比 cookie 探测更可信：以平台自己的响应为准。
    source="run" 的状态不会被 cookie 探测自动翻绿（旧 cookie 可能长期残留），
    必须靠：① 用户点「重新登录」完成新登录 ② 后续运行成功（投递成功自愈）。
    """
    cur = get_login_status(platform)
    msg = f"登录态已失效（{evidence}），请重新登录"
    if cur.get("status") == "not_logged" and cur.get("message") == msg:
        return
    logger.warning("[%s] %s", platform, msg)
    clear_persist_fingerprint(platform)   # 确认失效 → 指纹作废（必须重新登录）
    set_login_status(platform, "not_logged", msg, source="run")


def note_apply_success(platform: str) -> None:
    """运行期投递成功 → 登录态自愈（投递成功是会话有效的最强证据）。"""
    cur = get_login_status(platform)
    if cur.get("status") == "logged_in":
        return
    set_login_status(platform, "logged_in", "运行校验通过（投递成功，登录有效）", source="run")


async def clear_login_cookies(platform: str) -> int:
    """清掉该平台残留的登录特征 cookie（「重新登录」前置步骤）。

    目的：旧 cookie 残留会让登录等待任务产生「假成功」——
    用户还没完成新登录，探针就已读到旧特征 cookie 而翻绿。清掉后，
    只有新登录产生的 cookie 才会被认作成功。
    """
    meta = PLATFORM_LOGIN[platform]
    hints = set(meta.get("auth_hints") or ())
    if not hints:
        return 0
    # 指纹同步作废：否则旧 uid 残留会让「跨会话判定」在重登完成前就翻绿（假成功）
    clear_persist_fingerprint(platform)
    cookies = await browser.read_cookies(browser.SYSTEM_KEY, meta["cookie_domain"])
    targets = [c for c in (cookies or []) if c.get("name") in hints]
    if not targets:
        return 0
    n = await browser.delete_cookies(
        browser.SYSTEM_KEY,
        [{"name": c.get("name"), "domain": c.get("domain"), "path": c.get("path")} for c in targets],
    )
    if n:
        logger.info("[%s] 已清除 %s 个残留登录 cookie（重新登录前置）", platform, n)
    return n


async def check_platform_login(platform: str) -> bool:
    """适配器统一登录检查：读 cookie + 过期校验 + 持久指纹兜底，并同步状态缓存。

    - 读到特征 cookie → 通过（若缓存红色且非 run 来源 → 顺手翻绿）；同时刷新持久指纹
    - 特征缺失但持久指纹匹配 → 通过（会话跨浏览器重启保留，站点仍认登录——实测口径）
    - 特征与指纹都无 → 不通过，且缓存翻红（提示重新登录）
    - Edge 未运行 / 读取失败 → 回退缓存状态（乐观：缓存绿则放行，运行时自会暴露问题）
    """
    meta = PLATFORM_LOGIN[platform]
    cookies = await browser.read_cookies(browser.SYSTEM_KEY, meta["cookie_domain"])
    if cookies is None:
        return get_login_status(platform).get("status") == "logged_in"
    hits = _hint_hits(meta, cookies)
    cur = get_login_status(platform)
    if hits:
        _refresh_persist(platform, meta, cookies, hits)
        if cur.get("status") != "logged_in" and cur.get("source") != "run":
            set_login_status(
                platform, "logged_in",
                f"登录成功（特征: {', '.join(sorted(hits))}），登录状态已保存",
                source="cookie",
            )
        return True
    if _persist_match(meta, cookies, cur):
        # 会话特征丢失（浏览器重启），但持久指纹仍匹配 → 站点仍认登录，不误报
        if cur.get("source") == "run" and cur.get("status") != "logged_in":
            return False   # 运行期已确认失效的，等待重新登录
        if cur.get("status") != "logged_in":
            set_login_status(
                platform, "logged_in",
                "登录态跨会话保留（持久凭据复核通过）",
                source="cookie",
            )
        return True
    if cur.get("status") == "logged_in":
        set_login_status(platform, "not_logged", "本地登录特征缺失，请重新登录", source="cookie")
    return False


async def probe_login_states() -> None:
    """实时探测各平台登录态（只读 cookie，零页面接触），并刷新状态缓存。

    用于打开页面 / 点击登录时即时反映真实状态：
    - 检测到未过期的特征 cookie 且缓存非 logged_in（且非运行期判定的失效）→ 更新为已登录
    - 特征 cookie 消失且缓存为 logged_in → 更新为登录态已失效
    - Edge 未运行 / 读取失败 → 不动缓存（保持原状态）

    假绿灯修复（2026-10-09）：
    - 特征 cookie 带过期校验（_hint_hits），过期残余不再算登录
    - source="run" 的失效状态不被 cookie 探测翻绿——旧 cookie 可能长期残留
      （如 51job 的 _c_WBKFRo 有效期一年），必须重新登录或运行成功才能恢复
    """
    if not browser.is_running(browser.SYSTEM_KEY):
        return
    cookies = await browser.read_cookies(browser.SYSTEM_KEY, None)
    if cookies is None:
        return
    checked = time.strftime("%H:%M:%S")
    for name, meta in PLATFORM_LOGIN.items():
        hints = meta.get("auth_hints") or ()
        if not hints:
            continue
        hits = _hint_hits(meta, cookies)
        _checked_at[name] = checked
        cur = get_login_status(name)
        if hits:
            _refresh_persist(name, meta, cookies, hits)
            if cur.get("status") != "logged_in" and cur.get("source") != "run":
                set_login_status(
                    name,
                    "logged_in",
                    f"登录成功（特征: {', '.join(sorted(hits))}），登录状态已保存",
                    source="cookie",
                )
        elif _persist_match(meta, cookies, cur):
            # 会话特征丢失（浏览器重启），但持久指纹仍匹配 → 站点仍认登录，不误报
            if cur.get("status") != "logged_in" and cur.get("source") != "run":
                set_login_status(
                    name, "logged_in",
                    "登录态跨会话保留（持久凭据复核通过）",
                    source="cookie",
                )
        elif cur.get("status") == "logged_in":
            set_login_status(name, "not_logged", "登录态已失效，请重新登录", source="cookie")


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
    - 防假成功：启动时记录现有特征 cookie 的值；旧 cookie 值未变化前不判定成功
      （配合「重新登录」前置的 cookie 清理，双重保证）
    """
    meta = PLATFORM_LOGIN[platform]
    hints = meta.get("auth_hints") or ()
    if not hints:
        set_login_status(platform, "waiting", "请在 Edge 中完成登录（该平台自动检测未接入）", source="watch")
        return

    set_login_status(platform, "waiting", "等待在 Edge 中完成登录…", source="watch")
    deadline = time.time() + timeout_s
    closed_streak = 0
    seen_names: set[str] = set()
    # 基线：当前仍在的特征 cookie 值（未变化的旧 cookie 不算新登录）
    baseline: dict[str, str] = {}
    if browser.is_running(browser.SYSTEM_KEY):
        _c0 = await browser.read_cookies(browser.SYSTEM_KEY, meta["cookie_domain"])
        baseline = {
            c.get("name"): c.get("value") or ""
            for c in (_c0 or [])
            if c.get("name") in hints
        }

    while time.time() < deadline:
        await asyncio.sleep(4)
        if not browser.is_running(browser.SYSTEM_KEY):
            closed_streak += 1
            if closed_streak >= 5:
                if get_login_status(platform).get("status") == "waiting":
                    set_login_status(platform, "not_logged", "登录窗口已关闭，可重新发起", source="watch")
                return
            continue
        closed_streak = 0

        # 状态已被其他通道（cookie 探测 / 运行期校验）判定为已登录 → 本等待任务完成，退出。
        # （防止：probe 翻绿后本任务仍挂着，跑满超时再把已登录状态覆盖成 not_logged）
        if get_login_status(platform).get("status") == "logged_in":
            logger.info("[%s] 登录状态已由其他通道确认，结束等待任务", platform)
            return

        cookies = await browser.read_cookies(browser.SYSTEM_KEY, meta["cookie_domain"])
        if cookies is None:
            continue
        names = {c.get("name") for c in cookies}
        if names - seen_names:
            seen_names |= names
            logger.info("[%s] cookie 变化: %s", platform, sorted(names))
        hits = _hint_hits(meta, cookies)
        if hits:
            fresh = {
                c.get("name"): c.get("value") or ""
                for c in cookies
                if c.get("name") in hits
            }
            changed = any(
                (n not in baseline) or (fresh.get(n) != baseline.get(n))
                for n in hits
            )
            if baseline and not changed:
                # 旧 cookie 原样还在（值未变化）→ 用户尚未完成新登录，继续等待
                continue
            logger.info("[%s] 检测到登录特征 cookie: %s", platform, sorted(hits))
            _refresh_persist(platform, meta, cookies, hits)
            set_login_status(
                platform,
                "logged_in",
                f"登录成功（特征: {', '.join(sorted(hits))}），登录状态已保存",
                source="watch",
            )
            return

    # 超时收尾：仅当仍处于 waiting 时才落「未登录」，避免覆盖其他通道已确认的状态
    if get_login_status(platform).get("status") == "waiting":
        set_login_status(platform, "not_logged", "未检测到登录（超时），可重新发起", source="watch")


async def resume_pending_logins() -> None:
    """服务重启后，若 Edge 仍在运行且有等待中的登录，恢复轮询任务。"""
    for name, _meta in PLATFORM_LOGIN.items():
        if get_login_status(name).get("status") == "waiting" and browser.is_running(browser.SYSTEM_KEY):
            ensure_watcher(name)
