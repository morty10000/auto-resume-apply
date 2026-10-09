"""智联招聘（zhaopin.com）平台适配器。

架构与 Boss 相同：系统 Edge 原生标签 + 页面内请求（raw CDP，零 Playwright 挂接）。

采集（2026-10 实测）：
- 页面内 POST fe-api.zhaopin.com/c/i/search/positions（响应即全量字段，含岗位编号）
- 请求参数：at/rt（登录令牌，从 cookie 读）作为查询串；body 带关键词/城市/页码/cvNumber
- 薪资为纯文本（salary60 如「8000-15000元」），字段无字体混淆

投递（2026-10 实测）：
- 原生标签打开职位详情页 → 点击站点自带的「立即投递」按钮 → 站点完成
  「投递简历 + 发送招呼语」，弹窗「已向对方发送简历和打招呼语」即成功
- 已投递岗位按钮显示「继续沟通」→ 跳过，防重复投递
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

PLATFORM_NAME = "zhilian"

BASE_URL = "https://www.zhaopin.com"
API_SEARCH = "https://fe-api.zhaopin.com/c/i/search/positions"

AUTH_COOKIE_HINTS = ("at", "rt")

# 城市 → 城市代码（2026-10 从站点 base/data 接口提取）
CITY_CODES = {
    "北京": "530", "上海": "538", "广州": "763", "深圳": "765", "天津": "531",
    "武汉": "736", "西安": "854", "成都": "801", "南京": "635", "杭州": "653",
    "苏州": "639", "重庆": "551", "郑州": "719", "长沙": "749", "东莞": "779",
    "佛山": "768",
}

PAGE_SIZE = 20


def _build_search_js(keyword: str, city_code: str, page_index: int) -> str:
    """页面内调用智联搜索接口（站点同款请求：at/rt 走查询串，body 带条件）。"""
    return (
        "(async () => {"
        "  try {"
        "    const ck = document.cookie || '';"
        "    const at = (ck.match(/(?:^|; )at=([^;]+)/) || [])[1] || '';"
        "    const rt = (ck.match(/(?:^|; )rt=([^;]+)/) || [])[1] || '';"
        "    const st = window.__INITIAL_STATE__ || {};"
        "    const body = {"
        "      S_SOU_FULL_INDEX: %s, S_SOU_WORK_CITY: %s, order: 0,"
        "      actionid: (crypto.randomUUID ? crypto.randomUUID() : String(Date.now())),"
        "      pageSize: %d, pageIndex: %d, cvNumber: st.resumeNumber || ''"
        "    };"
        "    const u = '%s?at=' + encodeURIComponent(at) + '&rt=' + encodeURIComponent(rt)"
        "      + '&platform=13&version=0.0.0&_v=' + Math.random();"
        "    const r = await fetch(u, { method: 'POST', headers: {'Content-Type': 'application/json'},"
        "      credentials: 'include', body: JSON.stringify(body) });"
        "    return await r.text();"
        "  } catch (e) { return JSON.stringify({code: -1, message: 'fetch_error:' + e}); }"
        "})()" % (json.dumps(keyword), json.dumps(city_code), PAGE_SIZE, page_index, API_SEARCH)
    )


def _parse_api_job(it: dict) -> Job | None:
    """把智联搜索接口条目映射为标准化 Job。"""
    number = it.get("number")
    title = it.get("name")
    if not number or not title:
        return None
    city = "·".join(x for x in [it.get("workCity"), it.get("cityDistrict")] if x) or None
    skills = [
        s.get("value") for s in (it.get("skillLabel") or [])
        if isinstance(s, dict) and s.get("value")
    ]
    staff = it.get("staffCard") or {}
    return Job(
        platform=PLATFORM_NAME,
        platform_job_id=str(number),
        title=str(title),
        company=it.get("companyName") or "",
        salary=it.get("salary60"),
        city=city,
        url=f"{BASE_URL}/jobdetail/{number}.htm",
        extra={
            "city_id": it.get("cityId"),
            "experience": it.get("workingExp"),
            "degree": it.get("education"),
            "skills": skills,
            "labels": skills[:6],
            "welfare": list(it.get("welfareLabel") or []),
            "boss_online": bool(staff.get("hrOnlineState")),
            "hr_name": staff.get("staffName"),
            "brand_industry": it.get("industryName"),
            "brand_scale": it.get("companySize"),
            "brand_stage": it.get("financingStage") or "",
            "hr_active_desc": (staff.get("hrStateInfo") or ("在线" if staff.get("hrOnlineState") else None)),
            "hr_active_days": (
                parse_active_days(staff.get("hrStateInfo"))
                if staff.get("hrStateInfo")
                else (0 if staff.get("hrOnlineState") else None)
            ),
            "publish_time": it.get("publishTime"),
            "staff_id": staff.get("id"),
        },
    )


# 详情页正文选择器（「正文校验」用；取最长文本块，差时按「职位描述」窗口兜底）
_DETAIL_SELECTORS = [".main-jobs__left-content", '[class*="describtion"]', '[class*="job-detail"]', ".zp-content"]

class ZhilianPlatform(BasePlatform):
    name = PLATFORM_NAME
    display_name = "智联招聘"

    # ------------------------------------------------------------ 登录态

    async def check_login(self) -> bool:
        """统一走 edge_login.check_platform_login（带过期校验 + 状态缓存同步）。"""
        return await edge_login.check_platform_login(PLATFORM_NAME)

    # ------------------------------------------------------------ 标签页

    async def _find_tab(self) -> dict | None:
        for t in browser.list_targets(browser.SYSTEM_KEY):
            if t.get("type") == "page" and "zhaopin.com" in (t.get("url") or ""):
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

    async def _goto_search(self, tab: dict, keyword: str, city_code: str) -> bool:
        """把标签导航到对应搜索页（页面语境与请求一致）。"""
        url = f"{BASE_URL}/jobs?jl={city_code}&kw={quote(keyword)}"
        cur = tab.get("url") or ""
        if f"jl={city_code}" in cur and f"kw={quote(keyword)}" in cur:
            return True
        await browser.raw_navigate(browser.SYSTEM_KEY, tab["id"], url)
        js = "JSON.stringify({r: document.readyState, u: location.href, n: document.querySelectorAll('.job-list-panel .job-card').length})"
        for i in range(16):
            task_control.raise_if_cancelled()
            await task_control.cancellable_sleep(1.2)
            raw = await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], js)
            if not raw:
                continue
            try:
                st = json.loads(raw)
            except ValueError:
                continue
            u = st.get("u") or ""
            if f"jl={city_code}" in u and st.get("r") == "complete" and (st.get("n") or 0) > 0:
                await task_control.cancellable_sleep(random.uniform(1.0, 2.0))
                return True
        return False

    # ------------------------------------------------------------ 采集

    async def _fetch_page(
        self, target_id: str, keyword: str, city_code: str, page_index: int
    ) -> dict | None:
        """请求单页岗位数据；失败重试一次（等待后）。"""
        for attempt in range(2):
            raw = await browser.raw_evaluate(
                browser.SYSTEM_KEY, target_id,
                _build_search_js(keyword, city_code, page_index), await_promise=True,
            )
            if raw is not None:
                try:
                    data = json.loads(raw)
                except ValueError:
                    data = None
                    if edge_login.is_login_lost_text(raw[:3000]):
                        edge_login.mark_login_lost(PLATFORM_NAME, "搜索请求被重定向到登录页")
                if data is not None and data.get("code") == 200:
                    return data
                if data is not None:
                    logger.warning(
                        "智联第 %s 页返回 code=%s msg=%s（第 %s 次）",
                        page_index, data.get("code"), data.get("message"), attempt + 1,
                    )
                    if edge_login.is_login_lost_text(str(data.get("message") or "")):
                        edge_login.mark_login_lost(
                            PLATFORM_NAME, f"搜索接口提示：{str(data.get('message'))[:40]}"
                        )
            if attempt < 1:
                await random_delay(5.0, 12.0)
                task_control.raise_if_cancelled()
        return None

    def _city_code(self, city: str) -> str | None:
        code = CITY_CODES.get(city.strip())
        if not code:
            logger.warning("智联暂无「%s」的城市代码，跳过该城市", city)
        return code

    async def search_jobs(self, query: JobQuery) -> list[Job]:
        """按 关键词 × 城市 采集智联岗位（接口分页，拟人限速）。"""
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
        body_dropped = 0
        body_rescued = 0

        for kw_idx, keyword in enumerate(keywords):
            task_control.raise_if_cancelled()
            if kw_idx > 0:
                logger.info("关键词间隔停顿 %.0f-%.0f 秒…", query.kw_delay_min, query.kw_delay_max)
                await keyword_delay(query.kw_delay_min, query.kw_delay_max)
                task_control.raise_if_cancelled()   # 停止后不再发出新请求
            self.note(f"正在搜索「{keyword}」（第 {kw_idx + 1}/{len(keywords)} 个关键词）")
            for city in query.cities:
                city_code = self._city_code(city)
                if not city_code:
                    continue
                if tab is None:
                    first_url = f"{BASE_URL}/jobs?jl={city_code}&kw={quote(keyword)}"
                    tab = await self._ensure_tab(first_url)
                    if tab is None:
                        raise RuntimeError("无法获得浏览器标签页（Edge 实例不可用），本平台采集中断")
                    await random_delay(2.0, 4.0)
                for page_no in range(1, query.max_pages + 1):
                    task_control.raise_if_cancelled()
                    if first_fetch_done:
                        await page_delay(query.page_delay_min, query.page_delay_max)
                    task_control.raise_if_cancelled()   # 停止后不再发出新的页面请求
                    first_fetch_done = True
                    await self._goto_search(tab, keyword, city_code)
                    data = await self._fetch_page(tab["id"], keyword, city_code, page_no)
                    if data is None:
                        # 可疑失败：命中安全验证 → 跳过该平台（抛错）；否则继续原有处理
                        await verify.check_and_skip(tab["id"], self.display_name)
                    if data is None:
                        # 僵尸标签自愈：网络探活失败 → 关掉重开再试一次（与投递路径同款防护）
                        if await browser.page_network_alive(browser.SYSTEM_KEY, tab["id"]) is False:
                            logger.warning("智联标签页网络无响应（僵尸化），重开标签页重试")
                            self.note("标签页网络无响应，正在重开标签页…")
                            _surl = f"{BASE_URL}/jobs?jl={city_code}&kw={quote(keyword)}"
                            await browser.replace_tab(browser.SYSTEM_KEY, tab["id"], _surl)
                            await task_control.cancellable_sleep(2.0)
                            tab = await self._ensure_tab(_surl)
                            if tab is None:
                                raise RuntimeError("无法获得浏览器标签页（Edge 实例不可用），本平台采集中断")
                            await self._goto_search(tab, keyword, city_code)
                            data = await self._fetch_page(tab["id"], keyword, city_code, page_no)
                    if data is None:
                        logger.error("智联「%s」/ %s 第 %s 页失败，跳过", keyword, city, page_no)
                        self.note(f"「{keyword}」·{city} 第 {page_no} 页请求失败，跳过")
                        continue
                    zp = data.get("data") or {}
                    items = zp.get("list") or []
                    if not items:
                        if page_no == 1:
                            # 首页空结果：命中安全验证 → 跳过该平台；未命中 → 按原逻辑结束翻页
                            await verify.check_and_skip(tab["id"], self.display_name)
                        if not items:
                            logger.info("智联「%s」/ %s 第 %s 页无结果，停止翻页", keyword, city, page_no)
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
                            if body_fetched < body_budget:
                                if detail_tab is None:
                                    detail_tab = await ensure_detail_tab("zhaopin.com", tab["id"], job.url)
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
                        "智联「%s」/ %s 第 %s 页：命中 %s 个，过滤 %s 个",
                        keyword, city, page_no, page_kept, page_dropped,
                    )
                    self.note(
                        f"「{keyword}」·{city} 第 {page_no} 页：命中 {page_kept} 个"
                        f"（累计新增 {new_count}/{query.max_jobs}）"
                    )
                    if new_count >= query.max_jobs:
                        self.note(f"已达到本轮新增上限 {query.max_jobs} 个，停止采集")
                        break
                    if zp.get("isEndPage") or len(items) < PAGE_SIZE:
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
        """导航到职位详情页并等待投递按钮出现。"""
        if url not in (tab.get("url") or ""):
            await browser.raw_navigate(browser.SYSTEM_KEY, tab["id"], url)
        js = (
            "(() => {"
            "  const btns = [...document.querySelectorAll('button,a')].filter(b => b.offsetWidth || b.offsetHeight);"
            "  const apply = btns.some(b => (b.innerText || '').includes('立即投递'));"
            "  const chat = btns.some(b => (b.innerText || '').includes('继续沟通'));"
            "  return JSON.stringify({r: document.readyState, apply, chat});"
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
            if st.get("apply") or st.get("chat"):
                await task_control.cancellable_sleep(random.uniform(1.0, 2.0))
                return True
        return False

    async def apply(self, job: Job, greeting: str | None = None) -> ApplyResult:
        """投递智联岗位：详情页点击「立即投递」（站点自行发简历+招呼语）。"""
        if not await self.check_login():
            return ApplyResult(success=False, message="登录态失效，请重新登录")

        tab = await self._ensure_tab(job.url)
        if tab is None:
            return ApplyResult(success=False, message="无法获得智联标签页")
        if not await self._goto_detail(tab, job.url):
            # 失败路径先探测验证墙：整页拦截时页面上没有按钮，
            # 这里是识别「需要验证」的最后机会（否则误报超时、平台不被跳过、白跑重试）
            if await verify.detect(tab["id"]):
                return ApplyResult(success=False, message="平台触发安全验证（验证码），已跳过本平台", need_verify=True)
            return ApplyResult(success=False, message="职位详情页加载超时（未找到投递按钮）")

        # 预检：标签页网络僵尸化（后台停留过久）→ 关掉重开
        if await browser.page_network_alive(browser.SYSTEM_KEY, tab["id"]) is False:
            logger.warning("智联标签页网络无响应，重开标签页重试")
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

        click_js = (
            "(() => {"
            "  const btns = [...document.querySelectorAll('button,a')].filter(b => b.offsetWidth || b.offsetHeight);"
            "  const chat = btns.find(b => (b.innerText || '').includes('继续沟通'));"
            "  if (chat) return 'already';"
            "  const apply = btns.find(b => (b.innerText || '').includes('立即投递'));"
            "  if (!apply) return 'no-button';"
            "  apply.click();"
            "  return 'clicked';"
            "})()"
        )
        res = await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], click_js)
        if res == "already":
            return ApplyResult(success=True, message="此前已投递（跳过重复投递）")
        if res != "clicked":
            return ApplyResult(success=False, message="未找到「立即投递」按钮")

        await task_control.cancellable_sleep(random.uniform(3.0, 4.5))
        result_js = (
            "(() => {"
            "  const texts = [];"
            "  for (const sel of ['.deliver-greeting-modal', '[class*=dialog]', '[class*=modal]', '[class*=toast]', '[class*=message]']) {"
            "    for (const e of document.querySelectorAll(sel)) {"
            "      if ((e.offsetWidth || e.offsetHeight) && (e.innerText || '').trim()) texts.push(e.innerText.replace(/\\n+/g, ' | ').slice(0, 120));"
            "    }"
            "  }"
            "  return JSON.stringify([...new Set(texts)].slice(0, 5));"
            "})()"
        )
        raw = await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], result_js)
        texts: list[str] = []
        if raw:
            try:
                texts = json.loads(raw)
            except ValueError:
                texts = []
        joined = " ".join(texts)
        if "已向对方发送" in joined:
            return ApplyResult(success=True, message="已投递（简历 + 招呼语已送达）")
        if any(w in joined for w in ("已经投递", "重复投递", "已投递过")):
            return ApplyResult(success=True, message="此前已投递（跳过重复投递）")
        # 最终复核：标签页延迟时投递可能刚完成（按钮已变「继续沟通」）
        await task_control.cancellable_sleep(4.0)
        confirm_js = (
            "(() => {"
            "  const btns = [...document.querySelectorAll('button,a,div,span')].filter(b => b.offsetWidth || b.offsetHeight);"
            "  return btns.some(b => (b.innerText || '').trim() === '继续沟通') ? 'yes' : 'no';"
            "})()"
        )
        confirmed = await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], confirm_js, timeout_s=10)
        if confirmed == "yes":
            return ApplyResult(success=True, message="已投递（响应超时，平台侧已确认建立沟通）")
        if texts:
            return ApplyResult(success=False, message=f"投递未确认（页面提示：{texts[0][:80]}）")
        return ApplyResult(success=False, message="投递结果未确认（未捕获到成功弹窗）")


register(ZhilianPlatform())
