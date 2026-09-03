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

=============================================================================
多轮对话
=============================================================================
一段对话的全部状态就是一个 history 列表，见 Conversation。单轮的 ask() 只是
「用一次就扔的 Conversation」，所以两条路走的是同一份代码。
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

7. 多轮对话里用户会用指代（"这个项目""那它呢""第二个"）。指代必须落到明确的
   program_key 再查工具；如果上文里有多个候选、你不确定指的是哪一个，就直接问
   用户，不要挑一个来猜。猜错的代价是给出另一个项目的分数线，比多问一句糟得多。

用户说中文就用中文回答。回答要简洁，别堆砌无关信息。"""


def _log_call(round_index: int, name: str, arguments: dict[str, Any], verbose: bool,
              turn_index: int = 1) -> None:
    if not verbose:
        return
    shown = ", ".join(f"{k}={v!r}" for k, v in arguments.items() if v is not None)
    where = f"第 {round_index} 轮" if turn_index <= 1 else f"对话 {turn_index} / 第 {round_index} 轮"
    print(f"  [{where}] 调用 {name}({shown})")


def _add_usage(usage: dict[str, int] | None, response: Any) -> None:
    """累加一次请求的 token 用量。

    评估会把同一批题跑很多遍，不记账很容易在不知不觉中花掉不少钱。
    DeepSeek 还会返回缓存命中/未命中的拆分，命中部分便宜一个数量级，
    分开记才能看出缓存到底有没有起作用。
    """
    if usage is None or getattr(response, "usage", None) is None:
        return
    u = response.usage
    usage["requests"] = usage.get("requests", 0) + 1
    for field, key in [
        ("prompt_tokens", "input_tokens"),
        ("completion_tokens", "output_tokens"),
        ("prompt_cache_hit_tokens", "cached_input_tokens"),
        ("prompt_cache_miss_tokens", "uncached_input_tokens"),
    ]:
        value = getattr(u, field, None)
        if isinstance(value, int):
            usage[key] = usage.get(key, 0) + value


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
# 单轮：DeepSeek（OpenAI 兼容接口）
# =============================================================================
# 这两个 _*_turn 函数只负责「把一条用户消息跑完」：发消息 -> 有 tool_call 就
# 执行 -> 结果塞回去 -> 再发，直到模型不再调工具。
#
# 它们**就地修改传进来的 history**，而不是自己新建消息列表——多轮对话的全部
# 状态就是这个 list，谁持有它谁就持有这段对话。Conversation 持有它。

def _deepseek_turn(
    client: Any,
    history: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    model: str,
    max_rounds: int,
    verbose: bool,
    trace: list[dict[str, Any]] | None,
    usage: dict[str, int] | None,
    turn_index: int = 1,
) -> str:
    """手写的 agent loop。整个循环就这么点内容，值得读一遍。"""
    for round_index in range(1, max_rounds + 1):
        response = client.chat.completions.create(
            model=model,
            # system 不进 history：它每轮都一样，拼在请求里就行。
            # history 里只放真正的对话内容，裁剪逻辑才不用给它开特例。
            messages=[{"role": "system", "content": SYSTEM_PROMPT}] + history,
            tools=tools,
            tool_choice="auto",
        )
        _add_usage(usage, response)
        message = response.choices[0].message

        # 没有 tool_call 就说明模型说完了
        if not message.tool_calls:
            text = message.content or ""
            history.append({"role": "assistant", "content": text})
            return text

        # 把助手这轮的回复（含 tool_calls）原样放回历史，格式必须对得上，
        # 否则下一轮请求会被拒
        history.append({
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
                _log_call(round_index, call.function.name, {}, verbose, turn_index)
                _record(trace, round_index, call.function.name, {}, result)
            else:
                _log_call(round_index, call.function.name, arguments, verbose, turn_index)
                result = dispatch(call.function.name, arguments)
                _record(trace, round_index, call.function.name, arguments, result)

            history.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": result,
            })

    return _exhausted(history, max_rounds, verbose)


# =============================================================================
# 单轮：Anthropic
# =============================================================================

def _anthropic_turn(
    client: Any,
    history: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    model: str,
    max_rounds: int,
    verbose: bool,
    trace: list[dict[str, Any]] | None,
    usage: dict[str, int] | None,
    turn_index: int = 1,
) -> str:
    """用同样的手写循环跑 Anthropic。

    Anthropic SDK 其实提供了 tool_runner 能自动跑完循环，这里刻意不用：
    两边用同一个结构，做模型对比评估时才不会把"框架差异"混进结论里。
    多轮之后这个理由更强了——自动循环不会把历史交出来给我裁剪。
    """
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
            messages=history,
        )
        _add_usage(usage, response)

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        # 助手这轮的内容一律原样入历史。thinking 块也要留着：Anthropic 要求
        # 带 tool_use 的那条 assistant 消息把 thinking 块连签名一起带回来。
        history.append({"role": "assistant", "content": response.content})

        if not tool_uses:
            return "".join(b.text for b in response.content if b.type == "text")

        results = []
        for block in tool_uses:
            args = dict(block.input)
            _log_call(round_index, block.name, args, verbose, turn_index)
            result = dispatch(block.name, args)
            _record(trace, round_index, block.name, args, result)
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            })
        history.append({"role": "user", "content": results})

    return _exhausted(history, max_rounds, verbose)


def _exhausted(history: list[dict[str, Any]], max_rounds: int, verbose: bool) -> str:
    """轮数用光时收尾。

    必须往历史里补一条 assistant 消息，不能就这么停在工具结果上：
    下一轮 send() 会再追加一条 user 消息，而两条 user 消息挨在一起
    会被 Anthropic 直接拒掉（DeepSeek 那边则是历史里悬着一堆没人回应的
    工具结果）。补这一条既修好了格式，也让用户在 /history 里看得见断在哪。
    """
    if verbose:
        print(f"  [已达 {max_rounds} 轮上限，停止]")
    text = "（超过工具调用轮数上限，未能得出结论）"
    history.append({"role": "assistant", "content": text})
    return text


# =============================================================================
# 多轮对话
# =============================================================================
# 一段对话的全部状态就是一个 history 列表。这里刻意**不做跨供应商的统一
# 消息格式**：history 里存的就是各家 API 原生的那套结构，直接发回去。
# 中间加一层自己的格式，等于每次都要正反转换两遍，而这类转换最容易在
# thinking 块、tool_call id 这些边角上出错——那是多轮里最难查的一类 bug。
#
# 统一的只有对外的接口：send / reset / usage。

# 保留多少轮对话。超出就从最老的开始整轮丢弃。
# 为什么按"轮"而不是按 token 数裁：一轮里 tool_call 和它对应的 tool 结果
# 必须成对存在，从中间切一刀会让下一次请求直接 400。按整轮丢就不可能切坏。
# 代价是不够精细——但"永远不会发出非法请求"比"多塞进两百个 token"重要得多。
MAX_HISTORY_TURNS = 8


class Conversation:
    """一段多轮对话。

    典型用法：

        conv = Conversation(provider="deepseek")
        conv.send("双非 78 分能申哪些 Monash 的 IT 硕士")
        conv.send("第二个的雅思要求呢")     # 指代靠历史解决

    client 参数是给测试用的注入口，同时也让同一段对话复用一个 HTTP 连接。
    """

    def __init__(
        self,
        provider: Provider | None = None,
        model: str | None = None,
        max_rounds: int = MAX_TOOL_ROUNDS,
        max_history_turns: int = MAX_HISTORY_TURNS,
        verbose: bool = True,
        client: Any = None,
    ) -> None:
        provider = provider or os.environ.get("GO8_PROVIDER", "deepseek")  # type: ignore[assignment]
        if provider not in ("deepseek", "anthropic"):
            raise ValueError(f"未知 provider: {provider}（可选 deepseek / anthropic）")
        self.provider: Provider = provider  # type: ignore[assignment]
        self.model = model or (DEEPSEEK_MODEL if provider == "deepseek" else ANTHROPIC_MODEL)
        self.max_rounds = max_rounds
        self.max_history_turns = max_history_turns
        self.verbose = verbose

        self.history: list[dict[str, Any]] = []
        # 每一轮用户提问在 history 里的下标。裁剪只在这些位置下刀。
        self._turn_starts: list[int] = []
        # 跨轮累计：整段对话花了多少钱、一共查过什么，都要能回答。
        self.usage: dict[str, int] = {}
        self.tool_calls: list[dict[str, Any]] = []
        self.dropped_turns = 0
        self._client = client

    # ---- 只读视图 ----

    @property
    def turns(self) -> int:
        """当前历史里还剩几轮（不含已被裁掉的）。"""
        return len(self._turn_starts)

    @property
    def total_turns(self) -> int:
        return len(self._turn_starts) + self.dropped_turns

    def client(self) -> Any:
        """按需创建供应商 client，整段对话复用同一个。"""
        if self._client is not None:
            return self._client
        if self.provider == "deepseek":
            from openai import OpenAI

            api_key = require_api_key(
                "DEEPSEEK_API_KEY",
                "密钥从 https://platform.deepseek.com 获取（只在创建时显示一次）",
            )
            self._client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
        else:
            from anthropic import Anthropic

            require_api_key("ANTHROPIC_API_KEY", "密钥从 https://console.anthropic.com 获取")
            self._client = Anthropic()
        return self._client

    # ---- 主流程 ----

    def send(
        self,
        question: str,
        trace: list[dict[str, Any]] | None = None,
        usage: dict[str, int] | None = None,
    ) -> str:
        """追加一轮提问，跑完 agent loop，返回回答。

        trace / usage 传进来的话，只装**这一轮**的数据（评估按轮打分）；
        整段对话的累计值一直在 self.tool_calls 和 self.usage 里。
        """
        self._turn_starts.append(len(self.history))
        self.history.append({"role": "user", "content": question})
        self._trim()

        turn_index = self.total_turns
        turn_trace: list[dict[str, Any]] = []
        turn_usage: dict[str, int] = {}
        runner = _deepseek_turn if self.provider == "deepseek" else _anthropic_turn
        tools = to_openai_tools() if self.provider == "deepseek" else to_anthropic_tools()

        try:
            answer = runner(
                self.client(), self.history, tools, self.model,
                self.max_rounds, self.verbose, turn_trace, turn_usage, turn_index,
            )
        except Exception:
            # 请求失败时把这半截历史丢掉。留着的话，history 会停在一条没人
            # 回应的 user 消息（或一串悬空的工具结果）上，下一轮必然再失败一次——
            # 而用户看到的是"又错了"，根本联想不到是上一次的残留。
            self._rollback_last_turn()
            raise

        self.tool_calls.extend(turn_trace)
        for key, value in turn_usage.items():
            self.usage[key] = self.usage.get(key, 0) + value
        if trace is not None:
            trace.extend(turn_trace)
        if usage is not None:
            for key, value in turn_usage.items():
                usage[key] = usage.get(key, 0) + value
        return answer

    def reset(self) -> None:
        """清空对话历史，但**保留累计用量**。

        用量是这次进程花掉的真金白银，不该因为换了个话题就归零。
        """
        self.history.clear()
        self._turn_starts.clear()
        self.dropped_turns = 0

    # ---- 历史维护 ----

    def _trim(self) -> None:
        while len(self._turn_starts) > self.max_history_turns:
            cut = self._turn_starts[1]
            del self.history[:cut]
            self._turn_starts = [i - cut for i in self._turn_starts[1:]]
            self.dropped_turns += 1
            if self.verbose:
                print(f"  [历史超过 {self.max_history_turns} 轮，已丢弃最早一轮]")

    def _rollback_last_turn(self) -> None:
        if not self._turn_starts:
            return
        del self.history[self._turn_starts[-1]:]
        self._turn_starts.pop()

    def serialize(self) -> dict[str, Any]:
        """导出成可 json.dumps 的结构（存档、/save、事后复盘用）。

        Anthropic 的 assistant 内容是 SDK 对象，发回 API 没问题，但不能直接
        序列化——只在导出这一步转成 dict，历史里仍存原对象。
        """
        return {
            "provider": self.provider,
            "model": self.model,
            "turns": self.total_turns,
            "dropped_turns": self.dropped_turns,
            "usage": self.usage,
            "tool_calls": self.tool_calls,
            "history": [_jsonable(m) for m in self.history],
        }


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(exclude_none=True)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


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
    usage: dict[str, int] | None = None,
) -> str:
    """问一个问题，跑完 agent loop，返回最终回答。

    单轮问答 = 只用一次的 Conversation。评估走的就是这条路：每道题都必须从
    干净历史开始，否则上一题的工具结果会顺着历史漏进来，题目之间不再独立。

    provider 不传时读环境变量 GO8_PROVIDER，默认 deepseek。

    verbose=True 会打印每轮调了什么工具、传了什么参数。刚学 agent 时建议
    一直开着——你需要亲眼看到这个循环在做什么，而不是把它当黑箱。
    """
    conversation = Conversation(
        provider=provider, model=model, max_rounds=max_rounds, verbose=verbose
    )
    return conversation.send(question, trace=trace, usage=usage)
