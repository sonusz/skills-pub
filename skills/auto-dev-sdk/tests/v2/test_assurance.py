"""Unit tests — autodev/assurance.py (PRD `## Assurance` parsing)."""
from __future__ import annotations

from autodev.assurance import AssuranceMap, max_level, parse_assurance
from autodev.prd_intake import validate_prd_text

PRD_BASE = """# PRD: demo

## Problem
x

## Users
y

## Requirements

### R1: Alpha
a

### R2: Beta
b

### R3: Gamma
c

## Constraints
z

## Success Criteria
s

## Out of Scope
o
"""

ASSURANCE_OK = """
## Assurance

Default: loose

| Req | Rigor | Rationale |
|---|---|---|
| R1 | strict | core algorithm |
| R2 | core | cli entry point |
"""


def test_absent_section_is_all_strict_no_errors():
    m, errors = parse_assurance(PRD_BASE)
    assert errors == []
    assert m.present is False
    assert m.default == "strict"
    assert m.level_for("R1") == "strict"
    assert m.level_for("R99") == "strict"


def test_basic_parse_default_and_rows():
    m, errors = parse_assurance(PRD_BASE + ASSURANCE_OK)
    assert errors == []
    assert m.present is True
    assert m.default == "loose"
    assert m.level_for("R1") == "strict"
    assert m.level_for("R2") == "core"
    assert m.level_for("R3") == "loose"      # falls to default
    assert m.rationale["R1"] == "core algorithm"


def test_missing_default_line_errors():
    text = PRD_BASE + "\n## Assurance\n\n| Req | Rigor | Rationale |\n|---|---|---|\n| R1 | loose | x |\n"
    _, errors = parse_assurance(text)
    assert any("Default" in e for e in errors)


def test_bad_default_level_errors():
    text = PRD_BASE + "\n## Assurance\n\nDefault: extreme\n"
    _, errors = parse_assurance(text)
    assert any("Default level" in e for e in errors)


def test_bad_row_level_errors():
    text = PRD_BASE + "\n## Assurance\n\nDefault: loose\n\n| Req | Rigor | Rationale |\n|---|---|---|\n| R1 | mega | x |\n"
    _, errors = parse_assurance(text)
    assert any("rigor level" in e for e in errors)


def test_duplicate_row_errors():
    text = PRD_BASE + (
        "\n## Assurance\n\nDefault: loose\n\n| Req | Rigor | Rationale |\n"
        "|---|---|---|\n| R1 | strict | x |\n| R1 | loose | y |\n"
    )
    _, errors = parse_assurance(text)
    assert any("duplicate" in e for e in errors)


def test_missing_rationale_errors():
    text = PRD_BASE + (
        "\n## Assurance\n\nDefault: loose\n\n| Req | Rigor | Rationale |\n"
        "|---|---|---|\n| R1 | strict |  |\n"
    )
    _, errors = parse_assurance(text)
    assert any("rationale" in e for e in errors)


def test_unknown_r_errors_with_known_rs():
    text = PRD_BASE + (
        "\n## Assurance\n\nDefault: loose\n\n| Req | Rigor | Rationale |\n"
        "|---|---|---|\n| R9 | strict | x |\n"
    )
    _, errors = parse_assurance(text, known_rs={"R1", "R2", "R3"})
    assert any("R9" in e for e in errors)


def test_amendment_override_latest_wins():
    text = PRD_BASE + ASSURANCE_OK + (
        "\n## Amendment 2026-08-01\n\nAssurance: R2 core -> strict\n"
        "\n## Amendment 2026-08-02\n\nAssurance: R2 strict -> loose\n"
    )
    m, errors = parse_assurance(text)
    assert errors == []
    assert m.level_for("R2") == "loose"
    assert m.rationale["R2"].startswith("amended:")


def test_amendment_without_section_creates_deviation():
    text = PRD_BASE + "\n## Amendment 2026-08-01\n\n- Assurance: R3 -> core\n"
    m, errors = parse_assurance(text)
    assert errors == []
    assert m.present is True
    assert m.default == "strict"
    assert m.level_for("R3") == "core"


def test_amendment_bad_level_errors():
    text = PRD_BASE + "\n## Amendment 2026-08-01\n\nAssurance: R1 strict -> mega\n"
    _, errors = parse_assurance(text)
    assert any("override" in e for e in errors)


def test_max_level_rule_a_ordering():
    assert max_level(["loose", "core"]) == "core"
    assert max_level(["loose", "strict", "core"]) == "strict"
    assert max_level(["loose"]) == "loose"
    assert max_level([]) == "strict"          # fail closed


def test_prd_intake_accepts_valid_assurance():
    res = validate_prd_text(PRD_BASE + ASSURANCE_OK)
    assert res.ok, res.errors


def test_prd_intake_rejects_malformed_assurance():
    text = PRD_BASE + (
        "\n## Assurance\n\nDefault: loose\n\n| Req | Rigor | Rationale |\n"
        "|---|---|---|\n| R9 | strict | x |\n"
    )
    res = validate_prd_text(text)
    assert not res.ok
    assert any("R9" in e for e in res.errors)


def test_assurance_map_default_ctor_is_strict():
    m = AssuranceMap()
    assert m.level_for("R1") == "strict"
    assert m.present is False
