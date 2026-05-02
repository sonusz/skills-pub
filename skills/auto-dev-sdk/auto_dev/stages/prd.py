"""PRD stage: completeness check + intake.

Completeness model: the PRD must have the six anchor sections from the
legacy skill — Problem / Users / Requirements / Constraints / Success /
Out-of-scope. Detection is tolerant: any Markdown H2 whose title contains
the keyword counts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


_ANCHORS = {
    "problem":     re.compile(r"^##\s+\d*\.?\s*problem", re.IGNORECASE | re.MULTILINE),
    "users":       re.compile(r"^##\s+\d*\.?\s*users", re.IGNORECASE | re.MULTILINE),
    "requirements": re.compile(r"^##\s+\d*\.?\s*requirements", re.IGNORECASE | re.MULTILINE),
    "constraints": re.compile(r"^##\s+\d*\.?\s*constraints", re.IGNORECASE | re.MULTILINE),
    "success":     re.compile(r"^##\s+\d*\.?\s*success", re.IGNORECASE | re.MULTILINE),
    "out_of_scope": re.compile(r"^##\s+\d*\.?\s*out[\s-]of[\s-]scope", re.IGNORECASE | re.MULTILINE),
}


@dataclass
class CompletenessReport:
    complete: bool
    missing: list[str] = field(default_factory=list)


def check_completeness(prd_path: Path) -> CompletenessReport:
    text = Path(prd_path).read_text(encoding="utf-8")
    missing = [name for name, pattern in _ANCHORS.items() if not pattern.search(text)]
    return CompletenessReport(complete=not missing, missing=missing)


def ensure_prd(feature_active: Path, *, from_file: Path | None = None) -> Path:
    """Materialize `prd.md` inside `active/`.

    If `from_file` is provided, copy it. Otherwise the caller is expected
    to have already placed a `prd.md` (interactive interview path).
    """
    feature_active = Path(feature_active)
    prd = feature_active / "prd.md"
    if from_file is not None:
        src = Path(from_file)
        from auto_dev.state.atomic import atomic_write

        atomic_write(prd, src.read_bytes())
    return prd
