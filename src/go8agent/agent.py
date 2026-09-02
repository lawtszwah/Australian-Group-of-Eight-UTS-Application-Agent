"""Agent loop。支持 DeepSeek 与 Anthropic 两个供应商。

=============================================================================
模型在这套系统里的职责
=============================================================================
做：理解用户的自然语言、决定调哪个工具、把工具返回的结构化结果组织成人话。
不做：任何判断和计算。分数比大小在 eligibility.py 里用纯 Python 完成，
     录取要求从 SQLite 里查。模型一个数字都不许自己产出。

这条边界是整个项目可靠性的地基。模型擅长理解意图和组织语言，
不是记住 731 个项目的分数线——后者它一定会记错，而且错得很自信。

=============================================================================
为什么两个供应商的 loop 是分开手写的
=============================================================================
Anthropic SDK 有 tool_runner 能自动跑完循环；DeepSeek 走 OpenAI 兼容接口，
得自己写。与其硬造一个统一抽象，不如各写一个几十行的循环——它们本来就是
两套不同的消息格式，强行统一只会多一层看不透的间接。

而且手写这个循环对入门是好事：agent loop 的全部内容就是
「发消息 -> 看有没有 tool_call -> 执行 -> 把结果塞回去 -> 再发」，
亲手写一遍就不会再把它当黑箱。
"""

from __future__ import annotations

import json
import os
from typing import Any, Literal

from .config import require_api_key
from .tools import dispatch, to_anthropic_tools, to_openai_tools

Provider = Literal["deepseek", "anthropic"]

# DeepSeek 目前的模型（2026-09 核实）：
#   deepseek-v4-flash  便宜、快，够用
#   deepseek-v4-pro    更强，贵约 3 倍
# 旧的 deepseek-chat / deepseek-reasoner 已经不是当前命名。
DEEPSEEK_MODEL = "deepseek-v4-flash"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

ANTHROPIC_MODEL = "claude-opus-5"

# 单次问答里最多允许模型调多少轮工具。防止它绕圈子把钱烧光。
MAX_TOOL_ROUNDS = 12


# =============================================================================
# System prompt
# =============================================================================
# 写得短是刻意的。工具的 description 已经说清楚了各自怎么用，这里只放
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

6. 加免责声明不等于可以说。不许写"该校通常是 6.5""一般要求 70 分左右"
   这类带保留的记忆数字——哪怕你紧接着声明"仅供参考""以官网为准"也不行。
   用户扫一眼记住的是那个数字，不是那句声明；而这个数字很可能是错的。
   正确做法是只说"我们的数据里没有这一项，需到 XX 页面核实"，然后就停住，
   不要补一个数字。

