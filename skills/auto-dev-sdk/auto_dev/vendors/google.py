"""Google adapter (Gemini)."""
from __future__ import annotations

from typing import Any

from auto_dev.errors import VendorProtocolError
from auto_dev.vendors.base import SubagentResponse, VendorAdapter


class GoogleAdapter(VendorAdapter):
    name = "google"

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    def _lazy_client(self):
        if self._client is not None:
            return self._client
        try:
            from google import genai  # type: ignore
        except ImportError as e:
            raise VendorProtocolError(
                "google-genai SDK not installed; pip install google-genai"
            ) from e
        self._client = genai.Client()
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
                "google: multi-turn tool-use loop not yet implemented (ad-x8); "
                "use Anthropic for the implement subagent, or pass tools=None"
            )
        sys_prompt, user_msg = self._format_inputs(system, inputs)
        client = self._lazy_client()

        # Gemini prefers a single combined prompt; keep system + user separate via roles.
        config: dict[str, Any] = {
            "system_instruction": sys_prompt,
            "max_output_tokens": max_tokens,
        }
        if response_schema:
            config["response_mime_type"] = "application/json"

        response = client.models.generate_content(
            model=model,
            contents=user_msg,
            config=config,
        )

        content = (response.text or "").strip()
        if not content:
            raise VendorProtocolError("google: empty content")

        structured = self._extract_json(content)

        usage = {}
        if getattr(response, "usage_metadata", None):
            usage = {
                "input_tokens": getattr(response.usage_metadata, "prompt_token_count", 0),
                "output_tokens": getattr(response.usage_metadata, "candidates_token_count", 0),
            }

        return SubagentResponse(
            content=content,
            structured=structured,
            raw=response,
            usage=usage,
            vendor=self.name,
            model=model,
        )
