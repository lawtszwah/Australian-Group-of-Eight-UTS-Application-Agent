"""工具层测试。

这些测试**不调模型、不需要 API key**——它们检查的是工具本身的契约：
schema 对不对、错误有没有被当成数据返回、返回值里有没有来源。

模型行为的测试属于另一回事（golden set 评估），那是下一阶段的事。
这里锁住的是"无论模型怎么调，工具都不会骗它"。
"""

from __future__ import annotations

import json

import pytest

from go8agent.agent import SYSTEM_PROMPT
from go8agent.tools import (
    IMPLEMENTATIONS,
    TOOL_SCHEMAS,
    check_program_eligibility,
    dispatch,
    get_program_details,
    search_programs,
    to_anthropic_tools,
    to_openai_tools,
)


class TestToolSchemas:
    """schema 是手写的，模型只能看到这些字符串。"""

    def test_all_tools_have_descriptions(self):
        for schema in TOOL_SCHEMAS:
            assert schema["description"], f"{schema['name']} 没有描述"

    def test_every_parameter_is_documented(self):
        """漏写一个参数的说明，模型就会瞎猜该传什么。"""
        for schema in TOOL_SCHEMAS:
            for name, spec in schema["parameters"]["properties"].items():
                assert spec.get("description"), f"{schema['name']} 的参数 {name} 缺少说明"

    def test_schema_matches_function_signature(self):
        """手写 schema 的风险是和函数签名走偏，这里逐个比对。"""
        import inspect

        for schema in TOOL_SCHEMAS:
            func = IMPLEMENTATIONS[schema["name"]]
            sig_params = set(inspect.signature(func).parameters)
            schema_params = set(schema["parameters"]["properties"])
            assert schema_params == sig_params, (
                f"{schema['name']} 的 schema 与函数签名不一致："
                f"schema 多出 {schema_params - sig_params}，缺少 {sig_params - schema_params}"
            )

    def test_program_key_is_required_where_it_matters(self):
        by_name = {s["name"]: s for s in TOOL_SCHEMAS}
        assert by_name["get_program_details"]["parameters"]["required"] == ["program_key"]
        assert "program_key" in by_name["check_program_eligibility"]["parameters"]["required"]

    def test_search_has_no_required_params(self):
        """检索应当允许只给一个关键词就用，不该逼模型凑齐参数。"""
        by_name = {s["name"]: s for s in TOOL_SCHEMAS}
        assert by_name["search_programs"]["parameters"]["required"] == []


class TestProviderFormats:
    """同一套工具要能同时喂给两个供应商。"""

    def test_openai_format_nests_function(self):
        tools = to_openai_tools()
        assert all(t["type"] == "function" for t in tools)
        assert {t["function"]["name"] for t in tools} == set(IMPLEMENTATIONS)
        assert "parameters" in tools[0]["function"]

    def test_anthropic_format_uses_input_schema(self):
        tools = to_anthropic_tools()
        assert {t["name"] for t in tools} == set(IMPLEMENTATIONS)
        assert "input_schema" in tools[0]

    def test_both_formats_carry_the_same_schemas(self):
        """两边必须是同一份 schema，否则模型对比评估就没意义了。"""
        openai_schemas = {t["function"]["name"]: t["function"]["parameters"]
                          for t in to_openai_tools()}
        anthropic_schemas = {t["name"]: t["input_schema"] for t in to_anthropic_tools()}
        assert openai_schemas == anthropic_schemas


class TestArgumentValidation:
    """DeepSeek 官方文档写明：模型可能编造 schema 里没有的参数，需自行校验。

    没有这一层，一个编出来的参数会直接变成 TypeError 打断整个 loop。
    """

    def test_hallucinated_parameter_is_rejected_with_allowed_list(self):
        payload = json.loads(dispatch("search_programs",
                                      {"keyword": "IT", "sort_by": "ranking"}))
        assert "sort_by" in payload["error"]
        assert "keyword" in payload["allowed"]  # 告诉模型有哪些合法参数

    def test_unknown_tool_name_lists_available_tools(self):
        payload = json.loads(dispatch("book_flight", {}))
        assert "available" in payload

    def test_missing_required_parameter_is_reported(self):
        payload = json.loads(dispatch("get_program_details", {}))
        assert "program_key" in payload["error"]

    def test_invalid_enum_value_is_rejected(self):
        payload = json.loads(dispatch("search_programs", {"level": "phd"}))
        assert payload["allowed"] == ["bachelor", "master", "graduate_diploma",
                                      "graduate_certificate", "research", "other"]

    def test_numeric_string_is_coerced_not_rejected(self):
        """模型常把数字写成字符串，这种能救就救，不必浪费一轮。"""
        payload = json.loads(dispatch("search_programs",
                                      {"keyword": "data science", "max_wam": "70"}))
        assert payload["count"] > 0

    def test_boolean_is_not_accepted_as_number(self):
        """bool 是 int 的子类，不特判就会让 True 混过 number 检查。"""
        payload = json.loads(dispatch("search_programs", {"max_wam": True}))
        assert "布尔值" in payload["error"]

    def test_explicit_null_is_treated_as_omitted(self):
        payload = json.loads(dispatch("search_programs",
                                      {"keyword": "data science", "level": None}))
        assert payload["count"] > 0


