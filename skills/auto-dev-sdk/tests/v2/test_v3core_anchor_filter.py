"""v3-core R3 — anchor-filter post-processing."""
from __future__ import annotations

import pytest

from autodev.artifacts.verdict import PanelFinding
from autodev.panel.anchor_filter import filter_anchor_findings, _classify_target


@pytest.mark.parametrize("target,expected", [
    ("primary_pair.prd.md", "primary"),
    ("primary_pair.scope.json", "primary"),
    ("anchor.prd.md", "anchor"),
    ("anchor.scope.json", "anchor"),
    # Bare literals ("primary_pair" / "anchor") no longer recognized.
    # v3-core requires filename-qualified targets.
    ("primary_pair", "unknown"),
    ("anchor", "unknown"),
    ("", "unknown"),
    ("random", "unknown"),
    ("anchored.md", "unknown"),
])
def test_classify_target(target, expected):
    assert _classify_target(target) == expected


def _f(*, severity="risk", vendor="claude", targets=()) -> PanelFinding:
    return PanelFinding(
        severity=severity, vendor=vendor, summary="x",
        targets=list(targets),
    )


def test_empty_findings_list():
    kept, dropped = filter_anchor_findings([])
    assert kept == []
    assert dropped == []


def test_empty_targets_kept_as_primary_default():
    f = _f(targets=[])
    kept, dropped = filter_anchor_findings([f])
    assert len(kept) == 1
    assert dropped == []


def test_primary_only_target_kept():
    f = _f(targets=["primary_pair.prd.md"])
    kept, dropped = filter_anchor_findings([f])
    assert len(kept) == 1
    assert dropped == []


def test_blocking_anchor_only_target_kept():
    f = _f(targets=["anchor.prd.md"])
    kept, dropped = filter_anchor_findings([f])
    assert len(kept) == 1
    assert dropped == []


def test_bare_anchor_literal_kept_as_unknown():
    """Bare ``"anchor"`` is not a valid v3-core target — classified
    unknown, defaults to primary-pair (kept)."""
    f = _f(targets=["anchor"])
    kept, dropped = filter_anchor_findings([f])
    assert len(kept) == 1
    assert dropped == []


def test_bare_primary_pair_literal_kept_as_unknown():
    """Bare ``"primary_pair"`` is not a valid v3-core target — classified
    unknown, defaults to primary-pair (kept)."""
    f = _f(targets=["primary_pair"])
    kept, dropped = filter_anchor_findings([f])
    assert len(kept) == 1
    assert dropped == []


def test_mixed_primary_and_anchor_kept():
    f = _f(targets=["primary_pair.scope.json", "anchor.prd.md"])
    kept, dropped = filter_anchor_findings([f])
    assert len(kept) == 1
    assert dropped == []


def test_multi_distinct_anchor_finding_kept():
    """v3-core update: a cross-anchor finding (≥2 distinct anchors) is
    substantive — keep it so dispatch can route to upstream producer."""
    f = _f(targets=["anchor.prd.md", "anchor.scope.json"])
    kept, dropped = filter_anchor_findings([f])
    assert len(kept) == 1
    assert dropped == []


def test_repeated_same_anchor_kept():
    """Two entries pointing at the same anchor → ≥2 signals → keep.
    Reviewer doubling down on one anchor = substantive, not wordsmithing."""
    f = _f(targets=["anchor.prd.md", "anchor.prd.md"])
    kept, dropped = filter_anchor_findings([f])
    assert len(kept) == 1
    assert dropped == []


def test_unknown_target_kept_and_logged():
    warnings = []
    f = _f(targets=["something_weird"])
    kept, dropped = filter_anchor_findings([f], warn=warnings.append)
    assert len(kept) == 1
    assert dropped == []
    assert len(warnings) == 1
    assert "something_weird" in warnings[0]


def test_anchor_plus_unknown_kept():
    """Unknown defaults to primary → finding is kept."""
    warnings = []
    f = _f(targets=["anchor.prd.md", "weird"])
    kept, dropped = filter_anchor_findings([f], warn=warnings.append)
    assert len(kept) == 1
    assert dropped == []
    assert len(warnings) == 1


def test_dropped_preserves_severity_and_vendor():
    f = _f(severity="opinion", vendor="gemini",
           targets=["anchor.prd.md"])
    kept, dropped = filter_anchor_findings([f])
    assert dropped[0].severity == "opinion"
    assert dropped[0].vendor == "gemini"


def test_multiple_findings_mixed_outcomes():
    a = _f(targets=["primary_pair.prd.md"])
    b = _f(severity="opinion", targets=["anchor.prd.md"])
    c = _f(targets=[])
    kept, dropped = filter_anchor_findings([a, b, c])
    assert len(kept) == 2
    assert len(dropped) == 1
    assert dropped[0].targets == ["anchor.prd.md"]


def test_opinion_anchor_only_also_dropped():
    """Severity doesn't matter for anchor filter — targets do."""
    f = _f(severity="opinion", targets=["anchor.prd.md"])
    kept, dropped = filter_anchor_findings([f])
    assert kept == []
    assert len(dropped) == 1
