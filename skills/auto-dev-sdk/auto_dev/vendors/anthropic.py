"""Anthropic adapter (Claude).

Three call modes:

1. `tools=None, response_schema=None` — one-shot text response; JSON
   extracted from the content.
2. `tools=None, response_schema=<dict>` — forced `return_structured`
   tool_use call (R14); JSON arrives as block.input.
3. `tools=[...], executor=<ToolExecutor>` — **multi-turn tool-use loop**
   (R15/R16): dispatch each tool_use block through the executor, feed
   tool_result back, loop until stop_reason == "end_turn" or the
   iteration cap is hit.

The `implement` subagent uses mode 3; plan / spec / review / prd_review
use mode 1 or 2.
"""
from __future__ import annotations

from typing import Any

from auto_dev.errors import VendorProtocolError
from auto_dev.vendors.base import SubagentResponse, VendorAdapter


class AnthropicAdapter(VendorAdapter):
    name = "anthropic"

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    def _lazy_client(self):
        if self._client is not None:
            return self._client
        try:
            import anthropic  # type: ignore
        except ImportError as e:
            raise VendorProtocolError(
                "anthropic SDK not installed; pip install anthropic"
            ) from e
        self._client = anthropic.Anthropic()
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
        sys_prompt, user_msg = self._format_inputs(system, inputs)
        client = self._lazy_client()

        if tools is not None:
            if executor is None:
                raise VendorProtocolError(
                    "anthropic: tools provided but no executor to dispatch them through"
                )
            return self._run_tool_loop(
                client=client, model=model, system=sys_prompt, user=user_msg,
                tools=tools, executor=executor,
                max_tokens=max_tokens, max_iterations=max_iterations,
            )

        if response_schema is not None:
            return self._run_with_schema(
                client=client, model=model, system=sys_prompt, user=user_msg,
                schema=response_schema, max_tokens=max_tokens,
            )

        response = client.messages.create(
            model=model,
            system=sys_prompt,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": user_msg}],
        )
        content = _concat_text(response.content)
        if not content:
            raise VendorProtocolError("anthropic: no text blocks in response")
        structured = self._extract_json(content)
        return SubagentResponse(
            content=content, structured=structured, raw=response,
            usage=_usage(response), vendor=self.name, model=model,
        )

    # ------------------------------------------------------------------
    # Mode 2: forced structured-output tool_use (R14)

    def _run_with_schema(
        self,
        *,
        client: Any,
        model: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_tokens: int,
    ) -> SubagentResponse:
        tool = {
            "name": "return_structured",
            "description": "Return the structured result as JSON.",
            "input_schema": schema,
        }
        response = client.messages.create(
            model=model, system=system, max_tokens=max_tokens,
            messages=[{"role": "user", "content": user}],
            tools=[tool],
            tool_choice={"type": "tool", "name": "return_structured"},
        )
        structured: dict[str, Any] | None = None
        text_parts: list[str] = []
        for block in response.content:
            btype = getattr(block, "type", None)
            if btype == "tool_use" and getattr(block, "name", "") == "return_structured":
                structured = dict(block.input)  # type: ignore[arg-type]
            elif btype == "text":
                text_parts.append(block.text)
        if structured is None:
            raise VendorProtocolError(
                "anthropic: tool_use did not return `return_structured`; "
                "model may have declined schema-bound output"
            )
        return SubagentResponse(
            content="\n".join(text_parts).strip(),
            structured=structured, raw=response,
            usage=_usage(response), vendor=self.name, model=model,
        )

    # ------------------------------------------------------------------
    # Mode 3: multi-turn tool-use loop (R15/R16)

    def _run_tool_loop(
        self,
        *,
        client: Any,
        model: str,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        executor: Any,
        max_tokens: int,
        max_iterations: int,
    ) -> SubagentResponse:
        """Drive the {text | tool_use | tool_result} loop.

        Each iteration:
          1. Call `messages.create` with the running `messages` list.
          2. If `stop_reason == "end_turn"`: extract JSON from text blocks, done.
          3. If `stop_reason == "tool_use"`:
             * Append the assistant's content blocks as-is.
             * For each tool_use block, dispatch to executor.
             * Append a user turn with tool_result blocks.
             * Loop.
          4. Any other stop_reason: error.
        Raises VendorProtocolError on iteration-cap overrun or unknown stop_reason.
        """
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        last_response: Any = None
        text_parts: list[str] = []
        total_usage = {"input_tokens": 0, "output_tokens": 0}

        for iteration in range(max_iterations):
            response = client.messages.create(
                model=model, system=system, max_tokens=max_tokens,
                messages=messages, tools=tools,
            )
            last_response = response
            u = _usage(response)
            for k in ("input_tokens", "output_tokens"):
                total_usage[k] = total_usage.get(k, 0) + u.get(k, 0)

            stop_reason = getattr(response, "stop_reason", None)

            if stop_reason == "end_turn":
                turn_text = _concat_text(response.content)
                if turn_text:
                    text_parts.append(turn_text)
                full_text = "\n\n".join(text_parts).strip()
                if not full_text:
                    raise VendorProtocolError(
                        "anthropic: end_turn reached with no text output; "
                        "no JSON payload to extract"
                    )
                structured = self._extract_json(full_text)
                return SubagentResponse(
                    content=full_text, structured=structured,
                    raw=last_response, usage=total_usage,
                    vendor=self.name, model=model,
                )

            if stop_reason == "tool_use":
                turn_text = _concat_text(response.content)
                if turn_text:
                    text_parts.append(turn_text)
                # Record assistant turn verbatim (all blocks) for replay.
                messages.append({
                    "role": "assistant",
                    "content": _blocks_to_request_shape(response.content),
                })
                # Collect tool_use blocks, dispatch, build tool_result user turn.
                tool_results: list[dict[str, Any]] = []
                for block in response.content:
                    if getattr(block, "type", None) != "tool_use":
                        continue
                    tool_name = getattr(block, "name", "")
                    tool_input = dict(getattr(block, "input", {}) or {})
                    block_id = getattr(block, "id", "")
                    result = _dispatch_safely(executor, tool_name, tool_input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block_id,
                        "content": result["content"],
                        "is_error": result["is_error"],
                    })
                if not tool_results:
                    raise VendorProtocolError(
                        "anthropic: stop_reason=tool_use but no tool_use blocks found"
                    )
                messages.append({"role": "user", "content": tool_results})
                continue

            # Anthropic also returns max_tokens, stop_sequence, pause_turn.
            raise VendorProtocolError(
                f"anthropic: unexpected stop_reason={stop_reason!r}"
            )

        raise VendorProtocolError(
            f"anthropic: tool-use loop exceeded max_iterations={max_iterations}; "
            f"model may be stuck or the cap is too low"
        )


