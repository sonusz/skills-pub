"""prd-checklist.json -- mechanical PRD requirement index.

This artifact is intentionally not a coverage map. It lists requirement
IDs found in prd.md so close-approval reviewers cannot forget a PRD row,
but it contains no implementation mapping or satisfaction judgment.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from autodev.errors import SchemaError
from autodev.state.atomic import atomic_write_json
from autodev.state.hashing import hash_file


_REQ_RE = re.compile(r"^###\s+(R\d+)\s*:\s*(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class PrdRequirement:
    req_id: str
    title: str
    line: int

    def to_dict(self) -> dict[str, Any]:
        return {"req_id": self.req_id, "title": self.title, "line": self.line}


@dataclass(frozen=True)
class PrdChecklist:
    source: str
    source_hash: str
    written: str
    kind: str
    requirements: list[PrdRequirement]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_hash": self.source_hash,
            "written": self.written,
            "kind": self.kind,
            "requirements": [r.to_dict() for r in self.requirements],
        }


def _line_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _validate(raw: dict[str, Any]) -> None:
    required = ("source", "source_hash", "written", "kind", "requirements")
    for key in required:
        if key not in raw:
            raise SchemaError(f"prd-checklist.json missing {key}")
    if raw["kind"] != "prd-checklist":
        raise SchemaError("prd-checklist.json.kind must be prd-checklist")
    if not isinstance(raw["source_hash"], str) or not raw["source_hash"].startswith("sha256:"):
        raise SchemaError("prd-checklist.json.source_hash must start with sha256:")
    reqs = raw["requirements"]
    if not isinstance(reqs, list):
        raise SchemaError("prd-checklist.json.requirements must be list")
    seen: set[str] = set()
    forbidden = {"status", "evidence", "spec_section", "coverage", "implementation"}
    for i, req in enumerate(reqs):
        if not isinstance(req, dict):
            raise SchemaError(f"prd-checklist.json.requirements[{i}] must be object")
        extra_forbidden = forbidden.intersection(req)
        if extra_forbidden:
            raise SchemaError(
                "prd-checklist.json must not contain coverage fields "
                f"{sorted(extra_forbidden)!r}"
            )
        rid = req.get("req_id")
        if not isinstance(rid, str) or not rid:
            raise SchemaError(f"prd-checklist.json.requirements[{i}].req_id missing")
        if rid in seen:
            raise SchemaError(f"prd-checklist.json duplicate req_id {rid!r}")
        seen.add(rid)
        if not isinstance(req.get("title"), str):
            raise SchemaError(f"prd-checklist.json.requirements[{i}].title missing")
        if not isinstance(req.get("line"), int):
            raise SchemaError(f"prd-checklist.json.requirements[{i}].line missing")


def build_prd_checklist(feature_active: Path) -> PrdChecklist:
    prd_path = Path(feature_active) / "prd.md"
    text = prd_path.read_text(encoding="utf-8")
    requirements = [
        PrdRequirement(
            req_id=m.group(1),
            title=m.group(2).strip(),
            line=_line_for_offset(text, m.start()),
        )
        for m in _REQ_RE.finditer(text)
    ]
    return PrdChecklist(
        source=str(prd_path),
        source_hash=hash_file(prd_path),
        written=date.today().isoformat(),
        kind="prd-checklist",
        requirements=requirements,
    )


def write_prd_checklist(feature_active: Path) -> Path:
    path = Path(feature_active) / "prd-checklist.json"
    checklist = build_prd_checklist(feature_active)
    data = checklist.to_dict()
    _validate(data)
    atomic_write_json(path, data)
    return path


def load_prd_checklist(path: Path) -> PrdChecklist:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    _validate(raw)
    return PrdChecklist(
        source=raw["source"],
        source_hash=raw["source_hash"],
        written=raw["written"],
        kind=raw["kind"],
        requirements=[
            PrdRequirement(
                req_id=r["req_id"], title=r["title"], line=r["line"]
            )
            for r in raw["requirements"]
        ],
    )