用户说中文就用中文回答。回答要简洁，别堆砌无关信息。"""


def _log_call(round_index: int, name: str, arguments: dict[str, Any], verbose: bool) -> None:
    if not verbose:
        return
    shown = ", ".join(f"{k}={v!r}" for k, v in arguments.items() if v is not None)
    print(f"  [第 {round_index} 轮] 调用 {name}({shown})")


def _record(trace: list[dict[str, Any]] | None, round_index: int, name: str,
            arguments: dict[str, Any], result: str) -> None:
    """记录一次工具调用，供评估打分用。

    评估要判的不只是"最终答案对不对"，更重要的是"它有没有真的去查"。
    一个凭记忆编出正确答案的模型，和一个查了工具的模型，可靠性天差地别——
    前者只是这次蒙对了。没有 trace 就分不出这两者。
    """
    if trace is not None:
        trace.append({"round": round_index, "tool": name,
                      "args": arguments, "result": result})


# =============================================================================
# DeepSeek（OpenAI 兼容接口）
# =============================================================================

def run_deepseek(
    question: str,
    verbose: bool = True,
    max_rounds: int = MAX_TOOL_ROUNDS,
    model: str = DEEPSEEK_MODEL,
    trace: list[dict[str, Any]] | None = None,
) -> str:
    """手写的 agent loop。整个循环就这么点内容，值得读一遍。"""
    from openai import OpenAI

    api_key = require_api_key(
        "DEEPSEEK_API_KEY", "密钥从 https://platform.deepseek.com 获取（只在创建时显示一次）"
    )

    client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    tools = to_openai_tools()

    for round_index in range(1, max_rounds + 1):
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
        message = response.choices[0].message

        # 没有 tool_call 就说明模型说完了
        if not message.tool_calls:
            return message.content or ""

        # 把助手这轮的回复（含 tool_calls）原样放回历史，格式必须对得上，
        # 否则下一轮请求会被拒
        messages.append({
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in message.tool_calls
            ],
        })

        for call in message.tool_calls:
            # DeepSeek 文档明确说过模型"不一定生成合法 JSON"，所以这里必须
            # try 一下。解析失败同样是返回错误说明，让模型自己改正。
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                result = json.dumps(
                    {"error": "参数不是合法 JSON，请重新生成",
                     "received": call.function.arguments},
                    ensure_ascii=False,
                )
                _log_call(round_index, call.function.name, {}, verbose)
                _record(trace, round_index, call.function.name, {}, result)
            else:
                _log_call(round_index, call.function.name, arguments, verbose)
                result = dispatch(call.function.name, arguments)
                _record(trace, round_index, call.function.name, arguments, result)

            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": result,
            })

    if verbose:
        print(f"  [已达 {max_rounds} 轮上限，停止]")
    return "（超过工具调用轮数上限，未能得出结论）"


# =============================================================================
# Anthropic
# =============================================================================

def run_anthropic(
    question: str,
    verbose: bool = True,
    max_rounds: int = MAX_TOOL_ROUNDS,
    model: str = ANTHROPIC_MODEL,
    trace: list[dict[str, Any]] | None = None,
) -> str:
    """用同样的手写循环跑 Anthropic。

    Anthropic SDK 其实提供了 tool_runner 能自动跑完循环，这里刻意不用：
    两边用同一个结构，做模型对比评估时才不会把"框架差异"混进结论里。
    """
    from anthropic import Anthropic

    require_api_key(
        "ANTHROPIC_API_KEY", "密钥从 https://console.anthropic.com 获取"
    )

    client = Anthropic()
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
    tools = to_anthropic_tools()

    for round_index in range(1, max_rounds + 1):
        response = client.messages.create(
            model=model,
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            tools=tools,
            # adaptive：让模型自己决定要不要思考、思考多久。
            # 注意不要用 budget_tokens，那个参数在 Opus 5 上会直接报 400。
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            messages=messages,
        )

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return "".join(b.text for b in response.content if b.type == "text")

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in tool_uses:
            args = dict(block.input)
            _log_call(round_index, block.name, args, verbose)
            result = dispatch(block.name, args)
            _record(trace, round_index, block.name, args, result)
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            })
        messages.append({"role": "user", "content": results})

    if verbose:
        print(f"  [已达 {max_rounds} 轮上限，停止]")
    return "（超过工具调用轮数上限，未能得出结论）"


# =============================================================================
# 入口
# =============================================================================

def ask(
    question: str,
    provider: Provider | None = None,
    verbose: bool = True,
    max_rounds: int = MAX_TOOL_ROUNDS,
    model: str | None = None,
    trace: list[dict[str, Any]] | None = None,
) -> str:
    """问一个问题，跑完 agent loop，返回最终回答。

    provider 不传时读环境变量 GO8_PROVIDER，默认 deepseek。

    verbose=True 会打印每轮调了什么工具、传了什么参数。刚学 agent 时建议
    一直开着——你需要亲眼看到这个循环在做什么，而不是把它当黑箱。
    """
    provider = provider or os.environ.get("GO8_PROVIDER", "deepseek")  # type: ignore[assignment]
    if provider == "deepseek":
        return run_deepseek(question, verbose, max_rounds, model or DEEPSEEK_MODEL, trace)
    if provider == "anthropic":
        return run_anthropic(question, verbose, max_rounds, model or ANTHROPIC_MODEL, trace)
    raise ValueError(f"未知 provider: {provider}（可选 deepseek / anthropic）")
