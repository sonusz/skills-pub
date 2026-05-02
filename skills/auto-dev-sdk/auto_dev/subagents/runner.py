"""Shared subagent orchestration (hash guard, vendor call, schema enforcement)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from auto_dev.errors import StaleInputs, VendorProtocolError
from auto_dev.state.hashing import hash_file
from auto_dev.vendors import VendorAdapter, get_adapter
from auto_dev.vendors.config import StageSpec


class SubagentError(Exception):
    """Subagent returned `error` in its JSON payload."""

    def __init__(self, type_: str, detail: str, affected_items: list[str] | None = None):
        super().__init__(f"{type_}: {detail}")
        self.type = type_
        self.detail = detail
        self.affected_items = affected_items or []


@dataclass
class SubagentInput:
    name: str
    path: Path
    expected_hash: str


class SubagentRunner:
    def __init__(self, stage_spec: StageSpec, adapter: VendorAdapter | None = None) -> None:
        self.stage_spec = stage_spec
        self.adapter = adapter or get_adapter(stage_spec.vendor)

    def guard_inputs(self, inputs: list[SubagentInput]) -> None:
        """TOCTOU guard: recompute each input's hash and abort on mismatch."""
        for ref in inputs:
            if not ref.path.exists():
                raise StaleInputs(f"{ref.name}: {ref.path} does not exist")
            current = hash_file(ref.path)
            if current != ref.expected_hash:
                raise StaleInputs(
                    f"{ref.name}: {ref.path} hash mismatch "
                    f"(expected {ref.expected_hash}, got {current})"
                )

    def call(
        self,
        *,
        system: str,
        payload: dict[str, Any],
        max_tokens: int = 8192,
        tools: list[dict[str, Any]] | None = None,
        executor: "object | None" = None,
        max_iterations: int = 25,
    ):
        response = self.adapter.run_subagent(
            model=self.stage_spec.model,
            system=system,
            inputs=payload,
            max_tokens=max_tokens,
            tools=tools,
            executor=executor,
            max_iterations=max_iterations,
        )
        err = response.structured.get("error")
        if err:
            raise SubagentError(
                type_=err.get("type", "unknown"),
                detail=err.get("detail", ""),
                affected_items=err.get("affected_items", []),
            )
        return response

    @staticmethod
    def require_keys(obj: dict[str, Any], keys: tuple[str, ...], *, where: str) -> None:
        missing = [k for k in keys if k not in obj]
        if missing:
            raise VendorProtocolError(f"{where}: response missing keys {missing}")
