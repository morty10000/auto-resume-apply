"""采集侧岗位过滤：按用户配置（关键词 / 薪资 / 经验 / 学历）粗筛。

定位：采集时的「硬过滤」，保证入库的岗位与配置匹配；
精细化打分（技能重合度等）仍由后续匹配引擎负责。
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅用于类型标注，避免与 platforms 包循环导入
    from backend.platforms.base import Job, JobQuery


def parse_salary_k(text: str | None) -> tuple[float, float] | None:
    """把薪资文本换算成 (下限K, 上限K)；无法解析返回 None。

    支持："10-15K"、"12-15K·14薪"、"400-450元/天"、"1-1.8万"、"7.5千-1.5万"、
    "8000-15000元"（智联）等格式（含混合单位）。
    """
    if not text:
        return None
    s = str(text)
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-–~]\s*(\d+(?:\.\d+)?)\s*元\s*/\s*天", s)
    if m:  # 日薪 → 月薪（按 21.75 天）
        k = 21.75 / 1000
        return float(m.group(1)) * k, float(m.group(2)) * k
    m = re.search(r"(\d+(?:\.\d+)?)\s*元\s*/\s*天", s)
    if m:  # 单值日薪（如「300元/天」）→ 月薪
        v = float(m.group(1)) * (21.75 / 1000)
        return v, v

    def unit_of(v: str) -> str | None:
        mm = re.search(r"(\d+(?:\.\d+)?)\s*(万|千|[kK])", v)
        return mm.group(2) if mm else None

    def side(v: str, other_unit: str | None) -> float | None:
        mm = re.search(r"(\d+(?:\.\d+)?)\s*(万|千|[kK])?", v)
        if not mm:
            return None
        num, u = float(mm.group(1)), mm.group(2)
        if u == "万":
            return num * 10
        if u:
            return num
        if other_unit == "万":
            return num * 10
        if other_unit:
            return num
        return num / 1000  # 无单位 = 元/月

    parts = re.split(r"\s*[-–~]\s*", s, maxsplit=1)
    if len(parts) == 2:
        lo = side(parts[0], unit_of(parts[1]))
        hi = side(parts[1], unit_of(parts[0]))
        if lo is not None and hi is not None and 0 <= lo <= hi:
            if re.search(r"(?:/|每)\s*年|年薪", s):     # 「10-15万/年」→ 月薪
                return lo / 12, hi / 12
            return lo, hi
    m = re.search(r"(\d+(?:\.\d+)?)\s*(万|千|[kK])", s)
    if m:
        v = float(m.group(1)) * (10 if m.group(2) == "万" else 1)
        return v, v
    m = re.search(r"(\d{3,6})", s)
    if m:
        v = int(m.group(1)) / 1000
        return v, v
    return None


def parse_active_days(text: str | None) -> int | None:
    """把各平台的 HR 活跃文案换算成「约多少天前活跃」；无法判断返回 None。

    样例：「刚刚活跃」「2天前在线」「3日内活跃」「12小时内回复可能性大」「本周活跃」「半月前在线」。
    """
    s = str(text or "").strip()
    if not s:
        return None
    m = re.search(r"(\d+)\s*个?\s*月", s)
    if m:
        return int(m.group(1)) * 30
    m = re.search(r"(\d+)\s*周", s)
    if m:
        return int(m.group(1)) * 7
    m = re.search(r"(\d+)\s*[天日]", s)
    if m:
        return int(m.group(1))
    if "半年" in s:
        return 180
    if "一年" in s or "1年内" in s:
        return 365
    if "昨日" in s or "昨天" in s:
        return 1
    if "半月" in s:
        return 15
    if "三月" in s or "季度" in s:
        return 90
    if "一月" in s:
        return 30
    if "本周" in s or "近一周" in s:
        return 3
    if "一周" in s:
        return 7
    if re.search(r"刚刚|在线|秒|分钟|小时|今日|今天|本日", s):
        return 0
    return None


# 前端经验选项 → 平台经验标签的映射（经验不限 一律放行）
EXPERIENCE_MAP: dict[str, set[str]] = {
    "应届生": {"在校/应届", "应届", "应届生", "无经验"},
    "1年以内": {"1年以内", "1年以下", "在校/应届", "无经验"},
    "1-3年": {"1-3年"},
    "3-5年": {"3-5年"},
    "5-10年": {"5-10年"},
    "10年以上": {"10年以上"},
}

_UNLIMITED = {"经验不限", "学历不限", "不限", ""}


def keyword_in_list_fields(job: Job, keyword: str) -> bool:
    """关键词是否命中岗位「标题 / 标签(skills+labels)」任一处；关键词为空视为命中。"""
    tokens = [t for t in (keyword or "").split() if t]
    if not tokens:
        return True
    extra = job.extra or {}
    tags = (
        " ".join(str(x) for x in (extra.get("skills") or []))
        + " "
        + " ".join(str(x) for x in (extra.get("labels") or []))
    ).lower()
    title = (job.title or "").lower()
    return any(t.lower() in title or (tags and t.lower() in tags) for t in tokens)


def passes_filters(job: Job, keyword: str, query: JobQuery, check_keyword: bool = True) -> bool:
    """判断岗位是否通过配置过滤（关键词 / 薪资 / 经验 / 学历）。

    check_keyword=False：跳过关键词检查（正文校验模式由调用方用「标题/标签/正文」统一裁决）。
    """
    # ---- 关键词：标题或标签含搜索词任一词元即命中 ----
    if check_keyword and not keyword_in_list_fields(job, keyword):
        return False

    # ---- 薪资：区间有交集才保留（配置单位为 K/月） ----
    if query.salary_min is not None or query.salary_max is not None:
        rng = parse_salary_k(job.salary)
        if rng:
            lo, hi = rng
            if query.salary_min is not None and hi < query.salary_min:
                return False
            if query.salary_max is not None and lo > query.salary_max:
                return False

    # ---- 经验 ----
    if query.experience:
        exp = ((job.extra or {}).get("experience") or "").strip()
        if exp not in _UNLIMITED:
            allowed: set[str] = set()
            for e in query.experience:
                allowed |= EXPERIENCE_MAP.get(e, {e})
            if exp not in allowed:
                return False

    # ---- 学历 ----
    if query.education:
        deg = ((job.extra or {}).get("degree") or "").strip()
        if deg not in _UNLIMITED and deg not in set(query.education):
            return False

    # ---- HR 活跃度：超过阈值即过滤（信息缺失不拦截） ----
    if query.hr_active_days is not None:
        days = (job.extra or {}).get("hr_active_days")
        if isinstance(days, int) and days > query.hr_active_days:
            return False

    return True


def body_matches_keyword(description: str, keyword: str) -> bool:
    '''正文关键词检查：正文包含关键词任一词元即通过；关键词为空视为通过。'''
    tokens = [t for t in (keyword or "").split() if t]
    if not tokens:
        return True
    text = (description or "").lower()
    return any(t.lower() in text for t in tokens)
