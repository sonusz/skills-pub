"""v2-13: override API (skip-gate + acknowledge-dirty + cycle semantics)."""
from __future__ import annotations

import os

import pytest

from autodev import overrides_api as ov


def test_resolve_who_explicit_wins(monkeypatch):
    monkeypatch.setenv("AUTODEV_WHO", "env-user")
    monkeypatch.setenv("USER", "shell-user")
    assert ov.resolve_who("explicit-user") == "explicit-user"


def test_resolve_who_env_over_user(monkeypatch):
    monkeypatch.setenv("AUTODEV_WHO", "env-user")
    monkeypatch.setenv("USER", "shell-user")
    assert ov.resolve_who(None) == "env-user"


def test_resolve_who_user_fallback(monkeypatch):
    monkeypatch.delenv("AUTODEV_WHO", raising=False)
    monkeypatch.setenv("USER", "shell-user")
    assert ov.resolve_who(None) == "shell-user"


def test_resolve_who_unknown(monkeypatch):
    monkeypatch.delenv("AUTODEV_WHO", raising=False)
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("USERNAME", raising=False)
    assert ov.resolve_who(None) == "unknown"


def test_record_skip_gate_persists(feature_active):
    ov.record_skip_gate(feature_active, gate="design-review", reason="toy", who="dev")
    loaded = ov.load(feature_active)
    assert loaded.has_active_skip_gate("design-review")


def test_record_skip_gate_empty_reason_rejected(feature_active):
    with pytest.raises(ValueError):
        ov.record_skip_gate(feature_active, gate="design-review", reason="   ", who="dev")


def test_record_acknowledge_dirty(feature_active):
    ov.record_acknowledge_dirty(feature_active, reason="in-progress", who="dev")
    loaded = ov.load(feature_active)
    assert loaded.has_active_dirty_ack()


def test_advance_cycle_deactivates_active(feature_active):
    ov.record_skip_gate(feature_active, gate="design-review", reason="r", who="dev")
    ov.advance_cycle(feature_active)
    loaded = ov.load(feature_active)
    assert loaded.current_cycle == 2
    assert not loaded.has_active_skip_gate("design-review")
    assert len(loaded.historical_records()) == 1


def test_invalidate_clears_same_cycle_skip_gate(feature_active):
    ov.record_skip_gate(feature_active, gate="design-review", reason="r", who="dev")
    ov.record_acknowledge_dirty(feature_active, reason="r", who="dev")
    ov.invalidate_clears_skip_gate(feature_active, "design-review")
    loaded = ov.load(feature_active)
    # skip-gate cleared
    assert not loaded.has_active_skip_gate("design-review")
    # dirty-ack preserved (R4d / R2a — different axes)
    assert loaded.has_active_dirty_ack()
