"""Design and build stages receive PRD_PATH/PRD_HASH in prompt context.

Design reads PRD directly. Review already reads PRD. Spec intentionally
does NOT read PRD (context isolation). Build also declares PRD as input.

The assertion surface is `render_stage_prompt`'s rendered context section —
the same channel the vendor subprocess receives.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from autodev.orchestrator import _protect_context_inputs, _stage_write_contract
from autodev.prompts_loader import render_stage_prompt


def _seed(feature_active: Path) -> None:
    feature_active.mkdir(parents=True, exist_ok=True)
    (feature_active / "prd.md").write_text("# prd\n", encoding="utf-8")
    (feature_active / "design.md").write_text(
        "<!-- source: x -->\n<!-- source_hash: sha256:0 -->\n",
        encoding="utf-8",
    )
    (feature_active / "scope.json").write_text(
        '{"source":"x","source_hash":"sha256:0","written":"2026-04-20",'
        '"feature":"t","mode":"fresh","diff_base":"main",'
        '"in_scope":[],"excluded":[]}\n',
        encoding="utf-8",
    )
    (feature_active / "trace.md").write_text(
        "<!-- source: x -->\n<!-- source_hash: sha256:0 -->\n", encoding="utf-8"
    )
    (feature_active / "test-plan.md").write_text(
        "<!-- source: x -->\n<!-- source_hash: sha256:0 -->\n", encoding="utf-8"
    )
    (feature_active / "design-packet.json").write_text(
        '{"kind":"design-packet"}\n', encoding="utf-8"
    )
    (feature_active / "accepted-design.json").write_text(
        '{"kind":"accepted-design"}\n', encoding="utf-8"
    )
    (feature_active / "build.json").write_text(
        '{"source":"x","source_hash":"sha256:0","written":"2026-04-20",'
        '"test_cmd_run":"pytest","test_exit_code":0,'
        '"test_results":{"passed":1,"failed":0,"skipped":0},'
        '"files_changed":[],"lint":{"passed":true,"cmd":"n/a"},'
        '"deviations":[],"blocking":false,'
        '"workspace_dirty_at_stage_end":false}\n',
        encoding="utf-8",
    )
    (feature_active / "implementation-index.json").write_text(
        '{"source":"build.json","source_hash":"sha256:0",'
        '"written":"2026-04-20","kind":"implementation-index",'
        '"sealed_ref":null,"files_changed":["x.py"],'
        '"test_cmd_run":"pytest","test_exit_code":0,'
        '"test_results":{"passed":1,"failed":0,"skipped":0}}\n',
        encoding="utf-8",
    )
    (feature_active / "implemented-spec.md").write_text("# spec\n", encoding="utf-8")


def _render(stage: str, feature_active: Path, repo_root: Path) -> str:
    targets = {
        "design": (
            feature_active / "design.md",
            [
                feature_active / "scope.json",
                feature_active / "trace.md",
                feature_active / "test-plan.md",
            ],
        ),
        "build":  (feature_active / "build.json", []),
        "spec":   (feature_active / "implemented-spec.md", [feature_active / "README.md"]),
        "review": (feature_active / "review.json", []),
    }[stage]
    return render_stage_prompt(
        stage=stage,
        feature="t",
        feature_active=feature_active,
        repo_root=repo_root,
        primary_target=targets[0],
        extra_targets=targets[1],
    )


def test_design_stage_prompt_declares_prd_input(tmp_path):
    active = tmp_path / "active"
    _seed(active)
    body = _render("design", active, tmp_path)
    assert "PRD_PATH:" in body
    assert "PRD_HASH:" in body
    assert "TARGET_DESIGN:" in body
    assert "TARGET_SCOPE:" in body
    assert "TARGET_TRACE:" in body
    assert "TARGET_TEST_PLAN:" in body
    assert str(active / "prd.md") in body


def test_build_stage_prompt_declares_prd_input(tmp_path):
    active = tmp_path / "active"
    _seed(active)
    body = _render("build", active, tmp_path)
    assert "PRD_PATH:" in body
    assert "PRD_HASH:" in body
    assert "SCOPE_PATH:" in body
    assert "TRACE_PATH:" in body
    assert "TEST_PLAN_PATH:" in body


def test_spec_stage_does_not_see_prd(tmp_path):
    """Spec is intentionally PRD/scope/build-free (context isolation)."""
    active = tmp_path / "active"
    _seed(active)
    body = _render("spec", active, tmp_path)
    assert "PRD_PATH:" not in body
    assert "PRD_HASH:" not in body
    assert "SCOPE_PATH:" not in body
    assert "BUILD_JSON_PATH:" not in body
    assert "IMPLEMENTATION_INDEX_PATH:" in body
    assert "IMPLEMENTATION_INDEX_HASH:" in body


def test_ralph_review_prompt_gets_accepted_design_without_prd_or_build(tmp_path):
    active = tmp_path / "active"
    _seed(active)
    body = render_stage_prompt(
        stage="ralph-review",
        feature="t",
        feature_active=active,
        repo_root=tmp_path,
        primary_target=active / "ralph-review.json",
        extra_targets=[],
    )
    # Ralph independently checks implementation against trace + accepted
    # design, while remaining isolated from PRD/build/spec narration.
    assert "DESIGN_PACKET_PATH:" in body
    assert "ACCEPTED_DESIGN_PATH:" in body
    assert "DESIGN_PATH:" in body
    assert "SCOPE_PATH:" in body
    assert "TRACE_PATH:" in body
    assert "TARGET_RALPH_REVIEW:" in body
    assert "PRD_PATH:" not in body
    assert "BUILD_JSON_PATH:" not in body
    assert "SPEC_PATH:" not in body


def test_ralph_review_prompt_links_diff_context_and_previous_output(tmp_path):
    active = tmp_path / "active"
    _seed(active)
    iteration_context = active / "ralph-iteration-context.json"
    previous_review = active / "ralph-review.previous.json"
    iteration_context.write_text('{"before_ref":"aaa","after_ref":"bbb"}\n')
    previous_review.write_text('{"classifications":[]}\n')

    body = render_stage_prompt(
        stage="ralph-review",
        feature="t",
        feature_active=active,
        repo_root=tmp_path,
        primary_target=active / "ralph-review.json",
        extra_targets=[],
        invocation_bindings={
            "RALPH_ITERATION_CONTEXT_PATH": iteration_context,
            "BUILD_BEFORE_REF": "aaa",
            "BUILD_AFTER_REF": "bbb",
            "PREVIOUS_RALPH_REVIEW_PATH": previous_review,
        },
    )

    assert f"- RALPH_ITERATION_CONTEXT_PATH: `{iteration_context}`" in body
    assert "- BUILD_BEFORE_REF: `aaa`" in body
    assert "- BUILD_AFTER_REF: `bbb`" in body
    assert f"- PREVIOUS_RALPH_REVIEW_PATH: `{previous_review}`" in body
    # Bindings are paths/pointers only; linked file bodies are not inlined.
    assert '{"before_ref":"aaa","after_ref":"bbb"}' not in body
    assert '{"classifications":[]}' not in body


def test_stage_write_contract_is_stage_specific(tmp_path):
    active = tmp_path / "docs" / "features" / "t" / "active"
    active.mkdir(parents=True)

    design_targets = [
        active / "design.md",
        active / "scope.json",
        active / "trace.md",
        active / "test-plan.md",
        active / "design-changelog.json",
    ]
    writable, protected = _stage_write_contract(
        repo_root=tmp_path,
        active=active,
        stage="design",
        primary_target=design_targets[0],
        extra_targets=design_targets[1:],
    )
    assert set(writable) == {*design_targets, active / "scratch"}
    assert active not in writable
    assert active / "prd.md" in protected

    build_writable, build_protected = _stage_write_contract(
        repo_root=tmp_path,
        active=active,
        stage="build",
        primary_target=active / "build.json",
        extra_targets=[],
    )
    assert build_writable == [tmp_path]
    assert active / "prd.md" in build_protected
    assert active / "design.md" in build_protected
    assert active / "build.json" not in build_protected

    review_writable, review_protected = _stage_write_contract(
        repo_root=tmp_path,
        active=active,
        stage="ralph-review",
        primary_target=active / "ralph-review.json",
        extra_targets=[],
    )
    assert review_writable == [active / "ralph-review.json", active / "scratch"]
    assert active / "design-packet.json" in review_protected
    assert active / "accepted-design.json" in review_protected
    assert active / "design.md" in review_protected
    assert active / "scope.json" in review_protected
    assert active / "trace.md" in review_protected


def test_build_context_inputs_are_dynamically_protected(tmp_path):
    active = tmp_path / "docs" / "features" / "t" / "active"
    own_output = active / "build.json"
    close_verdict = active / "panel-close-approval.json"
    ralph_review = active / "ralph-review.json"
    ralph_state = active / "ralph-state.json"

    protected = _protect_context_inputs(
        [active / "prd.md"],
        context_artifacts=[
            str(own_output),
            str(close_verdict),
            str(ralph_review),
            str(ralph_state),
        ],
        owned_outputs=[own_output],
    )

    assert own_output not in protected
    assert close_verdict in protected
    assert ralph_review in protected
    assert ralph_state in protected


def test_rendered_initial_and_resume_prompts_repeat_write_contract(tmp_path):
    active = tmp_path / "active"
    _seed(active)
    target = active / "build.json"
    writable = [tmp_path]
    protected = [active / "prd.md", active / "design.md"]

    for continuation in (False, True):
        body = render_stage_prompt(
            stage="build",
            feature="t",
            feature_active=active,
            repo_root=tmp_path,
            primary_target=target,
            extra_targets=[],
            writable_paths=writable,
            protected_paths=protected,
            continuation=continuation,
        )
        assert "- WRITABLE_PATHS:" in body
        assert f"  - `{tmp_path.resolve()}`" in body
        assert "- PROTECTED_PATHS:" in body
        assert f"  - `{(active / 'prd.md').resolve()}`" in body
        assert "PROTECTED_PATHS stay read-only" in body


def test_design_prompt_body_mentions_prd():
    """Prompt-file body mentions PRD as input (not just context section)."""
    body = (
        Path(__file__).resolve().parent.parent.parent
        / "autodev" / "prompts" / "stage-design.md"
    ).read_text(encoding="utf-8")
    assert "PRD_PATH" in body
    assert "PRD_HASH" in body
    assert "WRITABLE_PATHS" in body
    assert "PROTECTED_PATHS" in body


def test_fresh_design_prompt_omits_old_iteration_history(
    feature_active, git_repo,
):
    (feature_active / "prd.md").write_text("# prd\n", encoding="utf-8")
    (feature_active / "log.jsonl").write_text(
        '{"ts":"2026-08-09T00:00:00+00:00","stage":"design",'
        '"event":"stage-complete","feature":"t","detail":{}}\n',
        encoding="utf-8",
    )

    body = render_stage_prompt(
        stage="design",
        feature="t",
        feature_active=feature_active,
        repo_root=git_repo,
        primary_target=feature_active / "design.md",
        extra_targets=[
            feature_active / "scope.json",
            feature_active / "trace.md",
            feature_active / "test-plan.md",
            feature_active / "design-changelog.json",
        ],
        context_artifacts=[],
        preseeded=False,
    )

    assert "CONTEXT_ARTIFACTS: [] (initial run)" in body
    assert "## Iteration history" not in body


def test_build_prompt_body_mentions_prd():
    body = (
        Path(__file__).resolve().parent.parent.parent
        / "autodev" / "prompts" / "stage-implement.md"
    ).read_text(encoding="utf-8")
    assert "PRD_PATH" in body
    assert "PRD_HASH" in body
    assert "WRITABLE_PATHS" in body
    assert "PROTECTED_PATHS" in body
    assert "as one work queue" in body
    assert "do not impose an arbitrary one-scope" in body
    assert "do not defer" in body
    assert "subagents are available" in body
    assert "bounded, non-overlapping\n  assignments" in body
    assert "Implement the accepted design package\ndirectly" in body
    assert "The accepted design package and its reviewed trace/test plan are the\n" in body
    assert "then begin code and test work" in body
    assert "PLAN_REVIEW_BLOCKERS" not in body
    assert "plan-review loop" not in body
    assert "write a concise plan" not in body
    assert "blocking-deviation path" in body
    assert "Mandatory iteration sizing" in body
    assert "If the whole runnable queue can fit" in body
    assert "largest coherent objective" in body
    assert "it need not\n  complete an entire scope" in body
    assert "verifiable forward status\n  delta for at least one scope" in body
    assert "Missing toward Partial" in body
    assert "Partial toward\n  Fully" in body
    assert "It may\n  advance one scope or several scopes" in body
    assert "you MUST use\n  them concurrently" in body
    assert "Derive each bounded, self-contained\n  brief directly from the accepted design" in body
    assert "fully completed iteration\nobjective" in body
    assert "does not\nrequire the affected scope to reach Fully" in body
    assert "Work outside the objective remains in the queue" in body
    assert "sole reason no remaining active work can advance" in body
    assert "repeat that inventory over every\nunfinished active row" in body
    assert "A failed credential check alone is not proof" in body
    assert "including work\n   awaiting external runtime" in body
    assert "Name\n  the remaining rows and why each lacks a local implementation path" in body
    assert "design_conformance.findings" in body
    assert "replace the differing implementation method" in body
    assert "Never use `git add -A`, `git add .`" in body
    assert "Run every commit synchronously" in body
    assert "Never leave a commit or hook running\n  in the background" in body


def test_ralph_prompt_allows_parallel_subagent_review():
    body = (
        Path(__file__).resolve().parent.parent.parent
        / "autodev" / "prompts" / "stage-ralph-review.md"
    ).read_text(encoding="utf-8")
    assert "use subagents on disjoint rows or changed files" in body
    assert "disjoint rows or changed files" in body
    assert "one complete" in body
    assert "have one subagent\nreview the plan" in body
    assert "accepted design" in body
    assert "design_conformance" in body
    assert "If the accepted design specifies a method and Dev implemented a different one" in body
    assert "WRITABLE_PATHS" in body
    assert "PROTECTED_PATHS" in body
