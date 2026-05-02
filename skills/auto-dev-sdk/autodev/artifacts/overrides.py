"""overrides.json — skip-gate + acknowledge-dirty (R4d, R2a ack)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from autodev.errors import SchemaError
from autodev.state.atomic import atomic_write_json

OverrideKind = Literal["skip_gate", "dirty_workspace"]
# G16: severity classification for skip-gate ceiling counting.
OverrideSeverity = Literal["low", "normal", "high"]
_VALID_SEVERITY = {"low", "normal", "high"}

# G16 ceiling rules, applied per-cycle to ACTIVE skip_gate records:
#   - severity=low records do not count
#   - severity=normal records count as 1 each
#   - severity=high records count as 2 each
#   - total >= 2 → WARNING on close (requires --yes)
#   - total >= 3 → REFUSE close (even with --yes)
SEVERITY_WEIGHT: dict[str, int] = {"low": 0, "normal": 1, "high": 2}
CEILING_WARNING_AT = 2
CEILING_REFUSE_AT = 3


@dataclass
class OverrideRecord:
    kind: OverrideKind
    reason: str
    who: str
    ts: str
    skipped_in_cycle: int
    # For skip-gate only
    gate: str | None = None
    # `active` vs historical — harness flips this when cycle rolls
    active: bool = True
    # G16: severity. Pre-G16 records load with "normal" for back-compat.
    severity: OverrideSeverity = "normal"

    def to_dict(self) -> dict:
        d: dict = {
            "kind": self.kind,
            "reason": self.reason,
            "who": self.who,
            "ts": self.ts,
            "skipped_in_cycle": self.skipped_in_cycle,
            "active": self.active,
            "severity": self.severity,
        }
        if self.gate is not None:
            d["gate"] = self.gate
        return d


@dataclass
class Overrides:
    current_cycle: int = 1
    records: list[OverrideRecord] = field(default_factory=list)

    def active_records(self) -> list[OverrideRecord]:
        return [r for r in self.records if r.active]

    def historical_records(self) -> list[OverrideRecord]:
        return [r for r in self.records if not r.active]

    def has_active_skip_gate(self, gate: str) -> bool:
        return any(
            r.active and r.kind == "skip_gate" and r.gate == gate
            and r.skipped_in_cycle == self.current_cycle
            for r in self.records
        )

    def has_active_dirty_ack(self) -> bool:
        return any(
            r.active and r.kind == "dirty_workspace"
            and r.skipped_in_cycle == self.current_cycle
            for r in self.records
        )

    def add_skip_gate(
        self, gate: str, reason: str, who: str,
        severity: OverrideSeverity = "normal",
    ) -> None:
        if severity not in _VALID_SEVERITY:
            raise ValueError(
                f"severity must be one of {sorted(_VALID_SEVERITY)}; got {severity!r}"
            )
        self.records.append(OverrideRecord(
            kind="skip_gate", reason=reason, who=who,
            ts=datetime.now(timezone.utc).isoformat(),
            skipped_in_cycle=self.current_cycle, gate=gate, active=True,
            severity=severity,
        ))

    def active_skip_gate_weight(self) -> int:
        """G16: sum severity weights across active skip_gate records in cycle."""
        return sum(
            SEVERITY_WEIGHT.get(r.severity, 1)
            for r in self.records
            if r.active and r.kind == "skip_gate"
            and r.skipped_in_cycle == self.current_cycle
        )

    def add_dirty_ack(self, reason: str, who: str) -> None:
        self.records.append(OverrideRecord(
            kind="dirty_workspace", reason=reason, who=who,
            ts=datetime.now(timezone.utc).isoformat(),
            skipped_in_cycle=self.current_cycle, active=True,
        ))

    def advance_cycle(self) -> None:
        """`autodev update` bumps cycle; all active records flip to historical."""
        self.current_cycle += 1
        for r in self.records:
            r.active = False  # historical

    def invalidate_cross_cycle(self, crossed_gate: str) -> None:
        """Same-cycle skip-gate for a crossed gate is cleared (R4d).

        acknowledge-dirty survives (different axis — see PRD).
        """
        for r in self.records:
            if (r.active and r.kind == "skip_gate" and r.gate == crossed_gate
                    and r.skipped_in_cycle == self.current_cycle):
                r.active = False  # moved to historical

    def to_dict(self) -> dict:
        return {
            "current_cycle": self.current_cycle,
            "records": [r.to_dict() for r in self.records],
        }


def _validate(obj: dict) -> None:
    for k in ("current_cycle", "records"):
        if k not in obj:
            raise SchemaError(f"overrides.json missing {k}")
    for i, r in enumerate(obj["records"]):
        for k in ("kind", "reason", "who", "ts", "skipped_in_cycle", "active"):
            if k not in r:
                raise SchemaError(f"records[{i}] missing {k}")
        if r["kind"] not in ("skip_gate", "dirty_workspace"):
            raise SchemaError(f"records[{i}].kind invalid")
        if r["kind"] == "skip_gate" and "gate" not in r:
            raise SchemaError(f"records[{i}] skip_gate missing gate")
        if not isinstance(r["reason"], str) or not r["reason"].strip():
            raise SchemaError(f"records[{i}].reason must be non-empty string")
        # G16: severity optional on read (back-compat); if present, validated.
        if "severity" in r and r["severity"] not in _VALID_SEVERITY:
            raise SchemaError(
                f"records[{i}].severity must be one of {sorted(_VALID_SEVERITY)}"
            )


def load_overrides(path: Path) -> Overrides:
    if not Path(path).exists():
        return Overrides()
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    _validate(raw)
    return Overrides(
        current_cycle=raw["current_cycle"],
        records=[
            OverrideRecord(
                kind=r["kind"], reason=r["reason"], who=r["who"], ts=r["ts"],
                skipped_in_cycle=r["skipped_in_cycle"],
                gate=r.get("gate"), active=r["active"],
                severity=r.get("severity", "normal"),
            )
            for r in raw["records"]
        ],
    )


def write_overrides(path: Path, o: Overrides) -> None:
    d = o.to_dict()
    _validate(d)
    atomic_write_json(Path(path), d)
