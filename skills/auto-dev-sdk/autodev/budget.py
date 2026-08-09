"""Feature budget accounting — metering, prompt injection, budget police.

Three responsibilities, all feature-scoped (one active/ dir):

1. **Metering** (`compute_spent`): aggregate what the feature has already
   cost from `log.jsonl` — vendor subprocess hours by stage, ralph
   iterations, design-review panel rounds, wall span. Computed on demand;
   no derived state file is written.
2. **Targets + injection** (`format_budget_lines`): optional operator-set
   ceilings live in `budget.json` (`{"targets": {...}}`, hand-edited).
   The formatted account is appended to stage prompts so every agent
   sizes its work against the feature's remaining budget, not only its
   own context window. Injection must never break prompt rendering: any
   error degrades to no lines.
3. **Budget police** (`select_budget_police` / `police_banner`): one
   reviewer per design-review round, chosen by fixed-order rotation over
   the configured vendors, is REPURPOSED for that round: instead of the
   standard coverage review, its sole task is challenging scope items
   whose cost is disproportionate to the PRD clause they serve. A
   dedicated seat, not a side duty — a gap-finder auditing cost "on the
   side" spends its context on coverage and produces token deletions.
   Rotation state persists in `budget-police.json`. Selection is keyed
   by the review-round key (design-packet hash) so a resumed round
   re-selects the same vendor; the pointer only advances when a new
   round key appears. Vendors currently quota-skipped are passed over;
   fairness is restored on the next natural cycle (no owed-turn debt —
   simpler, and fair among the vendors actually present).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from autodev.state.atomic import atomic_write_json

BUDGET_TARGETS_FILENAME = "budget.json"
POLICE_STATE_FILENAME = "budget-police.json"

# Targets an operator may set in budget.json. Anything else is ignored.
_KNOWN_TARGETS = (
    "vendor_hours", "iterations", "design_review_rounds", "wall_hours",
)


@dataclass
class BudgetSpent:
    vendor_hours_by_stage: dict[str, float] = field(default_factory=dict)
    iterations: int = 0
    design_review_rounds: int = 0
    wall_hours: float = 0.0

    @property
    def vendor_hours_total(self) -> float:
        return sum(self.vendor_hours_by_stage.values())


def _parse_ts(value: str) -> datetime | None:
    try:
        ts = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    # Normalize: a naive row (hand-edited, or an older tool logging local
    # time without tzinfo) must not make aware/naive comparison raise and
    # silently erase the whole account downstream. Assume UTC.
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _safe_row(line: str) -> dict | None:
    try:
        row = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return row if isinstance(row, dict) else None


def compute_spent(feature_active: Path) -> BudgetSpent:
    """Aggregate spend from the feature's log.jsonl. Missing log → zeros."""
    spent = BudgetSpent()
    log_path = Path(feature_active) / "log.jsonl"
    if not log_path.exists():
        return spent
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    review_round_keys: set[str] = set()
    def _consume(row: dict) -> None:
        nonlocal first_ts, last_ts
        ts = _parse_ts(row.get("ts", ""))
        if ts is not None:
            # min/max, not first/last: merged or hand-edited logs may be
            # out of file order and must not understate the span.
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
        event = row.get("event")
        detail = row.get("detail") or {}
        if not isinstance(detail, dict):
            detail = {}
        if event == "subprocess-end":
            stage = row.get("stage") or "unknown"
            elapsed = detail.get("elapsed_sec")
            if isinstance(elapsed, (int, float)) and elapsed > 0:
                spent.vendor_hours_by_stage[stage] = (
                    spent.vendor_hours_by_stage.get(stage, 0.0)
                    + float(elapsed) / 3600.0
                )
        elif event == "panel-vendor-elapsed":
            # Panel reviewer/synthesizer calls do not run through the stage
            # subprocess path; the panel runner emits this event per call.
            elapsed = detail.get("elapsed_sec")
            if isinstance(elapsed, (int, float)) and elapsed > 0:
                spent.vendor_hours_by_stage["panel"] = (
                    spent.vendor_hours_by_stage.get("panel", 0.0)
                    + float(elapsed) / 3600.0
                )
        elif event == "iteration-recorded":
            spent.iterations += 1
        elif (
            event == "panel-review-round"
            and detail.get("gate") == "design-review"
        ):
            # The panel runner emits this only when reviewer slots are
            # actually dispatched, carrying the round's packet hash.
            # Distinct keys = logical review rounds: resumed attempts of
            # one round share the key, and precheck-refused attempts
            # (which dispatch no vendor) never emit it. panel-start is
            # unsuitable (counts refusals); panel-quorum fires only on
            # quota skips.
            key = detail.get("round_key")
            if isinstance(key, str) and key:
                review_round_keys.add(key)

    try:
        # Streamed, not read_text().splitlines(): feature logs grow to
        # hundreds of KB and this runs on every prompt render.
        with log_path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                row = _safe_row(line)
                if row is not None:
                    _consume(row)
    except OSError:
        return spent
    spent.design_review_rounds = len(review_round_keys)
    if first_ts is not None and last_ts is not None and last_ts > first_ts:
        spent.wall_hours = (last_ts - first_ts).total_seconds() / 3600.0
    return spent


