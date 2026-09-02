"""数据模型。

设计原则：每条录取要求都必须能追溯到来源 URL 和原文片段。
解析不出来的字段一律留 None，绝不猜测——下游 agent 看到 None 会说"未查到"，
看到一个编出来的数字则会一本正经地误导用户。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Graduate Diploma / Certificate 是 AQF 8，和硕士（AQF 9）不是一回事，
# 对申请人来说是完全不同的产品，必须分开
Level = Literal[
    "bachelor", "graduate_certificate", "graduate_diploma",
    "master", "research", "other",
]


class EnglishRequirement(BaseModel):
    """语言要求。raw 是原文，其余是尽力解析出的结构化值。"""

    raw: str | None = None
    ielts_overall: float | None = None
    ielts_min_band: float | None = None
    toefl_ibt: int | None = None
    pte_overall: int | None = None
    pte_min_band: int | None = None

    @field_validator("ielts_overall", "ielts_min_band")
    @classmethod
    def _sane_ielts(cls, v: float | None) -> float | None:
        # 雅思分数只可能落在 0-9 且以 .0/.5 结尾，越界说明正则抓错了东西
        if v is not None and not (4.0 <= v <= 9.0 and v * 2 == int(v * 2)):
            return None
        return v

    @property
    def is_parsed(self) -> bool:
        return self.ielts_overall is not None


class EntryRequirement(BaseModel):
    """学术入学要求。"""

    raw: str | None = None
    min_wam_percent: float | None = None
    # UNSW 等学校常写 "with a credit average"——页面上没有数字，只有等级。
    # 换算成 65% 是推断而非事实，所以只记录等级本身，换算留给下游显式进行。
    min_grade_band: str | None = None
    requires_cognate_degree: bool | None = None
    entry_levels: list[str] = Field(default_factory=list)

    @field_validator("min_wam_percent")
    @classmethod
    def _sane_wam(cls, v: float | None) -> float | None:
        if v is not None and not (40 <= v <= 100):
            return None
        return v

    @property
    def is_parsed(self) -> bool:
        return self.min_wam_percent is not None or self.min_grade_band is not None


class Program(BaseModel):
    """一个可申请的项目。

    program_key 形如 "monash:C6001"，是全库唯一主键。
    """

    program_key: str
    university: str
    source: str
    code: str
    title: str
    level: Level = "other"

    # handbook 年份。各校的 sitemap 同时挂着多个年份的页面，停办已久的项目
    # 只存在于旧年份里——不记年份就会把停办项目当成在招项目推荐出去。
    handbook_year: int | None = None
    cricos_code: str | None = None
    credit_points: int | None = None
    duration_full_time: str | None = None
    faculty: str | None = None
    campus: list[str] = Field(default_factory=list)
    intakes: list[str] = Field(default_factory=list)

    english: EnglishRequirement = Field(default_factory=EnglishRequirement)
    entry: EntryRequirement = Field(default_factory=EntryRequirement)

    # 溯源三件套——缺一不可
    source_url: str
    source_updated_at: str | None = None  # 学校自己标注的数据更新时间
    fetched_at: datetime

    # 哪些字段没解析出来，方便人工优先复核
    def gaps(self) -> list[str]:
        missing = []
        if not self.english.is_parsed:
            missing.append("english")
        if not self.entry.is_parsed:
            missing.append("entry_wam")
        if not self.credit_points:
            missing.append("credit_points")
        return missing
