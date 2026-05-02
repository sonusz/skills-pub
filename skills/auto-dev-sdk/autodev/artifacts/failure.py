"""<stage>-failure.json — unified failure record (R2a taxonomy)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from autodev.errors import SchemaError
from autodev.state.atomic import atomic_write_json

FailureKind = Literal[
    "timeout",
    "exit_nonzero",
    "missing_artifact",
    "malformed_artifact",
    "detected_out_of_scope_write",
    "interrupted",
]

_VALID_KINDS = {
    "timeout", "exit_nonzero", "missing_artifact", "malformed_artifact",
    "detected_out_of_scope_write", "interrupted",
}


@dataclass
class FailureReport:
    stage: str
    kind: FailureKind
    detail: str
    subprocess_exit: int | None = None
    stderr_tail: str = ""
    ts: str = ""
    subprocess_reaped: bool = False  # True if SIGKILL was required
    workspace_dirty: bool = False

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "kind": self.kind,
            "detail": self.detail,
            "subprocess_exit": self.subprocess_exit,
            "stderr_tail": self.stderr_tail,
            "ts": self.ts or datetime.now(timezone.utc).isoformat(),
            "subprocess_reaped": self.subprocess_reaped,
            "workspace_dirty": self.workspace_dirty,
        }


def _validate(obj: dict) -> None:
    for k in ("stage", "kind", "detail", "ts"):
        if k not in obj:
            raise SchemaError(f"failure.json missing {k}")
    if obj["kind"] not in _VALID_KINDS:
        raise SchemaError(f"failure.kind must be one of {_VALID_KINDS}")


def load_failure(path: Path) -> FailureReport:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    _validate(raw)
    return FailureReport(
        stage=raw["stage"], kind=raw["kind"], detail=raw["detail"],
        subprocess_exit=raw.get("subprocess_exit"),
        stderr_tail=raw.get("stderr_tail", ""),
        ts=raw["ts"],
        subprocess_reaped=raw.get("subprocess_reaped", False),
        workspace_dirty=raw.get("workspace_dirty", False),
    )


def write_failure(path: Path, report: FailureReport) -> None:
    d = report.to_dict()
    _validate(d)
    atomic_write_json(Path(path), d)
