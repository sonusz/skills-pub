"""PRD update history — R-block extraction, numbering stability check, and
independent update-history storage.

Part of the "modify prd.md in place + keep history separately" spec (core
doc R1/R2; detail doc §1). `autodev update --from-file` replaces `prd.md`
wholesale instead of appending an amendment. Before the replace, this module
verifies the new PRD didn't silently rename a requirement onto a different
number (`check_r_stability`); after the replace, it snapshots the
pre-update PRD, a unified diff, and change metadata into
``docs/features/<f>/active/prd-history/`` — independent of `prd.md` itself,
so old, superseded requirement text never lingers in the file agents read.

R-block extraction reuses `prd_intake`'s section/fence logic (the original
Requirements section plus any back-compat Amendment sections) rather than
re-implementing it, so a marker counts here exactly when it counts for
Stage 0 schema validation.
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from autodev import prd_intake
from autodev.state.atomic import atomic_write, atomic_write_json
from autodev.state.hashing import hash_bytes


@dataclass
class RBlock:
    number: int
    title: str
    body: str


def _normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip())


def _normalize_body(body: str) -> str:
    lines = [line.strip() for line in body.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines)


def requirement_blocks(text: str) -> dict[int, RBlock]:
    """Extract every ``### R<N>:`` block from ``text``'s requirement-declaring
    sections (the ``## Requirements`` section plus any back-compat
    ``## Amendment`` sections), skipping fenced code exactly like
    ``validate_prd_text``'s marker scan does.
    """
    h2s = prd_intake._extract_h2_sections(text)
    blocks: dict[int, RBlock] = {}
    for body in prd_intake._requirement_declaration_bodies(text, h2s):
        current: RBlock | None = None
        current_lines: list[str] = []
        for line in prd_intake._unfenced_lines(body):
            m = prd_intake._R_MARKER_RE.match(line)
            if m:
                if current is not None:
                    current.body = "\n".join(current_lines)
                    blocks[current.number] = current
                current = RBlock(
                    number=int(m.group(1)), title=m.group(2), body="",
                )
                current_lines = []
            elif current is not None:
                current_lines.append(line)
        if current is not None:
            current.body = "\n".join(current_lines)
            blocks[current.number] = current
    return blocks


def _classify(
    old_blocks: dict[int, RBlock], new_blocks: dict[int, RBlock],
) -> tuple[set[int], set[int], set[int], set[int]]:
    """Return (removed, added, kept, changed) R-number sets.

    ``changed`` is a subset of ``kept`` whose normalized title or body
    differs from the old version.
    """
    old_n = set(old_blocks)
    new_n = set(new_blocks)
    removed = old_n - new_n
    added = new_n - old_n
    kept = old_n & new_n
    changed = {
        n for n in kept
        if _normalize_title(old_blocks[n].title) != _normalize_title(new_blocks[n].title)
        or _normalize_body(old_blocks[n].body) != _normalize_body(new_blocks[n].body)
    }
    return removed, added, kept, changed


def check_r_stability(
    old_text: str, new_text: str, *, ever_used: set[int],
) -> list[str]:
    """R-numbering stability check for `autodev update --from-file`.

    Returns a list of human-readable errors (empty == passes):

    - A deleted R (present in ``old_text``, absent from ``new_text``) never
      errors — deletion is implicit and only surfaces in the change summary.
    - A newly added R number must be greater than every number that has ever
      been used for this feature (``ever_used`` — accumulated from
      prd-history records — union the old PRD's current R numbers). Reusing
      a formerly-used number is flagged distinctly from merely being
      non-monotonic.
    - No kept-or-added R may carry the (normalized) title or body of a
      *different* old R number — that would be a silent renumbering/reorder,
      including "delete R4, then reintroduce its text as R9".
    """
    old_blocks = requirement_blocks(old_text)
    new_blocks = requirement_blocks(new_text)
    removed, added, kept, _changed = _classify(old_blocks, new_blocks)
    del removed  # deletion is not an error — see docstring

    errors: list[str] = []

    baseline = set(ever_used) | set(old_blocks)
    max_known = max(baseline) if baseline else 0
    for n in sorted(added):
        if n > max_known:
            continue
        if n in baseline:
            errors.append(f"R{n} reuses a retired requirement number")
        else:
            errors.append(
                f"new requirement R{n} must be numbered above R{max_known}"
            )

    for n in sorted(kept | added):
        new_block = new_blocks[n]
        new_title = _normalize_title(new_block.title)
        new_body = _normalize_body(new_block.body)
        for m, old_block in old_blocks.items():
            if m == n:
                continue
            same_title = new_title == _normalize_title(old_block.title)
            same_body = bool(new_body) and new_body == _normalize_body(old_block.body)
            if same_title or same_body:
                errors.append(
                    f"R{n} now carries the content of former R{m}; "
                    "renumbering is not allowed"
                )
                break

    return errors


def history_dir(feature_active: Path) -> Path:
    return Path(feature_active) / "prd-history"


def load_records(path: Path) -> list[dict]:
    """Load every ``prd.<TS>.json`` metadata record, sorted by filename
    (== chronological order given the timestamp-based names)."""
    path = Path(path)
    if not path.is_dir():
        return []
    records: list[dict] = []
    for p in sorted(path.glob("prd.*.json")):
        try:
            records.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return records


def ever_used_numbers(records: list[dict]) -> set[int]:
    """Every R number that has ever been part of this feature's PRD — each
    record's resulting R set (``r_numbers``) union its ``removed`` list —
    for feeding into `check_r_stability`'s ``ever_used``.

    ``removed`` numbers must be folded in explicitly: once a number is
    retired it drops out of every later record's ``r_numbers``, so without
    this a number retired two updates ago would look never-used and could
    be silently reused.
    """
    out: set[int] = set()
    for r in records:
        for n in r.get("r_numbers", []):
            out.add(int(n))
        for label in r.get("removed", []):
            out.add(int(str(label).lstrip("Rr")))
    return out


def _short_hash(h: str) -> str:
    return h.split(":", 1)[-1][:12]


def _reserve_ts_slug(hist_dir: Path, base_ts: str) -> str:
    """Same-second collisions append ``-1``, ``-2``, ... to the timestamp."""
    slug = base_ts
    n = 0
    while any(
        (hist_dir / f"prd.{slug}.{ext}").exists() for ext in ("md", "diff", "json")
    ):
        n += 1
        slug = f"{base_ts}-{n}"
    return slug


def _build_summary(
    removed: list[int], added: list[int], changed: list[int],
    added_lines: int, removed_lines: int,
) -> str:
    parts: list[str] = []
    if removed:
        parts.append("removed " + ", ".join(f"R{n}" for n in removed))
    if added:
        parts.append("added " + ", ".join(f"R{n}" for n in added))
    if changed:
        parts.append("changed " + ", ".join(f"R{n}" for n in changed))
    body = "; ".join(parts) if parts else "no requirement changes"
    return f"{body}; +{added_lines}/-{removed_lines} lines"


def write_update_record(
    feature_active: Path, *, old_text: str, new_text: str,
) -> dict:
    """Snapshot the pre-update PRD, a unified diff, and change metadata into
    ``<feature_active>/prd-history/``. Returns the metadata dict (also
    written as ``prd.<TS>.json``)."""
    hist_dir = history_dir(feature_active)
    hist_dir.mkdir(parents=True, exist_ok=True)

    old_blocks = requirement_blocks(old_text)
    new_blocks = requirement_blocks(new_text)
    removed, added, _kept, changed = _classify(old_blocks, new_blocks)

    now = datetime.now(timezone.utc)
    ts_slug = _reserve_ts_slug(hist_dir, now.strftime("%Y%m%dT%H%M%SZ"))

    old_hash = hash_bytes(old_text.encode("utf-8"))
    new_hash = hash_bytes(new_text.encode("utf-8"))

    diff_lines = list(difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"prd.md@{_short_hash(old_hash)}",
        tofile=f"prd.md@{_short_hash(new_hash)}",
    ))
    added_lines = sum(
        1 for line in diff_lines if line.startswith("+") and not line.startswith("+++")
    )
    removed_lines = sum(
        1 for line in diff_lines if line.startswith("-") and not line.startswith("---")
    )
    summary = _build_summary(
        sorted(removed), sorted(added), sorted(changed), added_lines, removed_lines,
    )

    snapshot_name = f"prd.{ts_slug}.md"
    diff_name = f"prd.{ts_slug}.diff"
    meta_name = f"prd.{ts_slug}.json"

    atomic_write(hist_dir / snapshot_name, old_text)
    atomic_write(hist_dir / diff_name, "".join(diff_lines))

    record = {
        "kind": "prd-history",
        "ts": now.isoformat(),
        "snapshot": snapshot_name,
        "diff": diff_name,
        "old_hash": old_hash,
        "new_hash": new_hash,
        "removed": [f"R{n}" for n in sorted(removed)],
        "added": [f"R{n}" for n in sorted(added)],
        "changed": [f"R{n}" for n in sorted(changed)],
        "r_numbers": sorted(new_blocks),
        "summary": summary,
    }
    atomic_write_json(hist_dir / meta_name, record)
    return record
