"""详情页正文抓取工具（「正文校验」与匹配增强共用，四平台通用）。

流程：确保有一个专用详情标签（不占用搜索标签）→ 导航到岗位详情页 →
等待就绪并提取正文（各平台自己的选择器 + 「职位描述」文本窗口兜底）。

所有等待均走 task_control（可取消 / 可暂停）；失败返回 None，由调用方决定放行策略。
"""
from __future__ import annotations

import json
import logging
import random

from backend.core import task_control
from backend.services import browser

logger = logging.getLogger(__name__)


def build_extract_js(selectors: list[str], min_len: int = 120, max_len: int = 4000) -> str:
    """按选择器取最长文本块；不足时退化为「职位描述」标记附近的正文窗口。"""
    sels = json.dumps(selectors, ensure_ascii=False)
    return (
        "(() => {"
        "  const sels = " + sels + ";"
        "  let best = '';"
        "  for (const s of sels) {"
        "    let els = [];"
        "    try { els = [...document.querySelectorAll(s)]; } catch (e) { continue; }"
        "    for (const e of els) {"
        "      if (e.closest('.love-job-container,[class*=\"recommend\"],[class*=\"similar\"],[class*=\"relate\"],[class*=\"love-job\"],[class*=\"hot-job\"],[class*=\"guess\"]')) continue;"
        "      const t = (e.innerText || '').trim();"
        "      if (t.length > best.length) best = t;"
        "    }"
        "  }"
        "  if (best.length < 800) {"
        "    let alt = '';"
        "    for (const e of document.querySelectorAll('div,section,article')) {"
        "      if (e.closest('.love-job-container,[class*=\"recommend\"],[class*=\"similar\"],[class*=\"relate\"],[class*=\"love-job\"],[class*=\"hot-job\"],[class*=\"guess\"]')) continue;"
        "      const t = (e.innerText || '').trim();"
        "      if (t.length <= alt.length || t.length > 8000) continue;"
        "      if (/职位描述|职位介绍|岗位职责|工作职责|任职要求|岗位要求/.test(t.slice(0, 300))) alt = t;"
        "    }"
        "    if (alt.length > best.length) best = alt;"
        "  }"
        "  if (best.length < " + str(min_len) + ") {"
        "    const b = (document.body && document.body.innerText) || '';"
        "    const i = b.search(/职位描述|职位介绍|岗位职责|工作职责|任职要求|岗位要求/);"
        "    if (i >= 0) best = b.slice(Math.max(0, i - 100), i + 2500);"
        "  }"
        "  return best.slice(0, " + str(max_len) + ");"
        "})()"
    )


def _find_tab(domain: str, exclude_id: str) -> dict | None:
    for t in browser.list_targets(browser.SYSTEM_KEY):
        if t.get("type") != "page":
            continue
        u = t.get("url") or ""
        if domain in u and t.get("id") != exclude_id:
            return t
    return None


async def ensure_detail_tab(domain: str, exclude_id: str, first_url: str) -> dict | None:
    """找一个该平台的非搜索标签做详情抓取；没有则原生转发新开一个。"""
    tab = _find_tab(domain, exclude_id)
    if tab is not None:
        return tab
    browser.forward_open(browser.SYSTEM_KEY, first_url)
    for _ in range(20):
        task_control.raise_if_cancelled()
        await task_control.cancellable_sleep(0.8)
        tab = _find_tab(domain, exclude_id)
        if tab is not None:
            return tab
    return None


async def fetch_detail_text(tab: dict, url: str, url_key: str, selectors: list[str], humanize: bool = False) -> str | None:
    """导航到详情页并提取正文；超时 / 为空返回 None（调用方自行决定放行策略）。"""
    await browser.raw_navigate(browser.SYSTEM_KEY, tab["id"], url)
    js = "JSON.stringify({r: document.readyState, u: location.href})"
    ready = False
    for _ in range(16):
        task_control.raise_if_cancelled()
        await task_control.cancellable_sleep(1.2)
        raw = await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], js)
        if not raw:
            continue
        try:
            st = json.loads(raw)
        except ValueError:
            continue
        if url_key in (st.get("u") or "") and st.get("r") == "complete":
            ready = True
            break
    if not ready:
        logger.warning("详情页加载超时：%s", url[:80])
        return None
    await task_control.cancellable_sleep(2.0)      # 页面水合
    if humanize:
        # 拟人化阅读：分三段滚动 + 短停顿（去掉「秒开秒走」的机器特征）
        _scroll_js = (
            "(() => { const h = Math.max(1, document.body.scrollHeight - innerHeight);"
            " window.scrollTo(0, Math.round(h * %s)); return 'ok'; })()"
        )
        await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], _scroll_js % "0.35")
        await task_control.cancellable_sleep(random.uniform(1.2, 2.2))
        await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], _scroll_js % "0.72")
        await task_control.cancellable_sleep(random.uniform(1.0, 2.0))
        await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], _scroll_js % "0.15")
        await task_control.cancellable_sleep(random.uniform(0.6, 1.2))
    extract_js = build_extract_js(selectors)
    text = await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], extract_js)
    if text and len(text) >= 120:
        return text
    await task_control.cancellable_sleep(2.5)      # 懒加载补一次
    text = await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], extract_js)
    return text if text and len(text) >= 120 else None
