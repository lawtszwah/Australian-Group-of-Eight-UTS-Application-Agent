"""把数据层包装成 Claude 可调用的工具，并跑起 agent loop。

=============================================================================
这一层做什么、不做什么
=============================================================================
做：把「用自然语言提问」翻译成「调哪个工具、传什么参数」，再把工具返回的
    结构化结果组织成人话。
不做：任何形式的判断和计算。分数比大小在 eligibility.py 里用纯 Python 完成，
    录取要求从 SQLite 里查。模型一个数字都不许自己产出。

这条边界是整个项目可靠性的地基。模型擅长的是理解意图和组织语言，
不是记住 731 个项目的分数线——后者它一定会记错，而且错得很自信。

=============================================================================
工具的 docstring 就是 prompt
=============================================================================
@beta_tool 会从函数签名和 docstring 自动生成 input_schema，模型**只能看到
这些**。所以 docstring 不是写给人看的注释，是写给模型看的说明书：
参数含义、什么时候该用这个工具、返回值里有什么，都要写清楚。

工具写得好，system prompt 就可以很短。反过来，靠 system prompt 去补救
一个语焉不详的工具，往往补不回来。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from anthropic import Anthropic, beta_tool

from .db import Database
from .eligibility import StudentProfile, check_eligibility

MODEL = "claude-opus-5"

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "go8.db"

# 单次问答里最多允许模型调多少轮工具。防止它绕圈子把钱烧光。
MAX_TOOL_ROUNDS = 12


def _db() -> Database:
    return Database(DB_PATH)


def _fail(message: str, **extra: Any) -> str:
    """把错误当成数据返回，而不是抛异常。

    工具抛异常会中断整个 agent loop；返回一条错误说明，模型能读懂并自己
    改用别的参数重试——这是 agent 比普通函数调用更有韧性的地方。
    """
    return json.dumps({"error": message, **extra}, ensure_ascii=False)


# =============================================================================
# 工具
# =============================================================================

@beta_tool
def search_programs(
    keyword: str | None = None,
    level: str | None = None,
    university: str | None = None,
    max_ielts: float | None = None,
    max_wam: float | None = None,
    limit: int = 15,
) -> str:
    """按条件检索澳洲高校的项目，返回项目清单。

    目前收录 Monash 和 UNSW 两校，共 731 个项目。这是查找项目的唯一入口——
    不要凭记忆说出项目名称或代码，一律先用本工具检索。

    Args:
        keyword: 项目名或院系关键词，英文，如 "information technology"、"data science"。
        level: 学历层次。可选 bachelor（本科）、master（硕士，AQF 9）、
            graduate_diploma（研究生文凭，AQF 8）、graduate_certificate（研究生证书，AQF 8）、
            research（研究型）、other。注意研究生文凭/证书不是硕士，别混。
        university: 学校名关键词，如 "Monash"、"UNSW"。不填则两校都查。
        max_ielts: 用户的雅思总分。填了则只返回雅思要求不高于此分数的项目。
        max_wam: 用户的加权均分（百分制）。填了则只返回均分要求不高于此分数的项目。
        limit: 最多返回多少条，默认 15。

    Returns:
        JSON 数组，每条含 program_key、标题、学校、层次、均分要求、雅思要求、
        handbook 年份和来源 URL。program_key 是后续调用其它工具的标识。
        注意：均分或雅思要求为 null 表示官网未登载该项要求，不代表没有要求。
    """
    with _db() as db:
        rows = db.search(
            keyword=keyword, university=university, level=level,
            max_ielts=max_ielts, max_wam=max_wam, limit=min(limit, 40),
        )
    results = [
        {
            "program_key": r["program_key"],
            "title": r["title"],
            "university": r["university"],
            "level": r["level"],
            "min_wam_percent": r["min_wam_percent"],
            "ielts_overall": r["ielts_overall"],
            "handbook_year": r["handbook_year"],
            "source_url": r["source_url"],
        }
        for r in rows
    ]
    if not results:
        return _fail("没有匹配的项目", hint="试试放宽关键词，或去掉 max_wam / max_ielts 限制")
    return json.dumps({"count": len(results), "programs": results}, ensure_ascii=False)


@beta_tool
def get_program_details(program_key: str) -> str:
    """获取单个项目的完整信息，含入学要求的官网原文。

    当用户想了解某个项目的细节，或你需要引用官网原文作为依据时使用。
    原文（entry_requirements_raw / english_requirements_raw）是唯一可信的依据，
    结构化字段只是我们的解析结果。两者不一致时以原文为准。

    Args:
        program_key: 项目标识，如 "monash:C6001"。由 search_programs 返回。

    Returns:
        JSON 对象，含项目基本信息、结构化的入学要求、以及要求原文和来源 URL。
    """
    with _db() as db:
        program = db.get(program_key)
    if program is None:
        return _fail(f"库里没有 {program_key}", hint="先用 search_programs 查到正确的 program_key")

    return json.dumps({
        "program_key": program.program_key,
        "title": program.title,
        "university": program.university,
        "level": program.level,
        "handbook_year": program.handbook_year,
        "duration_full_time": program.duration_full_time,
        "credit_points": program.credit_points,
        "campus": program.campus,
        "intakes": program.intakes,
        "cricos_code": program.cricos_code,
        "entry": {
            "min_wam_percent": program.entry.min_wam_percent,
            "min_grade_band": program.entry.min_grade_band,
            "requires_cognate_degree": program.entry.requires_cognate_degree,
            "raw": program.entry.raw,
        },
        "english": {
            "ielts_overall": program.english.ielts_overall,
            "ielts_min_band": program.english.ielts_min_band,
            "toefl_ibt": program.english.toefl_ibt,
            "pte_overall": program.english.pte_overall,
            "raw": program.english.raw,
        },
        "source_url": program.source_url,
        "fetched_at": program.fetched_at.isoformat(),
        "unresolved_fields": program.gaps(),
    }, ensure_ascii=False)


@beta_tool
def check_program_eligibility(
    program_key: str,
    wam_percent: float | None = None,
    ielts_overall: float | None = None,
    ielts_min_band: float | None = None,
    has_cognate_background: bool | None = None,
    home_institution: str | None = None,
    work_experience_years: float | None = None,
) -> str:
    """判断某个学生背景是否满足某个项目的入学要求。

    **凡是涉及"够不够格""差多少分"的问题，必须调用本工具，不要自己比大小。**
    判定由确定性代码完成，结果稳定可复现，并附官网原文作为依据。

    只填用户明确说过的字段。不知道的留空，工具会返回"数据不足"——
    这比替用户假设一个分数要好得多。

    Args:
        program_key: 项目标识，如 "monash:C6001"。
        wam_percent: 加权均分（百分制），如 78。
        ielts_overall: 雅思总分。
        ielts_min_band: 雅思各单项里的最低分。很多人总分够但单项不够被拒，
            所以这一项很重要，用户提到了就要填。
        has_cognate_background: 本科是否为该项目要求的相关专业。
        home_institution: 本科院校名称。目前不参与判定，只用于生成提醒。
        work_experience_years: 相关工作年限。目前不参与判定，只用于生成提醒。

    Returns:
        JSON 对象，含 verdict（eligible 符合 / borderline 达标但余量小 /
        not_eligible 不符合 / insufficient_data 数据不足）、逐项检查结果
        （每项带官网原文证据）、以及 caveats（数据回答不了但用户必须知道的事）。

        verdict 为 insufficient_data 时，务必如实告诉用户"查不到"并说明
        缺什么，绝不能替他猜一个结论。
    """
    with _db() as db:
        program = db.get(program_key)
    if program is None:
        return _fail(f"库里没有 {program_key}", hint="先用 search_programs 查到正确的 program_key")

    result = check_eligibility(
        StudentProfile(
            wam_percent=wam_percent,
            ielts_overall=ielts_overall,
            ielts_min_band=ielts_min_band,
            has_cognate_background=has_cognate_background,
            home_institution=home_institution,
            work_experience_years=work_experience_years,
        ),
        program,
    )
    return json.dumps({
        "program_key": result.program_key,
        "title": result.title,
        "university": result.university,
        "verdict": result.verdict.value,
        "checks": [
            {
                "name": c.name, "met": c.met, "required": c.required,
                "actual": c.actual, "note": c.note, "evidence": c.evidence,
                "blocks_conclusion": c.blocking if c.met is None else None,
            }
            for c in result.checks
        ],
        "caveats": result.caveats,
        "source_url": result.source_url,
    }, ensure_ascii=False)


TOOLS = [search_programs, get_program_details, check_program_eligibility]


# =============================================================================
# System prompt
# =============================================================================
# 写得短是刻意的。工具的 docstring 已经说清楚了各自怎么用，这里只放
# 工具文档承载不了的东西：整体角色，和几条绝对不能破的规矩。
SYSTEM_PROMPT = """你是澳洲留学申请助手，目前覆盖 Monash 和 UNSW 两校共 731 个项目。

