"""工具层测试。

这些测试**不调模型、不需要 API key**——它们检查的是工具本身的契约：
schema 对不对、错误有没有被当成数据返回、返回值里有没有来源。

模型行为的测试属于另一回事（golden set 评估），那是下一阶段的事。
这里锁住的是"无论模型怎么调，工具都不会骗它"。
"""

from __future__ import annotations

import json

import pytest

from go8agent.agent import (
    SYSTEM_PROMPT,
    TOOLS,
    check_program_eligibility,
    get_program_details,
    search_programs,
)


class TestToolSchemas:
    """schema 由函数签名和 docstring 自动生成，模型只能看到这些。"""

    def test_all_tools_have_descriptions(self):
        for tool in TOOLS:
            assert tool.description, f"{tool.name} 没有描述，模型将不知道它是干什么的"

    def test_every_parameter_is_documented(self):
        """漏写一个参数的说明，模型就会瞎猜该传什么。"""
        for tool in TOOLS:
            for name, spec in tool.input_schema["properties"].items():
                assert spec.get("description"), f"{tool.name} 的参数 {name} 缺少说明"

    def test_program_key_is_required_where_it_matters(self):
        assert get_program_details.input_schema["required"] == ["program_key"]
        assert "program_key" in check_program_eligibility.input_schema["required"]

    def test_search_has_no_required_params(self):
        """检索应当允许只给一个关键词就用，不该逼模型凑齐参数。"""
        assert not search_programs.input_schema.get("required")


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
