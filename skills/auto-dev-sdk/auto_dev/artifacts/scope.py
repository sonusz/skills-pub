"""scope.json read/write + schema.

Canonical state: `status == "active"` means the item is live; removed /
superseded items stay in the file for audit and reference stability.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

from auto_dev.errors import SchemaError
from auto_dev.state.atomic import atomic_write_json

Status = Literal["active", "removed", "superseded"]
Mode = Literal["fresh", "update", "reopen"]

_VALID_STATUS = {"active", "removed", "superseded"}
_VALID_MODE = {"fresh", "update", "reopen"}


@dataclass
class ScopeItem:
    id: str
    description: str
    prd_ref: str
    status: Status = "active"
    superseded_by: str | None = None

    def to_dict(self) -> dict:
        d: dict = {
            "id": self.id,
            "description": self.description,
            "prd_ref": self.prd_ref,
            "status": self.status,
        }
        if self.superseded_by:
            d["superseded_by"] = self.superseded_by
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

    def active_items(self) -> list[ScopeItem]:
        return [i for i in self.in_scope if i.status == "active"]

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "source_hash": self.source_hash,
            "written": self.written,
            "feature": self.feature,
            "mode": self.mode,
            "diff_base": self.diff_base,
            "in_scope": [i.to_dict() for i in self.in_scope],
            "excluded": [e.to_dict() for e in self.excluded],
        }


def _validate(obj: dict) -> None:
    required = ("source", "source_hash", "written", "feature", "mode", "diff_base", "in_scope")
    for k in required:
        if k not in obj:
            raise SchemaError(f"scope.json missing required key: {k}")
    if obj["mode"] not in _VALID_MODE:
        raise SchemaError(f"scope.json mode must be one of {_VALID_MODE}, got {obj['mode']!r}")
    if not obj["source_hash"].startswith("sha256:"):
        raise SchemaError("source_hash must start with 'sha256:'")
    seen: set[str] = set()
    for i, item in enumerate(obj["in_scope"]):
        for k in ("id", "description", "prd_ref", "status"):
            if k not in item:
                raise SchemaError(f"in_scope[{i}] missing {k}")
        if item["status"] not in _VALID_STATUS:
            raise SchemaError(
                f"in_scope[{i}].status must be one of {_VALID_STATUS}, got {item['status']!r}"
            )
        if item["id"] in seen:
            raise SchemaError(f"duplicate scope id: {item['id']}")
        seen.add(item["id"])
        if item["status"] == "superseded" and not item.get("superseded_by"):
            raise SchemaError(f"in_scope[{i}] superseded item missing superseded_by")


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
                id=i["id"],
                description=i["description"],
                prd_ref=i["prd_ref"],
                status=i["status"],
                superseded_by=i.get("superseded_by"),
            )
            for i in raw["in_scope"]
        ],
        excluded=[
            ExcludedItem(id=e["id"], description=e["description"], reason=e["reason"])
            for e in raw.get("excluded", [])
        ],
    )


def write_scope(path: Path, scope: Scope) -> None:
    d = scope.to_dict()
    _validate(d)
    if not scope.written:
        scope.written = date.today().isoformat()
        d["written"] = scope.written
    atomic_write_json(Path(path), d)
