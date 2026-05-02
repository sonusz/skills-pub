"""Design and build stages receive PRD_PATH/PRD_HASH in prompt context.

Design reads PRD directly. Review already reads PRD. Spec intentionally
does NOT read PRD (context isolation). Build also declares PRD as input.

The assertion surface is `render_stage_prompt`'s rendered context section —
the same channel the vendor subprocess receives.
"""
from __future__ import annotations

from pathlib import Path

import pytest

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


def test_ralph_review_prompt_uses_build_context_without_spec(tmp_path):
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
    # v3-core update: ralph-review is context-isolated — it only gets
    # trace+code. No PRD/scope/build.json/spec — it classifies
    # code-against-trace without knowing "intent".
    assert "TRACE_PATH:" in body
    assert "TARGET_RALPH_REVIEW:" in body
    assert "PRD_PATH:" not in body
    assert "SCOPE_PATH:" not in body
    assert "BUILD_JSON_PATH:" not in body
    assert "SPEC_PATH:" not in body


def test_design_prompt_body_mentions_prd():
    """Prompt-file body mentions PRD as input (not just context section)."""
    body = (
        Path(__file__).resolve().parent.parent.parent
        / "autodev" / "prompts" / "stage-scope.md"
    ).read_text(encoding="utf-8")
    assert "PRD_PATH" in body
    assert "PRD_HASH" in body


def test_build_prompt_body_mentions_prd():
    body = (
        Path(__file__).resolve().parent.parent.parent
        / "autodev" / "prompts" / "stage-implement.md"
    ).read_text(encoding="utf-8")
    assert "PRD_PATH" in body
    assert "PRD_HASH" in body
