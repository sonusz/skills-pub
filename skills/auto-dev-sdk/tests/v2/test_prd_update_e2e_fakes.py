"""`autodev update --from-file` through the fake-vendor pipeline.

Covers detail doc §7.3: the design-review → PRD-replace → re-dispatch
round trip, using the same fake-vendor wiring as
``test_pipeline_e2e_fakes.py`` (no live LLM calls).

IMPORTANT — a conflict discovered while writing this file (see the long
comment in ``test_prd_update_e2e_fakes_through_design_review``): the shared
fake vendor (``fakes/fake_vendor_cli_auto.py``) writes ``scope.json`` with
an ``in_scope`` list that references ``R1`` only, no matter what
``prd.md`` actually requires. ``autodev/panel/precheck.py``'s reverse-
coverage check (`` PRD requirement(s) ... not referenced in any active
scope item``) is a mechanical, pre-panel gate — it fires regardless of the
reviewer/synthesizer fakes' verdict. The practical result: design-review
can only ever reach ``pass`` while ``prd.md`` has exactly ``R1``; the
moment a second requirement (``R2``) exists, every design-review round is
rejected and the revision loop exhausts ``L_MAX`` and halts
(``GatePending``). Driving the fake pipeline all the way to ``done`` with
a multi-R PRD is therefore not achievable against the current shared
fixture without either editing that fixture (out of this task's scope,
and other tests depend on its current fixed output) or hand-constructing
the design-package artifacts outside the orchestrator (which would no
longer be exercising the fake pipeline at all). Per detail §7.3 step 5
(revised) / core §6.1, this file does not require reaching ``done``: it
calls ``artifacts.prd_checklist.write_prd_checklist`` directly — the
same function the cascade calls to rebuild ``prd-checklist.json`` — and
asserts its req_id set, instead of asserting the unreachable ``done``
state.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from autodev.cli import main as cli_main
from autodev.orchestrator import Orchestrator, OrchestratorConfig, PHASE_STOP_BEFORE
from autodev.state.hashing import hash_file
from autodev.vendors.config import (
    PanelConfig,
    PanelReviewerSpec,
    PanelSynthesizerSpec,
    ProbeConfig,
    STAGES,
    StageSpec,
    VendorsConfig,
)

FAKE_VENDOR = Path(__file__).resolve().parent / "fakes" / "fake_vendor_cli_auto.py"
FAKE_PANEL = Path(__file__).resolve().parent / "fakes" / "fake_panel_invoker.sh"


def _write_prd(git_repo: Path, feature: str = "prd-update-e2e") -> Path:
    """Same shape as ``test_pipeline_e2e_fakes._write_prd`` (six required
    sections + architecture.md + base-ref seeding), single ``R1``
    requirement — the only requirement the shared fake design stage's
    scope.json ever references (see module docstring).
    """
    active = git_repo / "docs" / "features" / feature / "active"
    active.mkdir(parents=True)
    (git_repo / "docs" / "architecture-proposal.md").write_text(
        "# Architecture Proposal\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "docs/architecture-proposal.md"],
        cwd=str(git_repo), check=True,
    )
    subprocess.run(
        ["git", "commit", "-qm", "seed architecture base"],
        cwd=str(git_repo), check=True,
    )
    base_ref = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(git_repo), text=True,
    ).strip()
    (active / "architecture.md").write_text(
        "# Architecture Input\n\n- `docs/architecture-proposal.md`\n\n"
        f"## Base ref\n\n`{base_ref}`\n",
        encoding="utf-8",
    )
    prd = active / "prd.md"
    prd.write_text(
        "# PRD: prd-update-e2e\n\n"
        "## Problem\nSmoke test exercising update-in-place through the "
        "fake pipeline.\n\n"
        "## Users\npytest.\n\n"
        "## Requirements\n"
        "### R1: feature does exactly one thing — pass through.\n\n"
        "## Constraints\nminimal.\n\n"
        "## Success criteria\npipeline runs end-to-end with fakes.\n\n"
        "## Out of scope\nreal vendor calls.\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=str(git_repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"seed {feature} prd"],
                   cwd=str(git_repo), check=True)
    return active


def _write_prd_r123(git_repo: Path, feature: str = "prd-update-e2e-reject") -> Path:
    """Same shape as ``_write_prd`` but seeded directly with three
    requirements (R1/R2/R3) — used by the rejection test, which never
    drives the fake design/design-review pipeline (see that test's
    docstring for why).
    """
    active = git_repo / "docs" / "features" / feature / "active"
    active.mkdir(parents=True)
    (git_repo / "docs" / "architecture-proposal.md").write_text(
        "# Architecture Proposal\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "docs/architecture-proposal.md"],
        cwd=str(git_repo), check=True,
    )
    subprocess.run(
        ["git", "commit", "-qm", "seed architecture base"],
        cwd=str(git_repo), check=True,
    )
    base_ref = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(git_repo), text=True,
    ).strip()
    (active / "architecture.md").write_text(
        "# Architecture Input\n\n- `docs/architecture-proposal.md`\n\n"
        f"## Base ref\n\n`{base_ref}`\n",
        encoding="utf-8",
    )
    prd = active / "prd.md"
    prd.write_text(
        "# PRD: prd-update-e2e-reject\n\n"
        "## Problem\nSmoke test exercising update-in-place through the "
        "fake pipeline.\n\n"
        "## Users\npytest.\n\n"
        "## Requirements\n"
        "### R1: feature does exactly one thing — pass through.\n\n"
        "### R2: original beta requirement text.\n\n"
        "### R3: original gamma requirement text, to be removed.\n\n"
        "## Constraints\nminimal.\n\n"
        "## Success criteria\npipeline runs end-to-end with fakes.\n\n"
        "## Out of scope\nreal vendor calls.\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=str(git_repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"seed {feature} prd"],
                   cwd=str(git_repo), check=True)
    return active


def _vendors_fake_everywhere(repo_root: Path) -> VendorsConfig:
    """All coding stages → claude vendor with the autodetecting fake."""
    return VendorsConfig(
        path=repo_root / "vendors.yml",
        stages={
            s: StageSpec(stage=s, vendor="claude", model="fake-model", probe_interval_sec=30)
            for s in STAGES
        },
        panel=PanelConfig(
            reviewers=(
                PanelReviewerSpec(vendor="claude", model="fake-panel-claude"),
                PanelReviewerSpec(vendor="agy", model="fake-panel-agy"),
                PanelReviewerSpec(vendor="codex", model="fake-panel-codex"),
            ),
            synthesizer=PanelSynthesizerSpec(vendor="claude", model="fake-panel-synth"),
        ),
        probe=ProbeConfig(vendor="claude", model="fake-probe"),
    )


def _prd_text(
    *, feature: str, r1: str = "### R1: feature does exactly one thing — pass through.\n\n",
    r2: str = "", r3: str = "",
) -> str:
    return (
        f"# PRD: {feature}\n\n"
        "## Problem\nSmoke test exercising update-in-place through the "
        "fake pipeline.\n\n"
        "## Users\npytest.\n\n"
        "## Requirements\n"
        f"{r1}{r2}{r3}"
        "## Constraints\nminimal.\n\n"
        "## Success criteria\npipeline runs end-to-end with fakes.\n\n"
        "## Out of scope\nreal vendor calls.\n"
    )


def test_prd_update_e2e_fakes_through_design_review(git_repo, monkeypatch, tmp_path):
    """detail §7.3 steps 1-5: design-review under the fake pipeline,
    replace-in-place dropping R3 and rewording R2, then assert the
    update-history artifacts, the `prd-amended` log detail, the next
    design-stage prompt's iteration-history summary, and (step 5,
    revised) the rebuilt prd-checklist.json's req_id set.

    The initial PRD driven through the fake pipeline carries only R1 (see
    module docstring for why a second requirement makes design-review
    unwinnable with the current shared fake vendor). R2/R3 are added via a
    legitimate, monotonic `update` immediately after design-review passes
    — modeling "the PRD grew after design-review" — so the update actually
    under test (delete R3, reword R2) has real prior content to work from,
    exactly as detail §7.3 describes for that update call itself.
    """
    feature = "prd-update-e2e"
    active = _write_prd(git_repo, feature)

    monkeypatch.setenv("AUTODEV_VENDOR_BIN_CLAUDE", str(FAKE_VENDOR))
    monkeypatch.setenv("AUTODEV_FAKE_FEATURE", feature)
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_INVOKER", str(FAKE_PANEL))
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "reviewers_all_pass")

    from autodev import overrides_api as ov
    ov.record_acknowledge_dirty(active, reason="prd-update e2e", who="pytest")

    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo,
        vendors=_vendors_fake_everywhere(git_repo),
        session_id="prd-update-e2e",
    ))

    # Step 1: run through design + design-review, stop before build.
    orch.run(feature, stop_before=PHASE_STOP_BEFORE["design"])
    verdict = json.loads((active / "panel-design-review.json").read_text(encoding="utf-8"))
    assert verdict["verdict"] == "pass"
    assert not (active / "build.json").exists()

    # Grow the PRD to R1/R2/R3 via a legitimate setup update (monotonic
    # additions, nothing retired yet) so the update under test below has
    # R2/R3 to rework/delete.
    setup_text = _prd_text(
        feature=feature,
        r2="### R2: original beta requirement text.\n\n",
        r3="### R3: original gamma requirement text, to be removed.\n\n",
    )
    setup_src = tmp_path / "setup-prd.md"
    setup_src.write_text(setup_text, encoding="utf-8")
    assert cli_main([
        "update", feature, "--from-file", str(setup_src), "--repo-root", str(git_repo),
    ]) == 0

    # Step 2: the update under test — drop R3, reword R2.
    new_text = _prd_text(
        feature=feature,
        r2="### R2: beta requirement, reworded for the update test.\n"
           "New body text for R2, distinct from the original.\n\n",
    )
    src = tmp_path / "new-prd.md"
    src.write_text(new_text, encoding="utf-8")
    assert cli_main([
        "update", feature, "--from-file", str(src), "--repo-root", str(git_repo),
    ]) == 0

    # Step 3: prd.md no longer contains R3; history dir has snapshot/diff/meta.
    prd_text = (active / "prd.md").read_text(encoding="utf-8")
    assert "R3" not in prd_text

    hist_dir = active / "prd-history"
    assert list(hist_dir.glob("prd.*.md")), "missing PRD snapshot"
    assert list(hist_dir.glob("prd.*.diff")), "missing unified diff"
    assert list(hist_dir.glob("prd.*.json")), "missing update metadata"

    log_lines = (active / "log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    amended = [json.loads(l) for l in log_lines if json.loads(l).get("event") == "prd-amended"]
    assert amended, "no prd-amended event logged"
    detail = amended[-1]["detail"]
    assert detail["removed"] == ["R3"]
    assert detail["changed"] == ["R2"]

    # Step 4: the next design-stage prompt surfaces the change as
    # iteration history — assert directly via the same rendering function
    # the orchestrator uses (the fake vendor does not record prompts it
    # receives).
    from autodev.prompts_loader import render_stage_prompt

    primary_name, extra_names, _ = Orchestrator._STAGE_MANIFEST["design"]
    primary_target = active / primary_name
    extra_targets = [active / n for n in extra_names]
    prompt_text = render_stage_prompt(
        stage="design",
        feature=feature,
        feature_active=active,
        repo_root=git_repo,
        primary_target=primary_target,
        extra_targets=extra_targets,
        preseeded=True,
    )
    assert "Iteration history" in prompt_text
    assert "removed R3" in prompt_text

    # Step 5 (revised, detail §7.3 step 5 / core §6.1): prd-checklist.json's
    # rebuild is just the cascaded call to
    # `artifacts.prd_checklist.write_prd_checklist(active)` — call it
    # directly and assert the checklist's req_id set, rather than driving
    # the fake pipeline to `done` (core does not require that, and this
    # fixture cannot achieve it: the shared fake vendor's static
    # scope.json only ever cites R1, so a multi-R prd.md can never clear
    # `autodev/panel/precheck.py`'s reverse-coverage check).
    from autodev.artifacts.prd_checklist import load_prd_checklist, write_prd_checklist

    checklist_path = write_prd_checklist(active)
    checklist = load_prd_checklist(checklist_path)
    assert [r.req_id for r in checklist.requirements] == ["R1", "R2"]


def test_prd_update_e2e_fakes_rejects_reused_and_reordered_numbers(git_repo, tmp_path):
    """detail §7.3 step 6 / core §6.2: reusing a retired R number, and
    renumbering by giving a kept R the content of a formerly-deleted one,
    must both be rejected (exit 1) with prd.md left byte-identical.

    Pure `autodev update` mechanics — independent of design-review, which
    is why this test does not drive the fake orchestrator (see the module
    docstring for why a multi-requirement PRD cannot clear design-review
    against the shared fake vendor).
    """
    feature = "prd-update-e2e-reject"
    active = _write_prd_r123(git_repo, feature)
    hash_before = hash_file(active / "prd.md")

    # (a) renumber: give R2 the exact content of R3 while dropping R3 —
    # against the ORIGINAL R1/R2/R3 PRD, in one single update call. This
    # must be rejected as a silent renumbering (core §6.2 / detail §7.3.6);
    # prd.md stays byte-identical.
    reorder_text = _prd_text(
        feature=feature,
        r2="### R2: original gamma requirement text, to be removed.\n\n",
    )
    reorder_src = tmp_path / "reorder.md"
    reorder_src.write_text(reorder_text, encoding="utf-8")
    assert cli_main([
        "update", feature, "--from-file", str(reorder_src), "--repo-root", str(git_repo),
    ]) == 1
    assert hash_file(active / "prd.md") == hash_before

    # Now legitimately drop R3 (only), so R3 becomes a retired number.
    legit_text = _prd_text(
        feature=feature,
        r2="### R2: original beta requirement text.\n\n",
    )
    legit_src = tmp_path / "legit.md"
    legit_src.write_text(legit_text, encoding="utf-8")
    assert cli_main([
        "update", feature, "--from-file", str(legit_src), "--repo-root", str(git_repo),
    ]) == 0

    hash_after_legit = hash_file(active / "prd.md")

    # (b) reuse the just-retired R3 number with brand-new content — must
    # be rejected too, and prd.md stays byte-identical to the post-legit
    # state.
    reuse_text = _prd_text(
        feature=feature,
        r2="### R2: original beta requirement text.\n\n",
        r3="### R3: brand-new requirement reusing a retired number.\n\n",
    )
    reuse_src = tmp_path / "reuse.md"
    reuse_src.write_text(reuse_text, encoding="utf-8")
    assert cli_main([
        "update", feature, "--from-file", str(reuse_src), "--repo-root", str(git_repo),
    ]) == 1
    assert hash_file(active / "prd.md") == hash_after_legit
