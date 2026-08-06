"""G15: internal panel runner tests.

Validates parallel reviewer dispatch, synthesizer invocation, verdict
construction, and mechanical-fallback degradation — all via a fake
invoker shell script so no live vendor subprocess runs.
"""
from __future__ import annotations

import json
import shlex
import time
from pathlib import Path

import pytest

from autodev.artifacts.verdict import IssueCluster, PanelFinding, PanelVerdict, load_verdict
from autodev.artifacts.fingerprint_history import record_verdict
from autodev.panel.runner import (
    FAKE_INVOKER_ENV, _compose_reviewer_prompt, _compose_synthesizer_prompt,
    _build_issue_clusters, _invoke_reviewer, _invoke_synthesizer,
    _synthesize_and_build_verdict,
    _read_only_native_args, run_panel_gate_internal,
)
from autodev.panel.schemas import synthesizer_output_schema
from autodev.vendors.config import (
    PanelConfig, PanelReviewerSpec, PanelSynthesizerSpec,
)
from autodev.errors import GatePending, SchemaError
from autodev.vendors.shared_call import cli_name_for_vendor, normalize_shared_vendor

FAKE_SCRIPT = Path(__file__).resolve().parent / "fakes" / "fake_panel_invoker.sh"


@pytest.fixture
def fake_invoker(monkeypatch):
    assert FAKE_SCRIPT.exists(), f"fake script missing: {FAKE_SCRIPT}"
    monkeypatch.setenv(FAKE_INVOKER_ENV, str(FAKE_SCRIPT))
    # v3-core R5: these tests stub synthetic single-artifact fixtures
    # and target the runner's dispatch path, not the pre-check.
    monkeypatch.setenv("AUTODEV_PANEL_SKIP_PRECHECK", "1")
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
    """Any missing reviewer is panel transport failure, not a content
    verdict. Successful reviewers are cached so restart retries only the
    missing reviewer."""
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
