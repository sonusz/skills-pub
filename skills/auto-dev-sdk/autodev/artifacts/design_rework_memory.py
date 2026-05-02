"""design-rework-memory.json -- cumulative design review memory.

The design loop overwrites panel-design-review.json on each run. This
artifact preserves the review history and summarizes durable constraints
so a design rerun can avoid regressing fixes from earlier iterations.
"""
from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path
from typing import Any

from autodev.artifacts.verdict import PanelVerdict
from autodev.state.atomic import atomic_write_json
from autodev.state.hashing import hash_file


MEMORY_FILENAME = "design-rework-memory.json"
HISTORY_DIRNAME = "design-review-history"


def _history_dir(feature_active: Path) -> Path:
    return Path(feature_active) / HISTORY_DIRNAME


def memory_path(feature_active: Path) -> Path:
    return Path(feature_active) / MEMORY_FILENAME


def load_design_rework_memory(feature_active: Path) -> dict[str, Any]:
    path = memory_path(feature_active)
    if not path.exists():
        return {
            "kind": "design-rework-memory",
            "written": date.today().isoformat(),
            "rounds": [],
            "established_constraints": [],
        }
    return json.loads(path.read_text(encoding="utf-8"))


def _next_round_number(memory: dict[str, Any]) -> int:
    rounds = memory.get("rounds", [])
    if not rounds:
        return 1
    return max(int(r.get("round", 0)) for r in rounds) + 1


def _already_recorded(memory: dict[str, Any], verdict: PanelVerdict) -> bool:
    signature = {
        "source_hash": verdict.source_hash,
        "run_ts": verdict.run_ts,
        "verdict": verdict.verdict,
    }
    for r in memory.get("rounds", []):
        if (
            r.get("source_hash") == signature["source_hash"]
            and r.get("run_ts") == signature["run_ts"]
            and r.get("verdict") == signature["verdict"]
        ):
            return True
    return False


def record_design_review_memory(
    feature_active: Path, verdict: PanelVerdict,
) -> Path:
    """Archive a design-review verdict and update cumulative memory.

    Duplicate calls with the same source hash, timestamp, and verdict are
    no-ops so cached verdict checks do not create duplicate rounds.
    """
    if verdict.gate != "design-review":
        return memory_path(feature_active)

    feature_active = Path(feature_active)
    memory = load_design_rework_memory(feature_active)
    if _already_recorded(memory, verdict):
        return memory_path(feature_active)

    round_no = _next_round_number(memory)
    hdir = _history_dir(feature_active)
    hdir.mkdir(parents=True, exist_ok=True)
    history_path = hdir / f"round-{round_no:03d}.json"
    live_verdict = feature_active / "panel-design-review.json"
    if live_verdict.exists():
        shutil.copyfile(live_verdict, history_path)
    else:
        atomic_write_json(history_path, verdict.to_dict())

    findings = [
        {
            "severity": f.severity,
            "vendor": f.vendor,
            "summary": f.summary,
            "targets": list(f.targets),
        }
        for f in verdict.findings
    ]
    constraints = [
        f"{f.severity}: {f.summary}"
        for f in verdict.findings
        if f.severity in ("invariant_violation", "risk")
    ]

    memory.setdefault("rounds", []).append({
        "round": round_no,
        "verdict": verdict.verdict,
        "source_hash": verdict.source_hash,
        "run_ts": verdict.run_ts,
        "history_path": str(history_path),
        "history_hash": hash_file(history_path),
        "findings": findings,
    })
    existing_constraints = list(memory.get("established_constraints", []))
    for c in constraints:
        if c not in existing_constraints:
            existing_constraints.append(c)
    memory["kind"] = "design-rework-memory"
    memory["written"] = date.today().isoformat()
    memory["established_constraints"] = existing_constraints
    atomic_write_json(memory_path(feature_active), memory)
    return memory_path(feature_active)
