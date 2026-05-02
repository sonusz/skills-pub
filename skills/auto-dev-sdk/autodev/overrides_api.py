"""Override helpers (R4d, R2a dirty-ack) — CLI-facing entry points."""
from __future__ import annotations

import os
from pathlib import Path

from autodev.artifacts.overrides import Overrides, load_overrides, write_overrides

WHO_ENV = "AUTODEV_WHO"


def resolve_who(explicit: str | None = None) -> str:
    """flag > env > $USER > 'unknown' (R4d)."""
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get(WHO_ENV)
    if env and env.strip():
        return env.strip()
    u = os.environ.get("USER") or os.environ.get("USERNAME")
    if u and u.strip():
        return u.strip()
    return "unknown"


def overrides_path(feature_active: Path) -> Path:
    return Path(feature_active) / "overrides.json"


def load(feature_active: Path) -> Overrides:
    return load_overrides(overrides_path(feature_active))


def save(feature_active: Path, o: Overrides) -> None:
    write_overrides(overrides_path(feature_active), o)


def record_skip_gate(
    feature_active: Path, *, gate: str, reason: str, who: str | None = None,
    severity: str = "normal",
) -> Overrides:
    if not reason or not reason.strip():
        raise ValueError("skip-gate requires a non-empty --reason")
    o = load(feature_active)
    o.add_skip_gate(gate, reason.strip(), resolve_who(who), severity=severity)  # type: ignore[arg-type]
    save(feature_active, o)
    return o


def record_acknowledge_dirty(
    feature_active: Path, *, reason: str, who: str | None = None,
) -> Overrides:
    if not reason or not reason.strip():
        raise ValueError("acknowledge-dirty requires a non-empty --reason")
    o = load(feature_active)
    o.add_dirty_ack(reason.strip(), resolve_who(who))
    save(feature_active, o)
    return o


def advance_cycle(feature_active: Path) -> Overrides:
    o = load(feature_active)
    o.advance_cycle()
    save(feature_active, o)
    return o


def invalidate_clears_skip_gate(feature_active: Path, gate: str) -> Overrides:
    o = load(feature_active)
    o.invalidate_cross_cycle(gate)
    save(feature_active, o)
    return o
