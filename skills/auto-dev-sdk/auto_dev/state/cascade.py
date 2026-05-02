"""Staleness cascade.

Each artifact carries the hash of its upstream source. If the current
upstream hash differs, the artifact is stale — discard it and invalidate
every downstream sibling.

Cascade chain:
    prd.md ──▶ scope.json ──▶ {trace.md, test-plan.md}
                          ──▶ build.json ──▶ spec.md ──▶ review.md
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from auto_dev.state.hashing import hash_file, parse_markdown_source_hash


@dataclass(frozen=True)
class ArtifactRef:
    name: str
    path_fragment: str
    is_json: bool
    upstream: tuple[str, ...]


# Declared once; the cascade is data-driven so adding a stage is one entry.
ARTIFACTS: tuple[ArtifactRef, ...] = (
    ArtifactRef("prd",       "prd.md",          False, ()),
    ArtifactRef("scope",     "scope.json",      True,  ("prd",)),
    ArtifactRef("trace",     "trace.md",        False, ("scope",)),
    ArtifactRef("test_plan", "test-plan.md",    False, ("scope",)),
    ArtifactRef("build",     "build.json",      True,  ("scope", "trace", "test_plan")),
    ArtifactRef("spec",      "spec.md",         False, ("scope", "build")),
    ArtifactRef("review",    "review.md",       False, ("scope", "prd", "spec", "trace")),
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
    """Which upstream this artifact's recorded `source_hash` refers to.

    scope.json records prd hash; trace/test-plan/build record scope hash;
    spec records scope hash; review records scope hash. First declared
    upstream is the canonical one.
    """
    if not ref.upstream:
        return ref.name
    # scope.json → prd; everything downstream → scope
    return ref.upstream[0]


class StalenessCascade:
    def __init__(self, feature_active: Path) -> None:
        self.root = Path(feature_active)

    def path_for(self, name: str) -> Path:
        return self.root / _BY_NAME[name].path_fragment

    def fresh(self) -> dict[str, bool]:
        """Return per-artifact freshness.

        An artifact is *fresh* iff:
          * its file exists, AND
          * its recorded `source_hash` matches the current upstream file's hash, AND
          * every upstream artifact is itself fresh.

        Missing artifacts are `False` (not fresh). Downstream artifacts of
        stale artifacts are also `False`.
        """
        result: dict[str, bool] = {}
        for ref in ARTIFACTS:
            p = self.path_for(ref.name)
            if not p.exists():
                result[ref.name] = False
                continue

            # Check every upstream fresh first
            if not all(result.get(u, False) or _BY_NAME[u].name == "prd" and self.path_for("prd").exists()
                       for u in ref.upstream):
                result[ref.name] = False
                continue

            if ref.name == "prd":
                result[ref.name] = True
                continue

            recorded = _recorded_hash(p, ref.is_json)
            canonical_upstream = _canonical_upstream(ref)
            upstream_path = self.path_for(canonical_upstream)
            if not upstream_path.exists() or recorded is None:
                result[ref.name] = False
                continue

            current = hash_file(upstream_path)
            result[ref.name] = recorded == current

        return result

    def stale(self) -> list[str]:
        """Names of artifacts that are stale (exist but mismatched, or downstream of one)."""
        fresh = self.fresh()
        return [a.name for a in ARTIFACTS if self.path_for(a.name).exists() and not fresh[a.name]]

    def next_stage(self) -> str:
        """Return the name of the next artifact to produce, or 'done'."""
        fresh = self.fresh()
        for ref in ARTIFACTS:
            if not fresh[ref.name]:
                return ref.name
        return "done"
