"""revision-state.json — per-gate L counters + pending feedback.

Shape::

    {
      "L": {                         # per-gate local counter, both gates
        "design-review": <int>,
        "close-approval": <int>
      },
      "prd_target_streak": {         # consecutive blocking PRD-target rounds
        "design-review": <int>,
        "close-approval": <int>
      },
      "pending_feedback": {          # stage → paths list (panel verdicts or build.json)
        "design": ["panel-design-review.json"]
      },
      "manual_rerun_credits": {      # one-shot human-authorized L_MAX extensions
        "design-review": <0|1>,
        "close-approval": <0|1>
      },
      "manual_rerun_grants": [       # audit trail; consumed grants remain visible
        {"gate": "design-review", "reason": "...", "who": "...",
         "granted_at": "...", "consumed_at": null}
      ]
    }

Invariants:
  - 0 ≤ L[gate] ≤ L_MAX (==10) for each gate
  - At most one unconsumed manual rerun credit exists per gate
  - File absent ≡ all zeros + empty feedback
  - Atomic writes only

Budget semantics:
  - Each blocking panel verdict whose dispatch picks a producer rerun
    bumps L[gate] by 1 and reruns the panel afterwards.
  - When L[gate] == L_MAX and the next panel run returns a blocking
    verdict, the orchestrator halts for human decision.
  - Total panel runs per gate per cycle: 1 initial + up to L_MAX
    reruns = L_MAX + 1.
  - Upstream diagnostic routing (route_to_layer) consumes the same
    L[gate] budget as a panel verdict rerun.
  - Design-review PRD targets get one automatic design rerun first; the
    second consecutive PRD-targeted design-review halt does not bump L.
  - Other halt conditions (arch-doc target, mixed producers,
    indeterminate+multi-producer, pre-check failure) do NOT bump L.
  - At L_MAX, an explicit human ``grant-rerun`` may authorize one more
    producer rerun without passing the gate or changing the PRD. The next
    blocking verdict halts again unless another human grant is made.
  - L and unconsumed credits reset on ``autodev update --amendment``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from autodev.errors import SchemaError
from autodev.state.atomic import atomic_write_json

# v3-core R4: uniform per-gate cap across gates.
L_MAX = 10

# PRD-targeted design-review findings first route back to design so the
# design agent can avoid an apparent PRD conflict. The second consecutive
# PRD-targeted blocking design-review halts for human.
PRD_TARGET_HALT_STREAK = 2

# All panel gates tracked by L[*].
ALL_PANEL_GATES: tuple[str, ...] = (
    "design-review", "close-approval",
)

# Producer stage for each gate's fallback path (when reviewer findings
# don't name a specific primary_pair artifact).
#
# close-approval has NO fallback: if a reviewer can't pin a finding to
# a specific upstream artifact, the harness should not guess a producer
# (previously fell back to spec, which is the wrong layer for shipped-
# behavior gaps). Indeterminate close-approval blocking findings halt
# for human decision.
GATE_FALLBACK_PRODUCER: dict[str, str] = {
    "design-review": "design",
}

# Filename → producer stage. Used to dispatch reruns based on the
# filename-qualified targets on blocking findings (v3-core R4).
FILENAME_TO_PRODUCER: dict[str, str] = {
    "design.md": "design",
    "scope.json": "design",
    "trace.md": "design",
    "test-plan.md": "design",
    "implemented-spec.md": "spec",
}
# prd.md and discovered architecture docs are NOT in this map: blocking
# findings that target them halt for human decision (not agent-rerunnable).


# Gate-specific producer maps override the global table when a filename
# has a different meaning inside that gate.
GATE_FILENAME_TO_PRODUCER: dict[str, dict[str, str]] = {
    "design-review": {
        "design.md": "design",
        "scope.json": "design",
        "trace.md": "design",
        "test-plan.md": "design",
    },
    # close-approval can route findings back to any upstream producer.
    # It deliberately omits implemented-spec.md (the spec stage just
    # describes shipped behavior — a finding about wrong shipped
    # behavior should rerun the producer that owns the behavior, not
    # ask spec to redescribe it). PRD targets halt for human; arch-doc
    # targets halt for human (handled in dispatch).
    "close-approval": {
        "design.md": "design",
        "scope.json": "design",
        "trace.md": "design",
        "test-plan.md": "design",
        "build.json": "build",
    },
}


def filename_to_producer(gate: str, filename: str) -> str | None:
    """Return the producer stage for a filename within ``gate``, or None
    if the filename is not a harness-rerunnable artifact in this gate
    (external arch-doc, prd.md, etc.). Gate-specific overrides win over
    the global table."""
    gate_map = GATE_FILENAME_TO_PRODUCER.get(gate)
    if gate_map is not None:
        return gate_map.get(filename)
    return FILENAME_TO_PRODUCER.get(filename)
# g-24 — map dev-named routable "layer" onto the gate-keyed L index.
_LAYER_TO_GATE: dict[str, str] = {
    "design": "design-review",
}


def gate_for_layer(layer: str) -> str:
    """Return the gate-keyed L index for a dev-named routable layer."""
    return _LAYER_TO_GATE[layer]


def producer_stage_for_layer(layer: str) -> str:
    """Return the stage that produces the given layer's primary artifact."""
    if layer == "design":
        return "design"
    raise KeyError(f"layer {layer!r} has no producer stage")


