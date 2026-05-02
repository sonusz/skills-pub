"""ad-14: Anthropic adapter routes response_schema via tool_use."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from auto_dev.errors import VendorProtocolError
from auto_dev.vendors.anthropic import AnthropicAdapter


class _FakeClient:
    """Captures the last call kwargs and returns a scripted response."""

    def __init__(self, response):
        self._response = response
        self.last_kwargs: dict | None = None

    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self._response


def _text_only_response(text="```json\n{\"ok\": true}\n```"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def _tool_use_response(payload):
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="tool_use", name="return_structured", input=payload),
        ],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def test_no_schema_falls_back_to_text_extraction():
    resp = _text_only_response()
    client = _FakeClient(resp)
    adapter = AnthropicAdapter(client=client)
    out = adapter.run_subagent(model="m", system="s", inputs={"q": 1})
    assert out.structured == {"ok": True}
    # tool_use path NOT taken
    assert "tools" not in client.last_kwargs
    assert "tool_choice" not in client.last_kwargs


def test_with_schema_forces_tool_use():
    resp = _tool_use_response({"ok": True, "why": "smoke"})
    client = _FakeClient(resp)
    adapter = AnthropicAdapter(client=client)
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    out = adapter.run_subagent(model="m", system="s", inputs={"q": 1}, response_schema=schema)

    assert out.structured == {"ok": True, "why": "smoke"}
    # tool_use parameters wired correctly
    kwargs = client.last_kwargs
    assert kwargs["tools"][0]["name"] == "return_structured"
    assert kwargs["tools"][0]["input_schema"] == schema
    assert kwargs["tool_choice"] == {"type": "tool", "name": "return_structured"}


def test_schema_path_raises_if_tool_use_missing():
    """Model returned only text — tool_use was expected but absent."""
    resp = _text_only_response("prose with no tool call")
    client = _FakeClient(resp)
    adapter = AnthropicAdapter(client=client)
    with pytest.raises(VendorProtocolError, match="tool_use"):
        adapter.run_subagent(
            model="m", system="s", inputs={}, response_schema={"type": "object"},
        )


def test_text_and_tool_use_both_present_prefers_tool_use():
    resp = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="reasoning..."),
            SimpleNamespace(type="tool_use", name="return_structured", input={"ok": True}),
        ],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    client = _FakeClient(resp)
    adapter = AnthropicAdapter(client=client)
    out = adapter.run_subagent(
        model="m", system="s", inputs={}, response_schema={"type": "object"},
    )
    assert out.structured == {"ok": True}
    assert out.content == "reasoning..."  # text preserved
