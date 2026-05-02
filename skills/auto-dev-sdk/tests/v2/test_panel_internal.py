"""G15: internal panel runner tests.

Validates parallel reviewer dispatch, synthesizer invocation, verdict
construction, and mechanical-fallback degradation — all via a fake
invoker shell script so no live vendor subprocess runs.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from autodev.artifacts.verdict import load_verdict
from autodev.panel.runner import (
    FAKE_INVOKER_ENV, _compose_reviewer_prompt, _compose_synthesizer_prompt,
    _invoke_reviewer, _invoke_synthesizer, _reviewer_context_files,
    _read_only_native_args, run_panel_gate_internal,
)
from autodev.panel.schemas import synthesizer_output_schema
from autodev.vendors.config import (
    PanelConfig, PanelReviewerSpec, PanelSynthesizerSpec,
)
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
            PanelReviewerSpec(vendor="gemini", model="fake-gemini"),
            PanelReviewerSpec(vendor="codex", model="fake-codex"),
        ),
        synthesizer=PanelSynthesizerSpec(vendor="claude", model="fake-sonnet"),
        reviewer_timeout_sec=10,
        synthesizer_timeout_sec=10,
    )


def _make_artifact(feature_active: Path) -> Path:
    p = feature_active / "prd.md"
    p.write_text("# demo PRD\n\nR1: do a thing.\n")
    return p


def test_schema_is_per_reviewer_extraction():
    """Synthesizer JSON schema keeps per-reviewer extraction and may add a canonical decision."""
    schema = synthesizer_output_schema()
    assert schema["required"] == ["per_reviewer"]
    entry = schema["properties"]["per_reviewer"]["items"]
    assert set(entry["required"]) == {"vendor", "verdict", "findings"}
    assert set(entry["properties"]["verdict"]["enum"]) == {
        "pass", "needs_revision", "fail"
    }
    finding_schema = entry["properties"]["findings"]["items"]
    # Findings live INSIDE a reviewer entry — they do NOT carry
    # originating_vendors; the reviewer's own vendor label owns them.
    assert set(finding_schema["required"]) == {"severity", "summary"}
    assert "originating_vendors" not in finding_schema["properties"]
    coverage_schema = entry["properties"]["coverage"]["items"]
    assert set(coverage_schema["required"]) == {"req_id", "status", "evidence"}
    assert set(coverage_schema["properties"]["status"]["enum"]) == {
        "satisfied", "partial", "missing", "deviated", "ambiguous",
    }
    decision = schema["properties"]["decision"]
    assert set(decision["required"]) == {
        "node", "outcome", "blocking", "severity", "summary",
    }
    assert set(decision["properties"]["outcome"]["enum"]) == {
        "pass", "retry_design", "halt_for_human",
    }


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


def test_reviewer_vendor_labels_route_through_shared_vendors():
    assert normalize_shared_vendor("Claude") == "claude"
    assert normalize_shared_vendor("gemini") == "gemini"
    assert normalize_shared_vendor("codex") == "openai"
    assert cli_name_for_vendor("openai") == "codex"


def test_panel_reviewers_use_vendor_native_read_only_hints():
    assert _read_only_native_args("codex") == ("--sandbox", "read-only")
    assert _read_only_native_args("openai") == ("--sandbox", "read-only")
    assert _read_only_native_args("claude") == (
        "--allowedTools", "Read,Glob,Grep,LS",
    )
    assert _read_only_native_args("gemini") == ("--approval-mode", "plan")


def test_reviewer_context_files_dedupe_primary_and_consulted(feature_active):
    primary = _make_artifact(feature_active)
    extra = feature_active / "design.md"
    extra.write_text("design", encoding="utf-8")
    got = _reviewer_context_files(
        primary_artifact=primary,
        consulted_docs=[
            {"path": str(primary)},
            {"path": str(extra)},
            {"path": str(feature_active / "missing.md")},
        ],
    )
    assert got == (primary, extra)


def test_compose_synthesizer_prompt_lists_responding_vendors(feature_active):
    artifact = _make_artifact(feature_active)
    from autodev.panel.runner import ReviewerResult
    results = [
        ReviewerResult(vendor="claude", model="m", ok=True,
                       output="Verdict: pass", elapsed_sec=1.0),
        ReviewerResult(vendor="gemini", model="m", ok=False, output="",
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
    assert "gemini" in sp
    # Synthesize prompt body itself is included (pure extractor wording)
    assert "extractor" in sp.lower()


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


def test_reviewer_invocation_via_fake(fake_invoker, monkeypatch):
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "reviewers_all_pass")
    spec = PanelReviewerSpec(vendor="claude", model="fake")
    r = _invoke_reviewer(spec, "test prompt", timeout_sec=10)
    assert r.ok
    assert "Verdict: pass" in r.output


def test_reviewer_empty_output_marked_not_ok(fake_invoker, monkeypatch):
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "reviewers_one_empty")
    spec = PanelReviewerSpec(vendor="gemini", model="fake")
    r = _invoke_reviewer(spec, "test prompt", timeout_sec=10)
    assert not r.ok
    assert r.output == ""
    assert "empty" in r.failure_detail.lower()


def test_reviewer_timeout(fake_invoker, monkeypatch):
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "reviewers_one_timeout")
    spec = PanelReviewerSpec(vendor="codex", model="fake")
    r = _invoke_reviewer(spec, "test prompt", timeout_sec=2)
    assert not r.ok
    assert "timeout" in r.failure_detail.lower()


def test_synthesizer_pass(fake_invoker, monkeypatch):
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "synth_pass")
    spec = PanelSynthesizerSpec(vendor="claude", model="fake")
    ok, parsed, detail = _invoke_synthesizer(spec, "prompt", timeout_sec=10)
    assert ok, detail
    assert "per_reviewer" in parsed
    assert all(e["verdict"] == "pass" for e in parsed["per_reviewer"])


def test_synthesizer_malformed_triggers_failure(fake_invoker, monkeypatch):
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "synth_empty")
    spec = PanelSynthesizerSpec(vendor="claude", model="fake")
    ok, parsed, detail = _invoke_synthesizer(spec, "prompt", timeout_sec=10)
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
    assert set(v.per_vendor_raw.keys()) == {"claude", "gemini", "codex"}
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
    assert "gemini" in vendors_on_findings


def test_degraded_panel_one_missing_is_still_synthesized(
    fake_invoker, monkeypatch, feature_active, panel_config,
):
    """With exactly 2 healthy reviewers, the synthesizer still runs.
    The missing reviewer's slot in per_vendor_raw records the no-response."""
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "reviewers_one_empty")
    artifact = _make_artifact(feature_active)
    v = run_panel_gate_internal(
        gate="design-review",
        feature_active=feature_active,
        primary_artifact=artifact,
        prompt_file_for_audit=artifact,
        consulted_docs=[],
        panel_config=panel_config,
    )
    # Gemini's slot in per_vendor_raw records the no-response reason.
    assert v.per_vendor_raw["gemini"].startswith("[NO RESPONSE")
    # Synthesizer ran (per_vendor_raw has ≥2 entries of real output).
    responded = [
        k for k, o in v.per_vendor_raw.items() if not o.startswith("[NO RESPONSE")
    ]
    assert len(responded) >= 2
    # Specifically, runner's degraded-panel finding MUST NOT be present
    # (only fires when < MIN_HEALTHY_REVIEWERS responded).
    assert not any(
        "only" in f.summary.lower() and "reviewers responded" in f.summary.lower()
        for f in v.findings
    )


