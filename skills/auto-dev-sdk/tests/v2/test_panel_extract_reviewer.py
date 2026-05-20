"""Tests for autodev.panel.extract_reviewer — script-based extraction of
structured data from reviewer markdown output."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from autodev.panel.extract_reviewer import (
    ExtractedFinding,
    ExtractedReview,
    parse_reviewer_output,
)


def test_extract_clean_verdict_pass():
    text = "## Review\n\nVerdict: pass\n\nLooks good overall."
    result = parse_reviewer_output(text)
    assert result.verdict == "pass"
    assert result.quality in ("clean", "partial")


def test_extract_clean_verdict_needs_revision():
    text = (
        "## Review\n\n"
        "- `severity`: `risk`\n"
        "- `summary`: something is wrong\n"
        "- `targets`: [primary_pair.design.md]\n\n"
        "verdict: needs_revision\n"
    )
    result = parse_reviewer_output(text)
    assert result.verdict == "needs_revision"


def test_extract_findings_basic():
    text = (
        "## Findings\n\n"
        "- `severity`: `risk`\n"
        "- `summary`: The design is missing error handling\n"
        "- `targets`: [primary_pair.design.md]\n\n"
        "Verdict: pass\n"
    )
    result = parse_reviewer_output(text)
    assert len(result.findings) >= 1
    finding = result.findings[0]
    assert finding.severity == "risk"
    assert "error handling" in finding.summary


def test_extract_targets_parsed():
    text = (
        "- `severity`: `risk`\n"
        "- `summary`: design issue\n"
        "- `targets`: [primary_pair.design.md, anchor.prd.md]\n\n"
        "Verdict: pass\n"
    )
    result = parse_reviewer_output(text)
    all_targets = [t for f in result.findings for t in f.targets]
    assert "primary_pair.design.md" in all_targets


def test_extract_coverage_table():
    text = (
        "## Coverage\n\n"
        "| req_id | status | evidence | notes |\n"
        "|---|---|---|---|\n"
        "| R1 | satisfied | design:Flow | |\n"
        "| R2 | partial | scope:s-2 | weak description |\n\n"
        "Verdict: needs_revision\n"
    )
    result = parse_reviewer_output(text)
    assert len(result.coverage) == 2
    r1 = next((c for c in result.coverage if c.req_id == "R1"), None)
    assert r1 is not None
    assert r1.status == "satisfied"
    r2 = next((c for c in result.coverage if c.req_id == "R2"), None)
    assert r2 is not None
    assert r2.status == "partial"


def test_coverage_gaps_computed():
    text = (
        "| req_id | status | evidence | notes |\n"
        "|---|---|---|---|\n"
        "| R1 | satisfied | design:Flow | |\n\n"
        "Verdict: pass\n"
    )
    result = parse_reviewer_output(text, prd_req_ids={"R1", "R2"})
    assert "R2" in result.coverage_gaps
    assert "R1" not in result.coverage_gaps


def test_coverage_gap_emits_finding():
    text = (
        "| req_id | status | evidence | notes |\n"
        "|---|---|---|---|\n"
        "| R1 | satisfied | design:Flow | |\n\n"
        "Verdict: pass\n"
    )
    result = parse_reviewer_output(text, prd_req_ids={"R1", "R2"})
    gap_findings = [f for f in result.findings if "R2" in f.summary and "coverage gap" in f.summary]
    assert len(gap_findings) >= 1
    assert gap_findings[0].severity == "risk"
    # design-review default target
    assert gap_findings[0].targets == ["primary_pair.design.md"]


def test_coverage_gap_target_varies_by_gate():
    text = (
        "| req_id | status | evidence | notes |\n"
        "|---|---|---|---|\n"
        "| R1 | satisfied | spec:§3 | |\n\n"
        "Verdict: pass\n"
    )
    dr = parse_reviewer_output(text, prd_req_ids={"R1", "R2"}, gate="design-review")
    ca = parse_reviewer_output(text, prd_req_ids={"R1", "R2"}, gate="close-approval")
    dr_gap = [f for f in dr.findings if "R2" in f.summary][0]
    ca_gap = [f for f in ca.findings if "R2" in f.summary][0]
    assert dr_gap.targets == ["primary_pair.design.md"]
    assert ca_gap.targets == ["primary_pair.build.json"]


def test_file_indirection_resolved():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as tf:
        tf.write("# Full Review\n\nVerdict: pass\n\nAll looks good.\n")
        tmp_path = tf.name

    stub = f"The review has been completed and the output has been written to `{tmp_path}`."
    result = parse_reviewer_output(stub)
    assert result.file_path == tmp_path
    assert result.verdict == "pass"

    Path(tmp_path).unlink(missing_ok=True)


def test_fallback_quality_when_unstructured():
    text = "Everything looks great! The design is solid and well thought out."
    result = parse_reviewer_output(text)
    assert result.quality == "fallback"


def test_partial_quality_when_verdict_only():
    text = "Verdict: pass\n\nNo issues found."
    result = parse_reviewer_output(text)
    # verdict is confident but no structured findings → partial
    assert result.quality == "partial"
    assert result.verdict == "pass"
