"""针对真实页面快照的回归测试。

固化两个真实页面作为 fixture，解析逻辑一改就能立刻发现有没有解析错。
测试完全离线——单元测试不该依赖学校的服务器。
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from go8agent.models import EnglishRequirement, EntryRequirement
from go8agent.sources import courseloop

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    with gzip.open(FIXTURES / name, "rt", encoding="utf-8", errors="replace") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def monash():
    return courseloop.parse(
        load("monash_C6001.html.gz"), "monash",
        "https://handbook.monash.edu/2026/courses/C6001",
    )


@pytest.fixture(scope="module")
def unsw():
    return courseloop.parse(
        load("unsw_8543.html.gz"), "unsw",
        "https://www.handbook.unsw.edu.au/postgraduate/programs/2026/8543",
    )


class TestMonash:
    def test_identity(self, monash):
        assert monash.program_key == "monash:C6001"
        assert monash.title == "Master of Information Technology"
        assert monash.level == "master"
        assert monash.cricos_code == "079055K"
        assert monash.credit_points == 96

    def test_english_parsed(self, monash):
        assert monash.english.ielts_overall == 6.5
        assert monash.english.ielts_min_band == 6.0
        assert monash.english.toefl_ibt == 79
        assert monash.english.pte_overall == 58

    def test_entry_parsed(self, monash):
        # 官方原文是 "at least a credit (60%) average"
        assert monash.entry.min_wam_percent == 60.0
        # "not necessarily in IT" -> 不要求本专业背景
        assert monash.entry.requires_cognate_degree is False
        assert len(monash.entry.entry_levels) >= 2

    def test_handbook_year_recorded(self, monash):
        """必须记住这条数据来自哪一年的 handbook。

        各校 sitemap 同时挂着多个年份，停办已久的项目只存在于旧年份里。
        不记年份，就会把停办项目当成在招项目推荐出去。
        """
        assert monash.handbook_year == 2026

    def test_provenance(self, monash):
        assert monash.source_url.startswith("https://handbook.monash.edu")
        assert monash.source_updated_at is not None
        assert monash.entry.raw and "Entry level 1" in monash.entry.raw

    def test_no_gaps(self, monash):
        assert monash.gaps() == []


class TestUNSW:
    def test_identity(self, unsw):
        assert unsw.program_key == "unsw:8543"
        # handbook 的 title 字段只有 "Information Technology"，
        # 完整学位名在 award_title_single 里
        assert unsw.title == "Master of Information Technology"
        assert unsw.level == "master"
        assert unsw.cricos_code == "061294J"

    def test_entry_parsed(self, unsw):
        # 原文 "weighted average mark of 65% (or equivalent)"
        assert unsw.entry.min_wam_percent == 65.0
        assert unsw.entry.requires_cognate_degree is True

    def test_english_absent_not_invented(self, unsw):
        """UNSW handbook 不登载语言要求——必须是 None，不能是编出来的数字。"""
        assert unsw.english.ielts_overall is None
        assert "english" in unsw.gaps()

    def test_duration_placeholder_rejected(self, unsw):
        """该项目 handbook 的时长字段是空的，渲染成 "0 "，不能当成数据。"""
        assert unsw.duration_full_time is None

    def test_faculty_is_a_name_not_a_blurb(self, unsw):
        assert unsw.faculty == "Faculty of Engineering"


class TestParsers:
    @pytest.mark.parametrize(
        "text,overall,band",
        [
            ("IELTS (Academic): 6.5 overall (no band lower than 6.0)", 6.5, 6.0),
            ("IELTS 7.0 overall, no band lower than 6.5", 7.0, 6.5),
            ("An overall IELTS score of 6.0 with a minimum of 5.5 in each band", 6.0, 5.5),
        ],
    )
    def test_ielts(self, text, overall, band):
        req = courseloop.parse_english(text)
        assert (req.ielts_overall, req.ielts_min_band) == (overall, band)

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("with a weighted average mark of 65%", 65.0),
            ("at least a credit (60%) average", 60.0),
            ("a WAM of at least 75%", 75.0),
            # Monash 商学院大量项目这样写，没有百分号
            ("in a relevant discipline, with a Weighted Average Mark (WAM) of 60.", 60.0),
            # 数字在 average 之前，且前面没有 minimum/at least
            ("An Australian bachelor degree, in a cognate discipline with a 60% average.", 60.0),
            # 澳洲成绩等级写法：pass/credit/distinction 后面括号里才是分数
            ("with a distinction grade (70%) average or above", 70.0),
            ("a recognised bachelor's degree with a pass (50%) average", 50.0),
            ("equivalent to a UNSW credit average of 65%", 65.0),
            ("no numbers here at all", None),
            # Graduate Certificate 常常真的没有分数要求，不能瞎凑一个
            ("An Australian bachelor degree or equivalent qualification.", None),
        ],
    )
    def test_wam(self, text, expected):
        assert courseloop.parse_entry(text).min_wam_percent == expected

    def test_out_of_range_values_rejected(self):
        """正则抓到离谱的数就该丢掉，而不是入库污染数据。"""
        assert EnglishRequirement(ielts_overall=65.0).ielts_overall is None
        assert EnglishRequirement(ielts_overall=6.3).ielts_overall is None  # 雅思没有 .3
        assert EntryRequirement(min_wam_percent=650.0).min_wam_percent is None

    @pytest.mark.parametrize("title,expected", [
        ("Master of Information Technology", "master"),
        ("Graduate Diploma in Mine Ventilation", "graduate_diploma"),
        ("Graduate Certificate of Business", "graduate_certificate"),
        ("Bachelor of Science", "bachelor"),
    ])
    def test_level_separates_aqf8_from_masters(self, title, expected):
        """研究生文凭/证书是 AQF 8，不能和硕士混为一谈。"""
        assert courseloop.guess_level(title, {}, []) == expected

    @pytest.mark.parametrize("text,band,wam", [
        ("a bachelor degree with a credit average", "credit", None),
        ("Bachelor degree with a distinction average", "distinction", None),
        # 同时给了等级和数字时，两者都要留下
        ("equivalent to a UNSW credit average of 65%", "credit", 65.0),
    ])
    def test_grade_band_recorded_not_converted(self, text, band, wam):
        """"credit average" 是等级不是分数。换算成 65% 属于推断，不能在解析层做。"""
        req = courseloop.parse_entry(text)
        assert req.min_grade_band == band
        assert req.min_wam_percent == wam

    def test_entry_domain_filter_excludes_rpl(self):
        """UNSW 的学分减免条款不能混进入学要求——里面的百分数会被误读成分数线。"""
        payload = [
            {"domain": "Limitations on Recognition of Prior Learning",
             "requirements": [{"description": "The maximum credit transfer is 50% of the specialisation."}]},
            {"domain": "Minimum Entry Requirements",
             "requirements": [{"description": "Bachelor Degree with a weighted average mark of 65%."}]},
        ]
        text = courseloop.extract_entry_text(payload)
        assert "credit transfer" not in text
        assert courseloop.parse_entry(text).min_wam_percent == 65.0

    def test_entry_domain_missing_returns_empty(self):
        """没有入学要求域时必须返回空，不能拿别的域顶替。"""
        payload = [{"domain": "Limitations on Recognition of Prior Learning",
                    "requirements": [{"description": "A maximum of 12 UOC of RPL."}]}]
        assert courseloop.extract_entry_text(payload) == ""

    def test_bad_html_raises(self):
        with pytest.raises(ValueError, match="__NEXT_DATA__"):
            courseloop.parse("<html><body>not a handbook</body></html>", "monash", "x")

    def test_unknown_university_raises(self):
        with pytest.raises(ValueError, match="未知学校"):
            courseloop.parse(load("monash_C6001.html.gz"), "usyd", "x")
