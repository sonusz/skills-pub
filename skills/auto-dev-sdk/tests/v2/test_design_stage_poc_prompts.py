"""Design-stage POC: pins the new prompt/reference sections exist.

The design-stage POC flow (run a bounded proof-of-concept for a high-risk
question the design agent cannot resolve from code/docs/measurements) was
previously only documented ad hoc, inside one project's PRD. This makes it
a first-class, documented part of auto-dev-sdk: a PRD clause to paste
(references/prd-authoring.md), a rule for the design stage that runs it
(autodev/prompts/stage-design.md), and a gate question for the design-review
panel that grades it (autodev/panel/prompts/review-design-review.md).

These are simple substring pins, matching the style of
test_v3core_misc.py's stage-design.md vocabulary tests. They are not a
content audit — just a tripwire so the sections cannot silently disappear.
"""
from __future__ import annotations

from pathlib import Path

SDK_ROOT = Path(__file__).resolve().parent.parent.parent
STAGE_PROMPTS_DIR = SDK_ROOT / "autodev" / "prompts"
PANEL_PROMPTS_DIR = SDK_ROOT / "autodev" / "panel" / "prompts"
REFERENCES_DIR = SDK_ROOT / "references"


def _read(path: Path) -> str:
    assert path.exists(), f"missing file: {path}"
    return path.read_text(encoding="utf-8")


def test_stage_design_has_poc_rule_block():
    text = _read(STAGE_PROMPTS_DIR / "stage-design.md")
    assert "## Design-stage POC" in text
    # Tool-permission claim must be phrased as verified, not assumed.
    assert "Verified: the harness dispatches every" in text
    assert "run_stage_subprocess" in text
    assert "yolo=True" in text
    # The credentials/fixable-obstacle rule, and the failure it prevents.
    assert "fixable obstacle" in text
    assert "the build stage's first blocking step" in text
    # The record location must match the stage's real write containment
    # (five owned artifacts + scratch — see _stage_write_contract), not an
    # arbitrary path under FEATURE_ACTIVE.
    assert "<FEATURE_ACTIVE>/scratch/poc-<slug>/" in text
    assert "out-of-scope write" in text


def test_stage_design_poc_rule_covers_record_layout():
    text = _read(STAGE_PROMPTS_DIR / "stage-design.md")
    for token in (
        "identity gate",
        "resource manifest",
        "CONFIRMED / REFUTED / INCONCLUSIVE",
        "teardown",
    ):
        assert token in text, f"stage-design.md POC rule missing {token!r}"


def test_design_review_has_poc_gate_question():
    text = _read(PANEL_PROMPTS_DIR / "review-design-review.md")
    assert "## Design-stage POC (gate question)" in text
    assert "invariant_violation` / `P0`" in text
    assert "conditional promise" in text
    assert "deferred to build" in text


def test_design_review_poc_finding_uses_existing_category_enum():
    """The panel finding schema's category enum is closed (schemas.py) and
    this task must not add a new machine token to it — the POC gate
    question should reuse an existing category (missing/other) rather than
    inventing e.g. "poc-deferred"."""
    text = _read(PANEL_PROMPTS_DIR / "review-design-review.md")
    assert "category `missing`" in text
    assert "or `other`" in text


def test_stage_design_poc_block_does_not_swallow_provenance_header():
    """The POC block must sit after Provenance header, not before it — the
    design subprocess's cwd is the target repo (orchestrator.py resolves
    self.cfg.repo_root there), so Provenance must read as a subsection of
    `## Task`, not get nested under `## Design-stage POC`."""
    text = _read(STAGE_PROMPTS_DIR / "stage-design.md")
    provenance_idx = text.index("### Provenance header (all four files)")
    poc_idx = text.index("## Design-stage POC")
    format_idx = text.index("## Format requirements")
    assert provenance_idx < poc_idx < format_idx


def test_stage_design_poc_manifest_bullet_mentions_tagging():
    text = _read(STAGE_PROMPTS_DIR / "stage-design.md")
    assert "uniquely tagged to this POC" in text


def test_neither_prompt_cites_a_target_repo_relative_references_path():
    """The design subprocess and the panel reviewers run with cwd set to the
    TARGET repo, not the skill root, so `references/prd-authoring.md` does
    not exist there. Neither prompt may cite that path."""
    for path in (
        STAGE_PROMPTS_DIR / "stage-design.md",
        PANEL_PROMPTS_DIR / "review-design-review.md",
    ):
        text = _read(path)
        assert "references/prd-authoring.md" not in text, (
            f"{path.name}: dangling references/ path — not readable from "
            "the target repo cwd"
        )


def test_design_review_poc_exception_does_not_excuse_a_build_handoff():
    """The exception for a genuine failed attempt must excuse only the
    missing *result*, never wording that hands the POC to build — the
    precedent failure this whole feature guards against was itself a
    genuine, logged NoCredentials attempt."""
    text = _read(PANEL_PROMPTS_DIR / "review-design-review.md")
    assert "excuses the missing" in text
    assert "never a handoff" in text
    assert "escalate as a normal design gap" not in text


def test_prd_authoring_has_design_stage_poc_clause():
    text = _read(REFERENCES_DIR / "prd-authoring.md")
    assert "## Design-stage POC clause" in text
    assert "the repository's designated non-production account" in text
    assert "### Evidence policy" in text
    assert "is evidence" in text
    assert "### Interaction with the rigor table" in text
    # The paste-ready clause itself must state the identity gate, not just
    # the itemized "what the clause requires" list below it.
    assert "verify the resolved account identity at the start of every phase" in text


def test_skill_md_references_the_poc_clause():
    text = (SDK_ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert "design-stage POC clause" in text
