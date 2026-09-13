"""v3-core R1 — PanelFinding.targets schema extension."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import validate

from autodev.artifacts.verdict import (
    DroppedFinding, IssueCluster, PanelFinding, PanelVerdict, load_verdict,
    write_verdict,
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


def test_priority_cluster_and_release_threshold_round_trip(tmp_path):
    finding = PanelFinding(
        severity="risk", priority="P0", finding_id="claude:1",
        vendor="claude", summary="core path broken",
    )
    verdict = PanelVerdict(
        gate="design-review", verdict="needs_revision", findings=[finding],
        source="src", source_hash="sha256:" + "0" * 64,
        prompt_file="p", prompt_hash="sha256:" + "0" * 64,
        harness_version="t", run_ts="t", release_threshold="P0",
        issue_clusters=[IssueCluster(
            cluster_id="issue-core", finding_ids=["claude:1"],
            summary="core path broken", priority="P0",
        )],
    )
    path = tmp_path / "panel.json"
    write_verdict(path, verdict)
    loaded = load_verdict(path)
    assert loaded.release_threshold == "P0"
    assert loaded.findings[0].priority == "P0"
    assert loaded.findings[0].finding_id == "claude:1"
    assert loaded.issue_clusters[0].cluster_id == "issue-core"
    assert loaded.effectively_blocks()


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
        severity="risk", vendor="agy", summary="x",
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


def test_synthesizer_schema_requires_targets_with_empty_list_default():
    schema = synthesizer_output_schema()
    entry = schema["properties"]["per_reviewer"]["items"]
    # Strict structured output requires every property.  A reviewer with no
    # target is represented by the prompt-mandated empty list.
    finding_schema = entry["properties"]["findings"]["items"]
    assert "targets" in finding_schema["required"]


def test_redundant_category_survives_schema_and_verdict_round_trip(tmp_path):
    from autodev.panel.runner import _finding_from_synth

    schema = synthesizer_output_schema()
    finding_schema = schema["properties"]["per_reviewer"]["items"] \
        ["properties"]["findings"]["items"]
    synthesized = {
        "severity": "risk",
        "priority": "P1",
        "finding_id": "claude:1",
        "summary": "duplicate adapter",
        "targets": ["primary_pair.build.json"],
        "category": "redundant",
        "evidence_refs": ["prd:R1", "code:src/x.py:12"],
        "failure_class": None,
        "missized_direction": None,
    }
    validate(synthesized, finding_schema)
    finding = _finding_from_synth("claude", synthesized)
    path = _base_verdict(tmp_path, [finding])
    loaded = load_verdict(path)
    assert loaded.findings[0].category == "redundant"
    assert loaded.findings[0].evidence_refs == [
        "prd:R1", "code:src/x.py:12",
    ]
    assert loaded.findings[0].targets == ["primary_pair.build.json"]