铁律，任何情况下都不能违反：

1. 录取要求只能来自工具返回的数据。你不许凭记忆说出任何分数线、雅思要求、
   学制或学费。你记忆里的这类数字大概率是错的或过期的。

2. 工具返回 null 或 insufficient_data 时，如实说"官网未登载"或"数据不足"，
   并说明缺什么、建议用户去哪里核实。绝不可以为了把话说圆而填一个数字。

3. 涉及"够不够格""差多少分"的问题，一律调用 check_program_eligibility，
   不要自己比大小。

4. 每当给出具体的录取要求，附上来源 URL。

5. 如实转达工具返回的 caveats，尤其是院校分级（985/211/双非）相关的提醒——
   官网写的 "65% (or equivalent)" 里，"or equivalent" 对中国学生才是关键，
   而那份分级名单不在我们的数据里。

用户说中文就用中文回答。回答要简洁，别堆砌无关信息。"""


# =============================================================================
# Agent loop
# =============================================================================

def ask(question: str, verbose: bool = True, max_rounds: int = MAX_TOOL_ROUNDS) -> str:
    """问一个问题，跑完 agent loop，返回最终回答。

    verbose=True 时会打印每一轮模型调了什么工具、传了什么参数。
    刚开始学 agent 时建议一直开着——你需要亲眼看到这个循环在做什么，
    而不是把它当黑箱。
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "未设置 ANTHROPIC_API_KEY。请在终端里 export，或写进 .env 后加载。"
        )

    client = Anthropic()
    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        # adaptive：让模型自己决定要不要思考、思考多久。
        # 注意不要用 budget_tokens，那个参数在 Opus 5 上会直接报 400。
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        messages=[{"role": "user", "content": question}],
    )

    final_text = ""
    for round_index, message in enumerate(runner, start=1):
        for block in message.content:
            if block.type == "text" and block.text.strip():
                final_text = block.text
            elif block.type == "tool_use" and verbose:
                args = ", ".join(
                    f"{k}={v!r}" for k, v in block.input.items() if v is not None
                )
                print(f"  [第 {round_index} 轮] 调用 {block.name}({args})")

        # 超过轮数上限就停。不设上限的话，一个绕圈子的 loop 能烧掉很多钱。
        if round_index >= max_rounds:
            if verbose:
                print(f"  [已达 {max_rounds} 轮上限，停止]")
            break

    return final_text