def load_targets(feature_active: Path) -> dict[str, float]:
    """Operator-set ceilings from budget.json; absent/invalid → {}."""
    path = Path(feature_active) / BUDGET_TARGETS_FILENAME
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    targets = raw.get("targets") if isinstance(raw, dict) else None
    if not isinstance(targets, dict):
        return {}
    out: dict[str, float] = {}
    for key in _KNOWN_TARGETS:
        value = targets.get(key)
        if isinstance(value, (int, float)) and value > 0:
            out[key] = float(value)
    return out


def _fmt_vs_target(spent_value: float, target: float | None, unit: str) -> str:
    if target is None:
        return f"{spent_value:.1f}{unit}"
    flag = " **OVER**" if spent_value > target else ""
    return f"{spent_value:.1f}{unit} of {target:.0f}{unit} target{flag}"


def format_budget_lines(feature_active: Path) -> list[str]:
    """Markdown context lines for stage-prompt injection. Errors → []."""
    try:
        spent = compute_spent(feature_active)
        targets = load_targets(feature_active)
    except Exception:
        return []
    no_spend = (
        spent.vendor_hours_total == 0
        and spent.iterations == 0
        and spent.design_review_rounds == 0
    )
    if no_spend and not targets:
        # Nothing recorded and no operator ceilings: stay silent rather
        # than injecting noise into repos that don't use budgets.
        return []
    if no_spend:
        # Operator set ceilings before the first spend: the initial design
        # prompt — the sizing decision the whole feature inherits — must
        # still see them.
        target_desc = ", ".join(
            f"{key} {value:.0f}" for key, value in sorted(targets.items())
        )
        return [
            "- BUDGET_SPENT: nothing recorded yet for this feature.",
            f"- BUDGET_TARGETS: {target_desc}",
            "- BUDGET_RULE: treat these ceilings as an input to sizing. "
            "Prefer the thinnest design that satisfies the PRD within them.",
        ]
    by_stage = ", ".join(
        f"{stage} {hours:.1f}h"
        for stage, hours in sorted(
            spent.vendor_hours_by_stage.items(), key=lambda kv: -kv[1],
        )
    )
    lines = [
        "- BUDGET_SPENT: vendor-hours "
        + _fmt_vs_target(
            spent.vendor_hours_total, targets.get("vendor_hours"), "h",
        )
        + (f" ({by_stage})" if by_stage else "")
        + "; iterations "
        + _fmt_vs_target(
            float(spent.iterations), targets.get("iterations"), "",
        )
        + "; design-review rounds "
        + _fmt_vs_target(
            float(spent.design_review_rounds),
            targets.get("design_review_rounds"), "",
        )
        + "; wall span "
        + _fmt_vs_target(spent.wall_hours, targets.get("wall_hours"), "h"),
    ]
    if not targets:
        lines.append(
            "- BUDGET_TARGETS: none set (operator may add "
            f"`{BUDGET_TARGETS_FILENAME}` with a `targets` object: "
            f"{', '.join(_KNOWN_TARGETS)})"
        )
    lines.append(
        "- BUDGET_RULE: treat the remaining budget as an input to sizing. "
        "Prefer objectives that land the deliverable path within what "
        "remains; when a target is exceeded, prefer deferral or a thinner "
        "mechanism over adding new machinery."
    )
    return lines


# ---------------------------------------------------------------------------
# Budget police rotation


def _load_police_state(feature_active: Path) -> dict:
    path = Path(feature_active) / POLICE_STATE_FILENAME
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


@dataclass
class PoliceSelection:
    vendor: str
    # The banner frozen at selection time, so every dispatch attempt of
    # the round shows the reviewer session the same budget account.
    banner: str | None
    # True when this call advanced the pointer and persisted state; False
    # when an existing round pin was returned (resume).
    newly_selected: bool


