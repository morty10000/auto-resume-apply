"""简历全文 → 结构化档案（计划书 3.4 输出格式 + 扩展字段）。

纯规则实现：正则（联系方式 / 学历 / 日期）+ 章节切分 + 技能词表匹配。
输出供「匹配打分」和「采集关键词建议」使用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .resume_parser import ExtractResult, extract_text


# ---------------------------------------------------------------- 词表

# 常用技能词表：canonical -> [别名...]（中文子串匹配；英文按词边界匹配）
SKILL_LEXICON: dict[str, list[str]] = {
    # 编程 / 开发
    "Python": ["Python"],
    "Java": ["Java"],
    "C语言": ["C语言", "C/C++"],
    "C++": ["C++"],
    "C#": ["C#"],
    "JavaScript": ["JavaScript", "JS"],
    "Go": ["Golang", "Go语言"],
    "SQL": ["SQL"],
    "HTML": ["HTML"],
    "CSS": ["CSS"],
    "Vue": ["Vue"],
    "React": ["React"],
    "FastAPI": ["FastAPI"],
    "Django": ["Django"],
    "Flask": ["Flask"],
    "Spring": ["Spring"],
    "MySQL": ["MySQL"],
    "Redis": ["Redis"],
    "MongoDB": ["MongoDB"],
    "Linux": ["Linux"],
    "Docker": ["Docker"],
    "Git": ["Git"],
    # 硬件 / 电子
    "单片机": ["单片机"],
    "嵌入式": ["嵌入式"],
    "STM32": ["STM32"],
    "C51": ["C51", "51单片机"],
    "Keil": ["Keil"],
    "PCB": ["PCB"],
    "电路设计": ["电路设计", "原理图"],
    "焊接": ["焊接", "贴片"],
    "示波器": ["示波器"],
    "万用表": ["万用表"],
    "物联网": ["物联网"],
    "计算机网络": ["计算机网络"],
    "通信协议": ["通信协议"],
    "FreeRTOS": ["FreeRTOS"],
    "ESP8266": ["ESP8266", "ESP-8266"],
    "ESP32": ["ESP32"],
    "MQTT": ["MQTT"],
    "I2C": ["I2C", "IIC"],
    "SPI": ["SPI"],
    "串口通信": ["串口通信", "串口"],
    "传感器": ["传感器"],
    "Altium Designer": ["Altium Designer", "Altium"],
    "立创EDA": ["立创EDA", "嘉立创 EDA", "嘉立创EDA"],
    # 设备 / 产品 / 服务
    "外设": ["外设"],
    "手柄": ["手柄", "游戏手柄"],
    "键盘": ["键盘"],
    "鼠标": ["鼠标"],
    "耳机": ["耳机"],
    "故障排查": ["故障排查", "问题排查", "故障诊断"],
    "设备调试": ["设备调试", "调试"],
    "驱动安装": ["驱动安装", "驱动配置"],
    "固件更新": ["固件更新", "固件升级"],
    "按键映射": ["按键映射", "按键设置"],
    "蓝牙": ["蓝牙"],
    "评测": ["评测", "测评", "横向对比"],
    "售后": ["售后"],
    "客服": ["客服"],
    "技术支持": ["技术支持"],
    "售前": ["售前"],
    "用户沟通": ["用户沟通", "用户反馈", "倾听用户", "沟通表达"],
    "软件测试": ["软件测试", "接口测试", "自动化测试", "测试用例"],
    "产品测试": ["产品测试"],
    "质量检测": ["质检", "质量检测"],
    # 办公 / 通用
    "Excel": ["Excel"],
    "Word": ["Word"],
    "PPT": ["PPT", "PowerPoint"],
    "Office": ["Office", "WPS"],
    "Photoshop": ["Photoshop", "PS"],
    "Premiere": ["Premiere", "PR"],
    "CAD": ["CAD", "AutoCAD"],
    "SolidWorks": ["SolidWorks"],
    "数据分析": ["数据分析"],
    "项目管理": ["项目管理"],
    "视频剪辑": ["视频剪辑", "剪辑"],
    "新媒体运营": ["新媒体运营", "公众号", "短视频运营"],
    # 语言
    "英语": ["英语", "英文"],
    "CET-4": ["CET-4", "CET4", "四级"],
    "CET-6": ["CET-6", "CET6", "六级"],
    "普通话": ["普通话"],
}

# 建议搜索关键词候选（岗位搜索场景的常用词）
SEARCH_TERMS = [
    "售后", "客服", "技术支持", "售前", "测试", "质检", "运维", "运营",
    "助理", "专员", "数据标注", "内容审核", "电商", "新媒体", "剪辑",
    "外设", "手柄", "键盘", "鼠标", "耳机", "硬件", "嵌入式", "单片机",
    "物联网", "Python", "Java", "前端", "后端", "行政", "人事", "采购",
    "物流", "销售", "美工", "设计", "教师", "会计", "出纳",
]

# 关联词扩展：命中 key 时补充 value（仅在搜索结果里作为可选词）
RELATED_TERMS = {
    "售后": ["客服", "技术支持"],
    "客服": ["售后", "技术支持"],
    "外设": ["手柄"],
    "手柄": ["外设"],
    "测试": ["质检"],
    "质检": ["测试"],
    "嵌入式": ["单片机", "硬件"],
    "单片机": ["嵌入式", "硬件"],
}


# 章节标题（精确匹配）
_SECTION_HEADINGS = {
    "个人简介": "summary", "自我介绍": "summary", "自我评价": "summary",
    "个人概述": "summary", "个人总结": "summary", "个人概况": "summary",
    "核心优势": "advantages", "个人优势": "advantages", "优势亮点": "advantages",
    "专业技能": "skills", "技能特长": "skills", "核心技能": "skills",
    "职业技能": "skills", "技能清单": "skills",
    "技术与能力": "skills", "技能与能力": "skills", "专业能力": "skills",
    "求职意向": "intention", "期望职位": "intention", "意向岗位": "intention",
    "教育背景": "education", "教育经历": "education", "学历信息": "education",
    "相关经历": "experience", "工作经历": "experience", "实习经历": "experience",
    "项目经历": "experience", "职业经历": "experience", "工作经验": "experience",
    "实践经历": "experience", "校园经历": "experience", "工作履历": "experience",
    "荣誉奖项": "awards", "获奖情况": "awards", "荣誉证书": "awards",
    "证书": "awards",
}

_FUZZY_HEADING_WORDS = ("简介", "优势", "技能", "经历", "背景", "教育", "评价", "概况", "证书", "奖项")

_DEGREE_RANK = [("博士", 5), ("硕士", 4), ("研究生", 4), ("本科", 3), ("大专", 2), ("专科", 2), ("中专", 1), ("高中", 1)]

_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_NAME_LABEL_RE = re.compile(r"姓\s*名[:：]\s*([\u4e00-\u9fa5·]{2,5})")
_DATE_RANGE_RE = re.compile(
    r"(20\d{2})\s*[.\-/年]?\s*(\d{1,2})?\s*月?\s*[-–—~至到]+\s*"
    r"(?:(20\d{2})\s*[.\-/年]?\s*(\d{1,2})?|至今|现在|今|目前)"
)


@dataclass
class Section:
    key: str
    label: str
    lines: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 章节切分

def _is_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 14:
        return False
    if s in _SECTION_HEADINGS:
        return True
    if any(ch.isdigit() for ch in s) or "@" in s or "|" in s or "｜" in s:
        return False
    if "  " in s or "\t" in s:
        return False
    if s.startswith(("-", "·", "•", "・")):
        return False
    if len(s) <= 10 and any(w in s for w in _FUZZY_HEADING_WORDS):
        return True
    return False


def _split_sections(lines: list[str]) -> tuple[list[str], list[Section]]:
    """返回（标题前的头部队列, 章节列表）。"""
    head: list[str] = []
    sections: list[Section] = []
    current: Section | None = None
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if _is_heading(s):
            key = _SECTION_HEADINGS.get(s) or _fuzzy_key(s)
            current = Section(key=key, label=s)
            sections.append(current)
        elif current is None:
            head.append(s)
        else:
            current.lines.append(s)
    return head, sections


def _fuzzy_key(label: str) -> str:
    """模糊标题 → 章节类别（如「实习与兼职经历」→ experience）。"""
    if any(w in label for w in ("技能", "能力")):
        return "skills"
    if any(w in label for w in ("经历", "经验", "履历")):
        return "experience"
    if any(w in label for w in ("教育", "学历", "背景")):
        return "education"
    if any(w in label for w in ("简介", "概况", "概述", "评价", "总结")):
        return "summary"
    if any(w in label for w in ("证书", "奖项", "荣誉")):
        return "awards"
    return "other"


def _section_text(sections: list[Section], key: str) -> str:
    for sec in sections:
        if sec.key == key:
            return "".join(sec.lines) if key == "summary" else "\n".join(sec.lines)
    return ""


def _cjk_join(lines: list[str]) -> str:
    """把被换行截断的中文段落拼回一行（行尾不是标点时直接接上）。"""
    out = ""
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        if out and not re.search(r"[。！？!?；;：:）)】】\"'.,，]$", out):
            out += ln
        else:
            out += ("\n" if out else "") + ln
    return out.strip()


# ---------------------------------------------------------------- 字段抽取

def _extract_name(head: list[str], text: str) -> str | None:
    for ln in head[:4]:
        s = ln.strip()
        if re.fullmatch(r"[\u4e00-\u9fa5·]{2,4}", s) and not _is_heading(s):
            if not any(w in s for w in ("简历", "个人", "求职")):
                return s
    m = _NAME_LABEL_RE.search(text)
    return m.group(1) if m else None


def _extract_degree(text: str) -> str | None:
    best: tuple[int, str] | None = None
    for word, rank in _DEGREE_RANK:
        if word in text:
            if best is None or rank > best[0]:
                best = (rank, word)
    if not best:
        return None
    return {"专科": "大专"}.get(best[1], best[1])


def _extract_dates(text: str) -> list[tuple[int, int, int, int]]:
    """(start_y, start_m, end_y, end_m)，'至今'按当前日期算。"""
    from datetime import datetime

    now = datetime.now()
    out = []
    for m in _DATE_RANGE_RE.finditer(text):
        sy, sm, ey, em = m.group(1), m.group(2), m.group(3), m.group(4)
        start_y, start_m = int(sy), int(sm or 1)
        if ey:
            end_y, end_m = int(ey), int(em or 12)
        else:
            end_y, end_m = now.year, now.month
        if start_m > 12:
            start_m = 12
        if end_m > 12:
            end_m = 12
        if (start_y, start_m) <= (end_y, end_m):
            out.append((start_y, start_m, end_y, end_m))
    return out


def _months_between(a: tuple[int, int], b: tuple[int, int]) -> int:
    return max(0, (b[0] - a[0]) * 12 + (b[1] - a[1]))


def _extract_work_years(text: str, sections: list[Section]) -> tuple[int | None, str]:
    """工作年限（整年）+ 说明。优先显式「X年经验」，其次正式工作日期；纯实习按应届算。"""
    m = re.search(r"(\d{1,2})\s*年(?:以上)?(?:的)?(?:工作)?经验", text)
    if m:
        return int(m.group(1)), "简历自述"

    full_text, intern_text = "", ""
    for sec in sections:
        if sec.key != "experience":
            continue
        if any(w in sec.label for w in ("工作", "职业")):
            full_text += "\n".join(sec.lines) + "\n"
        elif any(w in sec.label for w in ("实习", "兼职", "实践")):
            intern_text += "\n".join(sec.lines) + "\n"

    spans = _extract_dates(full_text) if full_text else []
    if spans:
        start = min((s[0], s[1]) for s in spans)
        end = max((e[2], e[3]) for e in spans)
        years = _months_between(start, end) // 12
        if years >= 1:
            return years, "按工作经历估算"
        return 0, "工作经历不足一年"

    if intern_text and _extract_dates(intern_text):
        return 0, "应届 / 以实习经历为主"

    # 没有工作/实习经历：判断是否应届（毕业时间在 近1年 ~ 未来1年半 之间）
    from datetime import datetime

    now = datetime.now()
    edu_text = "\n".join("\n".join(sec.lines) for sec in sections if sec.key == "education")
    dates = _extract_dates(edu_text) if edu_text else []
    if dates:
        grad = max((e[2], e[3]) for e in dates)
    else:
        m2 = re.search(r"(20\d{2})\s*届", text)     # 「2026 届」
        grad = (int(m2.group(1)), 7) if m2 else None
    if grad is not None:
        months_to_grad = _months_between((now.year, now.month), grad)
        months_since_grad = _months_between(grad, (now.year, now.month))
        if months_to_grad <= 18 or months_since_grad <= 12:
            return 0, "应届 / 暂无正式工作经历"
    if "应届" in text:
        return 0, "应届"
    return None, "未识别到工作经历"


def _extract_school(edu_text: str, text: str) -> str | None:
    m = re.search(r"([\u4e00-\u9fa5]{2,15}(?:大学|学院|学校))", edu_text)
    if m:
        return m.group(1)
    m = re.search(r"([\u4e00-\u9fa5]{2,15}(?:大学|学院|学校))", text)
    return m.group(1) if m else None


def _extract_major(edu_text: str) -> str | None:
    if not edu_text:
        return None
    m = re.search(r"专\s*业[:：]\s*([\u4e00-\u9fa5A-Za-z0-9（）()\-]{2,20})", edu_text)
    if m:
        return m.group(1).strip()
    for line in edu_text.split("\n"):
        parts = re.split(r"[|｜/、\s·]+", line)
        for part in parts:
            part = re.sub(r"[（(][^）)]*[）)]", "", part)      # 去掉（本科）等括注
            part = re.sub(r"20\d{2}.*$", "", part).strip(" |｜-·.")
            if not part or len(part) < 2 or len(part) > 20:
                continue
            if re.search(r"(大学|学院|学校|本科|硕士|博士|大专|专科|高中|中专|GPA|在读)", part):
                continue
            if re.search(r"[\u4e00-\u9fa5]", part):
                return part
    return None


def _extract_graduation(edu_text: str) -> str | None:
    dates = _extract_dates(edu_text)
    if not dates:
        m = re.search(r"(20\d{2})\s*届", edu_text)        # 「2026 届」
        if m:
            return m.group(1)
        # 单日期（只有毕业时间）：2026.06
        m = re.search(r"(20\d{2})\s*[.\-/年]\s*(\d{1,2})?", edu_text)
        if m:
            return f"{m.group(1)}.{int(m.group(2)):02d}" if m.group(2) and int(m.group(2)) <= 12 else m.group(1)
        return None
    end = max((e[2], e[3]) for e in dates)
    return f"{end[0]}.{end[1]:02d}"


def _hit(text: str, alias: str) -> bool:
    if re.search(r"[\u4e00-\u9fa5]", alias):
        # 中文别名忽略空白差异（「C 语言」≈「C语言」）
        return re.sub(r"\s+", "", alias) in re.sub(r"\s+", "", text)
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", text, re.IGNORECASE) is not None


def _extract_skills(text: str, sections: list[Section]) -> list[str]:
    found: list[str] = []

    def add(name: str) -> None:
        if name and name not in found:
            found.append(name)

    # 1) 专业技能章节的行首标签（如「手柄操作  飞智、盖世小鸡…」）
    for sec in sections:
        if sec.key != "skills":
            continue
        for ln in sec.lines:
            label = re.split(r"\s{2,}|[:：]", ln.strip(), maxsplit=1)[0].strip(" -·•")
            if 2 <= len(label) <= 8 and not label.endswith(("。", "，", ".", ",")):
                add(label)

    # 2) 词表匹配（按首次出现顺序）
    hits: list[tuple[int, str]] = []
    for canonical, aliases in SKILL_LEXICON.items():
        pos = -1
        for alias in aliases:
            if _hit(text, alias):
                p = text.lower().find(alias.lower())
                if p < 0:
                    p = len(text)          # 空格变体（如「C 语言」），排序靠后
                if p >= 0 and (pos < 0 or p < pos):
                    pos = p
        if pos >= 0:
            hits.append((pos, canonical))
    hits.sort()
    for _pos, canonical in hits:
        add(canonical)
    return found[:24]


def _extract_suggested_keywords(text: str, intention: str | None, major: str | None) -> list[str]:
    out: list[str] = []

    def add(w: str | None) -> None:
        if w and w not in out:
            out.append(w)

    add(intention)
    if intention:
        for key, related in RELATED_TERMS.items():
            if key in intention:
                for r in related:
                    add(r)
    for term in SEARCH_TERMS:
        if term in text:
            add(term)
    add(major)
    return out[:10]


def _clean_intention(raw: str) -> str | None:
    s = raw.strip().strip("|｜-·— ")
    s = re.sub(r"^(?:求职)?(?:期望|意向|目标)(?:职位|岗位|工作|职业|城市|地点)?[:：]?\s*", "", s)
    s = re.split(r"[\s|｜/、,，;；]", s)[0].strip()
    return s if 2 <= len(s) <= 14 else None


def _extract_intention(head: list[str], text: str, sections: list[Section]) -> str | None:
    # 1) 求职意向章节
    sec_text = _section_text(sections, "intention")
    if sec_text:
        cand = _clean_intention(sec_text.split("\n")[0])
        if cand:
            return cand
    # 2) 头部「求职意向：…」行（可含斜杠分隔的多个职位，取第一个）
    for ln in head[:8]:
        m = re.match(r"^(?:求职意向|期望职位|意向岗位|目标岗位|求职目标)[:：]\s*(.+)$", ln.strip())
        if m:
            cand = _clean_intention(m.group(1))
            if cand:
                return cand
    # 3) 联系方式所在行：手机号前的内容（如「技术售后 手机：136…」）
    for line in head + [ln for sec in sections for ln in sec.lines[:2]]:
        m = _PHONE_RE.search(line)
        if not m:
            continue
        before = line[: m.start()]
        before = re.sub(r"(手机|电话|手机号|联系方式|联系电话|Tel|Phone)[:：]?\s*$", "", before, flags=re.IGNORECASE)
        cand = _clean_intention(before)
        if cand:
            return cand
        break
    # 4) 全文找「(求职)意向/期望职位：xxx」
    m = re.search(r"(?:求职|期望|意向|目标)(?:职位|岗位|工作|职业)?[:：]\s*([^\n]{2,30})", text)
    if m:
        return _clean_intention(m.group(1))
    return None


def _extract_city(text: str) -> str | None:
    m = re.search(r"(?:期望|意向|目标)(?:工作)?(?:城市|地点|地区)[:：]?\s*([\u4e00-\u9fa5]{2,8})", text)
    if m:
        return re.sub(r"(市|区|县)$", "", m.group(1))
    return None


def _extract_advantages(sections: list[Section]) -> list[str]:
    out: list[str] = []
    for sec in sections:
        if sec.key != "advantages":
            continue
        for ln in sec.lines:
            for item in re.split(r"\s{2,}", ln):
                item = item.strip(" -·•")
                if 2 <= len(item) <= 8 and not any(ch in item for ch in "，。、；：！？"):
                    if item not in out:
                        out.append(item)
    return out[:8]


# ---------------------------------------------------------------- 对外入口

def parse_text(text: str) -> dict:
    """全文 → 结构化档案 dict（含 raw_text）。"""
    text = text or ""
    lines = text.split("\n")
    head, sections = _split_sections(lines)

    name = _extract_name(head, text)
    phone_m = _PHONE_RE.search(text)
    email_m = _EMAIL_RE.search(text)
    intention = _extract_intention(head, text, sections)
    years, years_note = _extract_work_years(text, sections)
    edu_text = "\n".join("\n".join(sec.lines) for sec in sections if sec.key == "education")
    graduation = _extract_graduation(edu_text) if edu_text else None

    summary = _section_text(sections, "summary")
    if not summary:
        # 没标「个人简介」时退而求其次：取证明前段落
        summary = _cjk_join(head[1:3]) if len(head) > 2 else ""

    experience = [
        {"section": sec.label, "content": "\n".join(sec.lines)[:800]}
        for sec in sections
        if sec.key == "experience"
    ]

    return {
        "name": name,
        "phone": phone_m.group(0) if phone_m else None,
        "email": email_m.group(0) if email_m else None,
        "expected_position": intention,
        "expected_city": _extract_city(text),
        "years_experience": years,
        "experience_note": years_note,
        "education": _extract_degree(text),
        "school": _extract_school(edu_text, text),
        "major": _extract_major(edu_text),
        "graduation": graduation,
        "skills": _extract_skills(text, sections),
        "advantages": _extract_advantages(sections),
        "suggested_keywords": _extract_suggested_keywords(text, intention, _extract_major(edu_text)),
        "summary": summary[:500],
        "experience": experience,
        "raw_text": text,
    }


def analyze(path: str | Path) -> tuple[dict, ExtractResult]:
    """完整管线：文件 → (结构化档案, 解析元信息)。"""
    result = extract_text(path)
    profile = parse_text(result.text)
    return profile, result
