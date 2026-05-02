"""JSONL event log (`log.jsonl` per feature-root).

Each line is a self-describing JSON object. Appends use advisory locking so
concurrent writers don't interleave lines.
"""
from __future__ import annotations

import fcntl
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


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
