"""Gate markers — stored as files under `active/.gates/`.

Two gate kinds (PRD R4):
  * `prd-review`
  * `close-approval`

Each gate has two states:
  * requested  — `.gates/<name>.pending`
  * approved   — `.gates/<name>.approved` (contains approver / ts)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from auto_dev.state.atomic import atomic_write, atomic_write_json


VALID_GATES = ("prd-review", "close-approval")


def gate_dir(feature_active: Path) -> Path:
    return Path(feature_active) / ".gates"


def request(feature_active: Path, gate: str, *, detail: str = "") -> None:
    if gate not in VALID_GATES:
        raise ValueError(f"unknown gate: {gate!r}")
    d = gate_dir(feature_active)
    d.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        d / f"{gate}.pending",
        {"gate": gate, "requested_at": datetime.now(timezone.utc).isoformat(), "detail": detail},
    )


def approve(feature_active: Path, gate: str, *, approver: str = "cli-user") -> None:
    if gate not in VALID_GATES:
        raise ValueError(f"unknown gate: {gate!r}")
    d = gate_dir(feature_active)
    d.mkdir(parents=True, exist_ok=True)
    pending = d / f"{gate}.pending"
    if pending.exists():
        pending.unlink()
    atomic_write_json(
        d / f"{gate}.approved",
        {"gate": gate, "approver": approver, "approved_at": datetime.now(timezone.utc).isoformat()},
    )


def is_pending(feature_active: Path, gate: str) -> bool:
    return (gate_dir(feature_active) / f"{gate}.pending").exists() and not is_approved(feature_active, gate)


def is_approved(feature_active: Path, gate: str) -> bool:
    return (gate_dir(feature_active) / f"{gate}.approved").exists()


def list_pending(feature_active: Path) -> list[str]:
    d = gate_dir(feature_active)
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.pending"))
