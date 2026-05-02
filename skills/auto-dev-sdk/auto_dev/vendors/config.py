"""vendors.yml loader + per-stage resolution.

Schema::

    # vendors.yml
    prd_intake:   { vendor: anthropic, model: claude-opus-4-7 }
    prd_review:   { vendor: openai,    model: gpt-5 }
    scope:        { vendor: anthropic, model: claude-opus-4-7 }
    plan:         { vendor: anthropic, model: claude-sonnet-4-6 }
    implement:    { vendor: anthropic, model: claude-opus-4-7 }
    spec:         { vendor: anthropic, model: claude-sonnet-4-6 }
    review:       { vendor: google,    model: gemini-2.5-pro }

Constraints (PRD R3):
  * Every stage in `STAGES` must be declared — missing stage → error at
    startup.
  * No fallback model: primary call failure exits with `VendorProtocolError`
    bubbling up; caller decides whether to edit `vendors.yml` and resume.
  * `panel_review` stage is NOT listed here — panel-review skill owns its
    own vendor selection. Attempting to override via this config is a no-op
    by design (see `resolve()` guard).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from auto_dev.errors import ConfigError

STAGES = (
    "prd_intake",
    "prd_review",
    "scope",
    "plan",
    "implement",
    "spec",
    "review",
)

# Stages whose vendor is NOT resolvable here — delegated external skills
# manage their own selection.
DELEGATED_STAGES = frozenset({"panel_review"})


@dataclass(frozen=True)
class StageSpec:
    stage: str
    vendor: str
    model: str


@dataclass
class VendorsConfig:
    path: Path
    stages: dict[str, StageSpec]

    def resolve(self, stage: str) -> StageSpec:
        if stage in DELEGATED_STAGES:
            raise ConfigError(
                f"{stage} is delegated to an external skill; vendors.yml does not override it"
            )
        if stage not in self.stages:
            raise ConfigError(f"vendors.yml missing stage: {stage}")
        return self.stages[stage]


def _validate_raw(raw: Any, path: Path) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top-level must be a mapping")
    missing = [s for s in STAGES if s not in raw]
    if missing:
        raise ConfigError(f"{path}: missing stages {missing}")
    for stage in STAGES:
        entry = raw[stage]
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: stage {stage} must be a mapping, got {type(entry).__name__}")
        for k in ("vendor", "model"):
            if k not in entry:
                raise ConfigError(f"{path}: stage {stage} missing key {k}")
        if not isinstance(entry["vendor"], str) or not isinstance(entry["model"], str):
            raise ConfigError(f"{path}: stage {stage} vendor/model must be strings")
    return raw


def load_vendors_config(path: Path) -> VendorsConfig:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"vendors.yml not found at {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    raw = _validate_raw(raw, p)
    stages = {
        s: StageSpec(stage=s, vendor=raw[s]["vendor"], model=raw[s]["model"])
        for s in STAGES
    }
    return VendorsConfig(path=p, stages=stages)
