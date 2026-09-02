"""CourseLoop handbook 解析器。

Monash 和 UNSW 用同一套 handbook 平台（Next.js + 内嵌 __NEXT_DATA__），
页面里已经带着结构化 JSON，不需要解析 HTML 标签。
两校的字段命名有差异，用 FIELD_MAP 抹平。
"""

from __future__ import annotations

import html as html_mod
import json
import re
from datetime import datetime, timezone
from typing import Any

from ..models import EnglishRequirement, EntryRequirement, Level, Program

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)

# 每所学校：概念字段 -> 该校 handbook 里的实际字段名（按优先级尝试）
FIELD_MAP: dict[str, dict[str, list[str]]] = {
    "monash": {
        "code": ["course_code", "code"],
        "title": ["award_titles", "title"],
        "entry": ["minimum_entry_requirements", "entry_requirements_onshore", "entry"],
        "english": ["english_language", "english_language_requirements"],
        "duration": ["full_time_duration", "duration_full_time"],
        "faculty": ["school", "academic_org", "owning_org"],
        "campus": ["location"],
        # Monash handbook 不以结构化字段登载入学季，留空而不是塞个错的进去
        "intakes": ["intake_period"],
        "level_hint": ["aqf_level", "type"],
        "updated": ["course_data_updated", "last_review_date"],
    },
    "unsw": {
        "code": ["code", "course_code"],
        "title": ["award_title_single", "title"],
        "entry": ["entry_requirements_v2", "entry_requirements_onshore"],
        # UNSW handbook 不登载语言要求（在 international 站点上），这里预期解析不到
        "english": ["english_language", "english_language_requirements"],
        "duration": ["duration_full_time", "duration_ft_std", "duration_hb_display"],
        "faculty": ["parent_academic_org", "academic_org"],
        "campus": ["campus", "location"],
        "intakes": ["intake_period"],
        "level_hint": ["award_type_single", "study_level_single"],
        "updated": ["course_data_updated", "last_review_date"],
    },
}

UNIVERSITY_NAMES = {
    "monash": "Monash University",
    "unsw": "UNSW Sydney",
}


# --------------------------------------------------------------------------
# 通用取值工具：handbook 的字段可能是 str / dict / list[dict]，统一压成文本
# --------------------------------------------------------------------------

