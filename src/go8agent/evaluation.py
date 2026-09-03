"""Golden set 评估。

=============================================================================
为什么不能用普通单元测试
=============================================================================
模型不是确定性的：同一个问题问两次，措辞可能完全不同。所以判的不是
"回答等于某个字符串"，而是**回答必须满足哪些性质**。

这里所有评分器都是确定性的——没有用 LLM 当裁判。这是这个项目的运气：
因为 check_eligibility 是纯代码，数据库里又有全部真实数字，
所以"判定对不对""有没有编数字"都能机器判。多数 agent 项目没这个条件。

=============================================================================
最重要的两个评分器
=============================================================================
1. must_call —— 它有没有真的去查工具。
   一个凭记忆编出正确答案的模型，和一个查了工具的模型，可靠性天差地别：
   前者只是这次蒙对了。只看最终答案分不出这两者。

2. no_invented_numbers —— 幻觉率。
   把回答里的百分数和 x.x 小数全抠出来，逐个去工具返回里找。找不到的
   就是编的。这是本项目的命门指标。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CASES_FILE = ROOT / "evals" / "cases.yaml"

# 只检查"有风险"的数字，不检查所有数字。
#   百分数     -> 均分线，编错了直接误导
#   x.x 小数   -> 雅思分数，同上
# 刻意不检查裸整数：学制"2 年"、学分"96"、列表序号"1."之类到处都是，
# 全查会产生大量假警报，反而让这个指标失去意义。
PERCENT_RE = re.compile(r"(\d{2,3})\s*%")
DECIMAL_RE = re.compile(r"\b(\d\.\d)\b")

# 引号内的内容视为「引用/举例」，不计入断言。
#
# 由来：第一次跑评估时，模型写了『官网英文写的是 "65%（or equivalent）"
# 这类表述时』——它在举例说明一种表述形式，不是在声称某项目要求 65%。
# 严格的数字检查把它判成了幻觉，是误判。
#
# 代价是模型理论上可以把编造的内容藏进引号里躲过检查。但「捏造一段引文」
# 和「随口说错一个数字」是两种不同性质的失败，前者罕见得多；
# 而放着这个误判不管，会让幻觉率这个指标失去可信度——
# **一个经常误报的指标，用不了几次就没人看了。**
QUOTED_RE = re.compile(r"[\"\u201c\u201d\u300c\u300d\u2018\u2019'][^\"\u201c\u201d\u300c\u300d\u2018\u2019'\n]{0,80}?[\"\u201c\u201d\u300c\u300d\u2018\u2019']")


# 每百万 token 的美元单价，取**峰时价**（保守估计，实际可能更低）。
# DeepSeek 的非峰时价是峰时的一半；缓存命中的输入便宜一个数量级。
# 价格核实于 2026-09，会变——这里只用于给出量级感，不是账单。
PRICING: dict[str, dict[str, float]] = {
    "deepseek-v4-flash": {"input": 0.44, "cached_input": 0.014, "output": 1.32},
    "deepseek-v4-pro": {"input": 1.32, "cached_input": 0.044, "output": 3.96},
    "claude-opus-5": {"input": 5.0, "output": 25.0},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
}


def estimate_cost(model: str, usage: dict[str, int]) -> float | None:
    """估算美元成本。未知模型返回 None——宁可不显示，也不显示一个错的数。"""
    price = PRICING.get(model)
    if not price:
        return None
    cached = usage.get("cached_input_tokens", 0)
    # 有缓存拆分就分开算，没有就全按未缓存价（保守）
    uncached = usage.get("uncached_input_tokens",
                         usage.get("input_tokens", 0) - cached)
    return (
        uncached / 1e6 * price["input"]
        + cached / 1e6 * price.get("cached_input", price["input"])
        + usage.get("output_tokens", 0) / 1e6 * price["output"]
    )


@dataclass
class Case:
    id: str
    question: str
    ground_truth: str | None = None
    must_call: list[str] = field(default_factory=list)
    must_mention: list[str] = field(default_factory=list)
    must_not_mention: list[str] = field(default_factory=list)
    must_say_any: list[str] = field(default_factory=list)
    must_cite: bool = False
    no_invented_numbers: bool = False


@dataclass
class CaseResult:
    case: Case
    answer: str
    trace: list[dict[str, Any]]
    failures: list[str] = field(default_factory=list)
    error: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    attempt: int = 1

    @property
    def passed(self) -> bool:
        return not self.failures and self.error is None

    @property
    def tools_called(self) -> list[str]:
        return [step["tool"] for step in self.trace]


def load_cases(path: Path | None = None) -> list[Case]:
    raw = yaml.safe_load((path or CASES_FILE).read_text(encoding="utf-8"))
    return [Case(**item) for item in raw]


# =============================================================================
# 评分器。每个返回失败原因列表，空列表表示通过。
# =============================================================================

def grade_tool_calls(case: Case, result: CaseResult) -> list[str]:
    """它有没有真的去查工具。

    must_call 里任意一个被调用即算通过——同一个意图常有多条合理路径
    （比如先 search 再 get_details，或直接 get_details）。要求走某条
    固定路径会把"风格差异"误判成"错误"。
    """
    if not case.must_call:
        return []
    called = set(result.tools_called)
    if called & set(case.must_call):
        return []
    return [f"没有调用 {case.must_call} 里的任何工具（实际调了 {sorted(called) or '无'}）"]


def grade_mentions(case: Case, result: CaseResult) -> list[str]:
    failures = []
    for text in case.must_mention:
        if text.lower() not in result.answer.lower():
            failures.append(f"回答里没有出现 {text!r}")
    for text in case.must_not_mention:
        if text.lower() in result.answer.lower():
            failures.append(f"回答里出现了不该有的 {text!r}")
    if case.must_say_any and not any(
        t.lower() in result.answer.lower() for t in case.must_say_any
    ):
        failures.append(f"回答里没有命中任何一个必要说法 {case.must_say_any}")
    return failures


def grade_citation(case: Case, result: CaseResult) -> list[str]:
    if not case.must_cite:
        return []
    return [] if "http" in result.answer else ["回答里没有附来源 URL"]


def _numbers_in(text: str, drop_quoted: bool = False) -> set[str]:
    if drop_quoted:
        text = QUOTED_RE.sub(" ", text)
    return {m.group(1) for m in PERCENT_RE.finditer(text)} | {
        m.group(1) for m in DECIMAL_RE.finditer(text)
    }


def grade_no_invented_numbers(case: Case, result: CaseResult) -> list[str]:
    """回答里的每个风险数字，都必须能在工具返回或问题原文里找到。

    找不到 = 模型自己编的。这是本项目最该盯的指标。
    """
    if not case.no_invented_numbers:
        return []

    # 允许的来源：工具返回的全部内容 + 用户问题本身（用户说"我 78 分"，
    # 模型复述 78 不算编造）
    grounded = case.question + " ".join(step["result"] for step in result.trace)
    grounded_numbers = _numbers_in(grounded)
    # 工具返回是 JSON，数字可能写成 78.0 / 6.5 等形式，统一比对时也看原始文本
    invented = sorted(
        n for n in _numbers_in(result.answer, drop_quoted=True)
        if n not in grounded_numbers and n not in grounded
    )
    if invented:
        return [f"回答里出现了工具返回中不存在的数字: {invented}"]
    return []


GRADERS = [grade_tool_calls, grade_mentions, grade_citation, grade_no_invented_numbers]


def grade(case: Case, answer: str, trace: list[dict[str, Any]]) -> CaseResult:
    result = CaseResult(case=case, answer=answer, trace=trace)
    for grader in GRADERS:
        result.failures.extend(grader(case, result))
    return result


# =============================================================================
# 跑一整轮
# =============================================================================

def run_case(case: Case, provider: str | None, model: str | None,
             attempt: int = 1) -> CaseResult:
    from .agent import ask

    trace: list[dict[str, Any]] = []
    usage: dict[str, int] = {}
    try:
        answer = ask(case.question, provider=provider, verbose=False,
                     model=model, trace=trace, usage=usage)
    except Exception as exc:
        result = CaseResult(case=case, answer="", trace=trace, usage=usage,
                            attempt=attempt)
        result.error = f"{type(exc).__name__}: {exc}"
        return result
    result = grade(case, answer, trace)
    result.usage = usage
    result.attempt = attempt
    return result


@dataclass
class CaseStability:
    """同一条题跑 N 次的汇总。

    分三类，因为它们要采取的行动完全不同：
      稳定通过    不用管
      稳定失败    确定性问题，改 prompt 或改工具能修
      **不稳定**  同样输入时对时错。这类最该优先看，因为它意味着系统在
                  这个场景下本质上不可靠，而单次评估会随机地把它报成
                  "通过"，给你一个虚假的安全感。
    """

    case_id: str
    runs: list[CaseResult]

    @property
    def passes(self) -> int:
        return sum(r.passed for r in self.runs)

    @property
    def total(self) -> int:
        return len(self.runs)

    @property
    def verdict(self) -> str:
        if self.passes == self.total:
            return "stable_pass"
        if self.passes == 0:
            return "stable_fail"
        return "flaky"

    @property
    def avg_tool_calls(self) -> float:
        return round(sum(len(r.trace) for r in self.runs) / self.total, 1)


def group_by_case(results: list[CaseResult]) -> list[CaseStability]:
    grouped: dict[str, list[CaseResult]] = {}
    for r in results:
        grouped.setdefault(r.case.id, []).append(r)
    return [CaseStability(case_id=k, runs=v) for k, v in grouped.items()]


def summarize(results: list[CaseResult], model: str | None = None) -> dict[str, Any]:
    total = len(results)
    passed = sum(r.passed for r in results)
    errored = sum(r.error is not None for r in results)
    # 按失败类型归类，看问题集中在哪儿比看总分更有用
    by_kind: dict[str, int] = {}
    for r in results:
        for f in r.failures:
            kind = ("编造数字" if "不存在的数字" in f
                    else "未调用工具" if "没有调用" in f
                    else "缺来源" if "来源 URL" in f
                    else "说法不符")
            by_kind[kind] = by_kind.get(kind, 0) + 1
    stability = group_by_case(results)
    by_verdict: dict[str, list[str]] = {"stable_pass": [], "flaky": [], "stable_fail": []}
    for st in stability:
        by_verdict[st.verdict].append(st.case_id)

    combined: dict[str, int] = {}
    for r in results:
        for k, v in r.usage.items():
            combined[k] = combined.get(k, 0) + v
    cost = estimate_cost(model, combined) if model else None

    return {
        "runs": total,
        "cases": len(stability),
        "repeat": round(total / len(stability)) if stability else 0,
        "passed": passed,
        "failed": total - passed,
        "errored": errored,
        "pass_rate": round(passed / total, 3) if total else 0.0,
        "stable_pass": len(by_verdict["stable_pass"]),
        "flaky": len(by_verdict["flaky"]),
        "stable_fail": len(by_verdict["stable_fail"]),
        "flaky_ids": by_verdict["flaky"],
        "stable_fail_ids": by_verdict["stable_fail"],
        "failures_by_kind": dict(sorted(by_kind.items(), key=lambda x: -x[1])),
        "avg_tool_calls": round(sum(len(r.trace) for r in results) / total, 1) if total else 0,
        "usage": combined,
        "estimated_cost_usd": round(cost, 4) if cost is not None else None,
    }


MARK = {"stable_pass": "  ", "flaky": "≈ ", "stable_fail": "✗ "}


def format_report(results: list[CaseResult], summary: dict[str, Any]) -> str:
    lines = ["", "=" * 72]
    for st in group_by_case(results):
        lines.append(f"{MARK[st.verdict]}[{st.passes}/{st.total}] {st.case_id}"
                     f"   (平均工具 {st.avg_tool_calls} 次)")
        for r in st.runs:
            if r.error:
                lines.append(f"        第{r.attempt}次 异常: {r.error}")
            for f in r.failures:
                lines.append(f"        第{r.attempt}次 - {f}")
    lines.append("=" * 72)
    lines.append(
        f"{summary['cases']} 条题 x {summary['repeat']} 次 = {summary['runs']} 次运行"
        f"   通过率 {summary['pass_rate']:.0%}"
    )
    lines.append(
        f"稳定通过 {summary['stable_pass']}"
        f"   不稳定 {summary['flaky']}"
        f"   稳定失败 {summary['stable_fail']}"
        f"   平均工具调用 {summary['avg_tool_calls']} 次"
    )
    if summary["flaky_ids"]:
        lines.append("不稳定（最该优先处理）: " + ", ".join(summary["flaky_ids"]))
    if summary["stable_fail_ids"]:
        lines.append("稳定失败: " + ", ".join(summary["stable_fail_ids"]))
    if summary["failures_by_kind"]:
        lines.append("失败归类: " + "  ".join(
            f"{k} {v}" for k, v in summary["failures_by_kind"].items()))

    u = summary.get("usage") or {}
    if u:
        parts = [f"请求 {u.get('requests', 0)} 次",
                 f"输入 {u.get('input_tokens', 0):,}",
                 f"输出 {u.get('output_tokens', 0):,}"]
        if u.get("cached_input_tokens"):
            parts.append(f"其中缓存命中 {u['cached_input_tokens']:,}")
        lines.append("token: " + "  ".join(parts))
    if summary.get("estimated_cost_usd") is not None:
        per_run = summary["estimated_cost_usd"] / max(summary["runs"], 1)
        lines.append(f"估算成本: ${summary['estimated_cost_usd']:.4f}"
                     f"（每次运行约 ${per_run:.4f}，按峰时价，实际可能更低）")
    return "\n".join(lines)
