"""ad-15 / ad-16: Anthropic multi-turn tool-use loop + executor wiring."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from auto_dev.errors import VendorProtocolError
from auto_dev.executor.tool_schemas import default_tools
from auto_dev.vendors.anthropic import AnthropicAdapter


# ---------- fakes ---------------------------------------------------------


class _StubTextBlock(SimpleNamespace):
    def __init__(self, text):
        super().__init__(type="text", text=text)


class _StubToolUseBlock(SimpleNamespace):
    def __init__(self, id, name, input_):
        super().__init__(type="tool_use", id=id, name=name, input=input_)


def _resp(blocks, stop_reason, usage=(1, 1)):
    return SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1]),
    )


class _ScriptedClient:
    """Returns a queue of pre-scripted responses from messages.create."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("ScriptedClient: ran out of scripted responses")
        return self._responses.pop(0)


class _FakeExecutor:
    def __init__(self, result_map=None):
        # map tool name -> callable(args) -> SimpleNamespace(ok, output)
        self.result_map = result_map or {}
        self.dispatched: list[tuple[str, dict]] = []

    def dispatch(self, tool, args):
        self.dispatched.append((tool, args))
        fn = self.result_map.get(tool)
        if fn is None:
            return SimpleNamespace(ok=True, output=f"(stub) {tool} {args}")
        return fn(args)


# ---------- tests ----------------------------------------------------------


def test_backward_compat_no_tools_one_shot():
    """tools=None runs exactly one call (pre-R15 behavior preserved)."""
    resp = _resp([_StubTextBlock('{"ok": true}')], stop_reason="end_turn")
    client = _ScriptedClient([resp])
    adapter = AnthropicAdapter(client=client)
    out = adapter.run_subagent(model="m", system="s", inputs={"x": 1})
    assert out.structured == {"ok": True}
    assert len(client.calls) == 1
    # No tools wired
    assert "tools" not in client.calls[0]


def test_tool_loop_requires_executor():
    adapter = AnthropicAdapter(client=_ScriptedClient([]))
    with pytest.raises(VendorProtocolError, match="no executor"):
        adapter.run_subagent(
            model="m", system="s", inputs={}, tools=default_tools(),
        )


def test_tool_loop_single_dispatch_then_end():
    """Turn 1: tool_use → dispatch → tool_result. Turn 2: end_turn with JSON."""
    turn1 = _resp(
        [_StubToolUseBlock(id="t1", name="bash", input_={"cmd": "ls"})],
        stop_reason="tool_use",
    )
    turn2 = _resp([_StubTextBlock('{"files_changed": ["a.py"]}')], stop_reason="end_turn")
    client = _ScriptedClient([turn1, turn2])
    executor = _FakeExecutor()
    adapter = AnthropicAdapter(client=client)

    out = adapter.run_subagent(
        model="m", system="s", inputs={}, tools=default_tools(), executor=executor,
    )

    assert out.structured == {"files_changed": ["a.py"]}
    assert executor.dispatched == [("bash", {"cmd": "ls"})]
    assert len(client.calls) == 2

    # Second call's messages should include the assistant's tool_use turn
    # and the user's tool_result turn.
    replay = client.calls[1]["messages"]
    assert replay[0]["role"] == "user"  # original input
    assert replay[1]["role"] == "assistant"
    assert replay[1]["content"][0]["type"] == "tool_use"
    assert replay[2]["role"] == "user"
    assert replay[2]["content"][0]["type"] == "tool_result"
    assert replay[2]["content"][0]["tool_use_id"] == "t1"


def test_tool_loop_multiple_blocks_single_turn():
    """Several tool_use blocks in ONE turn — all dispatched; one tool_result per."""
    turn1 = _resp(
        [
            _StubTextBlock("let me look around"),
            _StubToolUseBlock(id="t1", name="bash", input_={"cmd": "ls"}),
            _StubToolUseBlock(id="t2", name="read", input_={"path": "x"}),
        ],
        stop_reason="tool_use",
    )
    turn2 = _resp([_StubTextBlock('{"ok": true}')], stop_reason="end_turn")
    client = _ScriptedClient([turn1, turn2])
    executor = _FakeExecutor()
    adapter = AnthropicAdapter(client=client)

    out = adapter.run_subagent(
        model="m", system="s", inputs={}, tools=default_tools(), executor=executor,
    )

    assert out.structured == {"ok": True}
    assert [d[0] for d in executor.dispatched] == ["bash", "read"]
    replay = client.calls[1]["messages"][-1]["content"]
    # Two tool_result blocks, preserving order / ids
    assert [b["tool_use_id"] for b in replay] == ["t1", "t2"]


