"""v3-core R6 (cascade order), R7 (Source: tags), SC3 (prompt-shape verification)."""
from __future__ import annotations

from pathlib import Path

import pytest

from autodev.state.cascade import ARTIFACTS


# ---------- R6: cascade order ----------

def test_design_runs_before_panel_design_review():
    """v3-core R6: design must appear before panel_design_review in ARTIFACTS."""
    names = [a.name for a in ARTIFACTS]
    assert names.index("design") < names.index("design_packet")
    assert names.index("design_packet") < names.index("panel_design_review")
    assert names.index("panel_design_review") < names.index("accepted_design")


def test_panel_design_review_depends_on_design_packet():
    """R6: panel_design_review should list the full design packet as upstream."""
    rec = next(a for a in ARTIFACTS if a.name == "panel_design_review")
    assert rec.upstream == ("design_packet", "prd")


# ---------- R7: stage prompts declare Source: attribution ----------

PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "autodev" / "prompts"


def test_stage_design_prompt_declares_source_vocabulary():
    text = (PROMPTS_DIR / "stage-design.md").read_text(encoding="utf-8")
    assert "Source:" in text or "`Source`" in text
    # R7 vocabulary tags
    for tag in ("prd:<section>", "scope:<id>", "trace:<req-id>",
                "inferred", "commonsense"):
        assert tag in text, f"stage-design.md missing vocabulary tag {tag!r}"


def test_stage_spec_prompt_declares_source_vocabulary():
    text = (PROMPTS_DIR / "stage-spec.md").read_text(encoding="utf-8")
    for tag in ("code:<path:line>", "test:<path:line>", "index:<field>",
                "inferred", "commonsense"):
        assert tag in text, f"stage-spec.md missing vocabulary tag {tag!r}"


def test_stage_design_mentions_source_column():
    text = (PROMPTS_DIR / "stage-design.md").read_text(encoding="utf-8")
    # Column name + attribution-unit language appear somewhere.
    assert "Source" in text


def test_stage_design_prompt_uses_changelog_for_design_history():
    text = (PROMPTS_DIR / "stage-design.md").read_text(encoding="utf-8")
    assert "design-changelog.json" in text
    assert "design-rework-memory.json" not in text


def test_initial_design_is_a_true_rebaseline():
    text = (PROMPTS_DIR / "stage-design.md").read_text(encoding="utf-8")
    assert "On an initial run (`CONTEXT_ARTIFACTS: []`)" in text
    assert "Do not inspect `design-package-history`" in text
    assert "preserve old `ra-*` IDs" in text
    assert "not make a component required when the PRD does not require it" in text


# ---------- SC3: panel prompts have no "Do NOT flag" blocklists ----------

PANEL_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "autodev" / "panel" / "prompts"
REVIEW_PROMPTS = (
    "review-design-review.md",
    "review-close-approval.md",
)


@pytest.mark.parametrize("name", REVIEW_PROMPTS)
def test_no_do_not_flag_blocklist(name):
    text = (PANEL_PROMPTS_DIR / name).read_text(encoding="utf-8")
    assert "Do NOT flag" not in text, (
        f"{name}: remove 'Do NOT flag' blocklists (R2)"
    )


@pytest.mark.parametrize("name", REVIEW_PROMPTS)
def test_no_clarification_prefix_templating(name):
    text = (PANEL_PROMPTS_DIR / name).read_text(encoding="utf-8")
    assert "CLARIFICATION:" not in text, (
        f"{name}: remove dialogue-mode CLARIFICATION templating (R2)"
    )


@pytest.mark.parametrize("name", REVIEW_PROMPTS)
def test_no_best_guess_templating(name):
    text = (PANEL_PROMPTS_DIR / name).read_text(encoding="utf-8")
    assert "Best guess" not in text and "best guess" not in text, (
        f"{name}: remove dialogue-mode best-guess templating (R2)"
    )


@pytest.mark.parametrize("name", REVIEW_PROMPTS)
def test_prompt_mentions_targets_instruction(name):
    text = (PANEL_PROMPTS_DIR / name).read_text(encoding="utf-8")
    assert "targets" in text.lower(), (
        f"{name}: missing targets emission instruction (R2)"
    )


@pytest.mark.parametrize("name", REVIEW_PROMPTS)
def test_prompt_mentions_primary_pair_structure(name):
    text = (PANEL_PROMPTS_DIR / name).read_text(encoding="utf-8")
    assert "Primary pair" in text or "primary_pair" in text


@pytest.mark.parametrize("name", [
    # design-review after merge has no single anchor; close still does.
    # only gates that actually have anchors assert this.
    "review-close-approval.md",
])
def test_prompt_mentions_anchor_structure(name):
    text = (PANEL_PROMPTS_DIR / name).read_text(encoding="utf-8")
    assert "Anchor" in text or "anchor" in text


@pytest.mark.parametrize("name", REVIEW_PROMPTS)
def test_prompt_mentions_severity_taxonomy(name):
    text = (PANEL_PROMPTS_DIR / name).read_text(encoding="utf-8")
    for sev in ("invariant_violation", "risk", "opinion"):
        assert sev in text, f"{name}: missing severity {sev!r}"


@pytest.mark.parametrize("name", REVIEW_PROMPTS)
def test_prompt_mentions_finding_categories(name):
    text = (PANEL_PROMPTS_DIR / name).read_text(encoding="utf-8")
    # At minimum, MISSING + INVENTED + AMBIGUOUS should appear in each.
    for cat in ("MISSING", "INVENTED", "AMBIGUOUS"):
        assert cat in text, f"{name}: missing finding category {cat!r}"


@pytest.mark.parametrize("name", REVIEW_PROMPTS)
def test_prompt_mentions_evidence_citation_form(name):
    text = (PANEL_PROMPTS_DIR / name).read_text(encoding="utf-8")
    assert "Evidence:" in text, (
        f"{name}: missing Evidence: citation-form instruction"
    )
