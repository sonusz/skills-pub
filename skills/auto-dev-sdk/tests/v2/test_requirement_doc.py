"""R10 — requirement.md enters the feature directory, stage agents read-only.

Detail §3.2 (`cmd_prd --requirement`), §4 (protected names filtered by
existence), core R10 (four stage prompts read `requirement.md`; spec and
ralph-review excluded but still protect it).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from autodev import exit_codes
from autodev.cli import main
from autodev.orchestrator import _STAGE_PROTECTED_NAMES, _stage_write_contract
from autodev.prompts_loader import PROMPTS_DIR, render_stage_prompt
from autodev.state.cascade import StalenessCascade
from autodev.workspace import detect_out_of_scope_writes, snapshot

from .test_cascade_full_chain import _seed_all_ten

STAGE_NAMES = ("design", "build", "ralph-review", "spec", "arch-design", "arch-review")

STAGE_PROMPT_FILE = {
    "arch-design": "stage-arch-design.md",
    "arch-review": "stage-arch-review.md",
    "design": "stage-design.md",
    "build": "stage-implement.md",
}

REQUIREMENT_PARAGRAPH_MARKER = "**Requirement (read-only, optional).**"


# ---------------------------------------------------------------------------
# `autodev prd --requirement`
# ---------------------------------------------------------------------------


def test_requirement_alone_lands_in_planned_dir_for_new_feature(git_repo, tmp_path):
    req = tmp_path / "requirement-src.md"
    req.write_text("# Core requirement\n\nDo the thing.\n", encoding="utf-8")

    code = main([
        "prd", "demo", "--requirement", str(req), "--repo-root", str(git_repo),
    ])

    assert code == exit_codes.OK
    target = git_repo / "docs" / "features" / "demo" / "planned" / "requirement.md"
    assert target.exists()
    assert target.read_bytes() == req.read_bytes()
    # prd.md was never touched by --requirement alone.
    assert not (target.parent / "prd.md").exists()


def test_requirement_with_from_file_lands_next_to_prd(git_repo, tmp_path):
    prd_src = tmp_path / "draft.md"
    prd_src.write_text(
        "# PRD\n"
        "## Problem\np\n## Users\nu\n"
        "## Requirements\n### R1: do a thing\n"
        "## Constraints\nc\n"
        "## Success criteria\ns\n"
        "## Out of scope\no\n"
    )
    req_src = tmp_path / "requirement-src.md"
    req_src.write_text("# Core requirement\n\nDo the thing.\n", encoding="utf-8")

    code = main([
        "prd", "demo",
        "--from-file", str(prd_src),
        "--requirement", str(req_src),
        "--repo-root", str(git_repo),
    ])

    assert code == exit_codes.OK
    target_dir = git_repo / "docs" / "features" / "demo" / "planned"
    assert (target_dir / "prd.md").read_bytes() == prd_src.read_bytes()
    assert (target_dir / "requirement.md").read_bytes() == req_src.read_bytes()


def test_requirement_alone_lands_in_active_dir_for_active_feature(
    git_repo, feature_active, tmp_path,
):
    req = tmp_path / "requirement-src.md"
    req.write_text("# Core requirement\n\nActive feature update.\n", encoding="utf-8")

    code = main([
        "prd", "demo", "--requirement", str(req), "--repo-root", str(git_repo),
    ])

    assert code == exit_codes.OK
    target = feature_active / "requirement.md"
    assert target.exists()
    assert target.read_bytes() == req.read_bytes()


def test_requirement_import_emits_log_event(git_repo, tmp_path):
    req = tmp_path / "requirement-src.md"
    req.write_text("# Core requirement\n", encoding="utf-8")

    code = main([
        "prd", "demo", "--requirement", str(req), "--repo-root", str(git_repo),
    ])
    assert code == exit_codes.OK

    log_path = git_repo / "docs" / "features" / "demo" / "planned" / "log.jsonl"
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    events = [r for r in lines if r["event"] == "requirement-imported"]
    assert len(events) == 1
    record = events[0]
    assert record["stage"] == "orchestrator"
    assert record["detail"]["source"] == str(req)
    import hashlib
    assert record["detail"]["sha256"] == "sha256:" + hashlib.sha256(req.read_bytes()).hexdigest()


def test_requirement_neither_flag_errors(git_repo, capsys):
    code = main(["prd", "demo", "--repo-root", str(git_repo)])
    assert code == exit_codes.ERROR
    out = capsys.readouterr().out
    assert "use --from-file" in out


def test_missing_requirement_source_blocks_before_prd_write(git_repo, tmp_path, capsys):
    """C-stage review pin (detail §3.2 / §10): a missing --requirement source
    must be caught before the --from-file import writes prd.md, so the
    feature directory is left untouched rather than half-imported."""
    prd_src = tmp_path / "draft.md"
    prd_src.write_text(
        "# PRD\n"
        "## Problem\np\n## Users\nu\n"
        "## Requirements\n### R1: do a thing\n"
        "## Constraints\nc\n"
        "## Success criteria\ns\n"
        "## Out of scope\no\n"
    )
    missing_req = tmp_path / "does-not-exist.md"

    code = main([
        "prd", "demo",
        "--from-file", str(prd_src),
        "--requirement", str(missing_req),
        "--repo-root", str(git_repo),
    ])

    assert code == exit_codes.ERROR
    err = capsys.readouterr().err
    assert f"requirement source not found: {missing_req}" in err
    target_dir = git_repo / "docs" / "features" / "demo" / "planned"
    assert not (target_dir / "prd.md").exists()


# ---------------------------------------------------------------------------
# Staleness cascade: requirement.md is not a cascade node.
# ---------------------------------------------------------------------------


def test_cascade_fresh_unchanged_by_requirement_edits(feature_active):
    """requirement.md must not be a cascade node (core R10): creating or
    editing it must leave StalenessCascade.fresh() byte-for-byte
    identical. Uses _seed_all_ten (the same fully-seeded, all-fresh
    baseline the pipeline tests use) rather than a bare prd.md so the
    baseline actually has fresh nodes to potentially disturb -- a
    baseline where nothing is fresh yet would make this comparison pass
    vacuously even if requirement.md WERE wired into the cascade."""
    _seed_all_ten(feature_active)
    before = StalenessCascade(feature_active).fresh()
    assert any(before.values()), "baseline must have at least one fresh node"

    (feature_active / "requirement.md").write_text("# Requirement\n", encoding="utf-8")
    after_create = StalenessCascade(feature_active).fresh()
    assert after_create == before

    (feature_active / "requirement.md").write_text("# Requirement v2\n", encoding="utf-8")
    after_edit = StalenessCascade(feature_active).fresh()
    assert after_edit == before


# ---------------------------------------------------------------------------
# `_stage_write_contract`: requirement.md filtered by existence.
# ---------------------------------------------------------------------------


_STAGE_TARGETS = {
    "design": (
        lambda active: active / "design.md",
        lambda active: [active / "scope.json", active / "trace.md", active / "test-plan.md"],
    ),
    "build": (lambda active: active / "build.json", lambda active: []),
    "ralph-review": (lambda active: active / "ralph-review.json", lambda active: []),
    "spec": (lambda active: active / "implemented-spec.md", lambda active: []),
    "arch-design": (lambda active: active / "arch-design.md", lambda active: []),
    "arch-review": (lambda active: active / "arch-review.json", lambda active: []),
}


@pytest.mark.parametrize("stage", STAGE_NAMES)
def test_stage_protected_names_declares_requirement(stage):
    assert "requirement.md" in _STAGE_PROTECTED_NAMES[stage]


@pytest.mark.parametrize("stage", STAGE_NAMES)
def test_stage_write_contract_filters_requirement_by_existence(tmp_path, stage):
    repo_root = tmp_path
    active = repo_root / "docs" / "features" / "t" / "active"
    active.mkdir(parents=True)
    primary_fn, extra_fn = _STAGE_TARGETS[stage]
    primary = primary_fn(active)
    extra = extra_fn(active)

    _writable_absent, protected_absent = _stage_write_contract(
        repo_root=repo_root, active=active, stage=stage,
        primary_target=primary, extra_targets=extra,
    )
    assert active / "requirement.md" not in protected_absent

    (active / "requirement.md").write_text("# Requirement\n", encoding="utf-8")
    _writable_present, protected_present = _stage_write_contract(
        repo_root=repo_root, active=active, stage=stage,
        primary_target=primary, extra_targets=extra,
    )
    assert active / "requirement.md" in protected_present

    # The rest of the list is identical in both cases.
    rest_absent = [p for p in protected_absent if p.name != "requirement.md"]
    rest_present = [p for p in protected_present if p.name != "requirement.md"]
    assert rest_absent == rest_present


def test_stage_subprocess_write_to_requirement_is_rejected(git_repo):
    active = git_repo / "docs" / "features" / "demo" / "active"
    active.mkdir(parents=True)
    requirement = active / "requirement.md"
    requirement.write_text("original", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "baseline"], cwd=git_repo, check=True,
    )
    before = snapshot(git_repo)

    requirement.write_text("mutated by stage subprocess", encoding="utf-8")
    after = snapshot(git_repo)

    # Derive protected_scope from the contract itself (with requirement.md
    # present) rather than hand-building [requirement], so this test is
    # linked to the same contract detect_out_of_scope_writes is checked
    # against elsewhere (C-stage review pin, §10 "C 期复审 PASS 后 pin").
    _writable, protected_scope = _stage_write_contract(
        repo_root=git_repo, active=active, stage="build",
        primary_target=active / "build.json", extra_targets=[],
    )
    assert requirement in protected_scope

    escapes = detect_out_of_scope_writes(
        before, after,
        allowed_scope=[active],
        protected_scope=protected_scope,
        repo_root=git_repo,
    )

    assert any("requirement.md" in entry for entry in escapes)


# ---------------------------------------------------------------------------
# Prompts: four stages get the paragraph, spec and ralph-review do not.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage", sorted(STAGE_PROMPT_FILE))
def test_stage_prompt_contains_requirement_paragraph(stage):
    text = (PROMPTS_DIR / STAGE_PROMPT_FILE[stage]).read_text(encoding="utf-8")
    assert REQUIREMENT_PARAGRAPH_MARKER in text
    assert "<FEATURE_ACTIVE>/requirement.md" in text
    assert "Never modify this file." in text


@pytest.mark.parametrize("prompt_file", ["stage-spec.md", "stage-ralph-review.md"])
def test_excluded_prompt_does_not_contain_requirement_paragraph(prompt_file):
    text = (PROMPTS_DIR / prompt_file).read_text(encoding="utf-8")
    assert REQUIREMENT_PARAGRAPH_MARKER not in text
    assert "requirement.md" not in text


def test_only_panel_prompts_are_hash_pinned():
    """Confirms detail §4's claim: test_prompt_change_control.py pins only
    the four panel prompts (autodev/panel/prompts/), never the stage
    prompts under autodev/prompts/ — so the R10 prompt edits need no hash
    update."""
    control_text = (
        Path(__file__).resolve().parent / "test_prompt_change_control.py"
    ).read_text(encoding="utf-8")
    assert "autodev/panel/prompts" in control_text
    assert "stage-design.md" not in control_text
    assert "stage-arch-design.md" not in control_text
    assert "stage-implement.md" not in control_text
    assert "stage-ralph-review.md" not in control_text
    assert "stage-arch-review.md" not in control_text


# ---------------------------------------------------------------------------
# Rendered prompt: requirement.md shows up in PROTECTED_PATHS when present.
# ---------------------------------------------------------------------------


def test_rendered_design_prompt_lists_requirement_in_protected_paths_when_present(tmp_path):
    repo_root = tmp_path
    active = repo_root / "docs" / "features" / "t" / "active"
    active.mkdir(parents=True)
    (active / "prd.md").write_text("# prd\n", encoding="utf-8")
    (active / "arch-design.md").write_text(
        "<!-- source: x -->\n<!-- source_hash: sha256:0 -->\n", encoding="utf-8",
    )

    design_targets = [
        active / "scope.json", active / "trace.md", active / "test-plan.md",
    ]

    def _render():
        _writable, protected = _stage_write_contract(
            repo_root=repo_root, active=active, stage="design",
            primary_target=active / "design.md", extra_targets=design_targets,
        )
        return render_stage_prompt(
            stage="design",
            feature="t",
            feature_active=active,
            repo_root=repo_root,
            primary_target=active / "design.md",
            extra_targets=design_targets,
            protected_paths=protected,
        )

    body_absent = _render()
    assert str((active / "requirement.md").resolve()) not in body_absent

    (active / "requirement.md").write_text("# requirement\n", encoding="utf-8")
    body_present = _render()
    assert "- PROTECTED_PATHS:" in body_present
    assert f"`{(active / 'requirement.md').resolve()}`" in body_present
