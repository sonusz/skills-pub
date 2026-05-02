"""Ralph loop helpers for phase-5 / phase-5b.

Wraps build → review in an inner iteration loop so a single dev session
isn't required to complete all active scope items. Exit conditions:

1. **All Fully** — every active scope_id has ``status=Fully`` in the
   persisted loop-review artifact; loop exits and the pipeline advances
   to spec.
2. **Build-authored halt/route** — g-23 / g-24 deviations stop the
   natural loop before the review step.

Phase-5b removes the earlier stall-based exit from the orchestrator
wiring. ``detect_stall`` remains here as a compatibility primitive and
for regression coverage, but the loop no longer calls it.

The ralph loop does NOT invoke panels. Panel close-approval runs once
after ralph exits (outside this module's scope).

Module contract:

- ``RalphState`` — persisted in ``ralph-state.json`` (not in the main
  artifact cascade). Fields: iter count, per-iter Fully sets, regression
  log, started_at / last_iter_at. Cleared on close; cleared on
  ``autodev update --amendment`` (rescheduled against new scope.json);
  cleared on any g-24 route (upstream changed).
- ``parse_review_statuses(review_md_path)`` — return dict[scope_id, status].
- ``active_scope_ids(scope_json_path)`` — return set[str] of active items.
- ``detect_stall(fully_history, k=3)`` — True if the last k iterations
  added no new Fully entries.
- ``detect_regressions(statuses_history)`` — return list of regression
  events (scope_id transitioned Fully → lesser).

STATUS vocabulary: ``Fully`` / ``Partial`` / ``Missing`` / ``Deviated`` /
``Deferred``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from autodev.artifacts.scope import load_scope
from autodev.errors import SchemaError
from autodev.state.atomic import atomic_write_json

# Stall detection threshold (consecutive non-progress iterations).
K_STALL = 3

# Canonical status vocabulary.
STATUS_FULLY = "Fully"
STATUS_PARTIAL = "Partial"
STATUS_MISSING = "Missing"
STATUS_DEVIATED = "Deviated"
STATUS_DEFERRED = "Deferred"  # not a ralph "complete" signal, but valid

ALL_STATUSES: tuple[str, ...] = (
    STATUS_FULLY, STATUS_PARTIAL, STATUS_MISSING,
    STATUS_DEVIATED, STATUS_DEFERRED,
)

# Status rank (for regression detection): higher = more complete.
_STATUS_RANK: dict[str, int] = {
    STATUS_MISSING: 0,
    STATUS_DEVIATED: 1,
    STATUS_PARTIAL: 2,
    STATUS_DEFERRED: 2,  # same rank as Partial — acknowledged-dropped
    STATUS_FULLY: 3,
}


def parse_review_statuses(review_path: Path) -> dict[str, str]:
    """Parse a ralph-review.json artifact into {scope_id: canonical_status}.

    Schema::

        {
          "classifications": [
            {"req_id": "v3c-1.r1", "scope_id": "v3c-1",
             "classification": "Fully", "evidence": "..."},
            ...
          ],
          "summary": {"Fully": ..., "Partial": ..., ...}
        }

    Each trace row appears once. This function rolls up per-trace-row
    classifications into per-scope-item by taking the WORST rank —
    a single ``Missing`` row drops the scope item's rollup below
    ``Fully``. Rollup rule (rank: Missing=0, Deviated=1,
    Partial/Deferred=2, Fully=3): min rank across rows wins.

    Raises:
        SchemaError on invalid JSON, duplicate req_id, unrecognized
        classification, or missing required fields.
    """
    p = Path(review_path)
    if not p.exists():
        raise SchemaError(f"ralph-review not found at {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SchemaError(f"ralph-review at {p}: invalid JSON: {e}")
    if not isinstance(data, dict) or "classifications" not in data:
        raise SchemaError(
            f"ralph-review at {p}: missing 'classifications' field"
        )
    return _parse_ralph_review_json(data)


def _parse_ralph_review_json(data: dict) -> dict[str, str]:
    """Parse the v3-core JSON schema + roll up per-trace-row → per-scope."""
    classifications = data.get("classifications", [])
    if not isinstance(classifications, list):
        raise SchemaError("ralph-review.classifications must be a list")

    # Collect per-scope minimum rank (worst classification wins).
    per_scope_rank: dict[str, int] = {}
    per_scope_status: dict[str, str] = {}
    seen_req_ids: set[str] = set()

    for i, entry in enumerate(classifications):
        if not isinstance(entry, dict):
            raise SchemaError(f"classifications[{i}] must be object")
        req_id = entry.get("req_id")
        scope_id = entry.get("scope_id")
        cls = entry.get("classification")
        if not req_id or not scope_id or not cls:
            raise SchemaError(
                f"classifications[{i}] missing req_id/scope_id/classification"
            )
        if req_id in seen_req_ids:
            raise SchemaError(f"duplicate req_id {req_id!r}")
        seen_req_ids.add(req_id)
        if cls not in ALL_STATUSES:
            raise SchemaError(
                f"classifications[{i}] unknown classification {cls!r}"
            )
        rank = _STATUS_RANK[cls]
        if scope_id not in per_scope_rank or rank < per_scope_rank[scope_id]:
            per_scope_rank[scope_id] = rank
            per_scope_status[scope_id] = cls

    return per_scope_status


def active_scope_ids(scope_json_path: Path) -> set[str]:
    """Return the set of scope item IDs with ``status == "active"``."""
    p = Path(scope_json_path)
    if not p.exists():
        raise SchemaError(f"scope.json not found at {p}")
    scope = load_scope(str(p))
    return {item.id for item in scope.in_scope if item.status == "active"}


def fully_set(statuses: dict[str, str]) -> set[str]:
    """Extract the set of scope_ids whose status is Fully."""
    return {sid for sid, st in statuses.items() if st == STATUS_FULLY}


def detect_stall(fully_history: list[set[str]], *, k: int = K_STALL) -> bool:
    """True iff the last ``k`` entries failed to strictly grow.

    The history is expected to have ``fully_history[0] == set()`` as
    the pre-iter-1 baseline, with one entry appended per completed iter.
    We need at least ``k + 1`` total entries to evaluate ``k``
    consecutive non-progress iterations.

    Non-progress at iter N iff ``fully_N ⊆ fully_{N-1}`` (unchanged OR
    shrunk — a shrink is regression, but the stall detector treats it
    the same as no-progress for halt purposes; the separate regression
    detector surfaces the shrink event independently)."""
    if k < 1:
        return False
    if len(fully_history) < k + 1:
        return False
    tail = fully_history[-(k + 1):]
    for i in range(1, len(tail)):
        prev = tail[i - 1]
        curr = tail[i]
        if not curr.issubset(prev):
            # At least one new entry added → progress; not a stall.
            return False
    return True


@dataclass
class RegressionEvent:
    scope_id: str
    iter_index: int          # the iter at which the regression was observed
    prior_status: str
    new_status: str


def detect_regressions(
    statuses_history: list[dict[str, str]],
) -> list[RegressionEvent]:
    """Find all Fully → lesser transitions across the history.

    Returns one event per scope_id per iter at which it regressed
    (multiple regressions of the same scope_id across different iters
    each yield a separate event)."""
    events: list[RegressionEvent] = []
    for i in range(1, len(statuses_history)):
        prev = statuses_history[i - 1]
        curr = statuses_history[i]
        for sid, prev_st in prev.items():
            if prev_st != STATUS_FULLY:
                continue
            new_st = curr.get(sid, STATUS_MISSING)
            if _STATUS_RANK.get(new_st, 0) < _STATUS_RANK[STATUS_FULLY]:
                events.append(RegressionEvent(
                    scope_id=sid, iter_index=i,
                    prior_status=prev_st, new_status=new_st,
                ))
    return events


# ---- RalphState persistence -----------------------------------------


@dataclass
class RalphState:
    source: str = ""                 # scope.json path (relative, for provenance)
    source_hash: str = ""
    iter: int = 0
    # One entry per completed iter. fully_history[0] is the pre-iter-1
    # baseline (empty set); fully_history[N] is the Fully set observed
    # at end of iter N. Serialized as sorted list[str] for determinism.
    fully_history: list[set[str]] = field(default_factory=lambda: [set()])
    # Statuses at end of each completed iter (for regression detection).
    statuses_history: list[dict[str, str]] = field(default_factory=lambda: [{}])
    regressions: list[RegressionEvent] = field(default_factory=list)
    trace_hash: str = ""
    test_plan_hash: str = ""
    started_at: str = ""
    last_iter_at: str = ""

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "source_hash": self.source_hash,
            "iter": self.iter,
            "fully_history": [sorted(s) for s in self.fully_history],
            "statuses_history": [dict(m) for m in self.statuses_history],
            "regressions": [
                {
                    "scope_id": r.scope_id, "iter_index": r.iter_index,
                    "prior_status": r.prior_status, "new_status": r.new_status,
                }
                for r in self.regressions
            ],
            "trace_hash": self.trace_hash,
            "test_plan_hash": self.test_plan_hash,
            "started_at": self.started_at,
            "last_iter_at": self.last_iter_at,
        }


def state_path(feature_active: Path) -> Path:
    return Path(feature_active) / "ralph-state.json"


def load_ralph_state(feature_active: Path) -> RalphState:
    p = state_path(feature_active)
    if not p.exists():
        return RalphState(started_at=datetime.now(timezone.utc).isoformat())
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SchemaError("ralph-state.json root must be object")
    fh_raw = raw.get("fully_history", [[]])
    if not isinstance(fh_raw, list):
        raise SchemaError("ralph-state.fully_history must be list")
    fully_history = [set(entry) for entry in fh_raw]
    sh_raw = raw.get("statuses_history", [{}])
    if not isinstance(sh_raw, list):
        raise SchemaError("ralph-state.statuses_history must be list")
    statuses_history = [dict(m) for m in sh_raw]
    regs = [
        RegressionEvent(
            scope_id=r["scope_id"], iter_index=r["iter_index"],
            prior_status=r["prior_status"], new_status=r["new_status"],
        )
        for r in raw.get("regressions", [])
    ]
    return RalphState(
        source=raw.get("source", ""),
        source_hash=raw.get("source_hash", ""),
        iter=raw.get("iter", 0),
        fully_history=fully_history,
        statuses_history=statuses_history,
        regressions=regs,
        trace_hash=raw.get("trace_hash", ""),
        test_plan_hash=raw.get("test_plan_hash", ""),
        started_at=raw.get("started_at", ""),
        last_iter_at=raw.get("last_iter_at", ""),
    )


def write_ralph_state(feature_active: Path, state: RalphState) -> None:
    atomic_write_json(state_path(feature_active), state.to_dict())


def clear_ralph_state(feature_active: Path) -> None:
    state_path(feature_active).unlink(missing_ok=True)


# ---- High-level helpers for the orchestrator ------------------------


def record_iter(
    state: RalphState, *, statuses: dict[str, str],
) -> tuple[RalphState, list[RegressionEvent]]:
    """Append one iter's result to the state; return new regression events.

    Caller provides the full {scope_id: status} map from the latest
    review.md. Returns (updated_state, new_regressions_this_iter)."""
    state.iter += 1
    state.last_iter_at = datetime.now(timezone.utc).isoformat()

    prev_statuses = state.statuses_history[-1] if state.statuses_history else {}
    new_regs: list[RegressionEvent] = []
    for sid, prev_st in prev_statuses.items():
        if prev_st != STATUS_FULLY:
            continue
        new_st = statuses.get(sid, STATUS_MISSING)
        if _STATUS_RANK.get(new_st, 0) < _STATUS_RANK[STATUS_FULLY]:
            ev = RegressionEvent(
                scope_id=sid, iter_index=state.iter,
                prior_status=prev_st, new_status=new_st,
            )
            new_regs.append(ev)
            state.regressions.append(ev)

    state.statuses_history.append(dict(statuses))
    state.fully_history.append(fully_set(statuses))
    return state, new_regs


def is_complete(state: RalphState, active_ids: set[str]) -> bool:
    """True iff the most recent Fully set covers all active scope IDs."""
    if not state.fully_history:
        return False
    return active_ids.issubset(state.fully_history[-1])


def validate_active_review_coverage(
    statuses: dict[str, str], active_ids: set[str],
) -> None:
    """Require exactly one accepted classification for every active item."""
    missing = sorted(active_ids - set(statuses))
    if missing:
        raise SchemaError(
            "loop review missing active scope classifications: "
            + ", ".join(missing)
        )


def build_stall_message(
    state: RalphState, active_ids: set[str],
) -> str:
    """Human-readable halt message at stall time."""
    fully = state.fully_history[-1] if state.fully_history else set()
    incomplete = sorted(active_ids - fully)
    fully_sorted = sorted(fully & active_ids)
    regs_recent = [r for r in state.regressions
                   if r.iter_index > state.iter - K_STALL]
    reg_str = ""
    if regs_recent:
        reg_str = (
            "\n  Regression events (recent): " +
            ", ".join(
                f"{r.scope_id} (iter {r.iter_index}: {r.prior_status}→{r.new_status})"
                for r in regs_recent
            )
        )
    return (
        f"ralph stalled: Fully set unchanged (or shrank) for {K_STALL} "
        f"iterations (iter {state.iter - K_STALL + 1}→{state.iter}).\n"
        f"  Completed ({len(fully_sorted)}/{len(active_ids)}): "
        f"{', '.join(fully_sorted) or '(none)'}\n"
        f"  Incomplete: {', '.join(incomplete) or '(none)'}"
        f"{reg_str}"
    )
