"""Synthetic skip verdicts cite the latest applicable override, not the first."""

from copy import deepcopy
import json

import pytest

from autodev.artifacts.overrides import OverrideRecord, Overrides
from autodev.orchestrator import Orchestrator


def record(reason, *, gate="design-review", cycle=2, active=True, kind="skip_gate", ts="2026-01-01T00:00:00Z"):
    return OverrideRecord(kind=kind, reason=reason, who=reason + "-author", ts=ts,
                          skipped_in_cycle=cycle, gate=gate, active=active)


@pytest.mark.parametrize("gate", ["design-review", "trace-review", "close-approval"])
@pytest.mark.parametrize("latest_ts", ["2026-01-01T00:00:00Z", "2025-12-31T23:59:59Z"])
def test_skip_verdict_uses_latest_applicable_append_and_keeps_history(tmp_path, gate, latest_ts):
    active = tmp_path / "active"
    active.mkdir()
    (active / "design-packet.json").write_text("{}\n")
    (active / "implemented-spec.md").write_text("# Synthetic spec\n")
    overrides = Overrides(current_cycle=2, records=[
        record("first", gate=gate),
        record("latest", gate=gate, ts=latest_ts),
        record("inactive", gate=gate, active=False),
        record("other-gate", gate="unrelated-gate"),
        record("old-cycle", gate=gate, cycle=1),
        record("dirty-ack", gate=gate, kind="dirty_workspace"),
    ])
    before = deepcopy(overrides.to_dict())
    runner = Orchestrator.__new__(Orchestrator)
    runner._write_skip_verdict("synthetic", active, gate, overrides)
    paths = [active / f"panel-{gate}.json"]
    if gate == "design-review":
        paths.append(active / "panel-trace-review.json")
    for path in paths:
        verdict = json.loads(path.read_text())
        assert verdict["verdict"] == "skipped"
        assert verdict["skip_reason"] == "latest"
        assert verdict["skip_who"] == "latest-author"
    assert overrides.to_dict() == before


def test_skip_verdict_without_applicable_record_keeps_empty_provenance(tmp_path):
    (tmp_path / "implemented-spec.md").write_text("# Synthetic spec\n")
    overrides = Overrides(current_cycle=2, records=[
        record("inactive", gate="close-approval", active=False),
        record("old-cycle", gate="close-approval", cycle=1),
        record("other-gate"),
    ])
    runner = Orchestrator.__new__(Orchestrator)
    runner._write_skip_verdict("synthetic", tmp_path, "close-approval", overrides)
    verdict = json.loads((tmp_path / "panel-close-approval.json").read_text())
    assert verdict.get("skip_reason", "") == ""
    assert verdict.get("skip_who", "") == ""
