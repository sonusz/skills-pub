"""build.json — build-stage marker.

Required fields:
  * source / source_hash / written — provenance (upstream is scope.json)
  * test_results — dict with `passed`, `failed`, `skipped`, optional `cmd`/`output_path`
  * lint — dict with `passed: bool`, optional `cmd`/`output_path`
  * files_changed — list of paths (relative to repo root)
  * deviations — list of deviation records (may be empty)

Optional:
  * blocking — bool; true if any deviation is blocking and pipeline must halt before spec
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from auto_dev.errors import SchemaError
from auto_dev.state.atomic import atomic_write_json


@dataclass
class BuildReport:
    source: str
    source_hash: str
    written: str
    test_results: dict[str, Any]
    lint: dict[str, Any]
    files_changed: list[str]
    deviations: list[dict[str, Any]] = field(default_factory=list)
    blocking: bool = False

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "source_hash": self.source_hash,
            "written": self.written,
            "test_results": self.test_results,
            "lint": self.lint,
            "files_changed": self.files_changed,
            "deviations": self.deviations,
            "blocking": self.blocking,
        }


def _validate(obj: dict) -> None:
    required = ("source", "source_hash", "written", "test_results", "lint", "files_changed")
    for k in required:
        if k not in obj:
            raise SchemaError(f"build.json missing {k}")
    tr = obj["test_results"]
    for k in ("passed", "failed", "skipped"):
        if k not in tr:
            raise SchemaError(f"build.json.test_results missing {k}")
    if "passed" not in obj["lint"]:
        raise SchemaError("build.json.lint missing `passed`")


def load_build(path: Path) -> BuildReport:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    _validate(raw)
    return BuildReport(
        source=raw["source"],
        source_hash=raw["source_hash"],
        written=raw["written"],
        test_results=raw["test_results"],
        lint=raw["lint"],
        files_changed=raw["files_changed"],
        deviations=raw.get("deviations", []),
        blocking=raw.get("blocking", False),
    )


def write_build(path: Path, report: BuildReport) -> None:
    if not report.written:
        report.written = date.today().isoformat()
    d = report.to_dict()
    _validate(d)
    atomic_write_json(Path(path), d)
