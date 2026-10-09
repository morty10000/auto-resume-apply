"""Boss直聘（zhipin.com）平台适配器。

采集架构（实测结论，勿轻易改动）：
- Boss 对自动化环境有强检测：Playwright 创建/挂接的标签页会被清屏，因此采集走
  「原生标签 + 页面内 API 请求」——标签用 Edge 单实例原生转发打开（等同手动点 +），
  数据通过该页面上下文 fetch /wapi/zpgeek/search/joblist.json 获取，
  页面自带的请求栈会自动完成签名，全程不启用 CDP 调试域。
- 收益：薪资等字段拿到纯文本（页面 DOM 里的数字被字体反爬替换成私用区字符）。
- 筛选（薪资/经验/学历）由本地匹配引擎过滤，采集侧只负责翻页取数据。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import re
from urllib.parse import quote

from playwright.async_api import Error as PlaywrightError

from backend.core import task_control
from backend.core.job_filter import body_matches_keyword, keyword_in_list_fields, parse_active_days, passes_filters
from backend.core.throttle import keyword_delay, page_delay, random_delay
from backend.services import browser, edge_login, verify
from backend.utils.city_codes import resolve_city

from .base import ApplyResult, BasePlatform, Job, JobQuery
from .detail import ensure_detail_tab, fetch_detail_text
from .registry import register

logger = logging.getLogger(__name__)

PLATFORM_NAME = "boss"

BASE_URL = "https://www.zhipin.com"
SEARCH_URL = BASE_URL + "/web/geek/jobs?query={query}&city={city}&page={page}"
CHAT_URL = BASE_URL + "/web/geek/chat"

# 登录后才会出现的 cookie 名（读 cookie 判断登录态，零页面接触）
AUTH_COOKIE_HINTS = ("wt2", "bst", "zp_at")

SELECTORS: dict[str, str] = {
    # ---- 搜索列表页（2026-10 实测结构；采集走 API，此处仅作诊断参考）----
    "job_card": "li.job-card-box",
    "job_name": "a.job-name",
    "job_salary": ".job-salary",
    "job_company": ".boss-name",
    "job_area": ".company-location",
    "job_link": "a.job-name",
    # ---- 登录态判断 ----
    "login_marker": ".login-register, .btn-login",
    "user_marker": ".user-nav, .nav-user",
    # ---- 详情页 / 沟通 ----
    "start_chat": "a.btn-startchat, span.btn-startchat",
    "chat_input": "#chat-input, .chat-input-area .input-area, textarea.input",
}


# 头像旁活跃标签提取（DOM 与 joblist 接口同页；标题去空格归一后用字典匹配）
_ACTIVE_LABELS_JS = (
    "(() => {"
    "  const out = [];"
    "  for (const el of document.querySelectorAll('.boss-active-time')) {"
    "    const label = (el.innerText || '').trim().slice(0, 20);"
    "    if (!label) continue;"
    "    const card = el.closest('.job-detail-box') || el.closest('[class*=job-detail-container]') || el.closest('[class*=job-card]');"
    "    let title = null;"
    "    if (card) {"
    "      const t = card.querySelector('.job-name') || card.querySelector('[class*=job-name]');"
    "      title = t ? (t.innerText || '').trim().replace(/\\s+/g, '') : null;"
    "    }"
    "    if (title) out.push({ title: title, label: label });"
    "  }"
    "  return JSON.stringify(out);"
    "})()"
)


def _norm_title(t) -> str:
    return re.sub(r"\s+", "", str(t or ""))


def _build_search_url(keyword: str, city_code: str, page_no: int) -> str:
    """构造搜索页 URL（用于打开/复用页面）。"""
    return SEARCH_URL.format(query=quote(keyword), city=city_code, page=page_no)


def _extract_job_id(href: str) -> str | None:
    """从详情链接里提取平台岗位 ID（保留给解析/测试用）。"""
    m = re.search(r"/job_detail/([^.]+)\.html", href)
    return m.group(1) if m else None


def _build_fetch_js(keyword: str, city_code: str, page_no: int) -> str:
    """页面内 API 请求脚本：在 zhipin 页面上下文里 GET 岗位列表，返回响应文本。"""
    return (
        "(async () => {"
        "  const u = '/wapi/zpgeek/search/joblist.json?query=' + encodeURIComponent(%s)"
        "    + '&city=%s&page=%d&_=' + Date.now();"
        "  const r = await fetch(u, { credentials: 'include' });"
        "  return await r.text();"
        "})()" % (json.dumps(keyword), city_code, page_no)
    )


# 读取详情页「立即沟通」按钮携带的请求信息（站点就是用 data-url 发的请求）
_ADD_BTN_JS = (
    "(() => {"
    "  const els = [...document.querySelectorAll('[data-url*=\"friend/add.json\"]')];"
    "  const el = els.find(e => (e.offsetWidth || e.offsetHeight)) || els[0] || null;"
    "  const btn = document.querySelector('a.btn-startchat, .btn-startchat');"
    "  return JSON.stringify({"
    "    found: !!el,"
    "    dataUrl: el ? el.getAttribute('data-url') : null,"
    "    isFriend: el ? el.getAttribute('data-isfriend') : null,"
    "    btnText: btn ? (btn.innerText || '').trim() : null,"
    "  });"
    "})()"
)


def _build_add_call_js(url_path: str) -> str:
    """页面内「立即沟通」请求：POST data-url（参数在查询串，页面请求栈自动签名）。"""
    full = url_path if url_path.startswith("http") else BASE_URL + url_path
    return (
        "(async () => {"
        "  try {"
        "    const r = await fetch(%s, { method: 'POST', credentials: 'include' });"
        "    return await r.text();"
        "  } catch (e) { return JSON.stringify({code: -1, message: 'fetch_error:' + e}); }"
        "})()" % json.dumps(full)
    )


# 会话页：直接打开与该 HR 的聊天
_GET_REDIRECT_JS = (
    "(() => {"
    "  const els = [...document.querySelectorAll('[data-url*=\"friend/add.json\"]')];"
    "  const el = els.find(e => (e.offsetWidth || e.offsetHeight)) || els[0] || null;"
    "  return el ? (el.getAttribute('redirect-url') || '') : '';"
    "})()"
)


def _build_send_js(text: str) -> str:
    """会话页驱动输入框发送消息（填字 → 回车），返回是否已发出。"""
    return (
        "(async () => {"
        "  const input = document.querySelector('.chat-input');"
        "  if (!input) return JSON.stringify({sent: false, reason: 'no-input'});"
        "  input.focus();"
        "  document.execCommand('selectAll', false, null);"
        "  document.execCommand('insertText', false, %s);"
        "  await new Promise(r => setTimeout(r, 600));"
        "  const ev = new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true});"
        "  input.dispatchEvent(ev);"
        "  await new Promise(r => setTimeout(r, 2600));"
        "  const cleared = (input.innerText || '').trim().length === 0;"
        "  const mine = [...document.querySelectorAll('.message-item.item-myself')].slice(-1)[0];"
        "  const shown = mine ? (mine.innerText || '').includes(%s) : false;"
        "  return JSON.stringify({sent: cleared || shown, cleared, shown});"
        "})()" % (json.dumps(text), json.dumps(text[:14]))
    )


# 详情页正文选择器（「正文校验」用；取最长文本块，差时按「职位描述」窗口兜底）
_DETAIL_SELECTORS = [".job-detail-section", ".job-sec-text", '[class*="job-detail"]', ".job-box"]

class BossPlatform(BasePlatform):
    name = PLATFORM_NAME
    display_name = "Boss直聘"

    # ------------------------------------------------------------ 登录态

    async def check_login(self) -> bool:
        """读浏览器 cookie 判断登录态（零页面接触，防触发反爬）。

        统一走 edge_login.check_platform_login：带过期校验，且能同步状态缓存
        （cookie 消失时前端卡片会同步变红，不再出现「掉登录还显示绿灯」）。
        """
        return await edge_login.check_platform_login(PLATFORM_NAME)

    async def _extract_active_labels(self, tab: dict) -> dict[str, str]:
        """从当前搜索页 DOM 提取「标题 → 头像旁活跃标签」（失败静默返回空表）。"""
        raw = await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], _ACTIVE_LABELS_JS)
        if not raw:
            return {}
        try:
            arr = json.loads(raw)
        except ValueError:
            return {}
        out: dict[str, str] = {}
        for x in arr or []:
            t, lb = x.get("title"), x.get("label")
            if t and lb:
                out[str(t)] = str(lb)
        return out

    # ------------------------------------------------------------ 采集

    async def _find_fetch_tab(self) -> dict | None:
        """找一个可用于页面内请求的 zhipin 搜索标签页。"""
        return browser.find_target(browser.SYSTEM_KEY, "zhipin.com/web/geek/jobs")

    async def _ensure_fetch_tab(self, first_url: str) -> dict | None:
        """复用已有搜索标签；没有则原生转发打开一个（等同手动点 +），等它出现。"""
        tab = await self._find_fetch_tab()
        if tab is not None:
            return tab
        browser.forward_open(browser.SYSTEM_KEY, first_url)
        for _ in range(20):
            task_control.raise_if_cancelled()
            await task_control.cancellable_sleep(0.8)
            tab = await self._find_fetch_tab()
            if tab is not None:
                return tab
        return None

    async def _human_touch(self, tab: dict) -> None:
        """在页面里做一次轻度滚动，降低机械感。"""
        js = "(window.scrollBy({top: %d, behavior: 'smooth'}), 'ok')" % random.randint(200, 600)
        await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], js)

    async def _goto_page(self, tab: dict, keyword: str, city_code: str, page_no: int) -> None:
        """把标签页导航到对应页（页面语境与请求一致，避免风控误判「环境异常」）。"""
        target = _build_search_url(keyword, city_code, page_no)
        cur = tab.get("url") or ""
        same_page = re.search(rf"[?&]page={page_no}(?:&|$)", cur)
        if ("query=" + quote(keyword)) in cur and f"city={city_code}" in cur and same_page:
            return  # 已经在目标页
        ok = await browser.raw_navigate(browser.SYSTEM_KEY, tab["id"], target)
        if not ok:
            logger.warning("导航到搜索页失败：%s", target)
        await self._wait_page_ready(tab["id"], keyword, page_no)
        updated = browser.find_target(browser.SYSTEM_KEY, "zhipin.com/web/geek/jobs")
        if updated:
            tab.update(updated)

    async def _wait_page_ready(self, target_id: str, keyword: str, page_no: int) -> None:
        """等待搜索页加载就绪（URL 已切换到目标页且结果区出现，或加载完成超时）。"""
        needle = quote(keyword)
        js = (
            "JSON.stringify({r: document.readyState,"
            " n: document.querySelectorAll('li.job-card-box').length,"
            " u: location.href})"
        )
        await task_control.cancellable_sleep(2.5)
        for i in range(18):
            task_control.raise_if_cancelled()
            raw = await browser.raw_evaluate(browser.SYSTEM_KEY, target_id, js)
            if raw:
                try:
                    st = json.loads(raw)
                except ValueError:
                    st = {}
                u = st.get("u") or ""
                right_page = bool(re.search(rf"[?&]page={page_no}(?:&|$)", u))
                if needle in u and right_page and (st.get("n") or 0) > 0:
                    return
                if needle in u and right_page and st.get("r") == "complete" and i >= 8:
                    return
            await task_control.cancellable_sleep(1.2)

    async def _fetch_page(
        self, target_id: str, keyword: str, city_code: str, page_no: int
    ) -> dict | None:
        """请求单页岗位数据；风控/网络失败时等待后重试一次。"""
        for attempt in range(2):
            raw = await browser.raw_evaluate(
                browser.SYSTEM_KEY,
                target_id,
                _build_fetch_js(keyword, city_code, page_no),
                await_promise=True,
            )
            if raw is not None:
                try:
                    data = json.loads(raw)
                except ValueError:
                    data = None
                    logger.warning("第 %s 页响应非 JSON：%s", page_no, raw[:100])
                    if edge_login.is_login_lost_text(raw[:3000]):
                        edge_login.mark_login_lost(PLATFORM_NAME, "搜索请求被重定向到登录页")
                if data is not None and data.get("code") == 0:
                    return data
                if data is not None:
                    logger.warning(
                        "第 %s 页接口返回 code=%s message=%s（第 %s 次）",
                        page_no, data.get("code"), data.get("message"), attempt + 1,
                    )
                    if edge_login.is_login_lost_text(str(data.get("message") or "")):
                        edge_login.mark_login_lost(
                            PLATFORM_NAME, f"搜索接口提示：{str(data.get('message'))[:40]}"
                        )
            else:
                logger.warning("第 %s 页请求失败（第 %s 次）", page_no, attempt + 1)
            if attempt < 1:
                await random_delay(6.0, 15.0)
                task_control.raise_if_cancelled()
        return None

    @staticmethod
    def _parse_api_job(it: dict) -> Job | None:
        """把 joblist.json 的条目映射为标准化 Job。"""
        job_id = it.get("encryptJobId")
        title = it.get("jobName")
        if not job_id or not title:
            return None
        city = "·".join(
            x for x in [it.get("cityName"), it.get("areaDistrict"), it.get("businessDistrict")] if x
        ) or None
        return Job(
            platform=PLATFORM_NAME,
            platform_job_id=job_id,
            title=title,
            company=it.get("brandName") or "",
            salary=it.get("salaryDesc"),
            city=city,
            url=f"{BASE_URL}/job_detail/{job_id}.html",
            extra={
                "security_id": it.get("securityId"),
                "experience": it.get("jobExperience"),
                "degree": it.get("jobDegree"),
                "labels": it.get("jobLabels"),
                "skills": it.get("skills"),
                "welfare": it.get("welfareList"),
                "boss_name": it.get("bossName"),
                "boss_title": it.get("bossTitle"),
                "boss_online": it.get("bossOnline"),
                "brand_stage": it.get("brandStageName"),
                "brand_industry": it.get("brandIndustry"),
                "brand_scale": it.get("brandScaleName"),
                "hr_active_desc": "在线" if it.get("bossOnline") else None,
                "hr_active_days": 0 if it.get("bossOnline") else None,
            },
        )

    async def search_jobs(self, query: JobQuery) -> list[Job]:
        """按 关键词 × 城市 采集岗位；支持逐个/组合搜索与拟人化限速。

        每个关键词先把标签页导航到对应搜索页（模拟用户打开搜索），再在
        页面语境中请求各页数据——URL 与请求语境一致，避免触发「环境异常」风控。
        """
        keywords = [k.strip() for k in query.keywords if k and k.strip()]
        if not keywords:
            return []
        if query.keyword_mode == "combined" and len(keywords) > 1:
            keywords = [" ".join(keywords)]
        if query.shuffle_keywords and len(keywords) > 1:
            random.shuffle(keywords)

        jobs: list[Job] = []
        new_count = 0                                    # 新岗位计数（已入库岗位不占配额）
        self.last_partial_jobs = jobs   # 任务停止 / 验证超时时保留已采部分
        self.last_done_keywords = []    # 验证跳过续跑断点：已完成的关键词
        tab: dict | None = None
        first_fetch_done = False
        filtered_total = 0
        scanned_total = 0
        seen_ids: set[str] = set()
        self.last_filtered = 0
        self.last_scanned = 0
        self.last_body_dropped = 0
        self.last_body_rescued = 0
        active_map: dict[str, str] = {}
        detail_tab: dict | None = None
        body_fetched = 0
        body_budget = max(1, min(40, query.max_jobs * 2)) if query.verify_body else 0
        body_dropped = 0
        body_rescued = 0

        for kw_idx, keyword in enumerate(keywords):
            task_control.raise_if_cancelled()
            if kw_idx > 0:
                logger.info(
                    "关键词间隔停顿 %.0f-%.0f 秒…", query.kw_delay_min, query.kw_delay_max
                )
                await keyword_delay(query.kw_delay_min, query.kw_delay_max)
                task_control.raise_if_cancelled()   # 停止后不再发出新请求
            self.note(f"正在搜索「{keyword}」（第 {kw_idx + 1}/{len(keywords)} 个关键词）")
            for city in query.cities:
                try:
                    city_code = resolve_city(city)
                except KeyError:
                    logger.warning("Boss 暂无「%s」的城市码，跳过该城市", city)
                    self.note(f"「{city}」没有对应的 Boss 城市码，已跳过该城市")
                    continue
                if tab is None:
                    tab = await self._ensure_fetch_tab(_build_search_url(keyword, city_code, 1))
                    if tab is None:
                        raise RuntimeError("无法获得浏览器标签页（Edge 实例不可用），本平台采集中断")
                    await random_delay(2.0, 4.0)
                for page_no in range(1, query.max_pages + 1):
                    task_control.raise_if_cancelled()
                    if first_fetch_done:
                        await page_delay(query.page_delay_min, query.page_delay_max)
                    task_control.raise_if_cancelled()   # 停止后不再发出新的页面请求
                    first_fetch_done = True
                    # 每页先导航到对应页（页面语境与请求一致，避免风控误判）
                    await self._goto_page(tab, keyword, city_code, page_no)
                    if query.humanize_scroll:
                        await self._human_touch(tab)
                    active_map = await self._extract_active_labels(tab)

                    data = await self._fetch_page(tab["id"], keyword, city_code, page_no)
                    if data is None:
                        # 可疑失败：命中安全验证 → 跳过该平台（抛错）；否则继续原有重试流程
                        await verify.check_and_skip(tab["id"], self.display_name)
                    if data is None:
                        logger.warning("第 %s 页请求异常，检查标签页网络后重试", page_no)
                        # 僵尸标签自愈：网络探活失败 → 关掉重开（与投递路径同款防护）
                        if await browser.page_network_alive(browser.SYSTEM_KEY, tab["id"]) is False:
                            logger.warning("标签页网络无响应（僵尸化），重开标签页")
                            self.note("标签页网络无响应，正在重开标签页…")
                            await browser.replace_tab(
                                browser.SYSTEM_KEY, tab["id"],
                                _build_search_url(keyword, city_code, page_no),
                            )
                            await task_control.cancellable_sleep(2.0)
                        tab = await self._ensure_fetch_tab(
                            _build_search_url(keyword, city_code, page_no)
                        )
                        if tab is None:
                            raise RuntimeError("无法获得浏览器标签页（Edge 实例不可用），本平台采集中断")
                        await self._wait_page_ready(tab["id"], keyword, page_no)
                        data = await self._fetch_page(tab["id"], keyword, city_code, page_no)
                    if data is None:
                        logger.error("「%s」/ %s 第 %s 页最终失败，跳过", keyword, city, page_no)
                        continue
                    zp = data.get("zpData") or {}
                    items = zp.get("jobList") or []
                    if not items:
                        if page_no == 1:
                            # 首页空结果：命中安全验证 → 跳过该平台；未命中 → 按原逻辑结束翻页
                            await verify.check_and_skip(tab["id"], self.display_name)
                        if not items:
                            logger.info("「%s」/ %s 第 %s 页无结果，停止翻页", keyword, city, page_no)
                            self.note(f"「{keyword}」·{city} 第 {page_no} 页无更多结果，换下一组")
                            break
                    page_kept = 0
                    page_dropped = 0
                    for it in items:
                        job = self._parse_api_job(it)
                        if job is None:
                            continue
                        _lbl = active_map.get(_norm_title(job.title))
                        if _lbl:
                            job.extra["hr_active_desc"] = _lbl
                            job.extra["hr_active_days"] = parse_active_days(_lbl)
                        scanned_total += 1
                        if job.platform_job_id in seen_ids:
                            continue  # 跨页重复，跳过（不占名额）
                        # —— 关键词裁决：标题 / 标签 /（可选）正文 任一命中即可 ——
                        list_hit = keyword_in_list_fields(job, keyword)
                        if query.verify_body:
                            if not passes_filters(job, keyword, query, check_keyword=False):
                                page_dropped += 1
                                continue
                            verdict = list_hit
                            if body_fetched < body_budget:
                                if detail_tab is None:
                                    detail_tab = await ensure_detail_tab("zhipin.com", tab["id"], job.url)
                                    if detail_tab is None:
                                        body_budget = 0
                                        logger.warning("正文校验：无法获得详情标签，本轮改为不抓正文")
                                        self.note("正文校验：无法打开详情标签，本轮跳过")
                                    else:
                                        await random_delay(1.5, 3.0)
                                if detail_tab is not None:
                                    await page_delay(query.page_delay_min, query.page_delay_max)
                                    task_control.raise_if_cancelled()
                                    body_fetched += 1
                                    _text = await fetch_detail_text(
                                        detail_tab, job.url, job.platform_job_id, _DETAIL_SELECTORS
                                    )
                                    if _text:
                                        job.description = _text
                                        if not verdict and body_matches_keyword(_text, keyword):
                                            verdict = True
                                            body_rescued += 1
                                            logger.info(
                                                "正文救回：%s @ %s（标题/标签未中，正文命中「%s」）",
                                                job.title, job.company, keyword,
                                            )
                                            self.note(f"正文救回：{job.title[:14]}…（标题/标签未中，正文命中）")
                                    else:
                                        job.extra["body_missing"] = True
                                        await verify.check_and_skip(detail_tab["id"], self.display_name)
                                    if body_fetched % 8 == 0 and body_fetched < body_budget:
                                        self.note("正文抓取节奏：长休息一次（模拟人工停顿）…")
                                        await random_delay(15.0, 40.0)
                            if not verdict:
                                body_dropped += 1
                                page_dropped += 1
                                logger.info(
                                    "关键词未命中（标题/标签/正文）：%s @ %s", job.title, job.company
                                )
                                continue
                        else:
                            if not passes_filters(job, keyword, query):
                                page_dropped += 1
                                continue
                        seen_ids.add(job.platform_job_id)
                        jobs.append(job)
                        page_kept += 1
                        # 配额只统计新岗位：已入库岗位不占配额（仍返回，用于补录正文）
                        if not query.known_ids or job.platform_job_id not in query.known_ids:
                            new_count += 1
                        if new_count >= query.max_jobs:
                            break
                    filtered_total += page_dropped
                    logger.info(
                        "「%s」/ %s 第 %s 页：命中 %s 个，过滤 %s 个",
                        keyword, city, page_no, page_kept, page_dropped,
                    )
                    self.note(
                        f"「{keyword}」·{city} 第 {page_no} 页：命中 {page_kept} 个"
                        f"（累计新增 {new_count}/{query.max_jobs}）"
                    )
                    if new_count >= query.max_jobs:
                        logger.info("已达本轮新增上限 %s 个，停止采集", query.max_jobs)
                        self.note(f"已达到本轮新增上限 {query.max_jobs} 个，停止采集")
                        break
                    if not zp.get("hasMore"):
                        break
                if new_count >= query.max_jobs:
                    break
            if new_count >= query.max_jobs:
                break
            self.last_done_keywords.append(keyword)
        self.last_filtered = filtered_total
        self.last_scanned = scanned_total
        self.last_body_dropped = body_dropped
        self.last_body_rescued = body_rescued
        if query.verify_body:
            self.note(
                f"正文校验：抓取 {body_fetched} 份正文 · 正文救回 {body_rescued} 个 · 未命中丢弃 {body_dropped} 个"
            )
        self.note(
            f"搜索完成：收集 {len(jobs)} 个岗位（新增 {new_count} · 扫描 {scanned_total} · 过滤 {filtered_total}）"
        )
        return jobs

    # ------------------------------------------------------------ 投递

    async def _find_apply_tab(self) -> dict | None:
        """找任意一个 zhipin 标签页（投递时用它打开岗位详情页）。"""
        for t in browser.list_targets(browser.SYSTEM_KEY):
            if t.get("type") == "page" and "zhipin.com" in (t.get("url") or ""):
                return t
        return None

    async def _ensure_apply_tab(self, url: str) -> dict | None:
        """复用 zhipin 标签；没有则原生转发打开（等同手动点 +）。"""
        tab = await self._find_apply_tab()
        if tab is None:
            browser.forward_open(browser.SYSTEM_KEY, url)
            for _ in range(20):
                await task_control.cancellable_sleep(0.8)
                tab = await self._find_apply_tab()
                if tab is not None:
                    return tab
        return tab

    async def _goto_job_page(self, tab: dict, job_url: str, force: bool = False) -> bool:
        """把标签页导航到岗位详情页并等待就绪（页面与请求语境一致）。force 时强制重载。"""
        m = re.search(r"/job_detail/([^.]+)\.html", job_url)
        key = m.group(1) if m else job_url
        cur = tab.get("url") or ""
        if force or key not in cur:
            await browser.raw_navigate(browser.SYSTEM_KEY, tab["id"], job_url)
        js = "JSON.stringify({r: document.readyState, u: location.href})"
        for _ in range(16):
            await task_control.cancellable_sleep(1.2)
            raw = await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], js)
            if raw:
                try:
                    st = json.loads(raw)
                except ValueError:
                    continue
                u = st.get("u") or ""
                if key in u and st.get("r") == "complete":
                    await task_control.cancellable_sleep(random.uniform(1.5, 2.8))   # 留出页面水合时间
                    return True
        return False

    async def _send_greeting(
        self, tab: dict, text: str,
        job_url: str | None = None, enc_boss_id: str | None = None,
    ) -> bool:
        """打开与该 HR 的会话，发送自定义打招呼语（驱动聊天输入框 + 回车）。

        会话 URL 优先取详情页按钮的 redirect-url（投递后重载页面才会出现，含完整参数）；
        拿不到时回退用 add.json 响应里的 encBossId 拼 id。
        """
        url = ""
        if job_url:
            # 重载详情页，让按钮输出「已沟通」状态的完整会话跳转地址
            with contextlib.suppress(Exception):
                await self._goto_job_page(tab, job_url, force=True)
            redirect = await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], _GET_REDIRECT_JS)
            if redirect and "chat" in str(redirect):
                url = redirect if str(redirect).startswith("http") else BASE_URL + str(redirect)
        if not url and enc_boss_id:
            url = f"{BASE_URL}/web/geek/chat?id={enc_boss_id}"
        if not url:
            return False
        if not await browser.raw_navigate(browser.SYSTEM_KEY, tab["id"], url):
            return False
        for _ in range(14):
            await task_control.cancellable_sleep(1.0)
            has = await browser.raw_evaluate(
                browser.SYSTEM_KEY, tab["id"], "!!document.querySelector('.chat-input')"
            )
            if has == "true":
                break
        else:
            return False
        await task_control.cancellable_sleep(random.uniform(0.8, 1.6))
        raw = await browser.raw_evaluate(
            browser.SYSTEM_KEY, tab["id"], _build_send_js(text), await_promise=True
        )
        if not raw:
            return False
        try:
            r = json.loads(raw)
        except ValueError:
            return False
        return bool(r.get("sent"))

    async def _confirm_communicated(self, target_id: str, tries: int = 4) -> bool:
        """复核按钮是否已变为「已沟通」状态（用于请求响应丢失时的补偿确认）。"""
        for _ in range(tries):
            await task_control.cancellable_sleep(3.0)
            raw = await browser.raw_evaluate(browser.SYSTEM_KEY, target_id, _ADD_BTN_JS)
            if not raw:
                continue
            try:
                st = json.loads(raw)
            except ValueError:
                continue
            if st.get("isFriend") == "true" or (st.get("btnText") or "").strip() == "继续沟通":
                return True
        return False

    async def apply(self, job: Job, greeting: str | None = None) -> ApplyResult:
        """投递（立即沟通）单个岗位：原生标签打开详情页 → 页面内 POST friend/add.json。

        与采集同一套架构：不用 Playwright 挂接、不新开窗口；请求路径取自详情页按钮的
        data-url（站点同款），参数在查询串中由页面请求栈自动签名。
        """
        if not await self.check_login():
            return ApplyResult(success=False, message="登录态失效，请重新扫码登录")

        tab = await self._ensure_apply_tab(job.url)
        if tab is None:
            return ApplyResult(success=False, message="无法获得 zhipin 标签页")
        if not await self._goto_job_page(tab, job.url):
            # 失败路径先探测验证墙：整页拦截时页面上没有按钮，
            # 这里是识别「需要验证」的最后机会（否则误报超时、平台不被跳过、白跑重试）
            if await verify.detect(tab["id"]):
                return ApplyResult(success=False, message="平台触发安全验证（验证码），已跳过本平台", need_verify=True)
            return ApplyResult(success=False, message="岗位详情页加载超时")

        # 预检：标签页网络僵尸化（后台停留过久）→ 关掉重开
        if await browser.page_network_alive(browser.SYSTEM_KEY, tab["id"]) is False:
            logger.warning("Boss 标签页网络无响应，重开标签页重试")
            await browser.replace_tab(browser.SYSTEM_KEY, tab["id"], job.url)
            await task_control.cancellable_sleep(2.0)
            tab = await self._ensure_apply_tab(job.url)
            if tab is None or not await self._goto_job_page(tab, job.url):
                if tab is not None and await verify.detect(tab["id"]):
                    return ApplyResult(success=False, message="平台触发安全验证（验证码），已跳过本平台", need_verify=True)
                return ApplyResult(success=False, message="标签页重建失败，请稍后重试")

        # 安全验证检测：详情页若为验证码页 → 标记需验证（调度层跳过该平台，其余平台继续）
        if await verify.detect(tab["id"]):
            return ApplyResult(success=False, message="平台触发安全验证（验证码），已跳过本平台", need_verify=True)

        state_raw = await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], _ADD_BTN_JS)
        state: dict = {}
        if state_raw:
            try:
                state = json.loads(state_raw)
            except ValueError:
                state = {}
        if state.get("isFriend") == "true" or (state.get("btnText") or "").strip() == "继续沟通":
            return ApplyResult(success=True, message="此前已沟通（跳过重复投递）")

        url_path = state.get("dataUrl")
        if not url_path:
            security_id = (job.extra or {}).get("security_id")
            if not security_id:
                return ApplyResult(success=False, message="页面未找到沟通入口，且缺少备用的 security_id")
            url_path = f"/wapi/zpgeek/friend/add.json?securityId={security_id}"

        # 响应可能因后台标签页延迟而丢失：先复核状态、再重试一次，避免「假失败」
        raw = None
        for attempt in range(2):
            raw = await browser.raw_evaluate(
                browser.SYSTEM_KEY, tab["id"], _build_add_call_js(url_path), await_promise=True
            )
            if raw is not None:
                break
            if await self._confirm_communicated(tab["id"]):
                return ApplyResult(success=True, message="已发起沟通（响应超时，平台侧已确认建立沟通）")
            if attempt == 0:
                await task_control.cancellable_sleep(random.uniform(3.0, 5.0))
        if raw is None:
            if await self._confirm_communicated(tab["id"]):
                return ApplyResult(success=True, message="已发起沟通（响应超时，平台侧已确认建立沟通）")
            return ApplyResult(success=False, message="沟通请求无响应（页面可能已变化）")
        try:
            data = json.loads(raw)
        except ValueError:
            return ApplyResult(success=False, message=f"响应非 JSON：{raw[:80]}")

        code = data.get("code")
        message = str(data.get("message") or "")
        if code == 0:
            await task_control.cancellable_sleep(random.uniform(1.0, 2.0))
            if greeting and greeting.strip():
                enc_boss_id = str((data.get("zpData") or {}).get("encBossId") or "")
                ok = await self._send_greeting(
                    tab, greeting.strip(), job_url=job.url, enc_boss_id=enc_boss_id or None
                )
                if ok:
                    return ApplyResult(success=True, message="已发起沟通 + 自定义打招呼语已发送")
                return ApplyResult(success=True, message="已发起沟通（平台默认招呼已送达，自定义招呼语未发出）")
            return ApplyResult(success=True, message="已发起沟通（打招呼）")
        if any(w in message for w in ("已经", "已沟通", "沟通过", "打过招呼")):
            return ApplyResult(success=True, message=f"此前已沟通（{message}）")
        if code == 37 or "异常" in message:
            return ApplyResult(success=False, message=f"触发风控（{message or code}）", need_verify=True)
        return ApplyResult(success=False, message=f"沟通失败（code={code} {message}）")


register(BossPlatform())