def strip_html(raw: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", raw, flags=re.I)
    text = re.sub(r"</(p|li|div|h\d)>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def to_text(value: Any) -> str:
    """把任意 handbook 字段压成一段纯文本。"""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return strip_html(value)
    if isinstance(value, list):
        return " ".join(p for p in (to_text(v) for v in value) if p)
    if isinstance(value, dict):
        # 优先取有语义的键，避免把 cl_id 之类的内部 ID 混进正文
        for key in ("award_title", "description", "text", "value", "display_name",
                    "duration_display", "label", "name"):
            if key in value:
                return to_text(value[key])
        return " ".join(p for p in (to_text(v) for v in value.values()) if p)
    return ""


def to_labels(value: Any) -> list[str]:
    """把 list[{value: "Term 1"}] 之类的结构压成 ["Term 1"]。"""
    out: list[str] = []
    if isinstance(value, str):
        out = [strip_html(value)]
    elif isinstance(value, dict):
        out = [to_text(value)]
    elif isinstance(value, list):
        out = [to_text(v) for v in value]
    seen, result = set(), []
    for item in out:
        item = item.strip()
        if item and item.lower() not in seen:
            seen.add(item.lower())
            result.append(item)
    return result


def pick(content: dict, names: list[str]) -> Any:
    """按优先级取第一个"压成文本后非空"的字段。

    handbook 里到处是 {"value": "", "cl_id": null} 这类占位对象，
    只判断 `is not None` 会让它们盖住后面真正有值的字段。
    """
    for name in names:
        value = content.get(name)
        if value in (None, "", [], {}):
            continue
        if not to_text(value).strip():
            continue
        return value
    return None


def clean_title(text: str) -> str:
    """"Master of Information Technology - MIT" -> "Master of Information Technology"。"""
    return re.sub(r"\s*-\s*[A-Z]{2,6}$", "", text).strip()


def clean_duration(text: str) -> str | None:
    """UNSW 部分项目的时长字段是空的，会渲染成 "0 "——这不是数据，是占位。"""
    text = text.strip()
    if not text or re.fullmatch(r"0(\.0)?\s*", text):
        return None
    return text


# --------------------------------------------------------------------------
# 从自由文本里抽结构化分数。抓不到就返回 None，绝不猜。
# --------------------------------------------------------------------------

def parse_english(text: str) -> EnglishRequirement:
    req = EnglishRequirement(raw=text or None)
    if not text:
        return req

    # IELTS 总分。学校的写法五花八门，按优先级试几种句式：
    #   "IELTS (Academic): 6.5 overall (no band...)"  数字在前
    #   "An overall IELTS score of 6.0 with..."        数字在后
    for pattern in (
        r"IELTS[^.;]{0,100}?(\d(?:\.\d)?)\s*(?:overall|\(|,|;|$)",
        r"IELTS[^.;]{0,60}?(?:score|band|result)?\s*(?:of|:|is)\s*(\d(?:\.\d)?)",
        r"overall[^.;]{0,40}?IELTS[^.;]{0,40}?(\d(?:\.\d)?)",
    ):
        if m := re.search(pattern, text, re.I):
            req.ielts_overall = float(m.group(1))
            break
    m = re.search(
        r"(?:no band(?:s)? (?:score )?lower than|minimum of|no less than|each band)\D{0,20}(\d(?:\.\d)?)",
        text, re.I,
    )
    if m:
        req.ielts_min_band = float(m.group(1))

    m = re.search(r"TOEFL[^.;]{0,120}?(\d{2,3})", text, re.I)
    if m:
        value = int(m.group(1))
        req.toefl_ibt = value if 40 <= value <= 120 else None

    m = re.search(r"(?:Pearson|PTE)[^.;]{0,120}?(\d{2,3})", text, re.I)
    if m:
        value = int(m.group(1))
        req.pte_overall = value if 30 <= value <= 90 else None

    return req


def parse_entry(text: str) -> EntryRequirement:
    req = EntryRequirement(raw=text or None)
    if not text:
        return req

    # "weighted average mark of 65%" / "WAM of at least 65%" / "credit (60%) average"
    patterns = [
        r"(?:weighted average mark|WAM|average mark|GPA equivalent)\D{0,30}?(\d{2,3}(?:\.\d)?)\s*%",
        r"credit\s*\(\s*(\d{2,3})\s*%\s*\)",
        r"(?:minimum|at least)\D{0,25}?(\d{2,3})\s*%\D{0,20}(?:average|WAM|mark)",
    ]
    marks = [float(m.group(1)) for p in patterns if (m := re.search(p, text, re.I))]
    if marks:
        req.min_wam_percent = min(marks)  # 多个档位时取最低门槛

    if re.search(r"not necessarily in|any discipline|regardless of discipline", text, re.I):
        req.requires_cognate_degree = False
    elif re.search(r"cognate|related discipline|relevant discipline", text, re.I):
        req.requires_cognate_degree = True

    # Monash 的 "Entry level 1 / Entry level 2" 多入口结构
    req.entry_levels = re.findall(r"Entry level \d[^.]{0,80}", text, re.I)[:4]
    return req


def guess_level(title: str, content: dict, hint_fields: list[str]) -> Level:
    hint = " ".join(to_text(content.get(f)) for f in hint_fields)
    haystack = f"{title} {hint}".lower()
    if "bachelor" in haystack or "aqf 7" in haystack:
        return "bachelor"
    if "doctor" in haystack or "philosophy" in haystack or "research" in haystack:
        return "research"
    if "master" in haystack or "graduate" in haystack or "aqf 9" in haystack or "level 9" in haystack:
        return "master"
    return "other"


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def extract_page_content(html: str) -> dict:
    """从 handbook 页面 HTML 里取出内嵌的 JSON。"""
    match = NEXT_DATA_RE.search(html)
    if not match:
        raise ValueError("页面里没有 __NEXT_DATA__，可能不是 CourseLoop handbook 或页面结构变了")
    data = json.loads(match.group(1))
    content = data.get("props", {}).get("pageProps", {}).get("pageContent")
    if not content:
        raise ValueError("__NEXT_DATA__ 里没有 pageContent，可能是 404 页")
    return content


def parse(html: str, university: str, source_url: str, fetched_at: datetime | None = None) -> Program:
    if university not in FIELD_MAP:
        raise ValueError(f"未知学校 {university}，支持：{list(FIELD_MAP)}")
    fields = FIELD_MAP[university]
    content = extract_page_content(html)

    code = to_text(pick(content, fields["code"]))
    title = clean_title(to_text(pick(content, fields["title"])))
    if not code or not title:
        raise ValueError(f"缺少 code 或 title：code={code!r} title={title!r}")

    credit_points = None
    raw_cp = to_text(pick(content, ["credit_points"]))
    if raw_cp and (m := re.search(r"\d+", raw_cp)):
        credit_points = int(m.group())

    return Program(
        program_key=f"{university}:{code}",
        university=UNIVERSITY_NAMES[university],
        source="courseloop",
        code=code,
        title=title,
        level=guess_level(title, content, fields.get("level_hint", [])),
        cricos_code=to_text(pick(content, ["cricos_code"])) or None,
        credit_points=credit_points,
        duration_full_time=clean_duration(to_text(pick(content, fields["duration"]))),
        faculty=to_text(pick(content, fields["faculty"])) or None,
        campus=to_labels(pick(content, fields["campus"])),
        intakes=to_labels(pick(content, fields["intakes"])),
        english=parse_english(to_text(pick(content, fields["english"]))),
        entry=parse_entry(to_text(pick(content, fields["entry"]))),
        source_url=source_url,
        source_updated_at=to_text(pick(content, fields["updated"])) or None,
        fetched_at=fetched_at or datetime.now(timezone.utc),
    )
