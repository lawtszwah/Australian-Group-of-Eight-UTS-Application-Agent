"""资格判断的测试。

重点不在"算得对不对"（比大小很难写错），而在**边界行为**：
数据不足时会不会假装能判、只有等级时会不会偷偷换算成分数、
单项不达标会不会被总分达标盖过去。这些才是会真正误导用户的地方。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from go8agent.eligibility import (
    StudentProfile,
    Verdict,
    check_eligibility,
)
from go8agent.models import EnglishRequirement, EntryRequirement, Program


def make_program(**kwargs) -> Program:
    """构造一个测试用项目，默认是一个要求明确的普通硕士。"""
    defaults = dict(
        program_key="test:1", university="Monash University", source="courseloop",
        code="1", title="Master of Testing", level="master",
        source_url="https://example.test/1",
        fetched_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
        english=EnglishRequirement(
            raw="IELTS 6.5 overall (no band lower than 6.0)",
            ielts_overall=6.5, ielts_min_band=6.0,
        ),
        entry=EntryRequirement(
            raw="An Australian bachelor degree with a weighted average mark of 70%.",
            min_wam_percent=70.0,
        ),
    )
    defaults.update(kwargs)
    return Program(**defaults)


FULL = StudentProfile(wam_percent=80.0, ielts_overall=7.0, ielts_min_band=6.5,
                      has_cognate_background=True)


class TestBasicVerdicts:
    def test_clearly_eligible(self):
        result = check_eligibility(FULL, make_program())
        assert result.verdict == Verdict.ELIGIBLE
        assert result.gaps == []

    def test_below_wam_is_not_eligible(self):
        profile = StudentProfile(wam_percent=65.0, ielts_overall=7.0,
                                 ielts_min_band=6.5, has_cognate_background=True)
        result = check_eligibility(profile, make_program())
        assert result.verdict == Verdict.NOT_ELIGIBLE
        assert "差 5.0 分" in result.gaps[0].note

    def test_borderline_when_margin_is_thin(self):
        """刚过线不该和高出十几分一样被标成"符合要求"。"""
        profile = StudentProfile(wam_percent=71.0, ielts_overall=7.0,
                                 ielts_min_band=6.5, has_cognate_background=True)
        assert check_eligibility(profile, make_program()).verdict == Verdict.BORDERLINE

    def test_margin_is_configurable(self):
        """边缘幅度是产品设定，不是官方规则，必须可调。"""
        profile = StudentProfile(wam_percent=71.0, ielts_overall=7.0,
                                 ielts_min_band=6.5, has_cognate_background=True)
        result = check_eligibility(profile, make_program(), borderline_margin=0.5)
        assert result.verdict == Verdict.ELIGIBLE


class TestInsufficientDataIsNotRejection:
    """本模块最重要的一组测试：数据不足绝不能被当成不符合。"""

    def test_missing_student_wam_is_unknown_not_rejected(self):
        profile = StudentProfile(ielts_overall=7.0, ielts_min_band=6.5,
                                 has_cognate_background=True)
        result = check_eligibility(profile, make_program())
        assert result.verdict == Verdict.INSUFFICIENT_DATA
        assert result.verdict != Verdict.NOT_ELIGIBLE

    def test_program_without_requirements_is_unknown(self):
        program = make_program(entry=EntryRequirement(raw=""))
        result = check_eligibility(FULL, program)
        assert result.verdict == Verdict.INSUFFICIENT_DATA
        assert result.unknowns[0].note == "handbook 未登载学术成绩要求"

    def test_missing_english_data_is_unknown(self):
        """UNSW handbook 整个不登载语言要求——这不是解析失败，是数据源如此。"""
        program = make_program(english=EnglishRequirement())
        result = check_eligibility(FULL, program)
        assert result.verdict == Verdict.INSUFFICIENT_DATA
        assert "international" in result.unknowns[0].note

    def test_hard_failure_outranks_unknown(self):
        """有明确不达标项时，结论是"不符合"而不是"数据不足"——前者是决定性的。"""
        profile = StudentProfile(wam_percent=50.0)  # 均分明显不够，其余未提供
        result = check_eligibility(profile, make_program())
        assert result.verdict == Verdict.NOT_ELIGIBLE


class TestGradeBandIsNeverConverted:
    """只有等级没有分数时，绝不能换算出一个数字来判定。

    依据是我们自己的数据：Monash 的 "credit" 有 245 处是 60%、87 处是 65%。
    换算出来的结论看着精确，实际可能差 5 分，而 5 分足以决定成败。
    """

    def test_band_only_program_is_unknown(self):
        program = make_program(entry=EntryRequirement(
            raw="a bachelor degree with a credit average",
            min_grade_band="credit",
        ))
        result = check_eligibility(FULL, program)
        assert result.verdict == Verdict.INSUFFICIENT_DATA
        academic = result.checks[0]
        assert academic.met is None
        assert "不做换算判定" in academic.note

    def test_observed_range_is_shown_as_reference_only(self):
        program = make_program(entry=EntryRequirement(
            raw="credit average", min_grade_band="credit"))
        note = check_eligibility(FULL, program).checks[0].note
        assert "60–65%" in note and "仅供参考" in note

    def test_high_scorer_still_unknown_not_eligible(self):
        """就算学生 95 分远超任何可能的换算值，也仍然是"数据不足"。

        看起来保守，但这是对的：我们不知道那条线在哪，就不能宣称达标。
        """
        program = make_program(entry=EntryRequirement(
            raw="credit average", min_grade_band="credit"))
        profile = StudentProfile(wam_percent=95.0, ielts_overall=8.0,
                                 ielts_min_band=7.5, has_cognate_background=True)
        assert check_eligibility(profile, program).verdict == Verdict.INSUFFICIENT_DATA


class TestEnglishBands:
    def test_band_failure_not_masked_by_overall_pass(self):
        """总分够、单项不够照样被拒——很多人正是栽在这里。"""
        profile = StudentProfile(wam_percent=80.0, ielts_overall=7.0,
                                 ielts_min_band=5.5, has_cognate_background=True)
        result = check_eligibility(profile, make_program())
        assert result.verdict == Verdict.NOT_ELIGIBLE
        assert "单项差 0.5" in result.gaps[0].note

    def test_missing_band_score_is_not_assumed_pass(self):
        """学生没提供单项分时，不能替他做乐观假设。"""
        profile = StudentProfile(wam_percent=80.0, ielts_overall=7.0,
                                 has_cognate_background=True)
        result = check_eligibility(profile, make_program())
        assert result.verdict == Verdict.INSUFFICIENT_DATA
        assert "未提供各单项分数" in result.unknowns[0].note


class TestBlockingVsInformationalUnknown:
    """"查不到"有两种含义，不能一视同仁。"""

    def test_unstated_discipline_does_not_block_verdict(self):
        """handbook 没提专业限制 -> 属于"没有这条要求"，不该拖成"数据不足"。"""
        result = check_eligibility(FULL, make_program())
        assert result.verdict == Verdict.ELIGIBLE
        discipline = result.checks[2]
        assert discipline.met is None and discipline.blocking is False
        # 仍然出现在 unknowns 里作为提示，只是不阻断结论
        assert discipline in result.unknowns
        assert discipline not in result.blocking_unknowns

    def test_stated_requirement_with_unknown_answer_does_block(self):
        """项目明确要求相关专业、学生没说 -> 这是真的不知道，必须阻断。"""
        program = make_program(entry=EntryRequirement(
            raw="a bachelor degree in a cognate discipline with a WAM of 70%",
            min_wam_percent=70.0, requires_cognate_degree=True))
        profile = StudentProfile(wam_percent=80.0, ielts_overall=7.0, ielts_min_band=6.5)
        result = check_eligibility(profile, program)
        assert result.verdict == Verdict.INSUFFICIENT_DATA
        assert result.blocking_unknowns


class TestDiscipline:
    def test_cognate_required_but_missing(self):
        program = make_program(entry=EntryRequirement(
            raw="a bachelor degree in a cognate discipline with a WAM of 70%",
            min_wam_percent=70.0, requires_cognate_degree=True))
        profile = StudentProfile(wam_percent=80.0, ielts_overall=7.0,
                                 ielts_min_band=6.5, has_cognate_background=False)
        result = check_eligibility(profile, program)
        assert result.verdict == Verdict.NOT_ELIGIBLE

    def test_open_to_all_disciplines_passes(self):
        program = make_program(entry=EntryRequirement(
            raw="a bachelor degree not necessarily in IT, WAM 70%",
            min_wam_percent=70.0, requires_cognate_degree=False))
        profile = StudentProfile(wam_percent=80.0, ielts_overall=7.0,
                                 ielts_min_band=6.5)  # 未说明专业背景也不影响
        assert check_eligibility(profile, program).verdict == Verdict.ELIGIBLE


class TestProvenance:
    def test_every_check_carries_evidence(self):
        result = check_eligibility(FULL, make_program())
        for check in result.checks:
            assert check.evidence, f"{check.name} 缺少原文证据"

    def test_or_equivalent_caveat_always_present(self):
        """"or equivalent" 才是中国学生的关键信息，必须每次都提醒。"""
        result = check_eligibility(FULL, make_program())
        assert any("or equivalent" in c for c in result.caveats)

    def test_institution_tier_gap_is_flagged(self):
        profile = StudentProfile(wam_percent=80.0, ielts_overall=7.0,
                                 ielts_min_band=6.5, has_cognate_background=True,
                                 home_institution="某双非大学")
        result = check_eligibility(profile, make_program())
        assert any("院校分级数据待补" in c for c in result.caveats)

    def test_result_records_source_and_date(self):
        result = check_eligibility(FULL, make_program())
        assert result.source_url == "https://example.test/1"
        assert any("2026-09-02" in c for c in result.caveats)
