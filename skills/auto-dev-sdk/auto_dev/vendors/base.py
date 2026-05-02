"""Vendor adapter base class + unified response shape."""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from auto_dev.errors import VendorProtocolError


@dataclass
class SubagentResponse:
    """Unified response shape across vendors.

    `content` preserves the model's natural-language output verbatim.
    `structured` is the parsed JSON payload (once the adapter pulls it out
    of whatever vendor-specific envelope wrapped it).
    `raw` is the untouched vendor SDK response, kept for debugging.
    """

    content: str
    structured: dict[str, Any]
    raw: Any = None
    usage: dict[str, int] = field(default_factory=dict)
    vendor: str = ""
    model: str = ""


class VendorAdapter(ABC):
    """Each vendor implementation subclasses this."""

    name: str = "base"

    @abstractmethod
    def run_subagent(
        self,
        *,
        model: str,
        system: str,
        inputs: dict[str, Any],
        response_schema: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        executor: "object | None" = None,
        max_tokens: int = 4096,
        max_iterations: int = 25,
    ) -> SubagentResponse:
        """Call the vendor model, return unified response.

        Implementations should:
          * Serialize `inputs` to a single user message (JSON block).
          * Pass `system` as system prompt.
          * If the vendor supports structured JSON output, use it.
          * Extract the JSON payload into `structured`.
          * Raise `VendorProtocolError` if the response can't be coerced.

        When `tools` is provided (R15/R16), the adapter drives a multi-turn
        {text, tool_use, tool_result} loop: each `tool_use` content block is
        dispatched through `executor` (which MUST implement
        `ToolExecutor.dispatch(tool, args) -> ToolResult`), the result is
        appended as a `tool_result` block, and the conversation re-enters
        `messages.create`. The loop stops when the model returns
        `stop_reason == "end_turn"` OR `max_iterations` is reached (which
        raises VendorProtocolError — run-away guard).

        When `tools` is None (the default), the adapter issues a single
        call with no tools — preserves pre-R15 behavior exactly.
        """

    def _extract_json(self, text: str) -> dict[str, Any]:
        """Best-effort JSON extraction.

        Accepts:
          * Whole-response JSON
          * JSON inside ```json ... ``` fence
          * First {...} balanced block
        """
        text = text.strip()
        if not text:
            raise VendorProtocolError("empty response; no JSON payload")
        # Whole-response JSON.
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Fenced block.
        for fence in ("```json", "```JSON", "```"):
            if fence in text:
                i = text.find(fence)
                j = text.find("```", i + len(fence))
                if j > i:
                    inner = text[i + len(fence): j].strip()
                    try:
                        return json.loads(inner)
                    except json.JSONDecodeError:
                        continue
        # First balanced braces.
        depth = 0
        start = -1
        for k, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    start = k
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start != -1:
                    candidate = text[start: k + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        start = -1
        raise VendorProtocolError("no JSON payload found in response")

    def _format_inputs(self, system: str, inputs: dict[str, Any]) -> tuple[str, str]:
        """Return (system_prompt, user_message) pair."""
        body = json.dumps(inputs, indent=2, ensure_ascii=False)
        user = (
            "Inputs (JSON):\n\n"
            "```json\n"
            f"{body}\n"
            "```\n\n"
            "Return a single JSON object as your reply. No prose outside the JSON."
        )
        return system, user