class TestErrorsAreDataNotExceptions:
    """工具抛异常会中断整个 agent loop；返回错误说明，模型能自己重试。"""

    def test_unknown_program_key_returns_error_object(self):
        payload = json.loads(get_program_details("monash:NOPE"))
        assert "error" in payload and "hint" in payload

    def test_eligibility_with_unknown_key_returns_error_object(self):
        payload = json.loads(check_program_eligibility("unsw:NOPE", wam_percent=80))
        assert "error" in payload

    def test_no_match_returns_hint_not_empty_list(self):
        """空结果要给出下一步建议，否则模型只会原样再问一遍。"""
        payload = json.loads(search_programs(keyword="quidditch"))
        assert "error" in payload and "hint" in payload


@pytest.mark.skipif(
    not (__import__("pathlib").Path(__file__).parents[1] / "data" / "go8.db").exists(),
    reason="需要本地数据库，先跑 crawl",
)
class TestAgainstRealData:
    def test_search_returns_source_url_on_every_row(self):
        payload = json.loads(search_programs(keyword="information technology",
                                             level="master"))
        assert payload["count"] > 0
        for program in payload["programs"]:
            assert program["source_url"].startswith("http")
            assert program["handbook_year"]

    def test_details_include_raw_text_for_verification(self):
        """结构化字段只是解析结果，原文才是可信依据，必须一并返回。"""
        payload = json.loads(get_program_details("monash:C6001"))
        assert payload["entry"]["raw"]
        assert payload["english"]["raw"]
        assert payload["source_url"].startswith("https://handbook.monash.edu")

    def test_eligibility_returns_verdict_and_evidence(self):
        payload = json.loads(check_program_eligibility(
            "monash:C6001", wam_percent=78, ielts_overall=6.5, ielts_min_band=6.0))
        assert payload["verdict"] == "eligible"
        assert any(c["evidence"] for c in payload["checks"])
        assert any("or equivalent" in c for c in payload["caveats"])

    def test_missing_data_surfaces_as_insufficient_not_guess(self):
        """UNSW handbook 不登载语言要求——工具必须如实说不知道。"""
        payload = json.loads(check_program_eligibility(
            "unsw:8543", wam_percent=80, ielts_overall=7.0, has_cognate_background=True))
        assert payload["verdict"] == "insufficient_data"
        english = next(c for c in payload["checks"] if c["name"] == "语言成绩")
        assert english["met"] is None and english["blocks_conclusion"] is True


class TestSystemPrompt:
    def test_states_the_core_rules(self):
        for rule in ["不许凭记忆", "insufficient_data", "来源 URL", "or equivalent"]:
            assert rule in SYSTEM_PROMPT, f"system prompt 里缺少「{rule}」这条约束"


class TestDotenv:
    """.env 加载的边界行为。"""

    def test_parses_quotes_and_comments(self, tmp_path):
        from go8agent.config import load_dotenv

        env = tmp_path / ".env"
        env.write_text(
            "# 注释行\n"
            "\n"
            "PLAIN=abc\n"
            "SINGLE='sk-single'\n"
            "DOUBLE=\"sk-double\"\n"
            "WITH_SPACES = spaced \n",
            encoding="utf-8",
        )
        import os

        for key in ["PLAIN", "SINGLE", "DOUBLE", "WITH_SPACES"]:
            os.environ.pop(key, None)
        load_dotenv(env)
        assert os.environ["PLAIN"] == "abc"
        assert os.environ["SINGLE"] == "sk-single"
        assert os.environ["DOUBLE"] == "sk-double"
        assert os.environ["WITH_SPACES"] == "spaced"
        for key in ["PLAIN", "SINGLE", "DOUBLE", "WITH_SPACES"]:
            os.environ.pop(key, None)

    def test_real_env_wins_over_dotenv(self, tmp_path):
        """真实环境变量优先——临时 export 一个别的 key 就能立刻生效。"""
        from go8agent.config import load_dotenv
        import os

        os.environ["GO8_TEST_KEY"] = "from-shell"
        env = tmp_path / ".env"
        env.write_text("GO8_TEST_KEY=from-dotenv\n", encoding="utf-8")
        load_dotenv(env)
        assert os.environ["GO8_TEST_KEY"] == "from-shell"
        os.environ.pop("GO8_TEST_KEY", None)

    def test_missing_file_is_not_an_error(self, tmp_path):
        from go8agent.config import load_dotenv

        assert load_dotenv(tmp_path / "nope.env") == []
