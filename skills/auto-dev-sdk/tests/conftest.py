"""Shared test fixtures."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from auto_dev.vendors.base import SubagentResponse, VendorAdapter
from auto_dev.vendors.config import STAGES, StageSpec, VendorsConfig
from auto_dev.vendors.registry import register_adapter


class FakeVendor(VendorAdapter):
    """In-memory adapter driven by a scripted response map."""

    name = "fake"

    def __init__(self) -> None:
        self.responses: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []

    def script(self, *, stage_tag: str, response: dict[str, Any]) -> None:
        self.responses[stage_tag] = response

    def run_subagent(
        self, *, model, system, inputs,
        response_schema=None, tools=None, executor=None,
        max_tokens=4096, max_iterations=25,
    ):
        # Choose response by 'stage_tag' baked into `inputs` or by model string.
        tag = inputs.get("stage_tag") or model
        self.calls.append({
            "model": model, "inputs": inputs,
            "had_tools": tools is not None,
            "had_executor": executor is not None,
        })
        if tag not in self.responses:
            raise KeyError(f"FakeVendor: no scripted response for {tag!r}")
        body = self.responses[tag]
        return SubagentResponse(
            content=json.dumps(body),
            structured=body,
            raw=None,
            vendor=self.name,
            model=model,
        )


_fake_singleton: FakeVendor | None = None


@pytest.fixture
def fake_vendor(monkeypatch):
    global _fake_singleton
    fake = FakeVendor()
    _fake_singleton = fake
    # Register a class that returns the singleton.
    register_adapter("fake", lambda: fake)  # type: ignore[arg-type]
    yield fake
    _fake_singleton = None


@pytest.fixture
def fake_vendors_config(tmp_path) -> VendorsConfig:
    stages = {s: StageSpec(stage=s, vendor="fake", model=f"fake-{s}") for s in STAGES}
    return VendorsConfig(path=tmp_path / "vendors.yml", stages=stages)


@pytest.fixture
def repo_root(tmp_path) -> Path:
    (tmp_path / "docs" / "features").mkdir(parents=True)
    return tmp_path
