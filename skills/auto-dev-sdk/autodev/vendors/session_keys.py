"""Stable logical session keys for stateful pipeline agents.

Provider-specific conversation ids and persistence live in shared/vendors.
The harness supplies only opaque, role-scoped keys so no two features, stages,
gates, or reviewer slots can accidentally share model history.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


SESSION_KEY_VERSION = "v1"
DESIGN_SESSION_MAX_TURNS = 15
RALPH_REVIEW_SESSION_MAX_TURNS = 5
DEFAULT_SESSION_MAX_TURNS = 5


def _review_phase(feature_active: Path) -> tuple[int, str | None]:
    """Return the current design-review type epoch and type.

    Consecutive rounds of one type share an epoch.  A coverage/budget
    transition increments it.  Duplicate dispatch events for one logical
    round do not create a false transition.
    """

    log_path = feature_active / "log.jsonl"
    epoch = 0
    current_type: str | None = None
    seen: set[tuple[str, str]] = set()
    try:
        with log_path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if row.get("event") != "panel-review-round":
                    continue
                detail = row.get("detail")
                if not isinstance(detail, dict) or detail.get("gate") != "design-review":
                    continue
                round_key = detail.get("round_key")
                round_type = detail.get("round_type")
                if (
                    not isinstance(round_key, str) or not round_key
                    or round_type not in {"coverage", "budget"}
                ):
                    continue
                identity = (round_key, round_type)
                if identity in seen:
                    continue
                seen.add(identity)
                if current_type is None:
                    current_type = round_type
                elif round_type != current_type:
                    epoch += 1
                    current_type = round_type
    except OSError:
        pass
    return epoch, current_type


def _with_review_phase(feature_active: Path, role: str) -> str:
    """Give a role a fresh logical session after each review-type switch."""

    epoch, round_type = _review_phase(feature_active)
    if epoch == 0 or round_type is None:
        return role
    return f"{role}:review-phase-{epoch}:{round_type}"


def session_max_turns_for_role(role: str) -> int:
    """Return the automatic native-session rotation limit for one agent role."""

    normalized_role = role.strip()
    if normalized_role == "design":
        return DESIGN_SESSION_MAX_TURNS
    if normalized_role == "ralph-review":
        return RALPH_REVIEW_SESSION_MAX_TURNS
    return DEFAULT_SESSION_MAX_TURNS


def feature_session_key(feature_active: Path, role: str) -> str:
    """Return a stable key scoped to one repo, feature, and agent role."""

    active = feature_active.resolve()
    # .../<repo>/docs/features/<feature>/active
    feature = active.parent.name
    try:
        repo_root = active.parents[3]
    except IndexError:  # defensive for isolated unit-test paths
        repo_root = active.parent
    repo_hash = hashlib.sha256(str(repo_root).encode("utf-8")).hexdigest()[:24]
    normalized_role = role.strip().replace("\n", " ")
    if not normalized_role:
        raise ValueError("session role must not be empty")
    if normalized_role == "design":
        normalized_role = _with_review_phase(active, normalized_role)
    return f"autodev:{SESSION_KEY_VERSION}:{repo_hash}:{feature}:{normalized_role}"


def reviewer_session_key(
    feature_active: Path,
    *,
    gate: str,
    slot: int,
    configured_vendor: str,
    configured_model: str,
) -> str:
    """Return an isolated key for one configured panel reviewer slot."""

    role = (
        f"panel-reviewer:{gate}:slot-{slot}:"
        f"{configured_vendor.strip().lower()}:{configured_model.strip()}"
    )
    if gate == "design-review":
        role = _with_review_phase(feature_active.resolve(), role)
    return feature_session_key(feature_active, role)
