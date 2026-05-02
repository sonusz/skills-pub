"""Staleness cascade — v2 extends v0.1 chain with panel reports."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from autodev.artifacts.design_packet import (
    accepted_design_fresh,
    design_packet_fresh,
)
from autodev.state.hashing import hash_file, parse_markdown_source_hash


@dataclass(frozen=True)
class ArtifactRef:
    name: str
    path_fragment: str
    is_json: bool
    upstream: tuple[str, ...]


# Unified design-stage graph.
#   prd → {design, scope, trace, test_plan} → design_packet
#       → panel_design_review → accepted_design → build
#       → implementation_index → spec
#   prd → prd_checklist
#   {prd, prd_checklist, spec} → panel_close_approval
ARTIFACTS: tuple[ArtifactRef, ...] = (
    ArtifactRef("prd",                     "prd.md",                         False, ()),
    ArtifactRef("design",                  "design.md",                      False, ("prd",)),
    ArtifactRef("scope",                   "scope.json",                     True,  ("prd",)),
    ArtifactRef("trace",                   "trace.md",                       False, ("prd",)),
    ArtifactRef("test_plan",               "test-plan.md",                   False, ("prd",)),
    ArtifactRef("design_packet",           "design-packet.json",             True,  ("design", "scope", "trace", "test_plan", "prd")),
    ArtifactRef("panel_design_review",     "panel-design-review.json",       True,  ("design_packet", "prd")),
    ArtifactRef("accepted_design",         "accepted-design.json",           True,  ("panel_design_review", "design_packet")),
    ArtifactRef("build",                   "build.json",                     True,  ("scope", "accepted_design")),
    ArtifactRef("implementation_index",    "implementation-index.json",      True,  ("build",)),
    ArtifactRef("spec",                    "implemented-spec.md",            False, ("implementation_index",)),
    ArtifactRef("prd_checklist",           "prd-checklist.json",             True,  ("prd",)),
    ArtifactRef("panel_close_approval",    "panel-close-approval.json",      True,  ("spec", "prd_checklist", "prd")),
)

_BY_NAME = {a.name: a for a in ARTIFACTS}


def _recorded_hash(path: Path, is_json: bool) -> str | None:
    if not path.exists():
        return None
    if is_json:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data.get("source_hash")
    return parse_markdown_source_hash(path)


def _canonical_upstream(ref: ArtifactRef) -> str:
    return ref.upstream[0] if ref.upstream else ref.name


def _panel_consulted_docs_fresh(panel_verdict_path: Path) -> bool:
    """For panel verdicts: every ``consulted_docs`` entry's recorded
    ``hash`` must still match the current file hash. This covers the
    multi-upstream case where a gate's primary pair contains more than
    one artifact (e.g. G1 = prd.md + scope.json). The primary artifact's
    hash is checked against canonical upstream separately; this function
    validates the remaining pair members.
    """
    try:
        data = json.loads(panel_verdict_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    for cd in data.get("consulted_docs", []):
        p = Path(cd.get("path", ""))
        if not p.exists():
            return False
        if hash_file(p) != cd.get("hash", ""):
            return False
    return True


class StalenessCascade:
    def __init__(self, feature_active: Path) -> None:
        self.root = Path(feature_active)

    def path_for(self, name: str) -> Path:
        return self.root / _BY_NAME[name].path_fragment

    def fresh(self) -> dict[str, bool]:
        result: dict[str, bool] = {}
        for ref in ARTIFACTS:
            p = self.path_for(ref.name)
            if not p.exists():
                result[ref.name] = False
                continue
            # All upstreams must themselves be fresh.
            if not all(result.get(u, False) or
                       (_BY_NAME[u].name == "prd" and self.path_for("prd").exists())
                       for u in ref.upstream):
                result[ref.name] = False
                continue
            if ref.name == "prd":
                result[ref.name] = True
                continue
            if ref.name == "design_packet":
                result[ref.name] = design_packet_fresh(p)
                continue
            if ref.name == "accepted_design":
                result[ref.name] = accepted_design_fresh(p)
                continue
            recorded = _recorded_hash(p, ref.is_json)
            canon = _canonical_upstream(ref)
            up_path = self.path_for(canon)
            if not up_path.exists() or recorded is None:
                result[ref.name] = False
                continue
            if recorded != hash_file(up_path):
                result[ref.name] = False
                continue
            # Panel verdicts have multi-upstream primary pairs; verify
            # every consulted_docs hash matches current file. This
            # catches the case where scope regenerates but the panel
            # verdict's primary_hash (prd) is unchanged.
            if ref.is_json and p.name.startswith("panel-"):
                if not _panel_consulted_docs_fresh(p):
                    result[ref.name] = False
                    continue
            result[ref.name] = True
        return result

    def stale(self) -> list[str]:
        fresh = self.fresh()
        return [a.name for a in ARTIFACTS if self.path_for(a.name).exists() and not fresh[a.name]]

    def next_stage(self) -> str:
        """Return the name of the next artifact to produce, or 'done'."""
        fresh = self.fresh()
        for ref in ARTIFACTS:
            if not fresh[ref.name]:
                return ref.name
        return "done"

    def remaining_path(self) -> list[str]:
        """Ordered list of artifacts still to produce (PRD-v2 R2a + addressed Codex blocker from round 2)."""
        fresh = self.fresh()
        return [a.name for a in ARTIFACTS if not fresh[a.name]]