# ----------------------------------------------------------------------
# Helpers


def _concat_text(content: Any) -> str:
    parts: list[str] = []
    for block in content or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
    return "\n".join(parts).strip()


def _blocks_to_request_shape(content: Any) -> list[dict[str, Any]]:
    """Convert an assistant's response blocks back to the request-side shape.

    Needed for `messages` replay when we loop — the SDK returns typed
    objects on the response but expects plain dicts on the request.
    """
    out: list[dict[str, Any]] = []
    for block in content or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            out.append({"type": "text", "text": getattr(block, "text", "")})
        elif btype == "tool_use":
            out.append({
                "type": "tool_use",
                "id": getattr(block, "id", ""),
                "name": getattr(block, "name", ""),
                "input": dict(getattr(block, "input", {}) or {}),
            })
        # Other types (thinking, redacted) are passed through best-effort.
        else:
            # Fall back to object's __dict__ / asdict if available.
            if hasattr(block, "model_dump"):
                out.append(block.model_dump())
            elif hasattr(block, "to_dict"):
                out.append(block.to_dict())
    return out


def _dispatch_safely(executor: Any, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one tool call; convert exceptions to structured error content.

    Returns `{content: str, is_error: bool}` suitable for a tool_result block.
    Permission denials and tool errors are reported back to the model as
    tool_result with is_error=True; the model decides whether to retry,
    adjust, or record a deviation. This matches the prompt's escalation
    rubric: "DENIED command is NOT an error — record as deviation".
    """
    try:
        result = executor.dispatch(tool_name, args)
    except Exception as e:  # noqa: BLE001
        return {
            "content": f"{type(e).__name__}: {e}",
            "is_error": True,
        }
    # ToolResult has ok, output, exit_code, tool, args
    ok = getattr(result, "ok", True)
    output = getattr(result, "output", "") or ""
    # Keep tool_result payload compact — cap at 40 KB to avoid blowing context.
    if len(output) > 40_000:
        output = output[:40_000] + "\n\n[...truncated at 40 KB...]"
    return {"content": output if output else ("(no output)" if ok else "(no error output)"),
            "is_error": not ok}


def _usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    return {
        "input_tokens": getattr(usage, "input_tokens", 0),
        "output_tokens": getattr(usage, "output_tokens", 0),
    }
