"""Stable logical session keys for stateful pipeline agents.

Provider-specific conversation ids and persistence live in shared/vendors.
The harness supplies only opaque, role-scoped keys so no two features, stages,
gates, or reviewer slots can accidentally share model history.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


SESSION_KEY_VERSION = "v1"
DESIGN_SESSION_MAX_TURNS = 15
DEFAULT_SESSION_MAX_TURNS = 5


def session_max_turns_for_role(role: str) -> int:
    """Return the automatic native-session rotation limit for one agent role."""

    return DESIGN_SESSION_MAX_TURNS if role.strip() == "design" else DEFAULT_SESSION_MAX_TURNS


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
    return feature_session_key(feature_active, role)
