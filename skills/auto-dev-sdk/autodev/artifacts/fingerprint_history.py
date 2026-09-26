"""Per-gate finding-fingerprint history used to size panel rework.

Fingerprint = a coarse structural identity over category, targets,
evidence references, failure class, and missized direction. Summary wording
is deliberately excluded: reviewers routinely restate the same defect and
exact prose matching caused needless redesign rounds. Synthesizer cluster IDs
provide the semantic identity; this coarse key is the deterministic fallback.

Recurrence = the same fingerprint appearing under two DIFFERENT
``source_hash`` values — the reviewed package changed (producer ran)
and the finding survived it. Recurrence selects a broader ``root-cause``
rework mode; it never stops the revision loop. Rounds are recorded once per
enforced verdict (keyed by ``run_ts``, so re-enforcing the same on-disk verdict
is idempotent). A panel re-run on an unchanged package (cache
invalidation, consulted-doc drift, corrupted verdict file) shares the
prior round's source_hash and is treated as the idempotent re-review
it is — NOT as recurrence; otherwise the cached, byte-identical
reviewer outputs would incorrectly force broad rework after zero fix attempts.
Rounds persisted before source_hash existed contribute no recurrence evidence.

The file is reset alongside L[*] on ``autodev update``.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from autodev.artifacts.verdict import PanelFinding, PanelVerdict
from autodev.errors import SchemaError
from autodev.state.atomic import atomic_write_json

FILENAME = "fingerprint-history.json"

_BLOCKING = ("invariant_violation", "risk")


def compute_fingerprint(f: PanelFinding) -> str:
    key = json.dumps([
        f.category or "",
        sorted(f.targets),
        sorted(f.evidence_refs),
        f.failure_class or "",
        f.missized_direction or "",
    ], separators=(",", ":"))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def _cluster_fingerprint(findings: list[PanelFinding]) -> str:
    member_keys = sorted({compute_fingerprint(f) for f in findings})
    if len(member_keys) == 1:
        return member_keys[0]
    raw = json.dumps(member_keys, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


@dataclass
class RecurrenceReport:
    """Result of recording one enforced verdict."""
    new_round: bool
    # Fingerprints of this verdict's blocking findings that already
    # appeared in an earlier round for the same gate.
    recurring: set[str] = field(default_factory=set)
    # All blocking fingerprints of this verdict.
    blocking: set[str] = field(default_factory=set)
    # Semantic cluster IDs in the current verdict that matched either a
    # prior ID or a prior coarse fingerprint.
    recurring_cluster_ids: set[str] = field(default_factory=set)


@dataclass
class FingerprintHistory:
    # gate → list of {"run_ts": str, "fingerprints": [str, ...]}
    rounds: dict[str, list[dict]] = field(default_factory=dict)
    # Legacy declined re-audits retained for on-disk compatibility. New runs
    # do not create these after fingerprint-based early halts were removed.
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


def prior_cluster_catalog(feature_active: Path, gate: str) -> list[dict]:
    """Return the newest audit record for each prior semantic cluster."""
    h = load_history(feature_active)
    by_id: dict[str, dict] = {}
    for entry in reversed(h.rounds.get(gate, [])):
        for cluster in entry.get("clusters", []):
            cid = cluster.get("cluster_id") if isinstance(cluster, dict) else None
            if isinstance(cid, str) and cid and cid not in by_id:
                by_id[cid] = dict(cluster)
    return [by_id[cid] for cid in sorted(by_id)]


def clear_history(feature_active: Path) -> None:
    """Amendment reset — a new PRD cycle starts fingerprint-clean."""
    history_path(feature_active).unlink(missing_ok=True)


def record_verdict(
    feature_active: Path, gate: str, verdict: PanelVerdict,
) -> RecurrenceReport:
    """Record one enforced verdict's blocking fingerprints; report
    recurrence. Idempotent per verdict ``run_ts``."""
    h = load_history(feature_active)
    blocking_findings = verdict.blocking_findings()
    by_id = {f.finding_id: f for f in blocking_findings if f.finding_id}
    assigned: set[str] = set()
    cluster_records: list[dict] = []
    for cluster in verdict.issue_clusters:
        members = [by_id[i] for i in cluster.finding_ids if i in by_id]
        if not members:
            continue
        assigned.update(f.finding_id for f in members if f.finding_id)
        cluster_records.append({
            "cluster_id": cluster.cluster_id,
            "fingerprint": _cluster_fingerprint(members),
            "summary": cluster.summary,
            "targets": sorted({t for f in members for t in f.targets}),
            "evidence_refs": sorted({r for f in members for r in f.evidence_refs}),
            "categories": sorted({f.category for f in members if f.category}),
            "priority": cluster.priority,
        })
    for index, finding in enumerate(blocking_findings):
        if finding.finding_id and finding.finding_id in assigned:
            continue
        fp = compute_fingerprint(finding)
        cluster_records.append({
            "cluster_id": f"singleton-{fp}-{index}",
            "fingerprint": fp,
            "summary": finding.summary,
            "targets": sorted(finding.targets),
            "evidence_refs": sorted(finding.evidence_refs),
            "categories": [finding.category] if finding.category else [],
            "priority": finding.effective_priority(),
        })
    blocking = {c["fingerprint"] for c in cluster_records}
    cluster_ids = {c["cluster_id"] for c in cluster_records}
    entries = h.rounds.setdefault(gate, [])
    prior: set[str] = set()
    prior_cluster_ids: set[str] = set()
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
            prior_cluster_ids.update(e.get("cluster_ids", []))
    new_round = not any(e["run_ts"] == verdict.run_ts for e in entries)
    if new_round:
        entries.append({
            "run_ts": verdict.run_ts,
            "source_hash": verdict.source_hash,
            "fingerprints": sorted(blocking),
            "cluster_ids": sorted(cluster_ids),
            "clusters": cluster_records,
        })
        write_history(feature_active, h)
    recurring_ids = {
        c["cluster_id"] for c in cluster_records
        if c["cluster_id"] in prior_cluster_ids or c["fingerprint"] in prior
    }
    recurring_fingerprints = {
        c["fingerprint"] for c in cluster_records
        if c["cluster_id"] in recurring_ids
    }
    return RecurrenceReport(
        new_round=new_round,
        recurring=recurring_fingerprints,
        blocking=blocking,
        recurring_cluster_ids=recurring_ids,
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
