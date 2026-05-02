"""G19: test-plan-review panel gate.

A 4th panel gate that reviews test-plan.md with consulted docs
[scope.json, trace.md, prd.md]. Lives between the plan stage and
the build stage in the cascade.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodev.artifacts.verdict import load_verdict
from autodev.panel import prompt_path, run_panel_gate
from autodev.panel.runner import FAKE_INVOKER_ENV
from autodev.state.cascade import ARTIFACTS, StalenessCascade
from autodev.state.hashing import hash_file
from autodev.vendors.config import (
    PanelConfig, PanelReviewerSpec, PanelSynthesizerSpec,
)

FAKE_SCRIPT = Path(__file__).resolve().parent / "fakes" / "fake_panel_invoker.sh"


@pytest.fixture
def fake_invoker(monkeypatch):
    assert FAKE_SCRIPT.exists()
    monkeypatch.setenv(FAKE_INVOKER_ENV, str(FAKE_SCRIPT))
    yield


@pytest.fixture
def panel_config():
    return PanelConfig(
        reviewers=(
            PanelReviewerSpec(vendor="claude", model="fake"),
            PanelReviewerSpec(vendor="gemini", model="fake"),
            PanelReviewerSpec(vendor="codex", model="fake"),
        ),
        synthesizer=PanelSynthesizerSpec(vendor="claude", model="fake"),
        reviewer_timeout_sec=10,
        synthesizer_timeout_sec=10,
    )


def _seed(feature_active: Path) -> Path:
    """Seed a chain that is hash-fresh through test_plan."""
    import json as _json

    feature_active.mkdir(parents=True, exist_ok=True)
    prd = feature_active / "prd.md"
    prd.write_text(
        "# PRD: demo\n## 3. Requirements\nR1: the thing must hash.\n",
        encoding="utf-8",
    )
    prd_hash = hash_file(prd)

    scope = feature_active / "scope.json"
    scope.write_text(
        _json.dumps({
            "source": "prd.md",
            "source_hash": prd_hash,
            "written": "2026-04-20",
            "feature": "demo",
            "mode": "fresh",
            "diff_base": "main",
            "in_scope": [
                {"id": "d-1", "description": "hash a file",
                 "prd_ref": "§3.R1", "status": "active"},
            ],
            "excluded": [],
        }) + "\n",
        encoding="utf-8",
    )
    scope_hash = hash_file(scope)

    (feature_active / "trace.md").write_text(
        f"<!-- source: scope.json -->\n<!-- source_hash: {scope_hash} -->\n"
        "| # | Req ID | Scope ID | Requirement | Test(s) | Code Path | Status |\n"
        "| - | ------ | -------- | ----------- | ------- | --------- | ------ |\n"
        "| 1 | d-1.r1 | d-1 | hash file | -- | -- | pending |\n",
        encoding="utf-8",
    )
    tp = feature_active / "test-plan.md"
    tp.write_text(
        f"<!-- source: scope.json -->\n<!-- source_hash: {scope_hash} -->\n"
        "## Strategy\nunit tests only.\n## Cases\n"
        "| Scope | Desc | Tier | Edges | Fixtures |\n"
        "|---|---|---|---|---|\n| d-1 | hash empty file | unit | empty | none |\n",
        encoding="utf-8",
    )
    return tp


def test_prompt_path_resolves_for_test_plan_review():
    p = prompt_path("test-plan-review")
    assert p.exists()
    assert "test-plan-review" in p.name


def test_cascade_has_test_plan_review_between_test_plan_and_build():
    names = [a.name for a in ARTIFACTS]
    i_tp = names.index("test_plan")
    i_ptpr = names.index("panel_test_plan_review")
    i_build = names.index("build")
    assert i_tp < i_ptpr < i_build, f"order wrong: {names}"
    # panel_test_plan_review's upstream must be test_plan.
    ptpr = next(a for a in ARTIFACTS if a.name == "panel_test_plan_review")
    assert ptpr.upstream == ("test_plan",)


def test_cascade_next_stage_is_test_plan_review_after_plan(tmp_path):
    _seed(tmp_path)
    # Seed a passing prd-review so cascade advances past G1. (arch-review
    # merged into prd-review in v3-core update.)
    from autodev.artifacts.verdict import PanelVerdict, write_verdict
    from datetime import datetime, timezone
    for gate, primary in (
        ("prd-review", tmp_path / "prd.md"),
    ):
        v = PanelVerdict(
            gate=gate, verdict="pass", findings=[],
            source=str(primary), source_hash=hash_file(primary),
            prompt_file="x", prompt_hash="sha256:" + "0" * 64,
            harness_version="test", run_ts=datetime.now(timezone.utc).isoformat(),
        )
        write_verdict(tmp_path / f"panel-{gate}.json", v)
    cascade = StalenessCascade(tmp_path)
    # With trace+test_plan present and test-plan-review absent, the next
    # artifact to produce is panel_test_plan_review.
    assert cascade.next_stage() == "panel_test_plan_review"


def test_panel_test_plan_review_passes(git_repo, feature_active, fake_invoker,
                                        panel_config, monkeypatch):
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "reviewers_all_pass")
    tp = _seed(feature_active)
    v = run_panel_gate(
        gate="test-plan-review",
        feature_active=feature_active,
        repo_root=git_repo,
        feature="demo",
        primary_artifact=tp,
        panel_config=panel_config,
    )
    assert v.gate == "test-plan-review"
    assert v.verdict == "pass"
    # Verdict file lands at panel-test-plan-review.json
    out = feature_active / "panel-test-plan-review.json"
    assert out.exists()
    loaded = load_verdict(out)
    assert loaded.gate == "test-plan-review"


def test_panel_test_plan_review_records_consulted_docs(
    git_repo, feature_active, fake_invoker, panel_config, monkeypatch
):
    """R4c-style audit: panel-test-plan-review.docs.json lists scope+trace+prd."""
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "reviewers_all_pass")
    tp = _seed(feature_active)
    run_panel_gate(
        gate="test-plan-review",
        feature_active=feature_active,
        repo_root=git_repo,
        feature="demo",
        primary_artifact=tp,
        panel_config=panel_config,
    )
    docs_path = feature_active / "panel-test-plan-review.docs.json"
    assert docs_path.exists()
    docs = json.loads(docs_path.read_text())
    names = sorted(Path(d["path"]).name for d in docs)
    assert names == ["prd.md", "scope.json", "trace.md"]
    # Each doc records its hash.
    for d in docs:
        assert d["hash"].startswith("sha256:")


def test_panel_test_plan_review_blocks_on_fail(
    git_repo, feature_active, fake_invoker, panel_config, monkeypatch
):
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "reviewers_two_fail")
    tp = _seed(feature_active)
    v = run_panel_gate(
        gate="test-plan-review",
        feature_active=feature_active,
        repo_root=git_repo,
        feature="demo",
        primary_artifact=tp,
        panel_config=panel_config,
    )
    # v3-core R4: verdict = `needs_revision` for invariant/risk findings;
    # `fail` reserved for synthesizer errors.
    assert v.verdict == "needs_revision"
    assert v.effectively_blocks()
    assert v.has_invariant_violation()


def test_verdict_validator_accepts_test_plan_review_gate(tmp_path):
    """_VALID_GATE in verdict.py must include 'test-plan-review'."""
    from autodev.artifacts.verdict import PanelVerdict, write_verdict
    from datetime import datetime, timezone
    tp = tmp_path / "test-plan.md"
    tp.write_text("# tp\n")
    v = PanelVerdict(
        gate="test-plan-review", verdict="pass", findings=[],
        source=str(tp), source_hash=hash_file(tp),
        prompt_file="x", prompt_hash="sha256:" + "0" * 64,
        harness_version="test", run_ts=datetime.now(timezone.utc).isoformat(),
    )
    out = tmp_path / "panel-test-plan-review.json"
    write_verdict(out, v)  # must not raise
    assert out.exists()
