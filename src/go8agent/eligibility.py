"""资格判断：给定学生档案和一个项目，判断能不能申。

=============================================================================
为什么这个模块是纯 Python，一行模型调用都没有
=============================================================================
比大小是确定性计算，交给模型只会带来三个坏处：不稳定（同样输入可能给出
不同结论）、不可解释（说不清为什么）、还要花钱。

模型在整个系统里的职责是"理解用户的自然语言描述、决定调哪个工具、把结果
组织成人话"，不是"算 78 分够不够 75 分的线"。这条边界划清楚了，
agent 的可靠性就有了地基。

本模块以后会被包装成 agent 的一个 tool，但它自己不知道 agent 的存在。

=============================================================================
三条设计规则
=============================================================================
1. 数据不足时必须返回 INSUFFICIENT_DATA，绝不返回"符合条件"。
   宁可说"查不到"，也不能给一个可能让人错失或错报的结论。

2. 每条判断都要带原文证据（evidence）。用户和下游 agent 都能自己核对，
   不必相信我们的解析。

3. 推断必须显式。凡是页面上没写、由我们推出来的东西，一律进 caveats，
   绝不混进 verdict 当成事实。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum

from .models import Program

# =============================================================================
# 成绩等级的观测区间
# =============================================================================
# 这张表是从我们自己抓的 891 条真实数据里统计出来的，不是外部常识。
#
# 统计结果暴露了一个关键事实：**等级到分数的换算，连同一所学校内部都不统一。**
#   Monash "credit" 出现 245 次 = 60%，另有 87 次 = 65%
#   Monash "distinction" 出现 27 次 = 70%，另有 6 次 = 75%
#   UNSW  "credit" 出现 35 次 = 65%（该校比较一致）
#
# 所以本模块**不会**把等级换算成一个具体分数去做通过与否的判定。
# 这张表只用于在结果里给用户一个参考区间，帮助他判断要不要人工去核实。
GRADE_BAND_OBSERVED: dict[str, dict[str, tuple[float, float]]] = {
    "Monash University": {
        "pass": (50.0, 55.0),
        "credit": (60.0, 65.0),
        "distinction": (70.0, 75.0),
    },
    "UNSW Sydney": {
        "credit": (65.0, 65.0),
    },
}

# 等级的通行含义，仅用于把 "credit" 写成人类看得懂的话，不参与计算
GRADE_BAND_LABEL = {
    "pass": "及格 (Pass)",
    "credit": "良 (Credit)",
    "distinction": "优 (Distinction)",
    "high_distinction": "高优 (High Distinction)",
}


class Verdict(StrEnum):
    """判定结果。

    注意这里有四种而不是两种。真实世界里"数据不足"和"不符合"是完全不同的
    两件事，混为一谈会让用户错失本来能申的项目。
    """

    ELIGIBLE = "eligible"                    # 各项都达标
    BORDERLINE = "borderline"                # 达标但余量很小，或差一点点
    NOT_ELIGIBLE = "not_eligible"            # 有明确不达标的硬性条件
    INSUFFICIENT_DATA = "insufficient_data"  # 数据不足以判断


@dataclass
class StudentProfile:
    """学生档案。

    所有字段都可以为 None——用户一开始往往只知道几项。缺失的字段会让相关
    检查返回"数据不足"，而不是被当成 0 分处理。
    """

    # 学术成绩。中国学生一般是百分制，直接填 wam_percent。
    wam_percent: float | None = None

    # 语言成绩
    ielts_overall: float | None = None
    ielts_min_band: float | None = None  # 各单项里的最低分

    # 本科是否为该项目要求的相关专业背景。
    # 这一项目前只能由用户自己回答：判断"计算机"算不算"IT 的 cognate 学科"
    # 需要看各校的学科清单，那份清单不在 handbook 上。
    has_cognate_background: bool | None = None

    # 以下字段本模块暂不参与判定，但会带进 caveats 提醒用户
    home_institution: str | None = None   # 本科院校，将来接院校分级表用
    work_experience_years: float | None = None


@dataclass
class Check:
    """单项检查的结果。

    met 有三种取值，不是两种：
        True  达标
        False 不达标
        None  数据不足，无法判断  <- 这一种最重要，绝不能被当成 False
    """

    name: str
    met: bool | None
    required: str          # 项目要求，人类可读
    actual: str            # 学生实际情况
    evidence: str | None = None   # 官网原文片段，供用户自行核对
    note: str | None = None       # 补充说明，例如为什么判不了

    # met is None 时，这一项的"未知"是否应该阻断整体结论。
    #
    # 这个区分很要紧。"查不到某项要求"有两种截然不同的含义：
    #   a) 这确实是一条硬性要求，但我们不知道具体门槛  -> 必须阻断
    #   b) handbook 压根没提这条限制                  -> 不该阻断
    # 把两者混为一谈，会让绝大多数项目都变成"数据不足"，结果就是这个工具
    # 什么也回答不了——过度保守和乱下结论一样没用。
    blocking: bool = True

    @property
    def symbol(self) -> str:
        return {True: "✓", False: "✗", None: "?"}[self.met]


@dataclass
class EligibilityResult:
    program_key: str
    title: str
    university: str
    verdict: Verdict
    checks: list[Check] = field(default_factory=list)
    # caveats 装的是"数据回答不了、但用户必须知道"的事。
    # 把它单列出来，是为了不让这些不确定性混进 verdict 里被当成事实。
    caveats: list[str] = field(default_factory=list)
    source_url: str = ""
    data_fetched_at: str = ""

    @property
    def gaps(self) -> list[Check]:
        """明确不达标的项——用户最该看的部分。"""
        return [c for c in self.checks if c.met is False]

    @property
    def unknowns(self) -> list[Check]:
        """判不了的项——需要人工去官网核实的部分。"""
        return [c for c in self.checks if c.met is None]

    @property
    def blocking_unknowns(self) -> list[Check]:
        """判不了、且确实拦住了结论的项。"""
        return [c for c in self.checks if c.met is None and c.blocking]


# =============================================================================
# 证据提取
# =============================================================================

def _excerpt(text: str | None, keywords: tuple[str, ...], width: int = 160) -> str | None:
    """从原文里截出与某项要求相关的一段，作为证据附在结论后面。

    做法很朴素：找到第一个关键词，向两边各截一段。不追求精确的句子边界——
    证据的作用是让用户能顺着它去官网核对，不是替代官网。
    """
    if not text:
        return None
    lowered = text.lower()
    for keyword in keywords:
        index = lowered.find(keyword.lower())
        if index == -1:
            continue
        start = max(0, index - width // 3)
        end = min(len(text), index + width)
        snippet = text[start:end].strip()
        return ("…" if start > 0 else "") + snippet + ("…" if end < len(text) else "")
    # 一个关键词都没命中就退回开头一段，总比没有证据强
    return text[:width].strip() + ("…" if len(text) > width else "")


# =============================================================================
# 各项检查
# =============================================================================

def _check_academic(profile: StudentProfile, program: Program) -> Check:
    """学术成绩检查。

    三种情况，必须分开处理：
      A. 项目写了明确分数线      -> 可以直接比大小
      B. 项目只写了成绩等级      -> 判不了，因为等级换算连校内都不统一
      C. 项目什么都没写          -> 判不了
    """
    entry = program.entry
    student = (
        f"加权均分 {profile.wam_percent}%" if profile.wam_percent is not None
        else "未提供均分"
    )
    evidence = _excerpt(entry.raw, ("average", "WAM", "weighted", "mark", "degree"))

    # --- 情况 C：官网没有登载任何学术要求 ---
    if entry.min_wam_percent is None and entry.min_grade_band is None:
        note = (
            "handbook 未登载学术成绩要求" if not (entry.raw or "").strip()
            else "handbook 的入学要求里没有可比较的分数或等级"
        )
        return Check("学术成绩", None, "未查到明确分数线", student, evidence, note,
                     blocking=True)

    # --- 情况 B：只有等级，没有数字 ---
    # 这里是本模块最关键的一个判断。把 "credit average" 换算成 65% 再去比大小
    # 是很有诱惑力的做法，但我们自己的数据证明了这个换算不可靠：
    # Monash 的 credit 有 245 处是 60%、87 处是 65%。换算出来的结论看着精确，
    # 实际上可能差 5 分——而 5 分足以决定一个申请的成败。
    if entry.min_wam_percent is None:
        band = entry.min_grade_band or ""
        label = GRADE_BAND_LABEL.get(band, band)
        observed = GRADE_BAND_OBSERVED.get(program.university, {}).get(band)
        note = (
            f"官网只写了成绩等级「{label}」，没有给出具体分数。"
            "本校同类项目对该等级的取值并不统一，因此不做换算判定。"
        )
        if observed:
            low, high = observed
            note += f"（本数据集中该校此等级的观测区间为 {low:g}–{high:g}%，仅供参考）"
        return Check("学术成绩", None, f"{label}（无具体分数）", student, evidence, note)

    # --- 情况 A：有明确分数线，可以比较 ---
    required = f"加权均分不低于 {entry.min_wam_percent:g}%"
    if profile.wam_percent is None:
        return Check("学术成绩", None, required, student, evidence,
                     "学生未提供均分，无法判断")

    met = profile.wam_percent >= entry.min_wam_percent
    notes: list[str] = []
    if not met:
        notes.append(f"差 {entry.min_wam_percent - profile.wam_percent:.1f} 分")

    # 不少项目有多个入学档次（entry level），不同背景对应不同学制和学费。
    # 我们取的是最低那条门槛，所以必须提醒用户去看自己落在哪一档。
    if len(entry.entry_levels) > 1:
        notes.append(
            f"该项目有 {len(entry.entry_levels)} 个入学档次，学制和学费各不相同。"
            "此处比对的是最低门槛，请到官网确认你属于哪一档"
        )
    return Check("学术成绩", met, required, student, evidence,
                 "；".join(notes) if notes else None)


def _check_english(profile: StudentProfile, program: Program) -> Check:
    """语言成绩检查。总分和单项最低分要分别看——很多人栽在单项上。"""
    english = program.english
    evidence = _excerpt(english.raw, ("IELTS", "English", "TOEFL"))

    if english.ielts_overall is None:
        # UNSW 的 handbook 整个不登载语言要求，这不是解析失败，是数据源如此
        return Check(
            "语言成绩", None, "未查到雅思要求",
            f"雅思 {profile.ielts_overall}" if profile.ielts_overall else "未提供雅思",
            evidence,
            "该校 handbook 不登载语言要求，需到学校 international 页面查询",
        )

    required = f"雅思总分不低于 {english.ielts_overall:g}"
    if english.ielts_min_band is not None:
        required += f"，单项不低于 {english.ielts_min_band:g}"

    if profile.ielts_overall is None:
        return Check("语言成绩", None, required, "未提供雅思成绩", evidence,
                     "学生未提供雅思成绩，无法判断")

    actual = f"总分 {profile.ielts_overall:g}"
    if profile.ielts_min_band is not None:
        actual += f"，最低单项 {profile.ielts_min_band:g}"

    overall_met = profile.ielts_overall >= english.ielts_overall
    # 单项：只有在项目提了要求、且学生也提供了数据时才判。
    # 学生没提供单项分时不能默认它达标——那等于替用户做了乐观假设。
    band_met: bool | None = None
    if english.ielts_min_band is not None:
        if profile.ielts_min_band is None:
            band_met = None
        else:
            band_met = profile.ielts_min_band >= english.ielts_min_band

    if not overall_met:
        gap = english.ielts_overall - profile.ielts_overall
        return Check("语言成绩", False, required, actual, evidence, f"总分差 {gap:g}")
    if band_met is False:
        gap = english.ielts_min_band - profile.ielts_min_band
        return Check("语言成绩", False, required, actual, evidence,
                     f"总分达标，但单项差 {gap:g}——单项不达标同样会被拒")
    if band_met is None and english.ielts_min_band is not None:
        return Check("语言成绩", None, required, actual, evidence,
                     "总分达标，但学生未提供各单项分数，无法确认单项要求")
    return Check("语言成绩", True, required, actual, evidence)


def _check_discipline(profile: StudentProfile, program: Program) -> Check:
    """专业背景检查。

    注意这一项本质上判不了"客观正确"：某个具体专业算不算 cognate，
    要看各校自己的学科清单，那份清单不在 handbook 上。
    所以我们只能拿用户自己的回答去比对项目的要求。
    """
    requires = program.entry.requires_cognate_degree
    # 关键词顺序要跟着结论走：判"不限专业"就该引"not necessarily in"那句，
    # 而不是引另一档入口的 cognate 要求——否则证据会和结论自相矛盾。
    keywords = (
        ("not necessarily", "any discipline", "regardless of discipline")
        if requires is False
        else ("cognate", "relevant discipline", "related discipline", "discipline")
    )
    evidence = _excerpt(program.entry.raw, keywords)

    if requires is None:
        # handbook 没提专业限制。这属于"没有这条要求"而非"有要求但不知道门槛"，
        # 所以不阻断整体结论，只作为提示保留。
        return Check("专业背景", None, "未查到明确的专业背景要求",
                     "—", evidence, "handbook 未明确说明是否限制本科专业",
                     blocking=False)
    if requires is False:
        return Check("专业背景", True, "不限本科专业", "—", evidence,
                     "该项目接受非相关专业背景")

    required = "要求相关（cognate）专业背景"
    if profile.has_cognate_background is None:
        return Check("专业背景", None, required, "未说明", evidence,
                     "学生未说明本科专业是否相关。具体哪些专业算相关，"
                     "需查该校学科清单，handbook 未登载")
    if profile.has_cognate_background:
        return Check("专业背景", True, required, "本科为相关专业", evidence)
    return Check("专业背景", False, required, "本科非相关专业", evidence,
                 "可考虑该项目的非相关背景入口（如有）或加长学制版本")


# =============================================================================
# 主入口
# =============================================================================

def check_eligibility(
    profile: StudentProfile,
    program: Program,
    borderline_margin: float = 3.0,
) -> EligibilityResult:
    """判断学生是否满足某个项目的入学要求。

    borderline_margin 是个**产品决策，不是官方规则**：均分只高出这个幅度以内
    时标记为"边缘"，提醒用户别把它当保底校。学校从没说过"高 3 分才稳"。
    默认 3.0，可以按需要调。
    """
    checks = [
        _check_academic(profile, program),
        _check_english(profile, program),
        _check_discipline(profile, program),
    ]

    # ---------------------------------------------------------------------
    # 汇总成 verdict。顺序很重要：
    #   1. 只要有一项明确不达标 -> NOT_ELIGIBLE（这是决定性的）
    #   2. 否则只要有判不了的项 -> INSUFFICIENT_DATA（不能假装能判）
    #   3. 全部达标 -> 再看余量决定 ELIGIBLE 还是 BORDERLINE
    # ---------------------------------------------------------------------
    if any(c.met is False for c in checks):
        verdict = Verdict.NOT_ELIGIBLE
    elif any(c.met is None and c.blocking for c in checks):
        verdict = Verdict.INSUFFICIENT_DATA
    else:
        verdict = Verdict.ELIGIBLE
        required_wam = program.entry.min_wam_percent
        if (required_wam is not None and profile.wam_percent is not None
                and profile.wam_percent - required_wam < borderline_margin):
            verdict = Verdict.BORDERLINE

    # ---------------------------------------------------------------------
    # caveats：数据回答不了、但用户必须知道的事
    # ---------------------------------------------------------------------
    caveats: list[str] = []

    # 这一条是整个项目最重要的免责说明。官网写的 "65% (or equivalent)"，
    # 对中国学生来说 "or equivalent" 才是关键——各校对 985/211/双非有不同
    # 的分数线，而那份名单不在 handbook 上。
    if program.entry.min_wam_percent is not None:
        caveats.append(
            "官网分数线通常附带 “or equivalent”。各校对中国院校（985/211/双非）"
            "有不同的等效分数线，该名单不在 handbook 上，本结论未纳入院校分级因素。"
        )
    if profile.home_institution:
        caveats.append(
            f"你的本科院校「{profile.home_institution}」的分级尚未纳入判断"
            "——院校分级数据待补。"
        )
    if program.english.ielts_overall is None:
        caveats.append("语言要求需另行到该校 international 页面核实。")
    if profile.work_experience_years:
        caveats.append(
            f"你有 {profile.work_experience_years:g} 年工作经验，部分项目提供"
            "工作经验入口，本判断未纳入。"
        )
    caveats.append(
        f"数据抓取于 {program.fetched_at.date()}，入学要求以学校官方公布为准。"
    )

    return EligibilityResult(
        program_key=program.program_key,
        title=program.title,
        university=program.university,
        verdict=verdict,
        checks=checks,
        caveats=caveats,
        source_url=program.source_url,
        data_fetched_at=program.fetched_at.isoformat(),
    )


def format_result(result: EligibilityResult) -> str:
    """把结果排成人类可读的文本（命令行用）。"""
    label = {
        Verdict.ELIGIBLE: "符合要求",
        Verdict.BORDERLINE: "达标但余量小",
        Verdict.NOT_ELIGIBLE: "不符合要求",
        Verdict.INSUFFICIENT_DATA: "数据不足，无法判断",
    }[result.verdict]

    lines = [
        f"{result.program_key}  {result.title}",
        f"{result.university}",
        "",
        f"结论: {label}",
        "",
    ]
    for check in result.checks:
        lines.append(f"  {check.symbol} {check.name}")
        lines.append(f"      要求: {check.required}")
        lines.append(f"      你的: {check.actual}")
        if check.note:
            lines.append(f"      说明: {check.note}")
        if check.evidence:
            lines.append(f"      原文: {check.evidence}")
        lines.append("")
    if result.caveats:
        lines.append("  需要注意:")
        lines.extend(f"    - {c}" for c in result.caveats)
        lines.append("")
    lines.append(f"  来源: {result.source_url}")
    return "\n".join(lines)
