"""多轮对话测试。

**不调模型、不需要 API key**：用一个脚本化的假 client 顶替供应商 SDK。
这里锁住的不是模型说得对不对，而是历史这个数据结构不会被弄坏——

  - tool_call 和它的结果必须成对，裁剪不能从中间切
  - 不能出现两条挨着的 user 消息（Anthropic 会直接 400）
  - 一轮失败后历史要回到干净状态，不能把残骸留给下一轮

这几条错了，症状都是"下一轮莫名其妙报 400"，而且看回答完全看不出来。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from go8agent import agent
from go8agent.agent import Conversation


# =============================================================================
# 假 client
# =============================================================================

def _tool_call(call_id: str, name: str, arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


class FakeDeepSeek:
    """按脚本依次返回。脚本每项要么是一句话，要么是一串 tool_call。"""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.seen: list[list[dict]] = []   # 每次请求实际发出去的 messages
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, *, model, messages, tools, tool_choice):
        self.seen.append([dict(m) for m in messages])
        step = self.script.pop(0) if self.script else "说完了"
        if isinstance(step, str):
            message = SimpleNamespace(content=step, tool_calls=None)
        else:
            message = SimpleNamespace(content=None, tool_calls=step)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5,
                                  prompt_cache_hit_tokens=8, prompt_cache_miss_tokens=2),
        )


class FakeBlock:
    def __init__(self, type_: str, **fields) -> None:
        self.type = type_
        for key, value in fields.items():
            setattr(self, key, value)

    def model_dump(self, exclude_none: bool = False) -> dict:
        return {"type": self.type, **{k: v for k, v in vars(self).items() if k != "type"}}


class FakeAnthropic:
    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.seen: list[list[dict]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.seen.append(list(kwargs["messages"]))
        step = self.script.pop(0) if self.script else "说完了"
        if isinstance(step, str):
            content = [FakeBlock("text", text=step)]
        else:
            content = step
        return SimpleNamespace(content=content, usage=SimpleNamespace(
            prompt_tokens=10, completion_tokens=5))


@pytest.fixture(autouse=True)
def _stub_dispatch(monkeypatch):
    """工具本身有自己的测试，这里只需要它返回点东西。"""
    monkeypatch.setattr(agent, "dispatch",
                        lambda name, args: json.dumps({"tool": name, "args": args}))


def _conversation(client, **kwargs) -> Conversation:
    kwargs.setdefault("provider", "deepseek")
    kwargs.setdefault("verbose", False)
    return Conversation(client=client, **kwargs)


# =============================================================================
# 历史
# =============================================================================

class TestHistory:
    def test_previous_turns_are_sent_again(self):
        """多轮的全部意义：第二问能看见第一问。"""
        client = FakeDeepSeek(["第一个回答", "第二个回答"])
        conv = _conversation(client)
        conv.send("Monash 有哪些 IT 硕士")
        conv.send("那雅思要求呢")

        second_request = client.seen[1]
        contents = [m.get("content") for m in second_request]
        assert "Monash 有哪些 IT 硕士" in contents
        assert "第一个回答" in contents
        assert contents[-1] == "那雅思要求呢"

    def test_system_prompt_is_not_stored_in_history(self):
        """system 每轮拼进请求，但不进历史——否则裁剪逻辑要给它开特例。"""
        client = FakeDeepSeek(["好的"])
        conv = _conversation(client)
        conv.send("你好")

        assert client.seen[0][0]["role"] == "system"
        assert all(m["role"] != "system" for m in conv.history)

    def test_tool_call_and_result_stay_paired(self):
        client = FakeDeepSeek([
            [_tool_call("call_1", "search_programs", {"keyword": "it"})],
            "查到了 3 个",
        ])
        conv = _conversation(client)
        conv.send("有哪些 IT 硕士")

        roles = [m["role"] for m in conv.history]
        assert roles == ["user", "assistant", "tool", "assistant"]
        assert conv.history[1]["tool_calls"][0]["id"] == "call_1"
        assert conv.history[2]["tool_call_id"] == "call_1"

    def test_tool_calls_accumulate_across_turns(self):
        """整段对话查过什么要能回答；每轮的 trace 则各归各的。"""
        client = FakeDeepSeek([
            [_tool_call("c1", "search_programs", {"keyword": "it"})], "答一",
            [_tool_call("c2", "get_program_details", {"program_key": "monash:C6001"})], "答二",
        ])
        conv = _conversation(client)
        conv.send("一")
        turn_trace: list[dict] = []
        conv.send("二", trace=turn_trace)

        assert [c["tool"] for c in conv.tool_calls] == ["search_programs", "get_program_details"]
        assert [c["tool"] for c in turn_trace] == ["get_program_details"]


# =============================================================================
# 裁剪
# =============================================================================

class TestTrim:
    def _three_turns(self, **kwargs) -> Conversation:
        client = FakeDeepSeek([
            [_tool_call("c1", "search_programs", {"keyword": "a"})], "答一",
            [_tool_call("c2", "search_programs", {"keyword": "b"})], "答二",
            "答三",
        ])
        conv = _conversation(client, max_history_turns=2, **kwargs)
        for question in ("一", "二", "三"):
            conv.send(question)
        return conv

    def test_oldest_turn_is_dropped_whole(self):
        conv = self._three_turns()
        assert conv.turns == 2
        assert conv.dropped_turns == 1
        assert conv.total_turns == 3
        assert conv.history[0] == {"role": "user", "content": "二"}

    def test_trim_never_orphans_a_tool_result(self):
        """从中间切一刀会留下没有 tool_call 的 tool 消息，下一次请求直接 400。"""
        conv = self._three_turns()
        pending: set[str] = set()
        for message in conv.history:
            for call in message.get("tool_calls") or []:
                pending.add(call["id"])
            if message["role"] == "tool":
                assert message["tool_call_id"] in pending, "工具结果找不到对应的 tool_call"

    def test_no_two_user_messages_in_a_row(self):
        """Anthropic 要求角色交替，连着两条 user 会被拒。"""
        conv = self._three_turns()
        roles = [m["role"] for m in conv.history]
        assert not any(a == b == "user" for a, b in zip(roles, roles[1:]))


# =============================================================================
# 出错与边界
# =============================================================================

class TestFailureModes:
    def test_exhausted_rounds_still_close_the_turn(self):
        """轮数用光也要补一条 assistant，否则下一轮就是两条挨着的 user。"""
        client = FakeDeepSeek([
            [_tool_call("c1", "search_programs", {"keyword": "a"})],
            [_tool_call("c2", "search_programs", {"keyword": "b"})],
            "第二轮答案",
        ])
        conv = _conversation(client, max_rounds=2)
        answer = conv.send("一")
        assert "上限" in answer
        assert conv.history[-1]["role"] == "assistant"

        conv.send("二")
        roles = [m["role"] for m in conv.history]
        assert not any(a == b == "user" for a, b in zip(roles, roles[1:]))

    def test_failed_turn_is_rolled_back(self):
        """留下半截历史的话，下一轮会跟着一起失败，而且完全看不出原因。"""
        class Boom(FakeDeepSeek):
            def _create(self, **kwargs):
                if len(self.seen) >= 1:
                    raise ConnectionError("网络断了")
                return super()._create(**kwargs)

        client = Boom(["第一个回答"])
        conv = _conversation(client)
        conv.send("一")
        before = list(conv.history)

        with pytest.raises(ConnectionError):
            conv.send("二")

        assert conv.history == before
        assert conv.turns == 1

    def test_reset_clears_history_but_keeps_usage(self):
        client = FakeDeepSeek(["答一", "答二"])
        conv = _conversation(client)
        conv.send("一")
        spent = dict(conv.usage)
        conv.reset()

        assert conv.history == []
        assert conv.turns == 0
        assert conv.usage == spent

        conv.send("二")
        assert client.seen[-1][1]["content"] == "二"   # [0] 是 system
        assert conv.usage["input_tokens"] > spent["input_tokens"]

    def test_unknown_provider_fails_at_construction(self):
        with pytest.raises(ValueError, match="未知 provider"):
            Conversation(provider="gpt")   # type: ignore[arg-type]


# =============================================================================
# 用量与存档
# =============================================================================

class TestUsageAndSerialize:
    def test_usage_accumulates_across_turns(self):
        client = FakeDeepSeek(["答一", "答二"])
        conv = _conversation(client)
        conv.send("一")
        conv.send("二")
        assert conv.usage["requests"] == 2
        assert conv.usage["input_tokens"] == 20
        assert conv.usage["cached_input_tokens"] == 16

    def test_serialize_is_json_dumpable_for_anthropic(self):
        """Anthropic 的内容块是 SDK 对象，直接 json.dumps 会炸。"""
        client = FakeAnthropic([
            [FakeBlock("thinking", thinking="想一下", signature="sig"),
             FakeBlock("tool_use", id="tu_1", name="search_programs", input={"keyword": "it"})],
            "查到了",
        ])
        conv = _conversation(client, provider="anthropic")
        conv.send("有哪些 IT 硕士")

        dumped = json.dumps(conv.serialize(), ensure_ascii=False)
        assert "thinking" in dumped
        assert json.loads(dumped)["turns"] == 1

    def test_anthropic_keeps_thinking_block_with_tool_use(self):
        """带 tool_use 的那条 assistant 消息必须把 thinking 连签名一起带回去。"""
        client = FakeAnthropic([
            [FakeBlock("thinking", thinking="想一下", signature="sig"),
             FakeBlock("tool_use", id="tu_1", name="search_programs", input={})],
            "查到了",
        ])
        conv = _conversation(client, provider="anthropic")
        conv.send("问")

        sent_back = client.seen[1][1]["content"]
        assert [b.type for b in sent_back] == ["thinking", "tool_use"]


class TestAsk:
    def test_ask_starts_from_a_clean_history(self, monkeypatch):
        """评估靠这条：每道题必须独立，上一题的工具结果不能漏进来。"""
        client = FakeDeepSeek(["答一", "答二"])
        monkeypatch.setattr(Conversation, "client", lambda self: client)

        agent.ask("第一题", verbose=False)
        agent.ask("第二题", verbose=False)

        assert [m["content"] for m in client.seen[1] if m["role"] == "user"] == ["第二题"]