@dataclass
class RevisionState:
    L: dict[str, int] = field(default_factory=lambda: {g: 0 for g in ALL_PANEL_GATES})
    pending_feedback: dict[str, list[str]] = field(default_factory=dict)
    prd_target_streak: dict[str, int] = field(
        default_factory=lambda: {g: 0 for g in ALL_PANEL_GATES}
    )
    manual_rerun_credits: dict[str, int] = field(
        default_factory=lambda: {g: 0 for g in ALL_PANEL_GATES}
    )
    manual_rerun_grants: list[dict[str, str | None]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "L": {g: self.L.get(g, 0) for g in ALL_PANEL_GATES},
            "prd_target_streak": {
                g: self.prd_target_streak.get(g, 0) for g in ALL_PANEL_GATES
            },
            "pending_feedback": dict(self.pending_feedback),
            "manual_rerun_credits": {
                g: self.manual_rerun_credits.get(g, 0)
                for g in ALL_PANEL_GATES
            },
            "manual_rerun_grants": [dict(record) for record in self.manual_rerun_grants],
        }

    def remaining_local(self, gate: str) -> int:
        return max(0, L_MAX - self.L.get(gate, 0))


def _validate(raw: dict) -> None:
    if not isinstance(raw, dict):
        raise SchemaError("revision-state root must be object")
    L = raw.get("L", {})
    if not isinstance(L, dict):
        raise SchemaError("revision-state.L must be object")
    for gate, n in L.items():
        if not isinstance(n, int) or n < 0 or n > L_MAX:
            raise SchemaError(f"revision-state.L[{gate}] out of range 0..{L_MAX}")
    pts = raw.get("prd_target_streak", {})
    if not isinstance(pts, dict):
        raise SchemaError("revision-state.prd_target_streak must be object")
    for gate, n in pts.items():
        if not isinstance(n, int) or n < 0 or n > PRD_TARGET_HALT_STREAK:
            raise SchemaError(
                "revision-state.prd_target_streak"
                f"[{gate}] out of range 0..{PRD_TARGET_HALT_STREAK}"
            )
    pf = raw.get("pending_feedback", {})
    if not isinstance(pf, dict):
        raise SchemaError("revision-state.pending_feedback must be object")
    for stage, paths in pf.items():
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            raise SchemaError(
                f"revision-state.pending_feedback[{stage}] must be list[str]"
            )
    credits = raw.get("manual_rerun_credits", {})
    if not isinstance(credits, dict):
        raise SchemaError("revision-state.manual_rerun_credits must be object")
    for gate, n in credits.items():
        if gate not in ALL_PANEL_GATES or not isinstance(n, int) or n not in (0, 1):
            raise SchemaError(
                f"revision-state.manual_rerun_credits[{gate}] must be 0 or 1"
            )
    grants = raw.get("manual_rerun_grants", [])
    if not isinstance(grants, list):
        raise SchemaError("revision-state.manual_rerun_grants must be list")
    for record in grants:
        if not isinstance(record, dict):
            raise SchemaError("revision-state manual rerun grant must be object")
        if record.get("gate") not in ALL_PANEL_GATES:
            raise SchemaError("revision-state manual rerun grant has invalid gate")
        for key in ("reason", "who", "granted_at"):
            if not isinstance(record.get(key), str) or not record[key].strip():
                raise SchemaError(
                    f"revision-state manual rerun grant requires non-empty {key}"
                )
        if record.get("consumed_at") is not None and not isinstance(
            record.get("consumed_at"), str
        ):
            raise SchemaError(
                "revision-state manual rerun grant consumed_at must be string or null"
            )
    for gate in ALL_PANEL_GATES:
        pending = sum(
            1 for record in grants
            if record.get("gate") == gate and record.get("consumed_at") is None
        )
        if pending > 1 or pending != credits.get(gate, 0):
            raise SchemaError(
                f"revision-state manual rerun credit/audit mismatch for {gate}"
            )