def test_tool_result_reports_error_when_executor_raises():
    """PermissionDenied / other exceptions → is_error=True tool_result."""
    turn1 = _resp(
        [_StubToolUseBlock(id="t1", name="bash", input_={"cmd": "rm -rf /"})],
        stop_reason="tool_use",
    )
    turn2 = _resp([_StubTextBlock('{"deviations": [{"scope_id": "x", "blocking": false, "severity": "minor", "detail": "blocked"}]}')], stop_reason="end_turn")
    client = _ScriptedClient([turn1, turn2])

    def boom(args):
        from auto_dev.errors import PermissionDenied
        raise PermissionDenied(f"bash: {args['cmd']!r} — denied")

    executor = _FakeExecutor(result_map={"bash": boom})
    adapter = AnthropicAdapter(client=client)

    out = adapter.run_subagent(
        model="m", system="s", inputs={}, tools=default_tools(), executor=executor,
    )

    # Still succeeds — executor error is surfaced to the model, not to the caller.
    assert out.structured["deviations"][0]["blocking"] is False
    replay = client.calls[1]["messages"][-1]["content"][0]
    assert replay["is_error"] is True
    assert "PermissionDenied" in replay["content"]


def test_tool_loop_iteration_cap_raises():
    """If the model never emits end_turn, we stop and raise."""
    # Repeat tool_use forever.
    turn = _resp(
        [_StubToolUseBlock(id="loop", name="bash", input_={"cmd": "ls"})],
        stop_reason="tool_use",
    )
    client = _ScriptedClient([turn] * 5)
    adapter = AnthropicAdapter(client=client)

    with pytest.raises(VendorProtocolError, match="max_iterations"):
        adapter.run_subagent(
            model="m", system="s", inputs={},
            tools=default_tools(), executor=_FakeExecutor(),
            max_iterations=3,
        )
    assert len(client.calls) == 3


def test_tool_loop_text_accumulates_across_turns():
    """Text blocks across turns are preserved in `content` (usage observable)."""
    turn1 = _resp(
        [
            _StubTextBlock("Planning: I'll run ls first."),
            _StubToolUseBlock(id="t1", name="bash", input_={"cmd": "ls"}),
        ],
        stop_reason="tool_use",
    )
    turn2 = _resp(
        [_StubTextBlock('Result summary:\n```json\n{"done": true}\n```')],
        stop_reason="end_turn",
    )
    client = _ScriptedClient([turn1, turn2])
    executor = _FakeExecutor()
    adapter = AnthropicAdapter(client=client)

    out = adapter.run_subagent(
        model="m", system="s", inputs={}, tools=default_tools(), executor=executor,
    )
    assert out.structured == {"done": True}
    # Both turns' text should show up in content
    assert "Planning" in out.content
    assert "Result summary" in out.content


def test_unexpected_stop_reason_raises():
    resp = _resp([_StubTextBlock("...")], stop_reason="max_tokens")
    client = _ScriptedClient([resp])
    adapter = AnthropicAdapter(client=client)
    with pytest.raises(VendorProtocolError, match="stop_reason"):
        adapter.run_subagent(
            model="m", system="s", inputs={},
            tools=default_tools(), executor=_FakeExecutor(),
        )


def test_usage_accumulates_across_turns():
    turn1 = _resp(
        [_StubToolUseBlock(id="t1", name="bash", input_={"cmd": "ls"})],
        stop_reason="tool_use",
        usage=(10, 20),
    )
    turn2 = _resp(
        [_StubTextBlock('{"ok": true}')],
        stop_reason="end_turn",
        usage=(5, 15),
    )
    client = _ScriptedClient([turn1, turn2])
    executor = _FakeExecutor()
    adapter = AnthropicAdapter(client=client)
    out = adapter.run_subagent(
        model="m", system="s", inputs={}, tools=default_tools(), executor=executor,
    )
    assert out.usage["input_tokens"] == 15
    assert out.usage["output_tokens"] == 35


def test_openai_rejects_tools():
    from auto_dev.vendors.openai import OpenAIAdapter

    with pytest.raises(VendorProtocolError, match="not yet implemented"):
        OpenAIAdapter(client=object()).run_subagent(
            model="m", system="s", inputs={}, tools=default_tools(), executor=_FakeExecutor(),
        )


def test_google_rejects_tools():
    from auto_dev.vendors.google import GoogleAdapter

    with pytest.raises(VendorProtocolError, match="not yet implemented"):
        GoogleAdapter(client=object()).run_subagent(
            model="m", system="s", inputs={}, tools=default_tools(), executor=_FakeExecutor(),
        )
