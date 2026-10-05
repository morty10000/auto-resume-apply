"""岗位匹配引擎（计划书 3.5）。

第一层：硬过滤（城市 / 学历 / 经验 / 标题黑名单）—— 命中直接淘汰
第二层：软评分 0-100：
    score = 40 × 技能覆盖率 + 30 × 职位名称相似度 + 15 × 薪资匹配度 + 加分项(≤15)

输入是「简历结构化档案」（data/resumes/profile.json 的 parsed 部分）
与数据库岗位行；输出 (score, status, detail)，detail 全量写入 jobs.match_detail。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from backend.services.resume_profile import SKILL_LEXICON, _hit
from backend.core.job_filter import parse_salary_k

# 学历排序（岗位要求 > 简历学历 → 淘汰）
DEGREE_RANK = {
    "博士": 5, "硕士": 4, "研究生": 4, "本科": 3,
    "大专": 2, "专科": 2, "中专": 1, "高中": 1,
}

_NO_LIMIT_DEGREE = ("学历不限", "不限", "无要求", "")

# 各权重（合计 100）
W_SKILL, W_TITLE, W_SALARY, W_BONUS = 40, 30, 15, 15

_EXP_MIN_RE = re.compile(r"(\d{1,2})\s*[-–~至]\s*\d{1,2}\s*年")
_EXP_MAX_RE = re.compile(r"(\d{1,2})\s*年(?:以内|以下|及以下)")


def _norm(s: str) -> str:
    """归一化：去空白与常见连接符、小写。"""
    return re.sub(r"[\s·（）()\-—/\\|]+", "", str(s or "")).lower()


# ---------------------------------------------------------------- 技能

def detect_skills(text: str) -> set[str]:
    """词表扫描文本，返回命中的规范技能名集合。"""
    out: set[str] = set()
    for canonical, aliases in SKILL_LEXICON.items():
        for alias in aliases:
            if _hit(text, alias):
                out.add(canonical)
                break
    return out


def _job_skill_text(job: dict) -> str:
    extra = job.get("extra") or {}
    parts = [
        str(job.get("title") or ""),
        " ".join(str(x) for x in (extra.get("skills") or [])),
        " ".join(str(x) for x in (extra.get("labels") or [])),
        str(job.get("description") or ""),   # 详情页正文（正文校验抓取后可用，技能识别更准）
    ]
    return " ".join(parts)


def _skill_score(job_skills: set[str], resume_skills: list[str] | set[str]) -> tuple[float, str]:
    """技能覆盖率 = |简历 ∩ 岗位| / |岗位|；岗位未标注时给中性值。"""
    resume_set = set(resume_skills or [])
    if not job_skills:
        return 0.35, "岗位未标注技能，按中性值计"
    inter = job_skills & resume_set
    return len(inter) / len(job_skills), ""


# ---------------------------------------------------------------- 名称相似

# 标题同族判定词（保守列表）：标题命中其一、且该词在简历技能中出现 → 视作同方向职位
_TITLE_FAMILY_WORDS = (
    "单片机", "嵌入式", "stm32", "c51", "硬件", "pcb", "电路", "物联网", "传感器", "固件",
)


def _title_family_hit(title_norm: str, skills: list[str] | set[str] | None) -> str | None:
    """标题与简历方向是否同族；命中返回触发的方向词，否则 None。"""
    skills_norm = [_norm(x) for x in (skills or []) if x]
    for w in _TITLE_FAMILY_WORDS:
        if w in title_norm and any(w in x for x in skills_norm):
            return w
    return None


def _title_score(
    expected: str | None, title: str, resume_skills: list[str] | set[str] | None = None,
) -> tuple[float, str]:
    """职位名称相似度；标题命中简历方向词时保底 0.55（同族职位，避免字面比对低估）。"""
    t = _norm(title)
    if not t:
        return 0.0, ""
    hit = _title_family_hit(t, resume_skills)
    floor, floor_note = (0.55, f"标题命中方向词「{hit}」") if hit else (0.0, "")
    if not expected:
        return (floor, floor_note) if hit else (0.5, "简历未识别求职意向，按中性值计")
    p = _norm(expected)
    if not p:
        return (floor, floor_note) if hit else (0.0, "")
    if p in t:
        return 0.95, "标题包含意向职位"
    ratio = SequenceMatcher(None, p, t).ratio()
    cover = sum(1 for c in set(p) if c in t) / max(1, len(set(p)))
    score = max(ratio, cover * 0.8)
    if score < floor:
        return floor, floor_note
    return score, ""


# ---------------------------------------------------------------- 薪资

def _parse_salary_range(text: str | None) -> tuple[float, float] | None:
    """解析薪资字符串 → (低, 高)，单位 K/月。

    支持 '6-10K·14薪' / '1-1.5万' / '150-200元/天' / '8000-15000元'（智联）等格式。
    """
    return parse_salary_k(text)


def _salary_score(job_salary: str | None, cfg_min: int | None, cfg_max: int | None) -> tuple[float, str]:
    """薪资匹配度 = 岗位区间与期望区间的重叠 / 期望区间长度。"""
    target = None
    if cfg_min is not None and cfg_max is not None:
        target = (float(min(cfg_min, cfg_max)), float(max(cfg_min, cfg_max)))
    if target is None:
        return 0.5, "未设期望薪资，按中性值计"
    jr = _parse_salary_range(job_salary)
    if jr is None:
        return 0.5, "岗位薪资未标注（面议），按中性值计"
    t_lo, t_hi = target
    if t_hi == t_lo:
        return (1.0 if jr[0] <= t_lo <= jr[1] else 0.0), ""
    lo = max(jr[0], t_lo)
    hi = min(jr[1], t_hi)
    if hi < lo:
        return 0.0, "薪资区间无重叠"
    if hi == lo:
        return 0.1, "薪资区间仅边界相接"
    return min(1.0, (hi - lo) / (t_hi - t_lo)), ""


# ---------------------------------------------------------------- 经验

def _parse_exp_min(exp: str | None) -> int | None:
    """岗位经验要求下限（年）；'经验不限'/空 → None。"""
    s = str(exp or "").strip()
    if not s or "不限" in s or "应届" in s:
        return 0
    m = _EXP_MIN_RE.search(s)          # '1-3年' / '3-5年'
    if m:
        return int(m.group(1))
    m = _EXP_MAX_RE.search(s)          # '1年以内'
    if m:
        return 0
    m = re.search(r"(\d{1,2})\s*年", s)
    if m:
        return int(m.group(1))
    return None


# ---------------------------------------------------------------- 加分项

def _bonus(job: dict) -> tuple[int, list[str]]:
    extra = job.get("extra") or {}
    raw, notes = 0, []
    if extra.get("boss_online"):
        raw += 6
        notes.append("HR活跃 +6")
    if len(extra.get("welfare") or []) >= 5:
        raw += 4
        notes.append("福利标签丰富 +4")
    if extra.get("brand_industry") and extra.get("brand_scale"):
        raw += 4
        notes.append("企业信息完整 +4")
    return min(15, raw), notes


# ---------------------------------------------------------------- 硬过滤

def _hard_filter(job: dict, resume: dict, cfg: dict) -> list[str]:
    reasons: list[str] = []
    extra = job.get("extra") or {}

    cities = cfg.get("cities") or []
    if cities:
        city_full = str(job.get("city") or "")
        city0 = city_full.split("·")[0].strip()
        if city0 and city0 not in cities and not any(c in city_full for c in cities):
            reasons.append(f"城市不符（{city0}）")

    degree = str(extra.get("degree") or "").strip()
    if degree and degree not in _NO_LIMIT_DEGREE:
        jr = DEGREE_RANK.get(degree)
        rr = DEGREE_RANK.get(str(resume.get("education") or ""))
        if jr is not None and rr is not None and jr > rr:
            reasons.append(f"学历要求高于简历（要求{degree}）")

    exp_min = _parse_exp_min(extra.get("experience"))
    ry = resume.get("years_experience")
    if exp_min is not None and isinstance(ry, int) and exp_min > ry + 2:
        reasons.append(f"经验要求过高（要求{extra.get('experience')}）")

    title = str(job.get("title") or "")
    for w in cfg.get("blacklist") or []:
        w = str(w).strip()
        if w and w in title:
            reasons.append(f"标题含“{w}”")
            break
    return reasons


# ---------------------------------------------------------------- 对外入口

def score_job(job: dict, resume: dict, cfg: dict) -> dict:
    """给单个岗位打分。

    job: 扁平字典（title/company/salary/city/extra…）
    resume: 简历 parsed 档案
    cfg: {threshold, salary_min, salary_max, cities, blacklist}
    返回 {score, status, detail}
    """
    threshold = int(cfg.get("threshold") or 60)
    resume_skills = resume.get("skills") or []

    job_skills = detect_skills(_job_skill_text(job))
    sk, sk_note = _skill_score(job_skills, resume_skills)
    ti, ti_note = _title_score(
        resume.get("expected_position"), str(job.get("title") or ""), resume.get("skills")
    )
    sa, sa_note = _salary_score(job.get("salary"), cfg.get("salary_min"), cfg.get("salary_max"))
    bonus_raw, bonus_notes = _bonus(job)

    score = round(W_SKILL * sk + W_TITLE * ti + W_SALARY * sa + bonus_raw, 1)

    resume_set = set(resume_skills)
    hit = sorted(job_skills & resume_set)
    missing = sorted(job_skills - resume_set)

    reasons = _hard_filter(job, resume, cfg)
    if reasons:
        status = "rejected"
        reason = "；".join(reasons)
    elif sa == 0.0:
        # 薪资完全无交集（含单点不命中）→ 直接淘汰：不靠技能/名称堆分过线
        status = "rejected"
        reason = (
            f"薪资不匹配（岗位 {job.get('salary') or '未标注'} 与期望 "
            f"{cfg.get('salary_min')}-{cfg.get('salary_max')}K 无交集）"
        )
    elif score >= threshold:
        status = "matched"
        reason = ""
    else:
        status = "rejected"
        reason = f"分数低于阈值（{score} < {threshold}）"

    notes = [n for n in (sk_note, ti_note, sa_note) if n]
    detail = {
        "threshold": threshold,
        "weights": f"技能{W_SKILL}/名称{W_TITLE}/薪资{W_SALARY}/加分{W_BONUS}",
        "skill_coverage": round(sk, 4),
        "title_sim": round(ti, 4),
        "salary_fit": round(sa, 4),
        "bonus": bonus_raw,
        "bonus_notes": bonus_notes,
        "job_skills": sorted(job_skills),
        "hit_skills": hit,
        "missing_skills": missing,
        "job_experience": str((job.get("extra") or {}).get("experience") or ""),
        "job_degree": str((job.get("extra") or {}).get("degree") or ""),
        "notes": notes,
        "reason": reason,
    }
    return {"score": score, "status": status, "detail": detail}
