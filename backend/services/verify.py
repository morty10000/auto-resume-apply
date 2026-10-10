"""验证码 / 安全验证的检测与处理（跳过模式）。

机制：
- detect(tab_id)：检测页面是否出现验证特征（URL / 标题 / DOM 元素 / 正文关键词）
- check_and_skip(tab_id, label)：在「请求失败 / 首页无结果」等可疑时刻调用 ——
  命中验证后：① 把该标签切到浏览器前台（用户随时可处理）
              ② 记录日志
              ③ 立即抛出 VerifyRequired —— 调度层跳过该平台，其余平台继续
  （不阻塞、不等待；已采集的部分岗位由调用方保留）
  未命中验证：正常返回，调用方按原有逻辑继续。
- check_and_wait / mark_resolved：旧「等待人工」模式（当前流程未使用，保留备用）。

设计约束：
- 跳过期间绝不关闭 / 重建标签页（保留用户需要处理的验证页）
- 检测只在可疑时刻触发，并对同一标签做 3 秒防抖，正常采集几乎零开销
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from backend.core import task_control
from backend.services import browser, task_hub

logger = logging.getLogger(__name__)

WAIT_TIMEOUT_S = 480.0     # 等待人工处理的最长时间（8 分钟）
POLL_EVERY_S = 3.0         # 等待期间的轮询间隔
CHECK_THROTTLE_S = 3.0     # 同一标签页重复检测的最小间隔（防抖）

VERIFY_DETECT_JS = """(() => {
  const url = location.href;
  const title = document.title || '';
  const body = document.body ? (document.body.innerText || '').slice(0, 5000) : '';
  const hit = {
    url: /captcha|verify|security|challenge|punish|risk/i.test(url),
    title: /验证|安全|verify|captcha/i.test(title),
    // 仅认「可见」的验证组件：去掉 slider/drag/verify 等泛化选择器，防轮播/装饰元素误报
    dom: [...document.querySelectorAll(
      '[class*="captcha"],[id*="captcha"],.geetest_panel,.geetest_holder,' +
      'iframe[src*="captcha"],iframe[src*="verify"],#nc_1_wrapper,[id*="nc_1_"],' +
      '[class*="nc-container"],[class*="nc_scale"]'
    )].some(e => e.offsetWidth || e.offsetHeight),
    text: /请完成验证|安全验证|滑动验证|拖动滑块|按住滑块|人机验证|验证码|操作过于频繁|环境异常|异常流量/.test(body),
  };
  return JSON.stringify({hit, title: title.slice(0, 80), snippet: body.slice(0, 120).replace(/\\s+/g, ' ')});
})()"""

_last_check: dict[str, float] = {}
_resolved_event = asyncio.Event()


class VerifyTimeout(Exception):
    """等待人工验证超时（调用方应停止对该平台的后续操作）。"""


class VerifyRequired(Exception):
    """检测到安全验证（验证码/滑块）——调用方应跳过当前平台，继续其他平台。"""


async def detect(tab_id: str) -> dict | None:
    """检测指定标签页是否有验证特征；命中返回详情 dict，否则 None。"""
    raw = await browser.raw_evaluate(browser.SYSTEM_KEY, tab_id, VERIFY_DETECT_JS, timeout_s=10)
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    hit = d.get("hit") or {}
    if any(hit.values()):
        return d
    return None


async def check_and_skip(tab_id: str, label: str) -> None:
    """可疑时刻调用：命中安全验证 → 立即跳过当前平台（抛 VerifyRequired）；无验证正常返回。"""
    if await detect(tab_id) is None:
        return
    await task_control.cancellable_sleep(1.0)          # 二次确认，排除瞬时误报
    if await detect(tab_id) is None:
        return
    hub = task_hub.hub
    logger.warning("%s 检测到安全验证，已跳过该平台", label)
    browser.activate_target(browser.SYSTEM_KEY, tab_id)
    hub.log(
        "WARN",
        f"{label}：检测到安全验证（验证码/滑块），已跳过该平台；"
        f"请处理验证后重新运行（其余平台不受影响）",
    )
    raise VerifyRequired(f"{label} 需要安全验证（验证码）")


async def check_and_wait(tab_id: str, label: str, *, wait_timeout: float = WAIT_TIMEOUT_S) -> bool:
    """[备用·等待模式] 页面无验证则立即返回 False；有验证则等待人工处理。

    - 处理完成 → True（调用方应重试当前请求）
    - 超时未处理 → raise VerifyTimeout
    """
    now = time.time()
    if now - _last_check.get(tab_id, 0) < CHECK_THROTTLE_S:
        return False
    _last_check[tab_id] = now

    d = await detect(tab_id)
    if not d:
        return False

    # 二次确认（间隔 1 秒）：排除页面瞬时状态的误报
    await task_control.cancellable_sleep(1.0)
    d2 = await detect(tab_id)
    if not d2:
        return False

    hub = task_hub.hub
    logger.warning("%s 检测到安全验证：%s", label, json.dumps(d2, ensure_ascii=False)[:200])
    browser.activate_target(browser.SYSTEM_KEY, tab_id)
    hub.log("WARN", f"{label}：检测到安全验证（验证码/滑块），已把该页面切到前台，等待人工处理…")
    hub.set_verify_wait(label)
    _resolved_event.clear()

    deadline = time.time() + wait_timeout
    ok = False
    warned_at = 0.0
    while True:
        if task_control.is_cancelled():
            hub.clear_verify_wait()
            hub.log("WARN", f"{label}：任务已手动停止，结束验证等待")
            raise task_control.TaskCancelled("任务已手动停止（验证等待中）")
        if _resolved_event.is_set():
            ok = True
            hub.log("OK", f"{label}：已确认人工处理完成，继续采集")
            break
        await task_control.cancellable_sleep(POLL_EVERY_S)
        if time.time() > deadline:
            break
        if await detect(tab_id) is None:
            ok = True
            hub.log("OK", f"{label}：验证已通过，采集自动继续")
            break
        remain = int(deadline - time.time())
        if time.time() - warned_at >= 15:
            warned_at = time.time()
            hub.update(detail=f"【{label}】等待人工完成安全验证…（剩余 {max(0, remain)} 秒，完成后自动继续）")

    hub.clear_verify_wait()
    if not ok:
        hub.log("ERR", f"{label}：等待验证超时（{int(wait_timeout)} 秒），已停止该平台采集")
        raise VerifyTimeout(f"{label} 等待人工验证超时")
    return True


def mark_resolved() -> None:
    """[备用·等待模式] 用户点界面「继续运行」：视为人工已处理，立即放行等待中的流程。"""
    _resolved_event.set()
    logger.info("收到人工确认：验证已处理")


def note_verify_event(platform: str, source: str, detail: str = "") -> None:
    """记录一次平台级安全验证触发（平台卡片「验证情况」展示用）。

    - source: collect=采集 / apply=投递
    - 任何异常都不影响主流程（展示功能，尽力而为）
    """
    try:
        from backend.db.database import session_scope
        from backend.db.models import VerifyEvent

        with session_scope() as s:
            s.add(VerifyEvent(platform=platform, source=source, detail=detail[:200] or None))
    except Exception:  # noqa: BLE001
        logger.exception("记录验证事件失败（不影响主流程）")
