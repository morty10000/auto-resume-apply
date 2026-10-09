"""猎聘（liepin.com）平台适配器。

架构与 Boss 相同：系统 Edge 原生标签 + 页面内请求（raw CDP，无 Playwright 挂接）。

采集（2026-10 实测）：
- 页面内 POST api-c.liepin.com/api/com.liepin.searchfront4c.pc-search-job
- 必带头：X-Fscp-Std-Info / X-Fscp-Version / X-Client-Type / X-XSRF-TOKEN（cookie）等
- 无需 passThroughForm；翻页用 0 基 currentPage；响应 data.data.jobCardList

投递（2026-10 实测）：
- 详情页点「投简历」→ 弹窗（附件简历选择）→ 点弹窗「立即投递」
  → 跳转 c.liepin.com/job/apply/success?job_id=XXX 即成功
- 已投递/已沟通：按钮「聊一聊」变为「继续聊」→ 跳过
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from urllib.parse import quote

from backend.core import task_control
from backend.core.job_filter import body_matches_keyword, keyword_in_list_fields, parse_active_days, passes_filters
from backend.core.throttle import keyword_delay, page_delay, random_delay
from backend.services import browser, edge_login, verify

from .base import ApplyResult, BasePlatform, Job, JobQuery
from .detail import ensure_detail_tab, fetch_detail_text
from .registry import register

logger = logging.getLogger(__name__)

PLATFORM_NAME = "liepin"

BASE_URL = "https://www.liepin.com"
SEARCH_PAGE = BASE_URL + "/zhaopin/?key={kw}&dq={dq}"
API_SEARCH = "https://api-c.liepin.com/api/com.liepin.searchfront4c.pc-search-job"

AUTH_COOKIE_HINTS = ("lt_auth", "user_name")

# 城市 → dq 代码（2026-10 从站点城市字典接口提取并实测）
CITY_CODES = {
    "北京": "010", "上海": "020", "广州": "050020", "深圳": "050090",
    "佛山": "050050", "南京": "060020", "苏州": "060080", "杭州": "070020",
    "武汉": "170020", "西安": "270020", "成都": "280020",
}

PAGE_SIZE = 40


def _build_search_js(keyword: str, dq: str, page_index: int) -> str:
    """页面内调用猎聘搜索接口（站点同款请求头；无需 passThroughForm）。"""
    form = {
        "city": dq, "dq": dq, "currentPage": page_index, "pageSize": PAGE_SIZE,
        "key": keyword, "suggestTag": "", "workYearCode": "", "compId": "", "compName": "",
        "compTag": "", "industry": "", "salaryCode": "", "jobKind": "", "compScale": "",
        "compKind": "", "compStage": "", "eduLevel": "", "salaryLow": "", "salaryHigh": "",
        "hrActiveTimeCode": "",
    }
    body = {"data": {"mainSearchPcConditionForm": form}}
    return (
        "(async () => {"
        "  try {"
        "    const token = (document.cookie.match(/XSRF-TOKEN=([^;]+)/) || [])[1] || '';"
        "    const cid = window.__FE_CLIENT_ID || '40108';"
        "    const uuid = 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {"
        "      const r = Math.random() * 16 | 0; return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);"
        "    });"
        "    const r = await fetch(%s, {"
        "      method: 'POST', credentials: 'include',"
        "      headers: {"
        "        'Content-Type': 'application/json;charset=UTF-8',"
        "        'X-Client-Type': 'web', 'X-Fscp-Version': '1.1',"
        "        'X-Fscp-Std-Info': JSON.stringify({client_id: String(cid)}),"
        "        'X-Requested-With': 'XMLHttpRequest',"
        "        'X-Fscp-Trace-Id': uuid,"
        "        'X-XSRF-TOKEN': token"
        "      },"
        "      body: JSON.stringify(%s)"
        "    });"
        "    return await r.text();"
        "  } catch (e) { return JSON.stringify({flag: -1, msg: 'fetch_error:' + e}); }"
        "})()" % (json.dumps(API_SEARCH), json.dumps(body))
    )


def _parse_api_job(it: dict) -> Job | None:
    """把猎聘搜索接口条目映射为标准化 Job。"""
    job = it.get("job") or {}
    comp = it.get("comp") or {}
    recruiter = it.get("recruiter") or {}
    jid = job.get("jobId")
    title = job.get("title")
    if not jid or not title:
        return None
    return Job(
        platform=PLATFORM_NAME,
        platform_job_id=str(jid),
        title=str(title),
        company=comp.get("compName") or "",
        salary=job.get("salary"),
        city=job.get("dq"),
        url=job.get("link") or f"{BASE_URL}/job/1{jid}.shtml",
        extra={
            "experience": "",                                  # 列表数据不含经验要求
            "degree": job.get("requireEduLevel"),
            "skills": list(job.get("labels") or []),
            "labels": (job.get("labels") or [])[:6],
            "welfare": [],
            "boss_online": bool(recruiter.get("inDay")),
            "hr_name": recruiter.get("recruiterName"),
            "brand_industry": comp.get("compIndustry"),
            "brand_scale": comp.get("compScale"),
            "brand_stage": "",
            "hr_active_desc": recruiter.get("imShowText") or ("在线" if recruiter.get("inDay") else None),
            "hr_active_days": 0 if recruiter.get("inDay") else parse_active_days(recruiter.get("imShowText")),
            "publish_time": job.get("refreshTime"),
            "campus": job.get("campusJobKind") or "",
        },
    )


# 详情页正文选择器（「正文校验」用；取最长文本块，差时按「职位描述」窗口兜底）
_DETAIL_SELECTORS = [".job-intro-container", '[class*="job-intro"]']   # 勿加 job-detail：会命中「猜你喜欢」推荐卡

class LiepinPlatform(BasePlatform):
    name = PLATFORM_NAME
    display_name = "猎聘"

    # ------------------------------------------------------------ 登录态

    async def check_login(self) -> bool:
        """统一走 edge_login.check_platform_login（带过期校验 + 状态缓存同步）。"""
        return await edge_login.check_platform_login(PLATFORM_NAME)

    # ------------------------------------------------------------ 标签页

    async def _find_tab(self) -> dict | None:
        for t in browser.list_targets(browser.SYSTEM_KEY):
            if t.get("type") == "page" and "liepin.com" in (t.get("url") or ""):
                return t
        return None

    async def _ensure_tab(self, first_url: str) -> dict | None:
        tab = await self._find_tab()
        if tab is not None:
            return tab
        browser.forward_open(browser.SYSTEM_KEY, first_url)
        for _ in range(20):
            task_control.raise_if_cancelled()
            await task_control.cancellable_sleep(0.8)
            tab = await self._find_tab()
            if tab is not None:
                return tab
        return None

    async def _goto_search(self, tab: dict, keyword: str, dq: str) -> bool:
        url = SEARCH_PAGE.format(kw=quote(keyword), dq=dq)
        cur = tab.get("url") or ""
        if f"key={quote(keyword)}" in cur and f"dq={dq}" in cur:
            return True
        await browser.raw_navigate(browser.SYSTEM_KEY, tab["id"], url)
        js = "JSON.stringify({r: document.readyState, u: location.href})"
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
            if st.get("r") == "complete" and "liepin.com" in (st.get("u") or ""):
                await task_control.cancellable_sleep(random.uniform(1.2, 2.2))
                return True
        return False

    # ------------------------------------------------------------ 采集

    async def _fetch_page(self, target_id: str, keyword: str, dq: str, page_index: int) -> dict | None:
        for attempt in range(2):
            raw = await browser.raw_evaluate(
                browser.SYSTEM_KEY, target_id,
                _build_search_js(keyword, dq, page_index), await_promise=True,
            )
            if raw is not None:
                try:
                    data = json.loads(raw)
                except ValueError:
                    data = None
                    if edge_login.is_login_lost_text(raw[:3000]):
                        edge_login.mark_login_lost(PLATFORM_NAME, "搜索请求被重定向到登录页")
                if data is not None and int(data.get("flag") or 0) == 1:
                    return data
                if data is not None:
                    logger.warning(
                        "猎聘第 %s 页返回 flag=%s msg=%s（第 %s 次）",
                        page_index, data.get("flag"), data.get("msg"), attempt + 1,
                    )
                    if edge_login.is_login_lost_text(str(data.get("msg") or "")):
                        edge_login.mark_login_lost(
                            PLATFORM_NAME, f"搜索接口提示：{str(data.get('msg'))[:40]}"
                        )
            if attempt < 1:
                await random_delay(5.0, 12.0)
                task_control.raise_if_cancelled()
        return None

    def _city_code(self, city: str) -> str | None:
        code = CITY_CODES.get(city.strip())
        if not code:
            logger.warning("猎聘暂无「%s」的城市代码，跳过该城市", city)
        return code

    async def search_jobs(self, query: JobQuery) -> list[Job]:
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
        detail_tab: dict | None = None
        body_fetched = 0
        body_budget = max(1, min(40, query.max_jobs * 2)) if query.verify_body else 0
        # 详情页配额：把总预算摊到每个关键词（上限 8 份/词）——猎聘/51job 风控对「详情页连发」最敏感
        per_kw_body_quota = min(8, max(2, body_budget // max(1, len(keywords)))) if body_budget else 0
        body_dropped = 0
        body_rescued = 0

        for kw_idx, keyword in enumerate(keywords):
            task_control.raise_if_cancelled()
            if kw_idx > 0:
                logger.info("关键词间隔停顿 %.0f-%.0f 秒…", query.kw_delay_min, query.kw_delay_max)
                await keyword_delay(query.kw_delay_min, query.kw_delay_max)
                task_control.raise_if_cancelled()   # 停止后不再发出新请求
            self.note(f"正在搜索「{keyword}」（第 {kw_idx + 1}/{len(keywords)} 个关键词）")
            kw_body_used = 0   # 本关键词的正文配额计数（详情访问均匀摊开，避免连发）
            for city in query.cities:
                dq = self._city_code(city)
                if not dq:
                    continue
                if tab is None:
                    tab = await self._ensure_tab(SEARCH_PAGE.format(kw=quote(keyword), dq=dq))
                    if tab is None:
                        raise RuntimeError("无法获得浏览器标签页（Edge 实例不可用），本平台采集中断")
                    await random_delay(2.0, 4.0)
                for page_no in range(1, query.max_pages + 1):
                    task_control.raise_if_cancelled()
                    if first_fetch_done:
                        await page_delay(query.page_delay_min, query.page_delay_max)
                    task_control.raise_if_cancelled()   # 停止后不再发出新的页面请求
                    first_fetch_done = True
                    await self._goto_search(tab, keyword, dq)
                    data = await self._fetch_page(tab["id"], keyword, dq, page_no - 1)
                    if data is None:
                        # 可疑失败：命中安全验证 → 跳过该平台（抛错）；否则继续原有处理
                        await verify.check_and_skip(tab["id"], self.display_name)
                    if data is None:
                        # 僵尸标签自愈：网络探活失败 → 关掉重开再试一次（与投递路径同款防护）
                        if await browser.page_network_alive(browser.SYSTEM_KEY, tab["id"]) is False:
                            logger.warning("猎聘标签页网络无响应（僵尸化），重开标签页重试")
                            self.note("标签页网络无响应，正在重开标签页…")
                            _surl = SEARCH_PAGE.format(kw=quote(keyword), dq=dq)
                            await browser.replace_tab(browser.SYSTEM_KEY, tab["id"], _surl)
                            await task_control.cancellable_sleep(2.0)
                            tab = await self._ensure_tab(_surl)
                            if tab is None:
                                raise RuntimeError("无法获得浏览器标签页（Edge 实例不可用），本平台采集中断")
                            await self._goto_search(tab, keyword, dq)
                            data = await self._fetch_page(tab["id"], keyword, dq, page_no)
                    if data is None:
                        logger.error("猎聘「%s」/ %s 第 %s 页失败，跳过", keyword, city, page_no)
                        self.note(f"「{keyword}」·{city} 第 {page_no} 页请求失败，跳过")
                        continue
                    dd = (data.get("data") or {}).get("data") or {}
                    items = dd.get("jobCardList") or []
                    if not items:
                        if page_no == 1:
                            # 首页空结果：命中安全验证 → 跳过该平台；未命中 → 按原逻辑结束翻页
                            await verify.check_and_skip(tab["id"], self.display_name)
                        if not items:
                            logger.info("猎聘「%s」/ %s 第 %s 页无结果，停止翻页", keyword, city, page_no)
                            self.note(f"「{keyword}」·{city} 第 {page_no} 页无更多结果，换下一组")
                            break
                    page_kept = 0
                    page_dropped = 0
                    for it in items:
                        job = _parse_api_job(it)
                        if job is None:
                            continue
                        scanned_total += 1
                        if job.platform_job_id in seen_ids:
                            continue
                        # —— 关键词裁决：标题 / 标签 /（可选）正文 任一命中即可 ——
                        list_hit = keyword_in_list_fields(job, keyword)
                        if query.verify_body:
                            if not passes_filters(job, keyword, query, check_keyword=False):
                                page_dropped += 1
                                continue
                            verdict = list_hit
                            if body_fetched < body_budget and kw_body_used < per_kw_body_quota:
                                if detail_tab is None:
                                    detail_tab = await ensure_detail_tab("liepin.com", tab["id"], job.url)
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
                                    kw_body_used += 1
                                    _text = await fetch_detail_text(
                                        detail_tab, job.url, job.platform_job_id, _DETAIL_SELECTORS, humanize=True
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
                                    if body_fetched % 4 == 0 and body_fetched < body_budget:
                                        self.note("正文抓取节奏：长休息一次（每 4 份一次，模拟人工停顿）…")
                                        await random_delay(18.0, 45.0)
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
                        "猎聘「%s」/ %s 第 %s 页：命中 %s 个，过滤 %s 个",
                        keyword, city, page_no, page_kept, page_dropped,
                    )
                    self.note(
                        f"「{keyword}」·{city} 第 {page_no} 页：命中 {page_kept} 个"
                        f"（累计新增 {new_count}/{query.max_jobs}）"
                    )
                    if new_count >= query.max_jobs:
                        self.note(f"已达到本轮新增上限 {query.max_jobs} 个，停止采集")
                        break
                    if len(items) < 10:      # 末页特征
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

    async def _goto_detail(self, tab: dict, url: str) -> bool:
        if url.split("?")[0] not in (tab.get("url") or ""):
            await browser.raw_navigate(browser.SYSTEM_KEY, tab["id"], url)
        js = (
            "(() => {"
            "  const btns = [...document.querySelectorAll('a,button,div,span')].filter(b => b.offsetWidth || b.offsetHeight);"
            "  const canApply = btns.some(b => (b.innerText || '').trim() === '投简历');"
            "  const applied = btns.some(b => (b.innerText || '').trim() === '继续聊');"
            "  const chatOnly = btns.some(b => (b.innerText || '').trim() === '聊一聊');"
            "  return JSON.stringify({r: document.readyState, canApply, applied, chatOnly});"
            "})()"
        )
        for _ in range(18):
            await task_control.cancellable_sleep(1.2)
            raw = await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], js)
            if not raw:
                continue
            try:
                st = json.loads(raw)
            except ValueError:
                continue
            if st.get("canApply") or st.get("applied") or st.get("chatOnly"):
                await task_control.cancellable_sleep(random.uniform(1.0, 1.8))
                return True
        return False

    async def apply(self, job: Job, greeting: str | None = None) -> ApplyResult:
        """投递猎聘岗位：详情页「投简历」→ 弹窗「立即投递」→ apply/success 即成功。"""
        if not await self.check_login():
            return ApplyResult(success=False, message="登录态失效，请重新登录")

        tab = await self._ensure_tab(job.url)
        if tab is None:
            return ApplyResult(success=False, message="无法获得猎聘标签页")
        if not await self._goto_detail(tab, job.url):
            # 失败路径先探测验证墙：整页拦截时页面上没有按钮，
            # 这里是识别「需要验证」的最后机会（否则误报超时、平台不被跳过、白跑重试）
            if await verify.detect(tab["id"]):
                return ApplyResult(success=False, message="平台触发安全验证（验证码），已跳过本平台", need_verify=True)
            return ApplyResult(success=False, message="职位详情页加载超时（未找到投递按钮）")

        # 预检：标签页网络僵尸化（后台停留过久）→ 关掉重开
        if await browser.page_network_alive(browser.SYSTEM_KEY, tab["id"]) is False:
            logger.warning("猎聘标签页网络无响应，重开标签页重试")
            await browser.replace_tab(browser.SYSTEM_KEY, tab["id"], job.url)
            await task_control.cancellable_sleep(2.0)
            tab = await self._ensure_tab(job.url)
            if tab is None or not await self._goto_detail(tab, job.url):
                if tab is not None and await verify.detect(tab["id"]):
                    return ApplyResult(success=False, message="平台触发安全验证（验证码），已跳过本平台", need_verify=True)
                return ApplyResult(success=False, message="标签页重建失败，请稍后重试")

        # 安全验证检测：详情页若为验证码页 → 标记需验证（调度层跳过该平台，其余平台继续）
        if await verify.detect(tab["id"]):
            return ApplyResult(success=False, message="平台触发安全验证（验证码），已跳过本平台", need_verify=True)

        # 第 1 步：状态判定
        state_js = (
            "(() => {"
            "  const btns = [...document.querySelectorAll('a,button,div,span')].filter(b => b.offsetWidth || b.offsetHeight);"
            "  if (btns.some(b => (b.innerText || '').trim() === '继续聊')) return 'applied';"
            "  if (btns.some(b => (b.innerText || '').trim() === '投简历')) return 'can-apply';"
            "  if (btns.some(b => (b.innerText || '').trim() === '聊一聊')) return 'chat-only';"
            "  return 'no-button';"
            "})()"
        )
        state = await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], state_js, timeout_s=15)
        if state == "applied":
            return ApplyResult(success=True, message="此前已投递（跳过重复投递）")
        if state == "chat-only":
            # 该岗位无「投简历」，仅支持「聊一聊」：点击后平台会自动发出招呼语
            chat_js = (
                "(() => {"
                "  const btn = [...document.querySelectorAll('a.btn-main, a.btn-chat')]"
                "    .find(e => (e.innerText || '').trim() === '聊一聊' && (e.offsetWidth || e.offsetHeight));"
                "  if (!btn) return 'no-btn';"
                "  btn.click();"
                "  return 'clicked';"
                "})()"
            )
            res = await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], chat_js, timeout_s=15)
            if res != "clicked":
                return ApplyResult(success=False, message="点击「聊一聊」失败")
            await task_control.cancellable_sleep(random.uniform(2.5, 3.5))
            check_js = (
                "(() => {"
                "  const modal = document.querySelector('.ant-im-modal-wrap, .im-ui-chat-container');"
                "  const ok = !!modal && !!(modal.offsetWidth || modal.offsetHeight);"
                "  return JSON.stringify({ok});"
                "})()"
            )
            for _ in range(8):
                raw = await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], check_js, timeout_s=10)
                if raw:
                    try:
                        st = json.loads(raw)
                    except ValueError:
                        st = {}
                    if st.get("ok"):
                        return ApplyResult(success=True, message="已发起沟通（该岗位仅支持聊一聊，平台招呼语已送达）")
                await task_control.cancellable_sleep(1.2)
            return ApplyResult(success=False, message="「聊一聊」会话未打开")
        if state != "can-apply":
            return ApplyResult(success=False, message="未找到「投简历」按钮")

        # 第 2 步：点击「投简历」（弹窗在原页打开，不跳转）
        click_js = (
            "(() => {"
            "  const btn = [...document.querySelectorAll('a.btn-minor, a')].find(e => (e.innerText || '').trim() === '投简历' && (e.offsetWidth || e.offsetHeight));"
            "  if (!btn) return 'no-btn';"
            "  btn.click();"
            "  return 'clicked';"
            "})()"
        )
        res = await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], click_js, timeout_s=15)
        if res != "clicked":
            return ApplyResult(success=False, message="点击「投简历」失败")

        # 第 3 步：等弹窗出现并点「立即投递」（标签页延迟时重试；偶尔重开弹窗）
        modal_state_js = (
            "(() => {"
            "  const btns = [...document.querySelectorAll('a,button,div,span')].filter(e => (e.offsetWidth || e.offsetHeight));"
            "  if (btns.some(e => (e.innerText || '').trim() === '继续聊')) return 'applied';"
            "  const cands = btns.filter(e => (e.innerText || '').trim() === '立即投递');"
            "  if (cands.length) { cands[cands.length - 1].click(); return 'clicked:' + cands.length; }"
            "  return 'wait';"
            "})()"
        )
        modal_clicked = False
        for i in range(10):
            await task_control.cancellable_sleep(1.8)
            state_raw = await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], modal_state_js, timeout_s=12)
            if state_raw and str(state_raw).startswith("clicked"):
                modal_clicked = True
                break
            if state_raw == "applied":
                return ApplyResult(success=True, message="已投递（平台侧已确认）")
            if i in (3, 6):      # 弹窗迟迟不出现：再点一次「投简历」
                await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], click_js, timeout_s=15)
        if not modal_clicked:
            await task_control.cancellable_sleep(4.0)
            state_raw = await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], modal_state_js, timeout_s=12)
            if state_raw and str(state_raw).startswith("clicked"):
                modal_clicked = True
            elif state_raw == "applied":
                return ApplyResult(success=True, message="已投递（平台侧已确认）")

        # 第 4 步：轮询成功（apply/success 跳转 / 「投递成功」 / 「继续聊」出现）
        await task_control.cancellable_sleep(random.uniform(1.5, 2.5))
        check_js = (
            "(() => {"
            "  const u = location.href;"
            "  const applied = [...document.querySelectorAll('a,button,div,span')]"
            "    .some(e => (e.offsetWidth || e.offsetHeight) && (e.innerText || '').trim() === '继续聊');"
            "  const ok = u.includes('/job/apply/success') || document.body.innerText.includes('投递成功') || applied;"
            "  return JSON.stringify({u: u.slice(0, 120), ok});"
            "})()"
        )
        for _ in range(12):
            raw = await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], check_js, timeout_s=10)
            if raw:
                try:
                    st = json.loads(raw)
                except ValueError:
                    st = {}
                if st.get("ok"):
                    return ApplyResult(success=True, message="已投递（简历已送达）")
            await task_control.cancellable_sleep(1.5)
        # 最终复核：标签页延迟时请求可能在等待期间才完成
        await task_control.cancellable_sleep(4.0)
        raw = await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], check_js, timeout_s=10)
        if raw:
            try:
                st = json.loads(raw)
            except ValueError:
                st = {}
            if st.get("ok"):
                return ApplyResult(success=True, message="已投递（响应超时，平台侧已确认）")
        return ApplyResult(success=False, message="投递结果未确认（未出现成功页）")


register(LiepinPlatform())
