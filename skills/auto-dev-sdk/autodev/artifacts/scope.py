"""scope.json — copied from v0.1 with independent-module rename."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

from autodev.errors import SchemaError
from autodev.state.atomic import atomic_write_json

Status = Literal["active", "removed", "superseded"]
Mode = Literal["fresh", "update", "reopen"]
DesignDepth = Literal["contract", "full"]

_VALID_STATUS = {"active", "removed", "superseded"}
_VALID_MODE = {"fresh", "update", "reopen"}
_VALID_DESIGN_DEPTH = {"contract", "full"}


@dataclass
class ScopeItem:
    id: str
    description: str
    # prd_ref / design_ref are lists of token strings. Each token must
    # appear as a substring somewhere in the linked file (prd.md /
    # design.md). Stored as list[str]; constructors validate the type.
    prd_ref: list[str] = field(default_factory=list)
    design_ref: list[str] = field(default_factory=list)
    status: Status = "active"
    superseded_by: str | None = None
    # Mechanism 4 (rigor-tier proposal): "contract" defers interior
    # design to build time; "full" is current behavior. Absent in old
    # files -> "full".
    design_depth: DesignDepth = "full"

    def __post_init__(self) -> None:
        if not isinstance(self.prd_ref, list) or not all(isinstance(t, str) for t in self.prd_ref):
            raise SchemaError(
                f"ScopeItem({self.id!r}).prd_ref must be list[str], got {type(self.prd_ref).__name__}"
            )
        if not isinstance(self.design_ref, list) or not all(isinstance(t, str) for t in self.design_ref):
            raise SchemaError(
                f"ScopeItem({self.id!r}).design_ref must be list[str], got {type(self.design_ref).__name__}"
            )
        self.prd_ref = [t.strip() for t in self.prd_ref if t.strip()]
        self.design_ref = [t.strip() for t in self.design_ref if t.strip()]

    def to_dict(self) -> dict:
        d: dict = {
            "id": self.id,
            "description": self.description,
            "prd_ref": list(self.prd_ref),
            "status": self.status,
        }
        if self.design_ref:
            d["design_ref"] = list(self.design_ref)
        if self.superseded_by:
            d["superseded_by"] = self.superseded_by
        if self.design_depth != "full":
            d["design_depth"] = self.design_depth
        return d


@dataclass
class ExcludedItem:
    id: str
    description: str
    reason: str

    def to_dict(self) -> dict:
        return {"id": self.id, "description": self.description, "reason": self.reason}


@dataclass
class Scope:
    source: str
    source_hash: str
    written: str
    feature: str
    mode: Mode
    diff_base: str
    in_scope: list[ScopeItem] = field(default_factory=list)
    excluded: list[ExcludedItem] = field(default_factory=list)
    # Optional: populated in greenfield mode when scope commits to
    # architectural primitives no arch-doc yet describes. spec stage
    # reads these to crystallize the first architecture.md.
    design_notes: list[str] = field(default_factory=list)

    def active_items(self) -> list[ScopeItem]:
        return [i for i in self.in_scope if i.status == "active"]

    def to_dict(self) -> dict:
        d: dict = {
            "source": self.source,
            "source_hash": self.source_hash,
            "written": self.written,
            "feature": self.feature,
            "mode": self.mode,
            "diff_base": self.diff_base,
            "in_scope": [i.to_dict() for i in self.in_scope],
            "excluded": [e.to_dict() for e in self.excluded],
        }
        if self.design_notes:
            d["design_notes"] = list(self.design_notes)
        return d


def _validate(obj: dict) -> None:
    required = ("source", "source_hash", "written", "feature", "mode", "diff_base", "in_scope")
    for k in required:
        if k not in obj:
            raise SchemaError(f"scope.json missing required key: {k}")
    if obj["mode"] not in _VALID_MODE:
        raise SchemaError(f"scope.json.mode must be one of {_VALID_MODE}")
    if not obj["source_hash"].startswith("sha256:"):
        raise SchemaError("source_hash must start with 'sha256:'")
    seen: set[str] = set()
    for i, item in enumerate(obj["in_scope"]):
        for k in ("id", "description", "prd_ref", "status"):
            if k not in item:
                raise SchemaError(f"in_scope[{i}] missing {k}")
        for k in ("prd_ref", "design_ref"):
            if k not in item:
                continue
            v = item[k]
            if not isinstance(v, list) or not all(isinstance(t, str) for t in v):
                raise SchemaError(f"in_scope[{i}].{k} must be list[str], got {type(v).__name__}")
        if item["status"] not in _VALID_STATUS:
            raise SchemaError(f"in_scope[{i}].status must be one of {_VALID_STATUS}")
        if item["id"] in seen:
            raise SchemaError(f"duplicate scope id: {item['id']}")
        seen.add(item["id"])
        if item["status"] == "superseded" and not item.get("superseded_by"):
            raise SchemaError(f"in_scope[{i}] superseded missing superseded_by")
        if "design_depth" in item and item["design_depth"] not in _VALID_DESIGN_DEPTH:
            raise SchemaError(
                f"in_scope[{i}].design_depth must be one of {_VALID_DESIGN_DEPTH}"
            )
    if "design_notes" in obj:
        dn = obj["design_notes"]
        if not isinstance(dn, list) or not all(isinstance(s, str) for s in dn):
            raise SchemaError("scope.json.design_notes must be list[str]")


def load_scope(path: Path) -> Scope:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    _validate(raw)
    return Scope(
        source=raw["source"],
        source_hash=raw["source_hash"],
        written=raw["written"],
        feature=raw["feature"],
        mode=raw["mode"],
        diff_base=raw["diff_base"],
        in_scope=[
            ScopeItem(
                id=i["id"], description=i["description"], prd_ref=i["prd_ref"],
                design_ref=i.get("design_ref", []),
                status=i["status"], superseded_by=i.get("superseded_by"),
                design_depth=i.get("design_depth", "full"),
            )
            for i in raw["in_scope"]
        ],
        excluded=[
            ExcludedItem(id=e["id"], description=e["description"], reason=e["reason"])
            for e in raw.get("excluded", [])
        ],
        design_notes=list(raw.get("design_notes", [])),
    )


def write_scope(path: Path, scope: Scope) -> None:
    d = scope.to_dict()
    _validate(d)
    if not scope.written:
        scope.written = date.today().isoformat()
        d["written"] = scope.written
    atomic_write_json(Path(path), d)
