"""G15: internal panel runner tests.

Validates parallel reviewer dispatch, synthesizer invocation, verdict
construction, and mechanical-fallback degradation — all via a fake
invoker shell script so no live vendor subprocess runs.
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from autodev.artifacts.verdict import (
    IssueCluster, PanelFinding, PanelVerdict, load_verdict,
    panel_verdict_transport_incomplete,
)
from autodev.artifacts.design_package_history import archive_design_package
from autodev.artifacts.fingerprint_history import record_verdict
from autodev.panel.runner import (
    FAKE_INVOKER_ENV, _compose_reviewer_prompt, _compose_synthesizer_prompt,
    _build_issue_clusters, _invoke_reviewer, _invoke_reviewer_with_retry,
    _invoke_synthesizer,
    _PanelFailFastState, ReviewerResult,
    _synthesize_and_build_verdict,
    _read_only_native_args, run_panel_gate_internal,
)
from autodev.panel.schemas import synthesizer_output_schema
from autodev.vendors.config import (
    PanelConfig, PanelReviewerSpec, PanelSynthesizerSpec,
)
from autodev.errors import GatePending, QuotaHalt, SchemaError
from autodev.vendors.shared_call import cli_name_for_vendor, normalize_shared_vendor
from autodev.vendors.quota.base import QuotaResult, now_utc
from autodev.state.process_registry import read_processes

FAKE_SCRIPT = Path(__file__).resolve().parent / "fakes" / "fake_panel_invoker.sh"


@pytest.fixture
def fake_invoker(monkeypatch):
    assert FAKE_SCRIPT.exists(), f"fake script missing: {FAKE_SCRIPT}"
    monkeypatch.setenv(FAKE_INVOKER_ENV, str(FAKE_SCRIPT))
    # v3-core R5: these tests stub synthetic single-artifact fixtures
    # and target the runner's dispatch path, not the pre-check.
    monkeypatch.setenv("AUTODEV_PANEL_SKIP_PRECHECK", "1")
    # Reviewer-failure tests must not depend on the developer machine's live
    # vendor quota. Individual quota tests replace this with a numeric result.
    monkeypatch.setattr(
        "autodev.panel.runner.get_quota_remaining",
        lambda vendor, model=None, force=False: QuotaResult.unknown(
            vendor, "test quota intentionally unknown",
        ),
    )
    yield


@pytest.fixture
def panel_config():
    return PanelConfig(
        reviewers=(
            PanelReviewerSpec(vendor="claude", model="fake-sonnet"),
            PanelReviewerSpec(vendor="agy", model="fake-agy"),
            PanelReviewerSpec(vendor="codex", model="fake-codex"),
        ),
        synthesizer=PanelSynthesizerSpec(vendor="claude", model="fake-sonnet"),
        reviewer_probe_interval_sec=10,
        synthesizer_probe_interval_sec=10,
    )


def _make_artifact(feature_active: Path) -> Path:
    p = feature_active / "prd.md"
    p.write_text("# demo PRD\n\nR1: do a thing.\n")
    return p


def test_revision_reviewer_prompt_exposes_package_git_diff(feature_active):
    for name, content in {
        "design.md": "# Design\nold\n",
        "scope.json": "{}\n",
        "trace.md": "# Trace\n",
        "test-plan.md": "# Tests\n",
        "design-changelog.json": '{"kind":"design-changelog"}\n',
    }.items():
        (feature_active / name).write_text(content)
    archive_design_package(feature_active)
    (feature_active / "design.md").write_text("# Design\nnew\n")
    archive_design_package(feature_active)
    repo_root = feature_active.parents[3]

    prompt = _compose_reviewer_prompt(
        gate="design-review",
        artifact_path=feature_active / "design.md",
        consulted_docs=[],
        feature_active=feature_active,
        repo_root=repo_root,
        continuation=True,
    )

    assert "CURRENT_DESIGN_REF: `refs/autodev/design/demo/package-002`" in prompt
    assert "PREVIOUS_DESIGN_REF: `refs/autodev/design/demo/package-001`" in prompt
    assert "DIFF_COMMAND:" in prompt
    assert "design.md" in prompt
    assert "scope.json" in prompt
    assert "Read the revision diff first" in prompt


def test_schema_is_per_reviewer_extraction():
    """Synthesizer JSON schema keeps per-reviewer extraction and may add a canonical decision."""
    schema = synthesizer_output_schema()
    assert set(schema["required"]) == {
        "per_reviewer", "issue_clusters", "decision",
    }
    entry = schema["properties"]["per_reviewer"]["items"]
    assert set(entry["required"]) == {"vendor", "verdict", "findings", "coverage"}
    assert set(entry["properties"]["verdict"]["enum"]) == {
        "pass", "needs_revision", "fail"
    }
    finding_schema = entry["properties"]["findings"]["items"]
    # Findings live INSIDE a reviewer entry — they do NOT carry
    # originating_vendors; the reviewer's own vendor label owns them.
    assert set(finding_schema["required"]) == set(finding_schema["properties"])
    assert "originating_vendors" not in finding_schema["properties"]
    assert {"finding_id", "priority"} <= set(finding_schema["required"])
    cluster_schema = schema["properties"]["issue_clusters"]["items"]
    assert set(cluster_schema["required"]) == {
        "prior_cluster_id", "finding_ids", "summary",
    }
    coverage_schema = entry["properties"]["coverage"]["items"]
    assert set(coverage_schema["required"]) == {"req_id", "status", "evidence", "notes"}
    assert set(coverage_schema["properties"]["status"]["enum"]) == {
        "satisfied", "partial", "missing", "deviated", "ambiguous",
    }
    decision = schema["properties"]["decision"]["anyOf"][0]
    assert set(decision["required"]) == {
        "node", "outcome", "blocking", "severity", "summary",
    }
    assert set(decision["properties"]["outcome"]["enum"]) == {
        "pass", "retry_design", "halt_for_human",
    }


def test_schema_meets_openai_strict_required_property_rule():
    """Every object declares all properties required; optionality is nullable."""
    schema = synthesizer_output_schema()

    def visit(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert set(node.get("required", [])) == set(node.get("properties", {}))
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)


def test_compose_reviewer_prompt_references_artifact_file(feature_active):
    artifact = _make_artifact(feature_active)
    prompt = _compose_reviewer_prompt(
        gate="design-review", artifact_path=artifact, consulted_docs=[],
    )
    assert str(artifact) in prompt
    assert "PRIMARY_ARTIFACT" in prompt
    assert "R1: do a thing" not in prompt


def test_compose_reviewer_prompt_uses_file_refs_for_consulted_docs(feature_active):
    artifact = _make_artifact(feature_active)
    doc = feature_active / "long-design.md"
    tail = "TAIL_SENTINEL_DO_NOT_TRUNCATE"
    doc.write_text("x" * 25_000 + tail, encoding="utf-8")
    prompt = _compose_reviewer_prompt(
        gate="design-review",
        artifact_path=artifact,
        consulted_docs=[{"path": str(doc)}],
    )
    assert str(doc) in prompt
    assert "size_bytes=" in prompt
    assert tail not in prompt


def test_compose_reviewer_prompt_does_not_inline_coverage_map(feature_active):
    artifact = _make_artifact(feature_active)
    coverage = feature_active / "panel-coverage-map.json"
    sentinel = "COVERAGE_ROWS_MUST_STAY_ON_DISK"
    coverage.write_text(
        json.dumps({"kind": "panel-coverage-map", "sentinel": sentinel}),
        encoding="utf-8",
    )
    prompt = _compose_reviewer_prompt(
        gate="design-review",
        artifact_path=artifact,
        consulted_docs=[{"path": str(coverage), "priority": "harness"}],
        feature_active=feature_active,
    )
    assert str(coverage) in prompt
    assert "size_bytes=" in prompt
    assert sentinel not in prompt
    assert "Harness-precomputed context" not in prompt


def test_reviewer_vendor_labels_route_through_shared_vendors():
    assert normalize_shared_vendor("Claude") == "claude"
    assert normalize_shared_vendor("agy") == "agy"
    assert normalize_shared_vendor("codex") == "openai"
    assert cli_name_for_vendor("openai") == "codex"


def test_synthesizer_uses_vendor_native_read_only_hints():
    # Reviewers now run yolo (see test_panel_yolo_integrity); the synthesizer
    # stays read-only — it only reads reviewer outputs and emits a verdict.
    assert _read_only_native_args("codex") == ("--sandbox", "read-only")
    assert _read_only_native_args("openai") == ("--sandbox", "read-only")
    assert _read_only_native_args("claude") == (
        "--allowedTools", "Read,Glob,Grep,LS",
    )
    assert _read_only_native_args("agy") == ("--mode", "plan")


def test_compose_synthesizer_prompt_lists_responding_vendors(feature_active):
    artifact = _make_artifact(feature_active)
    from autodev.panel.runner import ReviewerResult
    results = [
        ReviewerResult(vendor="claude", model="m", ok=True,
                       output="Verdict: pass", elapsed_sec=1.0),
        ReviewerResult(vendor="agy", model="m", ok=False, output="",
                       elapsed_sec=0.5, failure_detail="empty stdout"),
        ReviewerResult(vendor="codex", model="m", ok=True,
                       output="Verdict: needs_revision", elapsed_sec=2.0),
    ]
    sp = _compose_synthesizer_prompt(
        gate="design-review", artifact_path=artifact, reviewer_results=results,
    )
    assert "Reviewers who responded" in sp
    assert "claude" in sp
    assert "codex" in sp
    assert "Reviewers who did NOT respond" in sp
    assert "agy" in sp
    # Synthesize prompt body itself is included (pure extractor wording)
    assert "extractor" in sp.lower()
    assert "Pre-extracted reviewer findings" not in sp
    assert "pre-parsed" not in sp
    assert '"quality"' not in sp
    assert "### Reviewer: claude (m)\n\nVerdict: pass" in sp
    assert "### Reviewer: codex (m)\n\nVerdict: needs_revision" in sp


def test_single_reviewer_synthesizer_prompt_allows_one_response(feature_active):
    artifact = _make_artifact(feature_active)
    from autodev.panel.runner import ReviewerResult
    sp = _compose_synthesizer_prompt(
        gate="design-review",
        artifact_path=artifact,
        reviewer_results=[ReviewerResult(
            vendor="claude", model="m", ok=True,
            output="Verdict: pass", elapsed_sec=1.0,
        )],
    )
    assert "receive N independent reviews (N ≥ 1)" in sp
    assert "N ≥ 2" not in sp


def test_budget_synth_prompt_allows_missing_coverage_and_extracts_verdict(
    feature_active,
):
    artifact = _make_artifact(feature_active)
    from autodev.panel.runner import ReviewerResult
    sp = _compose_synthesizer_prompt(
        gate="design-review", artifact_path=artifact,
        reviewer_results=[ReviewerResult(
            vendor="claude", model="m", ok=True,
            output=(
                "Minimality evidence\n\n"
                "- s-1 remains required by R1.\n\n"
                "Verdict: pass"
            ),
            elapsed_sec=1.0,
        )],
        round_type="budget",
    )
    assert "**Round type**: `budget`" in sp
    assert "do not add any coverage-gap finding" in sp
    assert "Verdict: pass" in sp


def test_coverage_synth_prompt_still_requires_per_r_table(feature_active):
    artifact = _make_artifact(feature_active)
    from autodev.panel.runner import ReviewerResult
    sp = _compose_synthesizer_prompt(
        gate="design-review", artifact_path=artifact,
        reviewer_results=[ReviewerResult(
            vendor="claude", model="m", ok=True,
            output="Verdict: pass", elapsed_sec=1.0,
        )],
        round_type="coverage",
    )
    assert "**Round type**: `coverage`" in sp
    assert "design-review` **coverage** rounds" in sp
    assert "coverage gap: reviewer omitted the per-R<n> coverage table" in sp


def test_compose_synthesizer_prompt_does_not_truncate_or_inline_artifact(feature_active):
    artifact = feature_active / "design-packet.json"
    tail = "SYNTH_ARTIFACT_TAIL_SENTINEL"
    artifact.write_text("x" * 50_000 + tail, encoding="utf-8")
    from autodev.panel.runner import ReviewerResult
    sp = _compose_synthesizer_prompt(
        gate="design-review",
        artifact_path=artifact,
        reviewer_results=[
            ReviewerResult(vendor="claude", model="m", ok=True,
                           output="Verdict: pass", elapsed_sec=1.0),
            ReviewerResult(vendor="codex", model="m", ok=True,
                           output="Verdict: pass", elapsed_sec=1.0),
        ],
    )
    assert str(artifact) in sp
    assert "Artifact hash" in sp
    assert tail not in sp


def test_harness_validates_clusters_and_keeps_raw_findings(feature_active):
    findings = [
        PanelFinding(
            severity="risk", priority="P1", finding_id="claude:1",
            vendor="claude", summary="cache race",
        ),
        PanelFinding(
            severity="risk", priority="P0", finding_id="codex:1",
            vendor="codex", summary="concurrent cache corruption",
        ),
    ]
    clusters = _build_issue_clusters(
        findings,
        {"issue_clusters": [{
            "prior_cluster_id": None,
            "finding_ids": ["claude:1", "codex:1"],
            "summary": "cache concurrency defect",
        }]},
        feature_active=feature_active, gate="design-review",
    )
    assert len(findings) == 2
    assert len(clusters) == 1
    assert clusters[0].priority == "P0"


def test_harness_rejects_cluster_that_drops_a_raw_finding(feature_active):
    findings = [
        PanelFinding(
            severity="risk", finding_id="claude:1", vendor="claude",
            summary="one",
        ),
        PanelFinding(
            severity="risk", finding_id="codex:1", vendor="codex",
            summary="two",
        ),
    ]
    with pytest.raises(SchemaError, match="assign every raw finding"):
        _build_issue_clusters(
            findings,
            {"issue_clusters": [{
                "prior_cluster_id": None,
                "finding_ids": ["claude:1"], "summary": "one",
            }]},
            feature_active=feature_active, gate="design-review",
        )


def test_synth_prompt_includes_prior_cluster_catalog(feature_active):
    artifact = _make_artifact(feature_active)
    finding = PanelFinding(
        severity="risk", priority="P1", finding_id="claude:1",
        vendor="claude", summary="cache race",
    )
    record_verdict(
        feature_active, "design-review",
        PanelVerdict(
            gate="design-review", verdict="needs_revision", findings=[finding],
            source=str(artifact), source_hash="sha256:first",
            prompt_file="p", prompt_hash="sha256:p", harness_version="t",
            run_ts="t1", issue_clusters=[IssueCluster(
                cluster_id="issue-cache", finding_ids=["claude:1"],
                summary="cache concurrency defect", priority="P1",
            )],
        ),
    )
    from autodev.panel.runner import ReviewerResult
    prompt = _compose_synthesizer_prompt(
        gate="design-review", artifact_path=artifact,
        reviewer_results=[ReviewerResult(
            vendor="claude", model="m", ok=True, output="Verdict: pass",
            elapsed_sec=0.1,
        )],
        feature_active=feature_active,
    )
    assert '"cluster_id": "issue-cache"' in prompt
    assert "Reuse a `cluster_id`" in prompt


def test_p0_policy_overrides_p1_halt_but_retains_finding(
    feature_active, panel_config, monkeypatch,
):
    artifact = feature_active / "design.md"
    artifact.write_text("design", encoding="utf-8")
    (feature_active / "prd.md").write_text(
        "# PRD\n\n## Assurance\n\nDefault: strict\nRelease threshold: P0\n",
        encoding="utf-8",
    )
    payload = {
        "per_reviewer": [{
            "vendor": "claude", "verdict": "fail", "coverage": [],
            "findings": [{
                "finding_id": "claude:1", "severity": "risk",
                "priority": "P1", "summary": "deferrable concern",
                "targets": ["primary_pair.design.md"], "category": "other",
                "evidence_refs": [], "failure_class": "mainline",
                "missized_direction": None,
            }],
        }],
        "issue_clusters": [{
            "prior_cluster_id": None, "finding_ids": ["claude:1"],
            "summary": "deferrable concern",
        }],
        "decision": {
            "node": "design_review", "outcome": "halt_for_human",
            "blocking": True, "severity": "risk", "summary": "halt",
        },
    }
    monkeypatch.setattr(
        "autodev.panel.runner._invoke_synthesizer",
        lambda *args, **kwargs: (True, payload, ""),
    )
    from autodev.panel.runner import ReviewerResult
    verdict, _path, error = _synthesize_and_build_verdict(
        gate_label="design-review",
        reviewer_results=[ReviewerResult(
            vendor="claude", model="m", ok=True, output="review",
            elapsed_sec=0.1,
        )],
        primary_artifact=artifact, prompt_file_for_audit=artifact,
        consulted_docs=[], per_vendor_raw={"claude": "review"},
        panel_config=panel_config, feature_active=feature_active,
        vendor_cwd=feature_active, probe_config=None, log_emit=None,
    )
    assert error is None
    assert verdict.verdict == "pass"
    assert verdict.findings[0].priority == "P1"
    assert verdict.decision.outcome == "pass"
    assert verdict.decision_overridden_by_policy["outcome"] == "halt_for_human"
    assert not verdict.effectively_blocks()


def test_synthesizer_cannot_omit_a_responding_reviewer(
    feature_active, panel_config, monkeypatch,
):
    artifact = _make_artifact(feature_active)
    payload = {
        "per_reviewer": [{
            "vendor": "claude", "verdict": "pass", "findings": [],
            "coverage": [],
        }],
        "issue_clusters": [],
        "decision": {
            "node": "design_review", "outcome": "pass", "blocking": False,
            "severity": "opinion", "summary": "pass",
        },
    }
    monkeypatch.setattr(
        "autodev.panel.runner._invoke_synthesizer",
        lambda *args, **kwargs: (True, payload, ""),
    )
    from autodev.panel.runner import ReviewerResult
    verdict, _path, error = _synthesize_and_build_verdict(
        gate_label="design-review",
        reviewer_results=[
            ReviewerResult(
                vendor="claude", model="m", ok=True, output="pass",
                elapsed_sec=0.1,
            ),
            ReviewerResult(
                vendor="codex", model="m", ok=True, output="P0 failure",
                elapsed_sec=0.1,
            ),
        ],
        primary_artifact=artifact, prompt_file_for_audit=artifact,
        consulted_docs=[], per_vendor_raw={}, panel_config=panel_config,
        feature_active=feature_active, vendor_cwd=feature_active,
        probe_config=None, log_emit=None,
    )
    assert "exactly match responding reviewers" in error
    assert verdict.verdict == "fail"
    assert verdict.findings[0].priority == "P0"
    assert verdict.effectively_blocks()


def test_blocking_p0_finding_overrides_design_pass_decision(
    feature_active, panel_config, monkeypatch,
):
    artifact = feature_active / "design.md"
    artifact.write_text("design", encoding="utf-8")
    (feature_active / "prd.md").write_text(
        "# PRD\n\n## Assurance\n\nDefault: strict\nRelease threshold: P0\n",
        encoding="utf-8",
    )
    payload = {
        "per_reviewer": [{
            "vendor": "claude", "verdict": "pass", "coverage": [],
            "findings": [{
                "finding_id": "claude:1", "severity": "risk",
                "priority": "P0", "summary": "core path cannot run",
                "targets": ["primary_pair.design.md"], "category": "missing",
                "evidence_refs": [], "failure_class": "mainline",
                "missized_direction": None,
            }],
        }],
        "issue_clusters": [{
            "prior_cluster_id": None, "finding_ids": ["claude:1"],
            "summary": "core path cannot run",
        }],
        "decision": {
            "node": "design_review", "outcome": "pass", "blocking": False,
            "severity": "opinion", "summary": "pass",
        },
    }
    monkeypatch.setattr(
        "autodev.panel.runner._invoke_synthesizer",
        lambda *args, **kwargs: (True, payload, ""),
    )
    from autodev.panel.runner import ReviewerResult
    verdict, _path, error = _synthesize_and_build_verdict(
        gate_label="design-review",
        reviewer_results=[ReviewerResult(
            vendor="claude", model="m", ok=True, output="review",
            elapsed_sec=0.1,
        )],
        primary_artifact=artifact, prompt_file_for_audit=artifact,
        consulted_docs=[], per_vendor_raw={}, panel_config=panel_config,
        feature_active=feature_active, vendor_cwd=feature_active,
        probe_config=None, log_emit=None,
    )
    assert error is None
    assert verdict.verdict == "needs_revision"
    assert verdict.decision.outcome == "retry_design"
    assert verdict.effectively_blocks()
    assert verdict.decision_overridden_by_policy["outcome"] == "pass"


def test_reviewer_invocation_via_fake(fake_invoker, monkeypatch):
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "reviewers_all_pass")
    spec = PanelReviewerSpec(vendor="claude", model="fake")
    r = _invoke_reviewer(spec, "test prompt", probe_interval_sec=10)
    assert r.ok
    assert "Verdict: pass" in r.output


def test_reviewer_empty_output_marked_not_ok(fake_invoker, monkeypatch):
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "reviewers_one_empty")
    spec = PanelReviewerSpec(vendor="agy", model="fake")
    r = _invoke_reviewer(spec, "test prompt", probe_interval_sec=10)
    assert not r.ok
    assert r.output == ""
    assert "empty" in r.failure_detail.lower()


def test_reviewer_timeout(fake_invoker, monkeypatch):
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "reviewers_one_timeout")
    spec = PanelReviewerSpec(vendor="codex", model="fake")
    r = _invoke_reviewer(spec, "test prompt", probe_interval_sec=2)
    assert not r.ok
    assert "timeout" in r.failure_detail.lower()


def test_quota_preflight_failure_is_rechecked_before_skip(monkeypatch):
    forced: list[bool] = []

    def quota_halt(*args, force_quota=False, **kwargs):
        forced.append(force_quota)
        raise QuotaHalt(
            role="reviewer:agy",
            diagnostics=[{
                "vendor": "agy",
                "model": "fake",
                "min_quota_pct": 10.0,
                "remaining_pct": 0.0,
                "resets_at": None,
                "error": None,
            }],
        )

    monkeypatch.setattr("autodev.panel.runner._invoke_reviewer", quota_halt)
    result = _invoke_reviewer_with_retry(
        PanelReviewerSpec(
            vendor="agy", model="fake", min_quota_pct=10.0,
        ),
        "prompt",
        10,
    )

    assert forced == [False, True]
    assert result.quota_skipped
    assert result.attempt_count == 2
    assert len(result.failure_history) == 2


def _confirmed_quota(vendor: str = "agy") -> dict:
    return {
        "confirmed": True,
        "source": "post-retry-quota-refresh",
        "vendor": vendor,
        "model": "fake",
        "remaining_pct": 0.0,
        "min_quota_pct": 0.000001,
        "configured_min_quota_pct": 0.0,
        "resets_at": None,
        "fetched_at": now_utc().isoformat(),
        "detail": "confirmed exhausted",
        "error": None,
    }


def test_fail_fast_confirmed_quota_stops_after_first_dispatch(monkeypatch):
    calls = 0

    def failed(*args, **kwargs):
        nonlocal calls
        calls += 1
        return ReviewerResult(
            vendor="agy", model="fake", ok=False, output="",
            elapsed_sec=0.01, failure_detail="provider exit 1",
        )

    monkeypatch.setattr("autodev.panel.runner._invoke_reviewer", failed)
    monkeypatch.setattr(
        "autodev.panel.runner._confirm_quota_after_failures",
        lambda spec, result: _confirmed_quota(),
    )
    state = _PanelFailFastState()
    with pytest.raises(QuotaHalt, match="panel-reviewers:design-review"):
        _invoke_reviewer_with_retry(
            PanelReviewerSpec(vendor="agy", model="fake"),
            "prompt", 10, gate_label="design-review", fail_fast_state=state,
        )

    assert calls == 1
    assert state.event.is_set()


def test_fail_fast_unknown_quota_keeps_normal_retry(monkeypatch):
    calls = 0

    def fail_then_pass(*args, **kwargs):
        nonlocal calls
        calls += 1
        return ReviewerResult(
            vendor="agy", model="fake", ok=calls == 2,
            output="pass" if calls == 2 else "", elapsed_sec=0.01,
            failure_detail="provider exit 1" if calls == 1 else "",
        )

    monkeypatch.setattr("autodev.panel.runner._invoke_reviewer", fail_then_pass)
    monkeypatch.setattr(
        "autodev.panel.runner._confirm_quota_after_failures",
        lambda spec, result: {**_confirmed_quota(), "confirmed": False,
                              "remaining_pct": None},
    )
    state = _PanelFailFastState()
    result = _invoke_reviewer_with_retry(
        PanelReviewerSpec(vendor="agy", model="fake"),
        "prompt", 10, gate_label="design-review", fail_fast_state=state,
    )

    assert calls == 2
    assert result.ok
    assert not state.event.is_set()


def test_fail_fast_confirmed_preflight_halts_without_retry(monkeypatch):
    calls = 0

    def quota_halt(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise QuotaHalt(
            role="reviewer:agy",
            diagnostics=[{
                "vendor": "agy", "model": "fake",
                "min_quota_pct": 10.0, "remaining_pct": 0.0,
                "resets_at": None, "error": None,
            }],
        )

    probes: list[tuple[str, bool]] = []

    def still_exhausted(candidates, *, role, logger=None, force=False):
        probes.append((role, force))
        raise QuotaHalt(
            role=role,
            diagnostics=[{
                "vendor": "agy", "model": "fake",
                "min_quota_pct": 10.0, "remaining_pct": 0.0,
                "resets_at": None, "error": None,
            }],
        )

    monkeypatch.setattr("autodev.panel.runner._invoke_reviewer", quota_halt)
    monkeypatch.setattr("autodev.panel.runner.resolve_candidate", still_exhausted)
    state = _PanelFailFastState()
    with pytest.raises(QuotaHalt, match="panel-reviewers:design-review"):
        _invoke_reviewer_with_retry(
            PanelReviewerSpec(vendor="agy", model="fake"),
            "prompt", 10, gate_label="design-review", fail_fast_state=state,
        )

    # The cached preflight reading is re-probed once, cache bypassed, before
    # the whole panel is cancelled on it.
    assert probes == [("reviewer:agy", True)]
    assert calls == 1
    assert state.event.is_set()


def test_fail_fast_cancels_dual_group_and_never_synthesizes(
    fake_invoker, monkeypatch, feature_active, panel_config,
):
    calls: list[tuple[str, threading.Event | None]] = []
    cancelled: list[str] = []
    first_failure = threading.Event()
    lock = threading.Lock()
    synth_calls = 0

    def reviewer(spec, *args, cancel_event=None, **kwargs):
        with lock:
            calls.append((spec.vendor, cancel_event))
            first = spec.vendor == "agy" and not first_failure.is_set()
            if first:
                first_failure.set()
        if first:
            return ReviewerResult(
                vendor=spec.vendor, model=spec.model, ok=False, output="",
                elapsed_sec=0.01, failure_detail="provider exit 1",
            )
        assert cancel_event is not None
        assert cancel_event.wait(timeout=2), "peer reviewer was not cancelled"
        cancelled.append(spec.vendor)
        return ReviewerResult(
            vendor=spec.vendor, model=spec.model, ok=False, output="",
            elapsed_sec=0.01, failure_detail="cancelled by panel quota stop",
        )

    def synthesizer(*args, **kwargs):
        nonlocal synth_calls
        synth_calls += 1
        return True, {}, ""

    monkeypatch.setattr("autodev.panel.runner._invoke_reviewer", reviewer)
    monkeypatch.setattr("autodev.panel.runner._invoke_synthesizer", synthesizer)
    monkeypatch.setattr(
        "autodev.panel.runner._confirm_quota_after_failures",
        lambda spec, result: _confirmed_quota(spec.vendor),
    )
    strict = PanelConfig(
        reviewers=panel_config.reviewers,
        synthesizer=panel_config.synthesizer,
        reviewer_probe_interval_sec=10,
        synthesizer_probe_interval_sec=10,
        fail_fast_confirmed_quota=True,
    )
    artifact = _make_artifact(feature_active)
    with pytest.raises(QuotaHalt):
        run_panel_gate_internal(
            gate="design-review", feature_active=feature_active,
            primary_artifact=artifact, prompt_file_for_audit=artifact,
            consulted_docs=[], panel_config=strict,
        )

    assert len(calls) <= len(strict.reviewers) * 2
    assert all(event is not None for _, event in calls)
    assert cancelled
    assert synth_calls == 0


def test_cancel_event_reaps_shared_call_registry_and_preserves_neighbor(
    git_repo, feature_active, monkeypatch,
):
    fake_cli = Path(__file__).resolve().parent / "fakes" / "fake_vendor_cli.sh"
    monkeypatch.delenv(FAKE_INVOKER_ENV, raising=False)
    monkeypatch.setenv("AUTODEV_VENDOR_BIN_CLAUDE", str(fake_cli))
    monkeypatch.setenv("AUTODEV_FAKE_BEHAVIOR", "timeout")
    monkeypatch.setattr(
        "autodev.panel.runner.resolve_candidate",
        lambda candidates, **kwargs: next(iter(candidates)),
    )
    stop = threading.Event()
    registry = feature_active / ".running-pids.json"
    neighbor = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                _invoke_reviewer,
                PanelReviewerSpec(vendor="claude", model="fake"),
                "prompt", 30,
                cwd=git_repo, feature_active=feature_active,
                cancel_event=stop,
            )
            deadline = time.monotonic() + 3
            while not read_processes(registry) and time.monotonic() < deadline:
                time.sleep(0.02)
            assert read_processes(registry)
            stop.set()
            result = future.result(timeout=5)
        assert not result.ok
        assert not registry.exists()
        assert neighbor.poll() is None
    finally:
        if neighbor.poll() is None:
            os.killpg(neighbor.pid, signal.SIGKILL)
        neighbor.wait(timeout=2)


def test_synthesizer_pass(fake_invoker, monkeypatch):
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "synth_pass")
    spec = PanelSynthesizerSpec(vendor="claude", model="fake")
    ok, parsed, detail = _invoke_synthesizer(spec, "prompt", probe_interval_sec=10)
    assert ok, detail
    assert "per_reviewer" in parsed
    assert all(e["verdict"] == "pass" for e in parsed["per_reviewer"])


def test_synthesizer_malformed_triggers_failure(fake_invoker, monkeypatch):
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "synth_empty")
    spec = PanelSynthesizerSpec(vendor="claude", model="fake")
    ok, parsed, detail = _invoke_synthesizer(spec, "prompt", probe_interval_sec=10)
    assert not ok
    assert parsed is None
    assert "json" in detail.lower()


def test_end_to_end_all_pass(fake_invoker, monkeypatch, feature_active, panel_config):
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "reviewers_all_pass")
    artifact = _make_artifact(feature_active)
    prompt_file = artifact  # hash it as a stand-in prompt-audit for the test
    v = run_panel_gate_internal(
        gate="design-review",
        feature_active=feature_active,
        primary_artifact=artifact,
        prompt_file_for_audit=prompt_file,
        consulted_docs=[],
        panel_config=panel_config,
    )
    assert v.verdict == "pass"
    assert not v.has_invariant_violation()
    # All 3 reviewers must be present in per_vendor_raw (audit)
    assert set(v.per_vendor_raw.keys()) == {"claude", "agy", "codex"}
    # verdict file written atomically
    written = feature_active / "panel-design-review.json"
    assert written.exists()
    reloaded = load_verdict(written)
    assert reloaded.verdict == "pass"
    assert reloaded.per_vendor_raw == v.per_vendor_raw


def test_end_to_end_inv_violation_fails(fake_invoker, monkeypatch, feature_active, panel_config):
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "reviewers_two_fail")
    artifact = _make_artifact(feature_active)
    v = run_panel_gate_internal(
        gate="design-review",
        feature_active=feature_active,
        primary_artifact=artifact,
        prompt_file_for_audit=artifact,
        consulted_docs=[],
        panel_config=panel_config,
    )
    # v3-core R4: verdict is `pass`/`needs_revision` derived from
    # surviving findings; `fail` is reserved for synthesizer errors.
    assert v.verdict == "needs_revision"
    assert v.has_invariant_violation()
    inv_findings = [f for f in v.findings if f.severity == "invariant_violation"]
    assert len(inv_findings) >= 2, (
        "each reviewer's finding stays separate — no cross-vendor merge"
    )
    # Each finding carries its single reviewer's vendor label verbatim.
    vendors_on_findings = {f.vendor for f in inv_findings}
    assert "claude" in vendors_on_findings
    assert "agy" in vendors_on_findings


def test_incomplete_panel_one_missing_halts_and_caches_successes(
    fake_invoker, monkeypatch, feature_active, panel_config,
):
    """Two responses do not excuse a non-quota reviewer failure."""
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "reviewers_one_empty")
    artifact = _make_artifact(feature_active)
    # The design-review gate runs the design-review and trace-review groups
    # concurrently; with a reviewer missing, BOTH groups are incomplete and
    # which group's GatePending propagates first is a thread race. Accept
    # either gate's message (both are the correct incomplete-panel signal).
    with pytest.raises(GatePending, match=r"panel (design-review|trace-review) incomplete"):
        run_panel_gate_internal(
            gate="design-review",
            feature_active=feature_active,
            primary_artifact=artifact,
            prompt_file_for_audit=artifact,
            consulted_docs=[],
            panel_config=panel_config,
        )

    assert not (feature_active / "panel-design-review.json").exists()
    cache = json.loads(
        (feature_active / "panel-design-review.reviewers.json").read_text()
    )
    assert set(cache["reviewers"]) == {"claude", "codex"}
    assert "agy" in cache["failures"]
    assert cache["failures"]["agy"]["attempt_count"] == 2
    assert len(cache["failures"]["agy"]["failure_history"]) == 2
    assert cache["quota_skipped"] == {}


def test_panel_quorum_skips_only_retried_quota_exhausted_reviewer(
    fake_invoker, monkeypatch, feature_active, panel_config,
):
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "reviewers_one_empty")
    quota_calls: list[tuple[str, str | None, bool]] = []

    def exhausted(vendor, model=None, *, force=False):
        quota_calls.append((vendor, model, force))
        return QuotaResult(
            vendor=vendor,
            remaining_pct=0.0,
            resets_at=None,
            fetched_at=now_utc(),
            detail="test quota exhausted",
        )

    monkeypatch.setattr("autodev.panel.runner.get_quota_remaining", exhausted)
    artifact = _make_artifact(feature_active)
    verdict = run_panel_gate_internal(
        gate="design-review",
        feature_active=feature_active,
        primary_artifact=artifact,
        prompt_file_for_audit=artifact,
        consulted_docs=[],
        panel_config=panel_config,
    )

    assert quota_calls == [
        ("agy", "fake-agy", True),
        ("agy", "fake-agy", True),
    ]
    assert verdict.per_vendor_raw["agy"].startswith(
        "[SKIPPED: quota confirmed after 2 dispatch attempt(s)"
    )
    assert not panel_verdict_transport_incomplete(verdict)
    cache = json.loads(
        (feature_active / "panel-design-review.reviewers.json").read_text()
    )
    assert cache["quota_skipped"]["agy"]["attempt_count"] == 2
    assert len(cache["quota_skipped"]["agy"]["failure_history"]) == 2
    confirmation = cache["quota_skipped"]["agy"]["quota_confirmation"]
    assert confirmation["confirmed"]
    assert confirmation["min_quota_pct"] > 0.0


def test_quota_shortfall_halts_then_retries_skipped_vendor_after_recovery(
    fake_invoker, monkeypatch, feature_active, panel_config,
):
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "reviewers_one_empty")
    monkeypatch.setattr(
        "autodev.panel.runner.get_quota_remaining",
        lambda vendor, model=None, force=False: QuotaResult(
            vendor=vendor,
            remaining_pct=0.0,
            resets_at=None,
            fetched_at=now_utc(),
        ),
    )
    strict_quorum = PanelConfig(
        reviewers=panel_config.reviewers,
        synthesizer=panel_config.synthesizer,
        reviewer_probe_interval_sec=panel_config.reviewer_probe_interval_sec,
        synthesizer_probe_interval_sec=panel_config.synthesizer_probe_interval_sec,
        min_responding_reviewers=3,
    )
    artifact = _make_artifact(feature_active)
    with pytest.raises(QuotaHalt, match="panel-reviewers"):
        run_panel_gate_internal(
            gate="close-approval",
            feature_active=feature_active,
            primary_artifact=artifact,
            prompt_file_for_audit=artifact,
            consulted_docs=[],
            panel_config=strict_quorum,
        )

    # A later run represents quota-resume recovery. Cached successful reviews
    # remain reusable, but the formerly quota-skipped reviewer must run again.
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "reviewers_all_pass")
    verdict = run_panel_gate_internal(
        gate="close-approval",
        feature_active=feature_active,
        primary_artifact=artifact,
        prompt_file_for_audit=artifact,
        consulted_docs=[],
        panel_config=strict_quorum,
    )
    assert verdict.verdict == "pass"
    assert not verdict.per_vendor_raw["agy"].startswith("[SKIPPED:")


def test_incomplete_panel_restart_retries_missing_reviewer_only(
    fake_invoker, monkeypatch, feature_active, panel_config,
):
    import tempfile, os

    artifact = _make_artifact(feature_active)

    # First run: agy fails; claude/codex succeed and should be cached.
    fd, path_str = tempfile.mkstemp(suffix=".sh")
    os.close(fd)
    first = Path(path_str)
    first.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "cat > /dev/null\n"
        "role=\"${AUTODEV_PANEL_FAKE_ROLE:-}\"\n"
        "vendor=\"${AUTODEV_PANEL_FAKE_VENDOR:-}\"\n"
        "if [[ \"$role\" == \"reviewer\" ]]; then\n"
        "  if [[ \"$vendor\" == \"agy\" ]]; then exit 0; fi\n"
        "  echo \"cached $vendor\"\n"
        "  echo 'Verdict: pass'\n"
        "  exit 0\n"
        "fi\n"
        "echo 'synthesizer should not run on incomplete panel' >&2\n"
        "exit 9\n"
    )
    first.chmod(0o755)
    monkeypatch.setenv(FAKE_INVOKER_ENV, str(first))
    with pytest.raises(GatePending):
        run_panel_gate_internal(
            gate="design-review",
            feature_active=feature_active,
            primary_artifact=artifact,
            prompt_file_for_audit=artifact,
            consulted_docs=[],
            panel_config=panel_config,
        )

    # Second run: only agy may be invoked; claude/codex must come from cache.
    calls = feature_active / "second-calls.txt"
    fd, path_str = tempfile.mkstemp(suffix=".sh")
    os.close(fd)
    second = Path(path_str)
    second.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "cat > /dev/null\n"
        "role=\"${AUTODEV_PANEL_FAKE_ROLE:-}\"\n"
        "vendor=\"${AUTODEV_PANEL_FAKE_VENDOR:-}\"\n"
        f"echo \"$role:$vendor\" >> {calls}\n"
        "if [[ \"$role\" == \"reviewer\" ]]; then\n"
        "  if [[ \"$vendor\" != \"agy\" ]]; then exit 97; fi\n"
        "  echo 'fresh agy'\n"
        "  echo 'Verdict: pass'\n"
        "  exit 0\n"
        "fi\n"
        "cat <<'EOF'\n"
            '{"per_reviewer":[{"vendor":"claude","verdict":"pass","findings":[]},'
            '{"vendor":"agy","verdict":"pass","findings":[]},'
            '{"vendor":"codex","verdict":"pass","findings":[]}],'
            '"issue_clusters":[],"decision":{"node":"design_review",'
            '"outcome":"pass","blocking":false,"severity":"opinion",'
            '"summary":"pass"}}\n'
        "EOF\n"
        "exit 0\n"
    )
    second.chmod(0o755)
    monkeypatch.setenv(FAKE_INVOKER_ENV, str(second))

    v = run_panel_gate_internal(
        gate="design-review",
        feature_active=feature_active,
        primary_artifact=artifact,
        prompt_file_for_audit=artifact,
        consulted_docs=[],
        panel_config=panel_config,
    )
    assert v.verdict == "pass"
    observed = calls.read_text().splitlines()
    assert "reviewer:claude" not in observed
    assert observed.count("reviewer:agy") == 2  # design + trace groups
    assert "reviewer:codex" not in observed


def test_synthesizer_broken_halts_with_invariant_violation(
    fake_invoker, monkeypatch, feature_active, panel_config,
):
    """When the synthesizer emits non-JSON / times out / exits non-zero,
    the runner must NOT fall back to a conservative needs_revision
    (which silently burns an L cycle on a fake panel rejection). It
    must produce verdict=fail with an `invariant_violation` finding
    from the harness, no rerunnable target, so revision_loop halts
    for human review."""
    # Reviewers produce invariant_violation markdown; synthesizer is
    # broken → expect harness invariant_violation, no mechanical scan.
    import tempfile, os
    fd, path_str = tempfile.mkstemp(suffix=".sh")
    os.close(fd)
    wrapper = Path(path_str)
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "role=\"${AUTODEV_PANEL_FAKE_ROLE:-}\"\n"
        "if [[ \"$role\" == \"reviewer\" ]]; then\n"
        "  AUTODEV_PANEL_FAKE_BEHAVIOR=reviewers_two_fail "
            f"  exec {shlex.quote(str(FAKE_SCRIPT))}\n"
        "else\n"
        "  AUTODEV_PANEL_FAKE_BEHAVIOR=synth_empty "
            f"  exec {shlex.quote(str(FAKE_SCRIPT))}\n"
        "fi\n"
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv(FAKE_INVOKER_ENV, str(wrapper))

    artifact = _make_artifact(feature_active)
    # Synthesizer broken: harness writes audit verdict AND raises
    # PreflightError so orchestrator halts the run.
    from autodev.errors import PreflightError
    with pytest.raises(PreflightError, match="synthesizer infrastructure error"):
        run_panel_gate_internal(
            gate="design-review",
            feature_active=feature_active,
            primary_artifact=artifact,
            prompt_file_for_audit=artifact,
            consulted_docs=[],
            panel_config=panel_config,
        )
    # Verdict file written for audit despite the raise.
    import json
    verdict_path = feature_active / "panel-design-review.json"
    assert verdict_path.exists(), "audit verdict must be written before raise"
    raw = json.loads(verdict_path.read_text())
    assert raw["verdict"] == "fail"
    harness_iv = [
        f for f in raw["findings"]
        if f.get("severity") == "invariant_violation"
        and f.get("vendor") == "harness"
        and "synthesizer_failed" in f.get("summary", "")
    ]
    assert harness_iv, f"expected harness synthesizer_failed invariant_violation; got {raw['findings']}"
    assert harness_iv[0].get("targets", []) == []


def test_parallel_dispatch_uses_threadpool(fake_invoker, monkeypatch, feature_active, panel_config):
    """All 3 reviewers should dispatch concurrently — total wall time
    should be close to the longest single reviewer, not the sum."""
    # reviewers_all_pass reviewers are instant; we sleep per-reviewer
    # via an inline wrapper that adds a short delay.
    import tempfile, os
    fd, path_str = tempfile.mkstemp(suffix=".sh")
    os.close(fd)
    wrapper = Path(path_str)
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "role=\"${AUTODEV_PANEL_FAKE_ROLE:-reviewer}\"\n"
        "cat > /dev/null\n"
        "if [[ \"$role\" == \"reviewer\" ]]; then\n"
        "  sleep 1\n"
        "  echo \"Verdict: pass\"\n"
        "  exit 0\n"
        "fi\n"
        '  echo \'{"per_reviewer":[{"vendor":"claude","verdict":"pass","findings":[]},{"vendor":"agy","verdict":"pass","findings":[]},{"vendor":"codex","verdict":"pass","findings":[]}],"issue_clusters":[],"decision":{"node":"design_review","outcome":"pass","blocking":false,"severity":"opinion","summary":"pass"}}\'\n'
        "exit 0\n"
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv(FAKE_INVOKER_ENV, str(wrapper))
    artifact = _make_artifact(feature_active)
    t0 = time.monotonic()
    v = run_panel_gate_internal(
        gate="design-review",
        feature_active=feature_active,
        primary_artifact=artifact,
        prompt_file_for_audit=artifact,
        consulted_docs=[],
        panel_config=panel_config,
    )
    elapsed = time.monotonic() - t0
    # design-review now dispatches 6 reviewers (2 groups × 3) in parallel.
    # Serial would be ~6s; parallel should still be ~1s-1.5s. Leave a
    # generous bound to absorb synthesizer setup overhead.
    assert elapsed < 3.0, f"panel elapsed {elapsed:.2f}s — not parallel"
    assert v.verdict == "pass"


def test_design_review_dual_group_writes_both_verdicts(
    fake_invoker, monkeypatch, feature_active, panel_config,
):
    """A single design-review panel run writes two verdict files —
    panel-design-review.json and panel-trace-review.json — each tagged
    with its own group name."""
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "reviewers_all_pass")
    artifact = _make_artifact(feature_active)
    run_panel_gate_internal(
        gate="design-review",
        feature_active=feature_active,
        primary_artifact=artifact,
        prompt_file_for_audit=artifact,
        consulted_docs=[],
        panel_config=panel_config,
    )
    dr_path = feature_active / "panel-design-review.json"
    tr_path = feature_active / "panel-trace-review.json"
    assert dr_path.exists()
    assert tr_path.exists()
    dr = load_verdict(dr_path)
    tr = load_verdict(tr_path)
    assert dr.gate == "design-review"
    assert tr.gate == "trace-review"
    # Both groups share the same primary artifact.
    assert dr.source == str(artifact)
    assert tr.source == str(artifact)
    assert dr.source_hash == tr.source_hash


# --- cache completeness: coverage-round verdict + coverage row formats ------

@pytest.mark.parametrize("verdict_line", [
    "Verdict: pass",
    "**Verdict:** pass",
    "Verdict: **needs_revision**",
    "## Verdict: fail",
    "- Verdict: pass",
    "**Verdict**\n\n**pass**",
    "Overall verdict: pass",
    "Final verdict: needs_revision — see F3",
    "Verdict: `pass`",
    "**Verdict:** `pass`",
])
def test_cached_coverage_output_accepts_decorated_verdict_lines(
    feature_active, verdict_line,
):
    """Coverage prompts never mandate a bare ``Verdict:`` line, so markdown
    decoration must not evict a valid cached success."""
    from autodev.panel.runner import _cached_reviewer_output_is_complete

    (feature_active / "prd.md").write_text(
        "# PRD\n\n### R1: one\n\n### R2: two\n", encoding="utf-8",
    )
    output = (
        "| req_id | status | evidence | notes |\n"
        "|---|---|---|---|\n"
        "| R1 | satisfied | x | y |\n"
        "| R2 | satisfied | x | y |\n\n"
        f"{verdict_line}\n"
    )
    assert _cached_reviewer_output_is_complete(
        output, metadata={"round_type": "coverage"}, feature_active=feature_active,
    )


@pytest.mark.parametrize("bad_output", [
    "| R1 | satisfied |\n| R2 | satisfied |\n\nVerdict: maybe\n",
    "| R1 | satisfied |\n| R2 | satisfied |\n\nno verdict at all\n",
])
def test_cached_coverage_output_still_requires_a_real_verdict(
    feature_active, bad_output,
):
    from autodev.panel.runner import _cached_reviewer_output_is_complete

    (feature_active / "prd.md").write_text(
        "# PRD\n\n### R1: one\n\n### R2: two\n", encoding="utf-8",
    )
    assert not _cached_reviewer_output_is_complete(
        bad_output, metadata={"round_type": "coverage"},
        feature_active=feature_active,
    )


def test_budget_round_cache_keeps_strict_verdict_line(feature_active):
    """The budget prompt mandates the literal line; decoration stays invalid."""
    from autodev.panel.runner import _cached_reviewer_output_is_complete

    output = "Minimality evidence: fine\n\n**Verdict:** pass\n"
    assert not _cached_reviewer_output_is_complete(
        output, metadata={"round_type": "budget"}, feature_active=feature_active,
    )
    assert _cached_reviewer_output_is_complete(
        output.replace("**Verdict:**", "Verdict:"),
        metadata={"round_type": "budget"}, feature_active=feature_active,
    )


@pytest.mark.parametrize("rows", [
    "| R1 | satisfied | x | y |\n| R2 | satisfied | x | y |\n",
    "R1 | satisfied | x | y\nR2 | satisfied | x | y\n",
    "| **R1** | satisfied | x | y |\n| `R2` | satisfied | x | y |\n",
    "| R1: auth | satisfied | x | y |\n| R2 (audit) | satisfied | x | y |\n",
    "| R1/R2 | satisfied | x | y |\n",
])
def test_cached_coverage_output_accepts_gfm_row_variants(feature_active, rows):
    """Every PRD requirement must have a row, in any reasonable GFM shape."""
    from autodev.panel.runner import _cached_reviewer_output_is_complete

    (feature_active / "prd.md").write_text(
        "# PRD\n\n### R1: one\n\n### R2: two\n", encoding="utf-8",
    )
    assert _cached_reviewer_output_is_complete(
        rows + "\nVerdict: pass\n",
        metadata={"round_type": "coverage"}, feature_active=feature_active,
    )
    assert not _cached_reviewer_output_is_complete(
        "| R1 | satisfied | x | y |\n\nVerdict: pass\n",
        metadata={"round_type": "coverage"}, feature_active=feature_active,
    )


@pytest.mark.parametrize("output", [
    "## Minimality evidence\n\nVerdict: pass\n",
    "Minimality evidence:\n\nVerdict: pass\n",
    "**Minimality evidence**\n\n\nVerdict: needs_revision\n",
    "**Minimality evidence:**\n\nVerdict: pass\n",
])
def test_budget_round_cache_rejects_evidence_less_output(feature_active, output):
    """An empty evidence header must not borrow the verdict line as evidence."""
    from autodev.panel.runner import _cached_reviewer_output_is_complete

    assert not _cached_reviewer_output_is_complete(
        output, metadata={"round_type": "budget"}, feature_active=feature_active,
    )
    assert _cached_reviewer_output_is_complete(
        output.replace("\n\n", "\n- s-1 is required by R1.\n\n", 1),
        metadata={"round_type": "budget"}, feature_active=feature_active,
    )


# --- fail-fast: one forced quota probe per failed attempt -------------------

def test_fail_fast_probes_quota_once_per_failed_attempt(monkeypatch):
    """The post-loop confirmation must reuse the in-loop probe, not re-probe."""
    invocations = 0
    probes: list[str] = []

    def always_fail(*args, **kwargs):
        nonlocal invocations
        invocations += 1
        return ReviewerResult(
            vendor="agy", model="fake", ok=False, output="",
            elapsed_sec=0.01, failure_detail=f"provider exit {invocations}",
        )

    def probe(spec, result):
        probes.append(result.failure_detail)
        return {**_confirmed_quota(), "confirmed": False, "remaining_pct": None}

    monkeypatch.setattr("autodev.panel.runner._invoke_reviewer", always_fail)
    monkeypatch.setattr(
        "autodev.panel.runner._confirm_quota_after_failures", probe,
    )
    result = _invoke_reviewer_with_retry(
        PanelReviewerSpec(vendor="agy", model="fake"),
        "prompt", 10, gate_label="design-review",
        fail_fast_state=_PanelFailFastState(),
    )

    assert invocations == 2
    assert probes == ["provider exit 1", "provider exit 2"]
    assert not result.ok
    assert not result.quota_skipped
    assert result.quota_confirmation["confirmed"] is False


def test_without_fail_fast_probes_quota_once_after_the_loop(monkeypatch):
    probes = 0

    def always_fail(*args, **kwargs):
        return ReviewerResult(
            vendor="agy", model="fake", ok=False, output="",
            elapsed_sec=0.01, failure_detail="provider exit 1",
        )

    def probe(spec, result):
        nonlocal probes
        probes += 1
        return {**_confirmed_quota(), "confirmed": False, "remaining_pct": None}

    monkeypatch.setattr("autodev.panel.runner._invoke_reviewer", always_fail)
    monkeypatch.setattr(
        "autodev.panel.runner._confirm_quota_after_failures", probe,
    )
    result = _invoke_reviewer_with_retry(
        PanelReviewerSpec(vendor="agy", model="fake"), "prompt", 10,
    )
    assert probes == 1
    assert not result.ok


# --- dual group: the failing group's error wins over the peer sentinel -----

def test_dual_group_reports_real_group_failure_not_peer_sentinel(
    fake_invoker, monkeypatch, feature_active, panel_config,
):
    """When the design group fails quorum and the trace group only sees the
    broken barrier, the raised error must be the quorum failure."""
    from autodev.panel.runner import _PeerGroupFailed

    def reviewer(spec, *args, session_key=None, **kwargs):
        if session_key and ":design-review:" in session_key:
            return ReviewerResult(
                vendor=spec.vendor, model=spec.model, ok=False, output="",
                elapsed_sec=0.01, failure_detail="provider exit 1",
            )
        return ReviewerResult(
            vendor=spec.vendor, model=spec.model, ok=True,
            output="| R1 | satisfied | x | y |\n\nVerdict: pass\n",
            elapsed_sec=0.01,
        )

    monkeypatch.setattr("autodev.panel.runner._invoke_reviewer", reviewer)
    monkeypatch.setattr(
        "autodev.panel.runner._invoke_synthesizer",
        lambda *args, **kwargs: (True, {}, ""),
    )
    artifact = _make_artifact(feature_active)
    with pytest.raises(GatePending) as excinfo:
        run_panel_gate_internal(
            gate="design-review", feature_active=feature_active,
            primary_artifact=artifact, prompt_file_for_audit=artifact,
            consulted_docs=[], panel_config=panel_config,
        )

    assert not isinstance(excinfo.value, _PeerGroupFailed)
    assert "peer group failed" not in excinfo.value.detail
    assert excinfo.value.gate.endswith("design-review")


# --- cache identity ignores volatile budget figures --------------------------

def test_reviewer_cache_identity_ignores_budget_spend_lines(feature_active):
    """Spend lines are recomputed from log.jsonl on every call and grow with
    each dispatch; they must not invalidate the reviewer cache on resume."""
    from autodev.panel.runner import _cache_matches, _reviewer_cache_metadata

    artifact = _make_artifact(feature_active)
    base = "## Shrink round\n\n{lines}\n\nGo through scope.json.\n"
    first = _reviewer_cache_metadata(
        gate_label="design-review", primary_artifact=artifact,
        prompt_file_for_audit=artifact, consulted_docs=[], round_type="budget",
        reviewer_prompt=base.format(
            lines="- BUDGET_SPENT: vendor-hours 1.0h; wall span 0.5h",
        ),
    )
    later = _reviewer_cache_metadata(
        gate_label="design-review", primary_artifact=artifact,
        prompt_file_for_audit=artifact, consulted_docs=[], round_type="budget",
        reviewer_prompt=base.format(
            lines="- BUDGET_SPENT: vendor-hours 3.5h; wall span 2.0h\n"
                  "- BUDGET_TARGETS: vendor_hours 10",
        ),
    )
    assert first["reviewer_prompt_hash"] == later["reviewer_prompt_hash"]
    assert _cache_matches({**first, "reviewers": {}}, later)

    reworded = _reviewer_cache_metadata(
        gate_label="design-review", primary_artifact=artifact,
        prompt_file_for_audit=artifact, consulted_docs=[], round_type="budget",
        reviewer_prompt=base.format(lines="- BUDGET_SPENT: 1.0h").replace(
            "Go through scope.json.", "Go through design.md.",
        ),
    )
    assert reworded["reviewer_prompt_hash"] != first["reviewer_prompt_hash"]


# --- fail-fast quota halt keeps the finished reviewers cached ---------------

def test_fail_fast_quota_halt_persists_finished_reviewers(
    fake_invoker, monkeypatch, feature_active, panel_config,
):
    """A QuotaHalt raised from a worker must not discard the other slots'
    successful outputs; quota-resume should re-run only the halted one."""
    lock = threading.Lock()
    successes: dict[str, int] = {}
    peers_done = threading.Event()

    def reviewer(spec, *args, session_key=None, **kwargs):
        group = "design-review" if ":design-review:" in (session_key or "") else "trace-review"
        if spec.vendor == "agy":
            # Let the healthy peers of this group finish first so the halt
            # is raised while their results are already collected.
            assert peers_done.wait(timeout=5), "peers never finished"
            return ReviewerResult(
                vendor=spec.vendor, model=spec.model, ok=False, output="",
                elapsed_sec=0.01, failure_detail="provider exit 1",
            )
        with lock:
            successes[group] = successes.get(group, 0) + 1
            if sum(successes.values()) >= 4:
                peers_done.set()
        return ReviewerResult(
            vendor=spec.vendor, model=spec.model, ok=True,
            output="| R1 | satisfied | x | y |\n\nVerdict: pass\n",
            elapsed_sec=0.01,
        )

    monkeypatch.setattr("autodev.panel.runner._invoke_reviewer", reviewer)
    monkeypatch.setattr(
        "autodev.panel.runner._invoke_synthesizer",
        lambda *args, **kwargs: (True, {}, ""),
    )
    monkeypatch.setattr(
        "autodev.panel.runner._confirm_quota_after_failures",
        lambda spec, result: _confirmed_quota(spec.vendor),
    )
    strict = PanelConfig(
        reviewers=panel_config.reviewers,
        synthesizer=panel_config.synthesizer,
        reviewer_probe_interval_sec=10,
        synthesizer_probe_interval_sec=10,
        fail_fast_confirmed_quota=True,
    )
    artifact = _make_artifact(feature_active)
    with pytest.raises(QuotaHalt):
        run_panel_gate_internal(
            gate="design-review", feature_active=feature_active,
            primary_artifact=artifact, prompt_file_for_audit=artifact,
            consulted_docs=[], panel_config=strict,
        )

    for gate in ("design-review", "trace-review"):
        cache_path = feature_active / f"panel-{gate}.reviewers.json"
        assert cache_path.exists(), f"{gate} cache was not persisted"
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        assert set(cache["reviewers"]) == {"claude", "codex"}, gate
        assert "agy" not in cache["reviewers"]


def test_cached_coverage_output_ignores_pipe_delimited_prose(feature_active):
    """Only table rows count toward coverage, not prose containing pipes."""
    from autodev.panel.runner import _cached_reviewer_output_is_complete

    (feature_active / "prd.md").write_text(
        "# PRD\n\n### R1: one\n\n### R2: two\n### R3: three\n", encoding="utf-8",
    )
    prose = (
        "| R1 | satisfied | x | y |\n"
        "- Evidence: prd:R2 | scope:S3\n"
        "- R3 missing | see above\n\nVerdict: pass\n"
    )
    assert not _cached_reviewer_output_is_complete(
        prose, metadata={"round_type": "coverage"}, feature_active=feature_active,
    )
    table = (
        "| R1 | satisfied | x | y |\n| R2 | satisfied | x | y |\n"
        "R3 | satisfied | x | y\n\nVerdict: pass\n"
    )
    assert _cached_reviewer_output_is_complete(
        table, metadata={"round_type": "coverage"}, feature_active=feature_active,
    )


def test_fail_fast_reprobes_cached_preflight_halt_before_cancelling(monkeypatch):
    """A preflight halt on attempt 1 may rest on a cached quota reading; a
    forced re-probe showing quota restored must keep the retry alive."""
    calls = 0
    probes: list[tuple[str, bool]] = []

    def preflight_then_pass(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise QuotaHalt(
                role="reviewer:agy",
                diagnostics=[{
                    "vendor": "agy", "model": "fake",
                    "min_quota_pct": 10.0, "remaining_pct": 0.0,
                    "resets_at": None, "error": None,
                }],
            )
        return ReviewerResult(
            vendor="agy", model="fake", ok=True, output="Verdict: pass",
            elapsed_sec=0.01,
        )

    def restored(candidates, *, role, logger=None, force=False):
        probes.append((role, force))
        return candidates[0]

    monkeypatch.setattr("autodev.panel.runner._invoke_reviewer", preflight_then_pass)
    monkeypatch.setattr("autodev.panel.runner.resolve_candidate", restored)
    state = _PanelFailFastState()
    result = _invoke_reviewer_with_retry(
        PanelReviewerSpec(vendor="agy", model="fake"),
        "prompt", 10, gate_label="design-review", fail_fast_state=state,
    )

    assert probes == [("reviewer:agy", True)]
    assert calls == 2
    assert result.ok
    assert not state.event.is_set()


def test_reprobed_quota_halt_carries_fresh_resume_time():
    """The re-probe replaces the stale preflight resume_at, not just the
    nested diagnostics, so the pause record schedules the right time."""
    from datetime import timedelta

    from autodev.panel.runner import _reconfirm_quota_halt

    stale = now_utc() - timedelta(hours=1)
    fresh = now_utc() + timedelta(hours=2)
    confirmation = {
        "confirmed": True, "source": "quota-preflight",
        "diagnostics": [{"vendor": "agy", "model": "fake",
                         "min_quota_pct": 10.0, "remaining_pct": 0.0}],
        "resume_at": stale.isoformat(),
    }

    def reprobe(candidates, *, role, logger=None, force=False):
        assert force is True
        raise QuotaHalt(
            role=role,
            diagnostics=[{"vendor": "agy", "model": "fake",
                          "min_quota_pct": 10.0, "remaining_pct": 1.0,
                          "resets_at": fresh.isoformat(), "error": None}],
            resume_at=fresh,
        )

    import autodev.panel.runner as runner
    original = runner.resolve_candidate
    runner.resolve_candidate = reprobe
    try:
        result = _reconfirm_quota_halt(
            PanelReviewerSpec(vendor="agy", model="fake"), confirmation,
        )
    finally:
        runner.resolve_candidate = original

    assert result["confirmed"] is True
    assert result["source"] == "quota-preflight-reprobed"
    assert result["resume_at"] == fresh.isoformat()


def test_reviewer_cache_identity_changes_with_budget_targets(feature_active):
    """Spend figures are excluded from the identity, but the operator's
    ceilings are what a minimality verdict was judged against."""
    from autodev.panel.runner import _cache_matches, _reviewer_cache_metadata

    artifact = _make_artifact(feature_active)
    common = dict(
        gate_label="design-review", primary_artifact=artifact,
        prompt_file_for_audit=artifact, consulted_docs=[], round_type="budget",
        reviewer_prompt="## Shrink round\n\n- BUDGET_SPENT: 1.0h\n",
    )
    forty = _reviewer_cache_metadata(**common, budget_targets=[["vendor_hours", 40.0]])
    ten = _reviewer_cache_metadata(**common, budget_targets=[["vendor_hours", 10.0]])
    assert _cache_matches({**forty, "reviewers": {}}, forty)
    assert not _cache_matches({**forty, "reviewers": {}}, ten)
