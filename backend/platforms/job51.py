"""51job（前程无忧）平台适配器。

架构与 Boss 相同：系统 Edge 原生标签 + 页面内请求（raw CDP，无 Playwright 挂接）。

采集（2026-10 实测）：
- 页面内 GET we.51job.com/api/job/search-pc（站点同款参数；实测 decode__1048 签名可省略）
- 字段全量返回（jobId/jobName/provideSalaryString/jobTags/jobAreaLevelDetail 等）
- 薪资为纯文本（如「1-1.8万」「7.5千-1.5万」）

投递（2026-10 实测）：
- 详情页点击「立即投递」→ 跳转 jobs.51job.com/applysuccess.php?jobid=XXX 即成功
- 已投递岗位按钮变为「已投递」（disabled）→ 跳过
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from urllib.parse import quote

from backend.core import task_control
from backend.core.job_filter import body_matches_keyword, keyword_in_list_fields, parse_active_days, passes_filters
from backend.core.throttle import keyword_delay, page_delay, random_delay
from backend.services import browser, edge_login, verify

from .base import ApplyResult, BasePlatform, Job, JobQuery
from .detail import ensure_detail_tab, fetch_detail_text
from .registry import register

logger = logging.getLogger(__name__)

PLATFORM_NAME = "job51"

BASE_URL = "https://we.51job.com"
SEARCH_PAGE = BASE_URL + "/pc/search?jobArea={area}&keyword={kw}"
API_SEARCH = "https://we.51job.com/api/job/search-pc"

# 登录特征（2026-10-02 对照实验校准：全新匿名 profile 无此 cookie，仅登录后出现；
# 旧特征「51job」cookie 已被站点弃用 → 会导致已登录误报失效）
AUTH_COOKIE_HINTS = ("_c_WBKFRo",)

# 城市 → 城市代码（2026-10 逐个实测验证）
CITY_CODES = {
    "北京": "010000", "上海": "020000", "广州": "030200", "深圳": "040000",
    "天津": "050000", "重庆": "060000", "南京": "070200", "苏州": "070300",
    "杭州": "080200", "成都": "090200", "武汉": "180200", "西安": "200200",
    "佛山": "030600", "东莞": "030800",
}

PAGE_SIZE = 20


def _build_search_js(keyword: str, city_code: str, page_index: int) -> str:
    """页面内调用 51job 搜索接口（去签名版，实测可用）。"""
    return (
        "(async () => {"
        "  try {"
        "    const u = '%s?api_key=51job&timestamp=' + Math.floor(Date.now()/1000)"
        "      + '&keyword=' + encodeURIComponent(%s)"
        "      + '&searchType=2&function=&industry=&jobArea=%s'"
        "      + '&jobArea2=&landmark=&metro=&salary=&workYear=&degree=&companyType=&companySize='"
        "      + '&jobType=&issueDate=&sortType=0&pageNum=%d&requestId=&pageSize=%d&source=1'"
        "      + '&pageCode=' + encodeURIComponent('sou|sou|soulb') + '&scene=7';"
        "    const r = await fetch(u, { credentials: 'include' });"
        "    return await r.text();"
        "  } catch (e) { return JSON.stringify({status: '-1', message: 'fetch_error:' + e}); }"
        "})()" % (API_SEARCH, json.dumps(keyword), city_code, page_index, PAGE_SIZE)
    )


# 头像旁活跃文案提取（joblist-item-jobinfo 文本块里挑活跃片段；标题去空格归一）
_ACTIVE_LABELS_JS = (
    "(() => {"
    "  const out = [];"
    "  const reAct = /活跃|在线|回复|搜人才|面试邀请|分钟前/;"
    "  for (const el of document.querySelectorAll('[class*=joblist-item-jobinfo]')) {"
    "    const raw = (el.innerText || '').trim().replace(/\\s+/g, ' ');"
    "    if (!raw || raw.length > 40) continue;"
    "    const frags = raw.split(' ').filter(x => reAct.test(x));"
    "    if (!frags.length) continue;"
    "    const label = frags.join(' ').slice(0, 24);"
    "    let root = null, n = el;"
    "    for (let i = 0; i < 6 && n; i++) {"
    "      n = n.parentElement; if (!n) break;"
    "      if (n.querySelector('a[href*=\"jobs.51job.com\"]')) { root = n; break; }"
    "    }"
    "    let title = null;"
    "    if (root) {"
    "      const a = root.querySelector('.jname') || root.querySelector('[class*=jname]') || root.querySelector('a[href*=\"jobs.51job.com\"]');"
    "      title = a ? (a.innerText || '').trim().replace(/\\s+/g, '') : null;"
    "    }"
    "    if (title) out.push({ title: title, label: label });"
    "  }"
    "  return JSON.stringify(out.slice(0, 60));"
    "})()"
)


def _norm_title(t) -> str:
    return re.sub(r"\s+", "", str(t or ""))


def _split_tags(tags: list[str]) -> tuple[str | None, str | None, list[str], list[str]]:
    """把 jobTags 拆成 经验 / 学历 / 技能 / 福利。"""
    exp = deg = None
    skills: list[str] = []
    welfare: list[str] = []
    welfare_words = ("五险", "双休", "假期", "奖金", "礼金", "团建", "体检", "补贴", "年金", "工资", "培训", "福利", "节日", "旅游", "零食", "餐补", "住房", "班车", "期权", "股票", "带薪", "住宿", "保险", "加班", "全勤")
    for t in tags or []:
        t = str(t).strip()
        if not t:
            continue
        if exp is None and (("年" in t and ("以上" in t or "经验" in t or "-" in t)) or t in ("应届生", "无经验")):
            exp = t
            continue
        if deg is None and t in ("初中及以下", "中专", "高中", "大专", "本科", "硕士", "博士", "学历不限"):
            deg = t
            continue
        if any(w in t for w in welfare_words):
            welfare.append(t)
            continue
        skills.append(t)
    return exp, deg, skills, welfare


def _parse_api_job(it: dict) -> Job | None:
    """把 51job 搜索接口条目映射为标准化 Job。"""
    job_id = it.get("jobId")
    title = it.get("jobName")
    if not job_id or not title:
        return None
    href = it.get("jobHref") or ""
    url = href.split("?")[0] or (
        f"https://jobs.51job.com/{it.get('hrefAreaPinYin') or 'job'}/{job_id}.html"
    )
    exp, deg, skills, welfare = _split_tags(it.get("jobTags") or [])
    detail = it.get("jobAreaLevelDetail") or {}
    return Job(
        platform=PLATFORM_NAME,
        platform_job_id=str(job_id),
        title=str(title),
        company=it.get("fullCompanyName") or it.get("companyName") or "",
        salary=it.get("provideSalaryString"),
        city=it.get("jobAreaString") or detail.get("cityString"),
        url=url,
        description=(str(it.get("jobDescribe"))[:4000] if it.get("jobDescribe") else None),
        extra={
            "city_id": it.get("jobAreaCode"),
            "experience": it.get("workYearString") or exp,
            "degree": it.get("degreeString") or deg,
            "skills": skills,
            "labels": (it.get("jobTags") or [])[:8],
            "welfare": welfare,
            "boss_online": bool(it.get("hrIsOnline") or it.get("isOnline")),
            "hr_name": it.get("hrName"),
            "brand_industry": it.get("companyIndustryType1Str") or it.get("industryType1Str"),
            "brand_scale": it.get("companySizeString"),
            "brand_stage": "",
            "hr_active_desc": "在线" if (it.get("hrIsOnline") or it.get("isOnline")) else None,
            "hr_active_days": 0 if (it.get("hrIsOnline") or it.get("isOnline")) else None,
            "publish_time": it.get("issueDateString") or it.get("updateDateTime"),
        },
    )


# 详情页正文选择器（「正文校验」用；取最长文本块，差时按「职位描述」窗口兜底）
_DETAIL_SELECTORS = ['[class*="job_msg"]', '[class*="bmsg"]', '[class*="job-detail"]', ".tCompany_main"]

class Job51Platform(BasePlatform):
    name = PLATFORM_NAME
    display_name = "51job"

    # ------------------------------------------------------------ 登录态

    async def check_login(self) -> bool:
        """统一走 edge_login.check_platform_login（带过期校验 + 状态缓存同步）。"""
        return await edge_login.check_platform_login(PLATFORM_NAME)

    # ------------------------------------------------------------ 标签页

    async def _find_tab(self) -> dict | None:
        for t in browser.list_targets(browser.SYSTEM_KEY):
            if t.get("type") == "page" and "51job.com" in (t.get("url") or ""):
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

    async def _goto_search(self, tab: dict, keyword: str, area_code: str) -> bool:
        """把标签导航到对应搜索页（页面语境与请求一致）。"""
        url = SEARCH_PAGE.format(area=area_code, kw=quote(keyword))
        cur = tab.get("url") or ""
        if f"jobArea={area_code}" in cur and f"keyword={quote(keyword)}" in cur:
            return True
        await browser.raw_navigate(browser.SYSTEM_KEY, tab["id"], url)
        js = "JSON.stringify({r: document.readyState, u: location.href, n: document.querySelectorAll('.j_joblist .j_job, .joblist .j_job, [class*=joblist] .j_job, .j_job').length})"
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
            u = st.get("u") or ""
            if f"jobArea={area_code}" in u and st.get("r") == "complete":
                await task_control.cancellable_sleep(random.uniform(1.0, 2.0))
                return True
        return False

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

    async def _fetch_page(
        self, target_id: str, keyword: str, area_code: str, page_index: int
    ) -> dict | None:
        for attempt in range(2):
            raw = await browser.raw_evaluate(
                browser.SYSTEM_KEY, target_id,
                _build_search_js(keyword, area_code, page_index), await_promise=True,
            )
            if raw is not None:
                try:
                    data = json.loads(raw)
                except ValueError:
                    data = None
                    if edge_login.is_login_lost_text(raw[:3000]):
                        edge_login.mark_login_lost(PLATFORM_NAME, "搜索请求被重定向到登录页")
                if data is not None and str(data.get("status")) == "1":
                    return data
                if data is not None:
                    logger.warning(
                        "51job 第 %s 页返回 status=%s msg=%s（第 %s 次）",
                        page_index, data.get("status"), data.get("message"), attempt + 1,
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
            logger.warning("51job 暂无「%s」的城市代码，跳过该城市", city)
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
                area_code = self._city_code(city)
                if not area_code:
                    continue
                if tab is None:
                    tab = await self._ensure_tab(SEARCH_PAGE.format(area=area_code, kw=quote(keyword)))
                    if tab is None:
                        raise RuntimeError("无法获得浏览器标签页（Edge 实例不可用），本平台采集中断")
                    await random_delay(2.0, 4.0)
                for page_no in range(1, query.max_pages + 1):
                    task_control.raise_if_cancelled()
                    if first_fetch_done:
                        await page_delay(query.page_delay_min, query.page_delay_max)
                    task_control.raise_if_cancelled()   # 停止后不再发出新的页面请求
                    first_fetch_done = True
                    await self._goto_search(tab, keyword, area_code)
                    active_map = await self._extract_active_labels(tab)
                    data = await self._fetch_page(tab["id"], keyword, area_code, page_no)
                    if data is None:
                        # 可疑失败：命中安全验证 → 跳过该平台（抛错）；否则继续原有处理
                        await verify.check_and_skip(tab["id"], self.display_name)
                    if data is None:
                        # 僵尸标签自愈：网络探活失败 → 关掉重开再试一次（与投递路径同款防护）
                        if await browser.page_network_alive(browser.SYSTEM_KEY, tab["id"]) is False:
                            logger.warning("51job 标签页网络无响应（僵尸化），重开标签页重试")
                            self.note("标签页网络无响应，正在重开标签页…")
                            _surl = SEARCH_PAGE.format(area=area_code, kw=quote(keyword))
                            await browser.replace_tab(browser.SYSTEM_KEY, tab["id"], _surl)
                            await task_control.cancellable_sleep(2.0)
                            tab = await self._ensure_tab(_surl)
                            if tab is None:
                                raise RuntimeError("无法获得浏览器标签页（Edge 实例不可用），本平台采集中断")
                            await self._goto_search(tab, keyword, area_code)
                            data = await self._fetch_page(tab["id"], keyword, area_code, page_no)
                    if data is None:
                        logger.error("51job「%s」/ %s 第 %s 页失败，跳过", keyword, city, page_no)
                        self.note(f"「{keyword}」·{city} 第 {page_no} 页请求失败，跳过")
                        continue
                    rb = data.get("resultbody") or {}
                    jw = rb.get("job") or {}
                    items = jw.get("items") or []
                    if not items:
                        if page_no == 1:
                            # 首页空结果：命中安全验证 → 跳过该平台；未命中 → 按原逻辑结束翻页
                            await verify.check_and_skip(tab["id"], self.display_name)
                        if not items:
                            logger.info("51job「%s」/ %s 第 %s 页无结果，停止翻页", keyword, city, page_no)
                            self.note(f"「{keyword}」·{city} 第 {page_no} 页无更多结果，换下一组")
                            break
                    page_kept = 0
                    page_dropped = 0
                    for it in items:
                        job = _parse_api_job(it)
                        if job is None:
                            continue
                        _lbl = active_map.get(_norm_title(job.title))
                        if _lbl:
                            job.extra["hr_active_desc"] = _lbl
                            job.extra["hr_active_days"] = parse_active_days(_lbl)
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
                            # 列表自带正文（jobDescribe）优先：已有正文 → 直接用于裁决 / 匹配，
                            # 不再单独访问详情页 —— 详情页访问是 51job 风控触发的主要来源，能省则省
                            if job.description:
                                if not verdict and body_matches_keyword(job.description, keyword):
                                    verdict = True
                                    body_rescued += 1
                                    logger.info(
                                        "正文救回（列表正文）：%s @ %s（标题/标签未中，正文命中「%s」）",
                                        job.title, job.company, keyword,
                                    )
                            elif body_fetched < body_budget and kw_body_used < per_kw_body_quota:
                                if detail_tab is None:
                                    detail_tab = await ensure_detail_tab("51job.com", tab["id"], job.url)
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
                        seen_ids.add(job.platform_job_id)
                        jobs.append(job)
                        page_kept += 1
                        if len(jobs) >= query.max_jobs:
                            break
                    filtered_total += page_dropped
                    logger.info(
                        "51job「%s」/ %s 第 %s 页：命中 %s 个，过滤 %s 个",
                        keyword, city, page_no, page_kept, page_dropped,
                    )
                    self.note(
                        f"「{keyword}」·{city} 第 {page_no} 页：命中 {page_kept} 个"
                        f"（累计 {len(jobs)}/{query.max_jobs}）"
                    )
                    if len(jobs) >= query.max_jobs:
                        self.note(f"已达到本轮上限 {query.max_jobs} 个，停止采集")
                        break
                    total = jw.get("totalcount") or jw.get("totalCount") or 0
                    if total and page_no * PAGE_SIZE >= total:
                        break
                    if len(items) < PAGE_SIZE:
                        break
                if len(jobs) >= query.max_jobs:
                    break
            if len(jobs) >= query.max_jobs:
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
        self.note(f"搜索完成：收集 {len(jobs)} 个岗位（扫描 {scanned_total} · 过滤 {filtered_total}）")
        return jobs

    # ------------------------------------------------------------ 投递

    async def _goto_detail(self, tab: dict, url: str) -> bool:
        """导航到职位详情页并等待投递按钮状态可判定。"""
        if url.split("?")[0] not in (tab.get("url") or ""):
            await browser.raw_navigate(browser.SYSTEM_KEY, tab["id"], url)
        js = (
            "(() => {"
            "  const btns = [...document.querySelectorAll('div,a,button')].filter(b => b.offsetWidth || b.offsetHeight);"
            "  const apply = btns.some(b => (b.innerText || '').trim() === '立即投递');"
            "  const applied = btns.some(b => (b.innerText || '').trim() === '已投递');"
            "  return JSON.stringify({r: document.readyState, apply, applied});"
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
            if st.get("apply") or st.get("applied"):
                await task_control.cancellable_sleep(random.uniform(1.0, 1.8))
                return True
        return False

    async def apply(self, job: Job, greeting: str | None = None) -> ApplyResult:
        """投递 51job 岗位：详情页点击「立即投递」（站点跳转 applysuccess 即成功）。"""
        if not await self.check_login():
            return ApplyResult(success=False, message="登录态失效，请重新登录")

        tab = await self._ensure_tab(job.url)
        if tab is None:
            return ApplyResult(success=False, message="无法获得 51job 标签页")
        if not await self._goto_detail(tab, job.url):
            # 失败路径先探测验证墙：整页拦截时页面上没有按钮，
            # 这里是识别「需要验证」的最后机会（否则误报超时、平台不被跳过、白跑重试）
            if await verify.detect(tab["id"]):
                return ApplyResult(success=False, message="平台触发安全验证（验证码），已跳过本平台", need_verify=True)
            return ApplyResult(success=False, message="职位详情页加载超时（未找到投递按钮）")

        # 预检：标签页网络僵尸化（后台停留过久）→ 关掉重开
        if await browser.page_network_alive(browser.SYSTEM_KEY, tab["id"]) is False:
            logger.warning("51job 标签页网络无响应，重开标签页重试")
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

        # 第 1 步：判定按钮状态（已投递 / 可投递 / 无按钮）
        state_js = (
            "(() => {"
            "  const btns = [...document.querySelectorAll('div,a,button')].filter(b => b.offsetWidth || b.offsetHeight);"
            "  if (btns.some(b => (b.innerText || '').trim() === '已投递')) return 'already';"
            "  const has = btns.some(b => (b.innerText || '').trim() === '立即投递');"
            "  return has ? 'has-button' : 'no-button';"
            "})()"
        )
        state = await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], state_js, timeout_s=15)
        if state == "already":
            return ApplyResult(success=True, message="此前已投递（跳过重复投递）")
        if state != "has-button":
            return ApplyResult(success=False, message="未找到「立即投递」按钮")

        # 第 2 步：触发点击。点击会让站点在同标签跳转 applysuccess.php，
        # 跳转会摧毁 JS 上下文，评估返回值可能丢失（None）——属正常，不能当作失败。
        click_js = (
            "(() => {"
            "  const btns = [...document.querySelectorAll('div,a,button')].filter(b => b.offsetWidth || b.offsetHeight);"
            "  const target = btns.find(b => (b.innerText || '').trim() === '立即投递');"
            "  if (!target) return 'no-button';"
            "  target.click();"
            "  return 'clicked';"
            "})()"
        )
        await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], click_js, timeout_s=15)

        # 第 3 步：轮询成功信号（applysuccess 页 / 按钮变已投递 / 提示文案）
        await task_control.cancellable_sleep(random.uniform(1.5, 2.5))
        check_js = (
            "(() => {"
            "  const u = location.href;"
            "  const txt = document.body.innerText.slice(0, 400);"
            "  const applied = [...document.querySelectorAll('*')].some(e => (e.innerText || '').trim() === '已投递' && (e.offsetWidth || e.offsetHeight));"
            "  return JSON.stringify({u: u.slice(0, 120), successPage: u.includes('applysuccess'), applied, txt: txt.replace(/\\n+/g, ' | ').slice(0, 200)});"
            "})()"
        )
        for _ in range(10):
            raw = await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], check_js, timeout_s=10)
            if raw:
                try:
                    st = json.loads(raw)
                except ValueError:
                    st = {}
                if st.get("successPage"):
                    return ApplyResult(success=True, message="已投递（简历已送达）")
                if st.get("applied"):
                    return ApplyResult(success=True, message="已投递（简历已送达）")
                txt = st.get("txt") or ""
                if "已投递" in txt and "立即投递" not in txt:
                    return ApplyResult(success=True, message="已投递（简历已送达）")
                if any(w in txt for w in ("投递失败", "职位已关闭", "无法投递")):
                    return ApplyResult(success=False, message=f"投递未成功（{txt[:60]}）")
            await task_control.cancellable_sleep(1.2)
        # 最终复核：标签页延迟时请求可能刚完成
        await task_control.cancellable_sleep(4.0)
        raw = await browser.raw_evaluate(browser.SYSTEM_KEY, tab["id"], check_js, timeout_s=10)
        if raw:
            try:
                st = json.loads(raw)
            except ValueError:
                st = {}
            if st.get("successPage") or st.get("applied"):
                return ApplyResult(success=True, message="已投递（响应超时，平台侧已确认）")
        return ApplyResult(success=False, message="投递结果未确认（未出现成功页）")


register(Job51Platform())
