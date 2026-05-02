"""OpenAI adapter (GPT-*)."""
from __future__ import annotations

from typing import Any

from auto_dev.errors import VendorProtocolError
from auto_dev.vendors.base import SubagentResponse, VendorAdapter


class OpenAIAdapter(VendorAdapter):
    name = "openai"

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    def _lazy_client(self):
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as e:
            raise VendorProtocolError("openai SDK not installed; pip install openai") from e
        self._client = OpenAI()
        return self._client

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
        if tools is not None:
            raise VendorProtocolError(
                "openai: multi-turn tool-use loop not yet implemented (ad-x8); "
                "use Anthropic for the implement subagent, or pass tools=None"
            )
        sys_prompt, user_msg = self._format_inputs(system, inputs)
        client = self._lazy_client()

        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_msg},
            ],
        )
        if response_schema:
            kwargs["response_format"] = {"type": "json_object"}

        response = client.chat.completions.create(**kwargs)

        try:
            content = response.choices[0].message.content or ""
        except (AttributeError, IndexError) as e:
            raise VendorProtocolError(f"openai: malformed response: {e}") from e

        if not content.strip():
            raise VendorProtocolError("openai: empty content")

        structured = self._extract_json(content)

        usage = {}
        if getattr(response, "usage", None):
            usage = {
                "input_tokens": getattr(response.usage, "prompt_tokens", 0),
                "output_tokens": getattr(response.usage, "completion_tokens", 0),
            }

        return SubagentResponse(
            content=content,
            structured=structured,
            raw=response,
            usage=usage,
            vendor=self.name,
            model=model,
        )
