"""评分器自己的测试。

评分器有 bug 比模型有 bug 更麻烦：模型出错你能看出来，评分器出错会让你
对着一个假分数做决策。第一次跑评估就抓到过一个误判——模型引用
『"65%（or equivalent）" 这类表述』被当成了编造数字——所以这些边界要锁住。
"""

from __future__ import annotations

import pytest

from go8agent.evaluation import (
    Case,
    CaseResult,
    _numbers_in,
    grade,
    load_cases,
    summarize,
)


def make_trace(*results: str):
    return [{"round": 1, "tool": "get_program_details", "args": {}, "result": r}
            for r in results]


class TestGoldenSetFile:
    def test_loads_and_has_ids(self):
        cases = load_cases()
        assert len(cases) >= 15
        assert len({c.id for c in cases}) == len(cases), "用例 id 有重复"

    def test_every_case_has_at_least_one_grader(self):
        """一条不判任何性质的用例等于没写，跑了也是白跑。"""
        for c in load_cases():
            assert (c.must_call or c.must_mention or c.must_not_mention
                    or c.must_say_any or c.must_cite or c.no_invented_numbers), \
                f"{c.id} 没有任何判定条件"


class TestNumberExtraction:
    def test_picks_up_percentages_and_decimals(self):
        assert _numbers_in("均分 65% 雅思 6.5") == {"65", "6.5"}

    def test_ignores_bare_integers(self):
        """学制「2 年」、学分「96」、序号「1.」到处都是，全查会淹没真信号。"""
        assert _numbers_in("学制 2 年，96 学分，第 3 条") == set()

    def test_quoted_numbers_are_not_assertions(self):
        """引号内是引用/举例，不是断言。放着这个误判不管，指标会失去可信度。"""
        text = '官网写的是 "65%（or equivalent）" 这类表述。你的均分 78% 达标。'
        assert _numbers_in(text) == {"65", "78"}
        assert _numbers_in(text, drop_quoted=True) == {"78"}


class TestHallucinationGrader:
    def test_number_absent_from_tools_is_flagged(self):
        case = Case(id="t", question="要求多少", no_invented_numbers=True)
        result = grade(case, "要求均分 88%", make_trace('{"min_wam_percent": 65}'))
        assert not result.passed
        assert "88" in result.failures[0]

    def test_number_present_in_tools_passes(self):
        case = Case(id="t", question="要求多少", no_invented_numbers=True)
        result = grade(case, "要求均分 65%", make_trace('{"min_wam_percent": 65}'))
        assert result.passed

    def test_number_from_the_question_is_not_invented(self):
        """用户说「我 78 分」，模型复述 78 不算编造。"""
        case = Case(id="t", question="我均分 78 能申吗", no_invented_numbers=True)
        result = grade(case, "你的 78% 达标", make_trace('{"min_wam_percent": 65}'))
        assert result.passed


class TestToolCallGrader:
    def test_missing_tool_call_fails(self):
        """凭记忆答对和查了工具答对，可靠性天差地别——必须能分开。"""
        case = Case(id="t", question="q", must_call=["check_program_eligibility"])
        result = grade(case, "可以申请", [])
        assert not result.passed
        assert "没有调用" in result.failures[0]

    def test_any_listed_tool_satisfies(self):
        """同一意图常有多条合理路径，要求走固定路径会把风格差异误判成错误。"""
        case = Case(id="t", question="q",
                    must_call=["get_program_details", "search_programs"])
        result = grade(case, "答案", make_trace("{}"))
        assert result.passed


class TestTextGraders:
    def test_must_say_any_needs_one_hit(self):
        case = Case(id="t", question="q", must_say_any=["未登载", "查不到"])
        assert grade(case, "官网未登载该要求", []).passed
        assert not grade(case, "要求是 6.5", []).passed

    def test_must_not_mention(self):
        case = Case(id="t", question="q", must_not_mention=["完全符合"])
        assert not grade(case, "你完全符合要求", []).passed

    def test_citation_required(self):
        case = Case(id="t", question="q", must_cite=True)
        assert not grade(case, "要求均分 65%", []).passed
        assert grade(case, "见 https://handbook.monash.edu/x", []).passed


class TestSummary:
    def test_counts_and_groups_failures(self):
        cases = [Case(id=f"c{i}", question="q") for i in range(3)]
        results = [CaseResult(case=cases[0], answer="", trace=[]),
                   CaseResult(case=cases[1], answer="", trace=[],
                              failures=["回答里出现了工具返回中不存在的数字: ['88']"]),
                   CaseResult(case=cases[2], answer="", trace=[], error="boom")]
        s = summarize(results)
        assert (s["total"], s["passed"], s["errored"]) == (3, 1, 1)
        assert s["failures_by_kind"]["编造数字"] == 1
