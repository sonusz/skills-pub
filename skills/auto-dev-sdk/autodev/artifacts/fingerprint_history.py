"""fingerprint-history.json — mechanism 2's per-gate finding-fingerprint
history + declined re-audit records (docs/proposals/rigor-tier.md).

Fingerprint = hash of normalized ``(category, sorted targets, sorted
evidence_refs, summary_sig)``. The summary component keeps two distinct
findings on the same section+category from colliding — a collision
triggers a spurious stop-and-diagnose (expensive); a reworded
recurrence the strict key misses merely costs one extra rerun (cheap).

Recurrence = the same fingerprint appearing under two DIFFERENT
``source_hash`` values — the reviewed package changed (producer ran)
and the finding survived it. Rounds are recorded once per enforced
verdict (keyed by ``run_ts``, so re-enforcing the same on-disk verdict
is idempotent). A panel re-run on an unchanged package (cache
invalidation, consulted-doc drift, corrupted verdict file) shares the
prior round's source_hash and is treated as the idempotent re-review
it is — NOT as recurrence; otherwise the cached, byte-identical
reviewer outputs would fabricate a stall diagnosis after zero fix
attempts. Rounds persisted before source_hash existed contribute no
recurrence evidence (missed recurrence costs one extra rerun; a false
diagnosis steers the human toward lowering rigor).

The file is reset alongside L[*] on ``autodev update --amendment``.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from autodev.artifacts.verdict import PanelFinding, PanelVerdict
from autodev.errors import SchemaError
from autodev.state.atomic import atomic_write_json

FILENAME = "fingerprint-history.json"

_BLOCKING = ("invariant_violation", "risk")


def _summary_sig(summary: str) -> str:
    normalized = re.sub(r"\s+", " ", summary.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def compute_fingerprint(f: PanelFinding) -> str:
    key = json.dumps([
        f.category or "",
        sorted(f.targets),
        sorted(f.evidence_refs),
        _summary_sig(f.summary),
    ], separators=(",", ":"))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


@dataclass
class RecurrenceReport:
    """Result of recording one enforced verdict."""
    new_round: bool
    # Fingerprints of this verdict's blocking findings that already
    # appeared in an earlier round for the same gate.
    recurring: set[str] = field(default_factory=set)
    # All blocking fingerprints of this verdict.
    blocking: set[str] = field(default_factory=set)


@dataclass
class FingerprintHistory:
    # gate → list of {"run_ts": str, "fingerprints": [str, ...]}
    rounds: dict[str, list[dict]] = field(default_factory=dict)
    # Declined re-audits: {"fingerprint": str, "r": str}
    declined: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"rounds": self.rounds, "declined": self.declined}

    def declined_pairs(self) -> set[tuple[str, str]]:
        return {(d["fingerprint"], d["r"]) for d in self.declined}


def _validate(raw: dict) -> None:
    if not isinstance(raw, dict):
        raise SchemaError("fingerprint-history root must be object")
    rounds = raw.get("rounds", {})
    if not isinstance(rounds, dict):
        raise SchemaError("fingerprint-history.rounds must be object")
    for gate, entries in rounds.items():
        if not isinstance(entries, list):
            raise SchemaError(f"fingerprint-history.rounds[{gate}] must be list")
        for e in entries:
            if not isinstance(e, dict) or "run_ts" not in e or "fingerprints" not in e:
                raise SchemaError(
                    f"fingerprint-history.rounds[{gate}] entries need "
                    "run_ts + fingerprints"
                )
    declined = raw.get("declined", [])
    if not isinstance(declined, list) or not all(
        isinstance(d, dict) and "fingerprint" in d and "r" in d
        for d in declined
    ):
        raise SchemaError(
            "fingerprint-history.declined must be list of "
            "{fingerprint, r} objects"
        )


def history_path(feature_active: Path) -> Path:
    return Path(feature_active) / FILENAME


def load_history(feature_active: Path) -> FingerprintHistory:
    p = history_path(feature_active)
    if not p.exists():
        return FingerprintHistory()
    raw = json.loads(p.read_text(encoding="utf-8"))
    _validate(raw)
    return FingerprintHistory(
        rounds={g: list(v) for g, v in raw.get("rounds", {}).items()},
        declined=list(raw.get("declined", [])),
    )


def write_history(feature_active: Path, h: FingerprintHistory) -> None:
    d = h.to_dict()
    _validate(d)
    atomic_write_json(history_path(feature_active), d)


def clear_history(feature_active: Path) -> None:
    """Amendment reset — a new PRD cycle starts fingerprint-clean."""
    history_path(feature_active).unlink(missing_ok=True)


def record_verdict(
    feature_active: Path, gate: str, verdict: PanelVerdict,
) -> RecurrenceReport:
    """Record one enforced verdict's blocking fingerprints; report
    recurrence. Idempotent per verdict ``run_ts``."""
    h = load_history(feature_active)
    blocking = {
        compute_fingerprint(f)
        for f in verdict.findings if f.severity in _BLOCKING
    }
    entries = h.rounds.setdefault(gate, [])
    prior: set[str] = set()
    for e in entries:
        # Recurrence evidence requires an intervening producer rerun,
        # witnessed by a source_hash change. Same-hash rounds are
        # idempotent re-reviews; hash-less legacy rounds are ignored
        # (fail toward missed recurrence, never false diagnosis).
        if (
            e["run_ts"] != verdict.run_ts
            and e.get("source_hash")
            and e["source_hash"] != verdict.source_hash
        ):
            prior.update(e["fingerprints"])
    new_round = not any(e["run_ts"] == verdict.run_ts for e in entries)
    if new_round:
        entries.append({
            "run_ts": verdict.run_ts,
            "source_hash": verdict.source_hash,
            "fingerprints": sorted(blocking),
        })
        write_history(feature_active, h)
    return RecurrenceReport(
        new_round=new_round,
        recurring=blocking & prior,
        blocking=blocking,
    )


def record_declined(
    feature_active: Path, pairs: set[tuple[str, str]],
) -> None:
    """Persist declined re-audit pairs (fingerprint, R) — the same
    question is never asked twice for the same stall."""
    if not pairs:
        return
    h = load_history(feature_active)
    existing = h.declined_pairs()
    for fp, r in sorted(pairs):
        if (fp, r) not in existing:
            h.declined.append({"fingerprint": fp, "r": r})
    write_history(feature_active, h)
