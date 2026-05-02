"""v3-core R1 — PanelFinding.targets schema extension."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodev.artifacts.verdict import (
    DroppedFinding, PanelFinding, PanelVerdict, load_verdict, write_verdict,
)
from autodev.panel.schemas import synthesizer_output_schema


def _base_verdict(tmp_path: Path, findings) -> Path:
    v = PanelVerdict(
        gate="design-review", verdict="needs_revision",
        findings=findings,
        source="src", source_hash="sha256:" + "0" * 64,
        prompt_file="p", prompt_hash="sha256:" + "0" * 64,
        harness_version="t", run_ts="2026-04-20T00:00:00Z",
    )
    p = tmp_path / "panel.json"
    write_verdict(p, v)
    return p


def test_panel_finding_has_targets_field():
    f = PanelFinding(severity="risk", vendor="claude", summary="x")
    assert hasattr(f, "targets")
    assert f.targets == []


def test_panel_finding_targets_round_trip(tmp_path):
    f = PanelFinding(
        severity="invariant_violation", vendor="claude", summary="x",
        targets=["primary_pair.prd.md", "anchor.scope.json"],
    )
    p = _base_verdict(tmp_path, [f])
    loaded = load_verdict(p)
    assert loaded.findings[0].targets == [
        "primary_pair.prd.md", "anchor.scope.json",
    ]


def test_panel_finding_omits_empty_targets_in_json(tmp_path):
    f = PanelFinding(severity="opinion", vendor="claude", summary="x")
    p = _base_verdict(tmp_path, [f])
    raw = json.loads(p.read_text())
    # Empty targets list omitted to keep JSON tidy.
    assert "targets" not in raw["findings"][0]


def test_finding_without_targets_loads_with_empty_list(tmp_path):
    """A finding without an explicit targets field loads with empty
    list (anchor-filter treats empty as kept, per conservative default)."""
    raw = {
        "gate": "design-review", "verdict": "needs_revision",
        "findings": [{
            "severity": "risk", "vendor": "claude", "summary": "no-targets finding",
        }],
        "source": "src", "source_hash": "sha256:" + "0" * 64,
        "prompt_file": "p", "prompt_hash": "sha256:" + "0" * 64,
        "harness_version": "t", "run_ts": "2026-04-20T00:00:00Z",
    }
    p = tmp_path / "panel.json"
    p.write_text(json.dumps(raw))
    loaded = load_verdict(p)
    assert loaded.findings[0].targets == []


def test_dropped_finding_dataclass_has_drop_reason():
    df = DroppedFinding(
        severity="risk", vendor="gemini", summary="x",
        targets=["anchor.prd.md"], drop_reason="all-anchor-targets",
    )
    d = df.to_dict()
    assert d["drop_reason"] == "all-anchor-targets"
    assert d["targets"] == ["anchor.prd.md"]


def test_panel_verdict_has_dropped_findings_field():
    v = PanelVerdict(
        gate="design-review", verdict="pass", findings=[],
        source="x", source_hash="sha256:" + "0" * 64,
        prompt_file="p", prompt_hash="sha256:" + "0" * 64,
        harness_version="t", run_ts="2026-04-20T00:00:00Z",
    )
    assert v.dropped_findings == []


def test_dropped_findings_round_trip(tmp_path):
    df = DroppedFinding(
        severity="opinion", vendor="codex", summary="phrasing",
        targets=["anchor.prd.md"], drop_reason="all-anchor-targets",
    )
    v = PanelVerdict(
        gate="design-review", verdict="pass", findings=[],
        source="x", source_hash="sha256:" + "0" * 64,
        prompt_file="p", prompt_hash="sha256:" + "0" * 64,
        harness_version="t", run_ts="2026-04-20T00:00:00Z",
        dropped_findings=[df],
    )
    p = tmp_path / "panel.json"
    write_verdict(p, v)
    loaded = load_verdict(p)
    assert len(loaded.dropped_findings) == 1
    assert loaded.dropped_findings[0].drop_reason == "all-anchor-targets"


def test_synthesizer_schema_accepts_targets_field():
    schema = synthesizer_output_schema()
    finding_schema = schema["properties"]["per_reviewer"]["items"] \
        ["properties"]["findings"]["items"]
    assert "targets" in finding_schema["properties"]
    targets_schema = finding_schema["properties"]["targets"]
    assert targets_schema["type"] == "array"
    assert targets_schema["items"]["type"] == "string"


def test_synthesizer_schema_targets_is_optional():
    schema = synthesizer_output_schema()
    entry = schema["properties"]["per_reviewer"]["items"]
    # targets is allowed but not required — synthesizer copies what
    # reviewers emit; reviewers may legitimately emit no targets.
    finding_schema = entry["properties"]["findings"]["items"]
    assert "targets" not in finding_schema["required"]
