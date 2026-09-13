"""arch-review.json -- single-agent architecture review of arch-design.md.

Produced by the ``arch-review`` stage (core R2 / detail §2): a single agent
checks ``arch-design.md`` against the PRD for coverage, invention, redundancy,
and reuse-of-existing-design, and emits a pass/needs_revision verdict with
structured findings. Not a panel artifact -- ``autodev/panel/schemas.py`` is
untouched, this module owns its own schema.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from autodev.errors import SchemaError
from autodev.state.hashing import hash_file

_CATEGORIES = {"missing", "invented", "redundant", "reuse"}
_VERDICTS = {"pass", "needs_revision"}


@dataclass(frozen=True)
class ArchReviewFinding:
    category: str
    prd_ref: str | None
    evidence: str
    problem: str
    correction: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "prd_ref": self.prd_ref,
            "evidence": self.evidence,
            "problem": self.problem,
            "correction": self.correction,
        }


@dataclass(frozen=True)
class ArchReview:
    kind: str
    source: str
    source_hash: str
    prd_hash: str
    written: str
    verdict: str
    findings: list[ArchReviewFinding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source,
            "source_hash": self.source_hash,
            "prd_hash": self.prd_hash,
            "written": self.written,
            "verdict": self.verdict,
            "findings": [f.to_dict() for f in self.findings],
        }


def _validate(raw: dict[str, Any]) -> None:
    if not isinstance(raw, dict):
        raise SchemaError("arch-review.json root must be object")
    required = (
        "kind", "source", "source_hash", "prd_hash", "written", "verdict",
        "findings",
    )
    for key in required:
        if key not in raw:
            raise SchemaError(f"arch-review.json missing {key}")
    if raw["kind"] != "arch-review":
        raise SchemaError("arch-review.json.kind must be arch-review")
    if not isinstance(raw["source_hash"], str) or not raw["source_hash"].startswith("sha256:"):
        raise SchemaError("arch-review.json.source_hash must start with sha256:")

    verdict = raw["verdict"]
    if verdict not in _VERDICTS:
        raise SchemaError(
            f"arch-review.json.verdict must be one of {sorted(_VERDICTS)!r}"
        )

    findings = raw["findings"]
    if not isinstance(findings, list):
        raise SchemaError("arch-review.json.findings must be list")

    # verdict == "pass" iff findings == [] (core R2).
    if verdict == "pass" and findings:
        raise SchemaError(
            "arch-review.json.verdict is pass but findings is non-empty"
        )
    if verdict == "needs_revision" and not findings:
        raise SchemaError(
            "arch-review.json.verdict is needs_revision but findings is empty"
        )

    for i, entry in enumerate(findings):
        if not isinstance(entry, dict):
            raise SchemaError(f"arch-review.json.findings[{i}] must be object")
        category = entry.get("category")
        if category not in _CATEGORIES:
            raise SchemaError(
                f"arch-review.json.findings[{i}].category must be one of "
                f"{sorted(_CATEGORIES)!r}"
            )
        for key in ("evidence", "problem", "correction"):
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                raise SchemaError(
                    f"arch-review.json.findings[{i}].{key} must be a "
                    "non-empty string"
                )
        prd_ref = entry.get("prd_ref")
        if category == "missing":
            if not isinstance(prd_ref, str) or not prd_ref.strip():
                raise SchemaError(
                    f"arch-review.json.findings[{i}].prd_ref is required "
                    "when category is 'missing'"
                )
        elif prd_ref is not None and not isinstance(prd_ref, str):
            raise SchemaError(
                f"arch-review.json.findings[{i}].prd_ref must be string or null"
            )


def load_arch_review(path: Path, arch_design_path: Path) -> ArchReview:
    """Load and validate arch-review.json.

    Beyond the shape checks in ``_validate``, the review's recorded
    ``source_hash`` must equal the current ``arch-design.md`` hash -- a
    review of a superseded arch-design.md is stale/invalid, not silently
    accepted (core R2).
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise SchemaError(f"arch-review.json: cannot read {path}: {e}") from e
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise SchemaError(f"arch-review.json: invalid JSON: {e}") from e
    _validate(raw)

    arch_design_path = Path(arch_design_path)
    if not arch_design_path.exists():
        raise SchemaError(
            "arch-review.json: arch-design.md missing for source_hash check"
        )
    current_hash = hash_file(arch_design_path)
    if raw["source_hash"] != current_hash:
        raise SchemaError(
            f"arch-review.json.source_hash {raw['source_hash']!r} != "
            f"hash(arch-design.md) {current_hash!r}"
        )

    return ArchReview(
        kind=raw["kind"],
        source=raw["source"],
        source_hash=raw["source_hash"],
        prd_hash=raw["prd_hash"],
        written=raw["written"],
        verdict=raw["verdict"],
        findings=[
            ArchReviewFinding(
                category=f["category"],
                prd_ref=f.get("prd_ref"),
                evidence=f["evidence"],
                problem=f["problem"],
                correction=f["correction"],
            )
            for f in raw["findings"]
        ],
    )


def arch_review_fresh(path: Path, arch_design_path: Path) -> bool:
    """Cascade freshness check (core R2, detail §1).

    Fresh iff: the file exists, is parseable/valid, its ``source_hash``
    matches the current ``arch-design.md``, and ``verdict == "pass"``.
    A ``needs_revision`` verdict is not fresh -- the cascade halts at
    this node so the arch-design/arch-review loop can dispatch a
    revision.
    """
    path = Path(path)
    if not path.exists():
        return False
    try:
        review = load_arch_review(path, arch_design_path)
    except (SchemaError, OSError, json.JSONDecodeError):
        return False
    return review.verdict == "pass"