def test_degraded_panel_below_min_fails_fast(
    fake_invoker, monkeypatch, feature_active, panel_config,
):
    """<2 healthy reviewers → fail verdict pointing at doctor.sh. No
    synthesizer call — a single-vendor opinion is not a panel."""
    # Force a wrapper where only claude responds; gemini and codex emit nothing.
    import tempfile, os
    fd, path_str = tempfile.mkstemp(suffix=".sh")
    os.close(fd)
    wrapper = Path(path_str)
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "cat > /dev/null\n"
        "role=\"${AUTODEV_PANEL_FAKE_ROLE:-}\"\n"
        "vendor=\"${AUTODEV_PANEL_FAKE_VENDOR:-}\"\n"
        "if [[ \"$role\" == \"reviewer\" ]]; then\n"
        "  if [[ \"$vendor\" == \"claude\" ]]; then\n"
        "    echo 'Verdict: pass'\n"
        "    exit 0\n"
        "  fi\n"
        "  exit 0  # empty stdout for gemini + codex\n"
        "fi\n"
        # synthesizer should NEVER be called in this case. If it is, we
        # emit JSON that would pass so the test catches the mistake via
        # the verdict assertion below.
        "echo 'SYNTHESIZER-SHOULD-NOT-RUN' >&2\n"
        'echo \'{"per_reviewer":[{"vendor":"claude","verdict":"pass","findings":[]}]}\'\n'
        "exit 0\n"
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv(FAKE_INVOKER_ENV, str(wrapper))

    artifact = _make_artifact(feature_active)
    v = run_panel_gate_internal(
        gate="design-review",
        feature_active=feature_active,
        primary_artifact=artifact,
        prompt_file_for_audit=artifact,
        consulted_docs=[],
        panel_config=panel_config,
    )
    assert v.verdict == "fail"
    assert v.has_invariant_violation()
    # Finding must point at doctor.sh
    degraded = [f for f in v.findings if "panel degraded" in f.summary.lower()]
    assert degraded, f"expected panel-degraded finding; got {v.findings}"
    assert "doctor" in degraded[0].summary.lower() or "panel-review" in degraded[0].summary.lower()
    # Two reviewers have [NO RESPONSE] markers
    no_resp = [k for k, out in v.per_vendor_raw.items() if out.startswith("[NO RESPONSE")]
    assert set(no_resp) == {"gemini", "codex"}


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
        f"  exec {FAKE_SCRIPT}\n"
        "else\n"
        "  AUTODEV_PANEL_FAKE_BEHAVIOR=synth_empty "
        f"  exec {FAKE_SCRIPT}\n"
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
        '  echo \'{"per_reviewer":[{"vendor":"claude","verdict":"pass","findings":[]},{"vendor":"gemini","verdict":"pass","findings":[]},{"vendor":"codex","verdict":"pass","findings":[]}]}\'\n'
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
    # 3 reviewers × 1s each serial = 3s; parallel should be ~1s-1.5s.
    assert elapsed < 2.5, f"panel elapsed {elapsed:.2f}s — not parallel"
    assert v.verdict == "pass"
