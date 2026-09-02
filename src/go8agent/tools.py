"""与模型供应商无关的工具层。

=============================================================================
为什么工具定义要独立于供应商
=============================================================================
Anthropic 和 DeepSeek(OpenAI 兼容) 的工具格式不同，但**工具本身是同一套**。
把实现和 schema 集中在这里，两边各写一个薄适配器去转格式，好处有二：

  1. 换供应商不用重写工具
  2. 做评估时可以拿同一套工具去比不同模型——这正是判断"该用哪个模型"
     的唯一靠谱方式

=============================================================================
为什么 schema 是手写的
=============================================================================
Anthropic SDK 的 @beta_tool 能从签名自动生成 schema，很方便，但那样 schema
就绑死在 Anthropic 的包上了。这里改成手写：多几行代码，换来的是明确——
**你看到的就是模型看到的**，没有中间的生成逻辑。

手写的风险是 schema 和函数签名会走偏，所以有一个测试专门比对两者。

=============================================================================
为什么每次调用都要校验参数
=============================================================================
DeepSeek 官方文档明确写着：模型"不一定生成合法 JSON，也可能编造出 schema
里没有定义的参数"，要求调用方自行校验。Anthropic 的模型这方面稳得多，
但校验是零成本的保险——而且校验失败时返回一条错误说明，模型还能自己改正重试。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .db import Database
from .eligibility import StudentProfile, check_eligibility

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "go8.db"


def _db() -> Database:
    return Database(DB_PATH)


def _fail(message: str, **extra: Any) -> str:
    """把错误当成数据返回，而不是抛异常。

    抛异常会中断整个 agent loop；返回一条错误说明，模型能读懂并改用别的
    参数重试——这是 agent 比普通函数调用更有韧性的地方。
    """
    return json.dumps({"error": message, **extra}, ensure_ascii=False)


# =============================================================================
# 工具实现
# =============================================================================

def search_programs(
    keyword: str | None = None,
    level: str | None = None,
    university: str | None = None,
    max_ielts: float | None = None,
    max_wam: float | None = None,
    limit: int = 15,
) -> str:
    with _db() as db:
        rows = db.search(
            keyword=keyword, university=university, level=level,
            max_ielts=max_ielts, max_wam=max_wam, limit=min(int(limit), 40),
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
        return _fail("没有匹配的项目",
                     hint="试试放宽关键词，或去掉 max_wam / max_ielts 限制")
    return json.dumps({"count": len(results), "programs": results}, ensure_ascii=False)


def get_program_details(program_key: str) -> str:
    with _db() as db:
        program = db.get(program_key)
    if program is None:
        return _fail(f"库里没有 {program_key}",
                     hint="先用 search_programs 查到正确的 program_key")
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


def check_program_eligibility(
    program_key: str,
    wam_percent: float | None = None,
    ielts_overall: float | None = None,
    ielts_min_band: float | None = None,
    has_cognate_background: bool | None = None,
    home_institution: str | None = None,
    work_experience_years: float | None = None,
) -> str:
    with _db() as db:
        program = db.get(program_key)
    if program is None:
        return _fail(f"库里没有 {program_key}",
                     hint="先用 search_programs 查到正确的 program_key")

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


# =============================================================================
# Schema —— 模型能看到的全部内容
# =============================================================================
# description 不是注释，是**说明书**。模型只能看到这些字符串，所以每个参数
# 的含义、什么时候该用这个工具、返回值里有什么，都得写清楚。
# 工具描述写好了，system prompt 就可以很短。

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "search_programs",
        "description": (
            "按条件检索澳洲高校的项目，返回项目清单。目前收录 Monash 和 UNSW "
            "两校共 731 个项目。这是查找项目的唯一入口——不要凭记忆说出项目名称"
            "或代码，一律先用本工具检索。返回的每条记录都含 program_key（后续"
            "调用其它工具的标识）和 source_url。注意：均分或雅思要求为 null 表示"
            "官网未登载该项要求，不代表没有要求。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "项目名或院系关键词，英文，如 'information technology'、'data science'",
                },
                "level": {
                    "type": "string",
                    "enum": ["bachelor", "master", "graduate_diploma",
                             "graduate_certificate", "research", "other"],
                    "description": (
                        "学历层次。master 是硕士(AQF 9)；graduate_diploma 和 "
                        "graduate_certificate 是研究生文凭/证书(AQF 8)，不是硕士，别混"
                    ),
                },
                "university": {
                    "type": "string",
                    "description": "学校名关键词，如 'Monash'、'UNSW'。不填则两校都查",
                },
                "max_ielts": {
                    "type": "number",
                    "description": "用户的雅思总分。填了则只返回雅思要求不高于此分数的项目",
                },
                "max_wam": {
                    "type": "number",
                    "description": "用户的加权均分(百分制)。填了则只返回均分要求不高于此分数的项目",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回多少条，默认 15，上限 40",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_program_details",
        "description": (
            "获取单个项目的完整信息，含入学要求的官网原文。当用户想了解某个项目的"
            "细节，或你需要引用官网原文作为依据时使用。原文(entry.raw / english.raw)"
            "是唯一可信的依据，结构化字段只是我们的解析结果；两者不一致时以原文为准。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "program_key": {
                    "type": "string",
                    "description": "项目标识，如 'monash:C6001'。由 search_programs 返回",
                },
            },
            "required": ["program_key"],
            "additionalProperties": False,
        },
    },
    {
        "name": "check_program_eligibility",
        "description": (
            "判断某个学生背景是否满足某个项目的入学要求。凡是涉及「够不够格」"
            "「差多少分」的问题，必须调用本工具，不要自己比大小。判定由确定性代码"
            "完成，结果稳定可复现，并附官网原文作为依据。"
            "只填用户明确说过的字段，不知道的留空——工具会返回 insufficient_data，"
            "这比替用户假设一个分数好得多。"
            "返回的 verdict 有四种：eligible(符合) / borderline(达标但余量小) / "
            "not_eligible(不符合) / insufficient_data(数据不足)。"
            "遇到 insufficient_data 时，务必如实告诉用户查不到什么，绝不能替他猜结论。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "program_key": {
                    "type": "string",
                    "description": "项目标识，如 'monash:C6001'",
                },
                "wam_percent": {
                    "type": "number",
                    "description": "加权均分(百分制)，如 78",
                },
                "ielts_overall": {"type": "number", "description": "雅思总分"},
                "ielts_min_band": {
                    "type": "number",
                    "description": (
                        "雅思各单项里的最低分。很多人总分够但单项不够被拒，"
                        "所以用户提到了就要填"
                    ),
                },
                "has_cognate_background": {
                    "type": "boolean",
                    "description": "本科是否为该项目要求的相关专业",
                },
                "home_institution": {
                    "type": "string",
                    "description": "本科院校名称。目前不参与判定，只用于生成提醒",
                },
                "work_experience_years": {
                    "type": "number",
                    "description": "相关工作年限。目前不参与判定，只用于生成提醒",
                },
            },
            "required": ["program_key"],
            "additionalProperties": False,
        },
    },
]

IMPLEMENTATIONS: dict[str, Callable[..., str]] = {
    "search_programs": search_programs,
    "get_program_details": get_program_details,
    "check_program_eligibility": check_program_eligibility,
}

_SCHEMA_BY_NAME = {schema["name"]: schema for schema in TOOL_SCHEMAS}

# JSON Schema 类型 -> 可接受的 Python 类型
_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
}


def dispatch(name: str, arguments: dict[str, Any]) -> str:
    """校验参数后调用工具，返回 JSON 字符串。

    这是所有供应商共用的入口。校验不是形式主义——DeepSeek 官方文档明确写着
    模型"可能编造出 schema 里没有定义的参数"。没有这一层，一个编出来的参数
    会直接变成 TypeError 把整个 loop 打断。

    校验失败时返回错误说明而不是抛异常，模型读到之后往往能自己改正重试。
    """
    schema = _SCHEMA_BY_NAME.get(name)
    if schema is None:
        return _fail(f"没有名为 {name} 的工具",
                     available=sorted(_SCHEMA_BY_NAME))

    if not isinstance(arguments, dict):
        return _fail("参数必须是一个 JSON 对象")

    properties = schema["parameters"]["properties"]

    # 1. 编造出来的参数：直接拒绝，并告诉模型有哪些合法参数
    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        return _fail(f"传入了未定义的参数: {unknown}",
                     allowed=sorted(properties))

    # 2. 必填参数
    missing = [k for k in schema["parameters"].get("required", []) if k not in arguments]
    if missing:
        return _fail(f"缺少必填参数: {missing}")

    # 3. 类型与枚举值
    cleaned: dict[str, Any] = {}
    for key, value in arguments.items():
        if value is None:
            continue  # 显式传 None 等同于不传，交给函数默认值
        spec = properties[key]
        expected = _TYPE_MAP.get(spec.get("type", "string"), (object,))
        # bool 是 int 的子类，不做这层排除会让 True 混过 number 的检查
        if spec.get("type") in {"number", "integer"} and isinstance(value, bool):
            return _fail(f"参数 {key} 需要数字，收到布尔值")
        if not isinstance(value, expected):
            # 模型常把数字写成字符串，这种能救就救
            if spec.get("type") in {"number", "integer"} and isinstance(value, str):
                try:
                    value = float(value) if spec["type"] == "number" else int(value)
                except ValueError:
                    return _fail(f"参数 {key} 需要{spec['type']}，收到 {value!r}")
            else:
                return _fail(f"参数 {key} 类型不对，需要 {spec.get('type')}，"
                             f"收到 {type(value).__name__}")
        if "enum" in spec and value not in spec["enum"]:
            return _fail(f"参数 {key} 的取值不合法: {value!r}", allowed=spec["enum"])
        cleaned[key] = value

    try:
        return IMPLEMENTATIONS[name](**cleaned)
    except Exception as exc:  # 兜底：工具内部出错也不该炸掉整个 loop
        return _fail(f"工具执行出错: {exc}")


# =============================================================================
# 供应商格式转换
# =============================================================================

def to_anthropic_tools() -> list[dict[str, Any]]:
    """Anthropic 的格式：input_schema。"""
    return [
        {
            "name": s["name"],
            "description": s["description"],
            "input_schema": s["parameters"],
        }
        for s in TOOL_SCHEMAS
    ]


def to_openai_tools() -> list[dict[str, Any]]:
    """OpenAI / DeepSeek 的格式：嵌一层 function，字段叫 parameters。"""
    return [
        {
            "type": "function",
            "function": {
                "name": s["name"],
                "description": s["description"],
                "parameters": s["parameters"],
            },
        }
        for s in TOOL_SCHEMAS
    ]