def state_path(feature_active: Path) -> Path:
    return Path(feature_active) / "revision-state.json"


def load_state(feature_active: Path) -> RevisionState:
    p = state_path(feature_active)
    if not p.exists():
        return RevisionState()
    raw = json.loads(p.read_text(encoding="utf-8"))
    _validate(raw)
    L = {g: 0 for g in ALL_PANEL_GATES}
    for gate, n in raw.get("L", {}).items():
        if gate in ALL_PANEL_GATES:
            L[gate] = n
    prd_target_streak = {g: 0 for g in ALL_PANEL_GATES}
    for gate, n in raw.get("prd_target_streak", {}).items():
        if gate in ALL_PANEL_GATES:
            prd_target_streak[gate] = n
    manual_rerun_credits = {g: 0 for g in ALL_PANEL_GATES}
    for gate, n in raw.get("manual_rerun_credits", {}).items():
        if gate in ALL_PANEL_GATES:
            manual_rerun_credits[gate] = n
    return RevisionState(
        L=L,
        prd_target_streak=prd_target_streak,
        pending_feedback={
            k: list(v) for k, v in raw.get("pending_feedback", {}).items()
        },
        manual_rerun_credits=manual_rerun_credits,
        manual_rerun_grants=[
            dict(record) for record in raw.get("manual_rerun_grants", [])
        ],
    )


def write_state(feature_active: Path, s: RevisionState) -> None:
    d = s.to_dict()
    _validate(d)
    atomic_write_json(state_path(feature_active), d)


def clear_state(feature_active: Path) -> None:
    state_path(feature_active).unlink(missing_ok=True)


def reset_on_amendment(feature_active: Path) -> RevisionState:
    """v3-core R4: `autodev update --amendment` resets L[*] and
    pending_feedback."""
    s = load_state(feature_active)
    s.L = {g: 0 for g in ALL_PANEL_GATES}
    s.prd_target_streak = {g: 0 for g in ALL_PANEL_GATES}
    s.pending_feedback = {}
    s.manual_rerun_credits = {g: 0 for g in ALL_PANEL_GATES}
    s.manual_rerun_grants = []
    write_state(feature_active, s)
    return s


def grant_manual_rerun(
    feature_active: Path, gate: str, *, reason: str, who: str,
) -> RevisionState:
    """Grant one auditable rerun beyond L_MAX without passing the gate."""
    if gate not in ALL_PANEL_GATES:
        raise ValueError(f"unknown panel gate {gate!r}")
    if not reason.strip() or not who.strip():
        raise ValueError("manual rerun grant requires non-empty reason and who")
    s = load_state(feature_active)
    if s.L.get(gate, 0) < L_MAX:
        raise ValueError(
            f"L[{gate}]={s.L.get(gate, 0)} has not reached L_MAX={L_MAX}"
        )
    if s.manual_rerun_credits.get(gate, 0):
        raise ValueError(f"manual rerun credit for {gate} is already pending")
    s.manual_rerun_credits[gate] = 1
    s.manual_rerun_grants.append({
        "gate": gate,
        "reason": reason.strip(),
        "who": who.strip(),
        "granted_at": datetime.now(timezone.utc).isoformat(),
        "consumed_at": None,
    })
    write_state(feature_active, s)
    return s


def consume_manual_rerun_credit(s: RevisionState, gate: str) -> bool:
    """Consume one in-memory credit; caller persists the surrounding state."""
    if not s.manual_rerun_credits.get(gate, 0):
        return False
    for record in s.manual_rerun_grants:
        if record.get("gate") == gate and record.get("consumed_at") is None:
            record["consumed_at"] = datetime.now(timezone.utc).isoformat()
            s.manual_rerun_credits[gate] = 0
            return True
    raise SchemaError(
        f"manual rerun credit for {gate} has no matching unconsumed audit record"
    )


def reset_prd_target_streak(feature_active: Path, gate: str) -> RevisionState:
    """Clear a gate's PRD-target streak after a non-PRD blocking round
    or a clean panel pass."""
    s = load_state(feature_active)
    if gate in ALL_PANEL_GATES and s.prd_target_streak.get(gate, 0):
        s.prd_target_streak[gate] = 0
        write_state(feature_active, s)
    return s