def select_budget_police(
    feature_active: Path,
    ordered_vendors: list[str],
    *,
    round_key: str,
    unavailable: set[str] | None = None,
    banner_factory: Callable[[], str] | None = None,
) -> PoliceSelection | None:
    """Pick this round's budget-police vendor by fixed-order rotation.

    Same ``round_key`` → same vendor with its frozen banner
    (resume-stable, nothing re-persisted). A new round key scans from the
    persisted pointer, skipping ``unavailable`` vendors, builds the
    banner via ``banner_factory`` BEFORE persisting (a banner failure
    must not consume a turn), then advances the pointer past the
    selected vendor. All vendors unavailable → None (nothing persisted).
    """
    if not ordered_vendors:
        return None
    unavailable = unavailable or set()
    state = _load_police_state(feature_active)
    if state.get("last_round_key") == round_key:
        last_vendor = state.get("last_vendor")
        if isinstance(last_vendor, str) and last_vendor in ordered_vendors:
            frozen = state.get("last_banner")
            return PoliceSelection(
                vendor=last_vendor,
                banner=frozen if isinstance(frozen, str) and frozen else None,
                newly_selected=False,
            )
        # The round already consumed a rotation slot but its vendor is no
        # longer configured (vendors.yml edited mid-round). Re-selecting
        # would charge a second slot to the same round — decline instead.
        return None
    pointer = state.get("pointer")
    if not isinstance(pointer, int) or not 0 <= pointer < len(ordered_vendors):
        pointer = 0
    selected: str | None = None
    selected_index = pointer
    for offset in range(len(ordered_vendors)):
        index = (pointer + offset) % len(ordered_vendors)
        vendor = ordered_vendors[index]
        if vendor in unavailable:
            continue
        selected = vendor
        selected_index = index
        break
    if selected is None:
        return None
    banner = banner_factory() if banner_factory is not None else None
    history = state.get("history")
    if not isinstance(history, list):
        history = []
    history.append({"round_key": round_key, "vendor": selected})
    atomic_write_json(Path(feature_active) / POLICE_STATE_FILENAME, {
        "pointer": (selected_index + 1) % len(ordered_vendors),
        "last_round_key": round_key,
        "last_vendor": selected,
        "last_banner": banner,
        "history": history[-50:],
    })
    return PoliceSelection(
        vendor=selected, banner=banner, newly_selected=True,
    )


def police_banner(feature_active: Path) -> str:
    """The minimality-review role body for the selected reviewer.

    This REPLACES the coverage-review body in that vendor's prompt (the
    runner keeps only the orchestrator context and file manifest from the
    shared prompt) — a dedicated seat reads no coverage instructions.
    """
    budget_lines = "\n".join(format_budget_lines(feature_active)) or (
        "- BUDGET_SPENT: no spend recorded yet for this feature."
    )
    return (
        "## Your role this round: minimality review\n\n"
        "This panel verifies that the design is a correct plan for the "
        "PRD. A plan can be wrong in two directions: insufficient (a PRD "
        "clause is satisfied by nothing in the plan) or non-minimal "
        "(something in the plan is required by no clause, or costs more "
        "than its clause needs). The other reviewers check sufficiency. "
        "You check minimality — this round you do only that.\n\n"
        "An item belongs in the plan iff (a) some PRD clause fails "
        "without it, and (b) no cheaper mechanism satisfies that clause "
        "equally. Your proof obligation mirrors the coverage reviewers': "
        "they must show a clause fails without an addition; you must "
        "show a clause still holds without an item, or holds with a "
        "cheaper one. What non-minimality costs here in practice:\n\n"
        f"{budget_lines}\n\n"
        "Go through scope.json item by item, with design.md for the "
        "mechanism and prd.md for the clauses:\n\n"
        "1. For each item you challenge, name the clause it claims to "
        "serve, then either show no clause requires it, or name the "
        "cheaper mechanism (or deferral to a follow-up feature) that "
        "satisfies the same clause, with a rough cost comparison.\n"
        "2. Report each as a finding whose summary starts with "
        "`[budget]`, targeting the scope item id. Severity `opinion`; "
        "`risk` when the item endangers the budget targets and a "
        "clause-satisfying cheaper alternative exists. No findings of "
        "any other kind.\n"
        "3. A design can already be minimal. Zero findings is then the "
        "correct report — state it explicitly. Do not manufacture "
        "cuts.\n"
    )
