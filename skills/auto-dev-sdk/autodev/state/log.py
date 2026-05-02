"""JSONL event log per feature (append-only, fcntl-guarded)."""
from __future__ import annotations

import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2  # bumped from v0.1


# (stage, event) tuples that produce a one-line stdout alert when
# AUTODEV_WATCH=1 is set. Tuned to fire only on real state transitions
# the outer agent should react to; routine progress markers
# (subprocess-dispatch, artifact-written, ralph stage-complete on each
# iteration) are intentionally excluded.
_ALERT_EVENT_KEYS: set[tuple[str, str]] = {
    ("orchestrator", "pipeline-done"),
    ("orchestrator", "paused"),
    ("gate", "panel-done"),
    ("gate", "revision-loop-triggered"),
    ("gate", "revision-loop-halt"),
}

# Events that always alert regardless of which stage emitted them.
_ALERT_EVENT_NAMES: set[str] = {"subprocess-failed"}


def _should_alert(stage: str, event: str) -> bool:
    if event in _ALERT_EVENT_NAMES:
        return True
    return (stage, event) in _ALERT_EVENT_KEYS


def _format_alert(record: dict[str, Any]) -> str:
    stage = record.get("stage", "")
    event = record.get("event", "")
    detail = record.get("detail") or {}
    parts = [f"[autodev:{stage}]", event]
    # Surface the most informative detail fields, in priority order.
    for key in ("verdict", "gate", "kind", "vendor", "model", "reason"):
        if key not in detail:
            continue
        value = detail[key]
        if isinstance(value, str) and len(value) > 80:
            value = value[:77] + "..."
        parts.append(f"{key}={value}")
    return " ".join(parts)


class JsonlLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(
        self,
        *,
        stage: str,
        event: str,
        feature: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "schema": SCHEMA_VERSION,
            "stage": stage,
            "event": event,
            "feature": feature,
            "detail": detail or {},
        }
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with open(self.path, "a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(line)
                f.flush()
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        # Watch-mode alert: emit a one-line stdout marker for key
        # transitions so an outer agent monitoring stdout can react
        # without polling. Off by default; opt in with AUTODEV_WATCH=1
        # (set by `autodev run --watch`).
        if os.environ.get("AUTODEV_WATCH") == "1" and _should_alert(stage, event):
            try:
                sys.stdout.write(_format_alert(record) + "\n")
                sys.stdout.flush()
            except (OSError, ValueError):
                # stdout closed or otherwise unavailable; swallow so
                # alert path can never break the actual log write.
                pass

    def tail(self, n: int = 20) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with open(self.path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-n:]
        out: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out
