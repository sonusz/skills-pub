#!/usr/bin/env python3
"""Generate a machine-local vendors.yaml / vendors.yml from a tracked sample.

Every skill that calls LLM vendors ships a tracked ``sample-vendors.yaml``
listing every vendor the shared module supports. The file the scripts read,
``vendors.yaml``, is machine-local and git-ignored: it is the sample pruned to
the vendors that work on this host. This script does the pruning.

Usage:
  init-vendors.py --sample skills/panel-review/sample-vendors.yaml \
                  --out    skills/panel-review/vendors.yaml
  init-vendors.py --sample ... --out ... --vendor openai --vendor claude   # explicit keep-list
  init-vendors.py --sample ... --out ... --force                          # overwrite
  init-vendors.py --sample ... --out -                                    # print to stdout

Without --vendor, a vendor is kept when its CLI binary is on PATH:
  openai/codex -> codex, claude -> claude, agy -> agy, cursor -> cursor-agent, grok -> grok
Binary presence is not readiness (auth, quota, model access): run the skill's
doctor script afterwards and delete entries that fail.

Supported file shapes (line-based; comments and ordering are preserved; no
YAML library is required):
  1. panel-review shape: top-level ``panel:`` list of ``- id:`` entries, each
     with a ``vendor:`` key, plus a ``synthesis:`` mapping with ``vendor:``.
     Unavailable panel entries are removed. If the synthesis vendor is
     unavailable it is pointed at the first kept panel entry.
  2. auto-dev-sdk shape: top-level ``panel:`` mapping with a ``reviewers:``
     list of ``- vendor:`` entries and ``min_responding_reviewers:``.
     Unavailable reviewers are removed and the minimum is clamped. Other
     roles (``stages``, ``synthesizer``, ``probe``) are only checked: the
     script warns when their vendor is missing and leaves the text alone,
     because a coding stage cannot simply be deleted.

Exit status: 0 written; 1 written but fewer than two panel entries survive
(the skills refuse to run a one-vendor panel); 2 usage or refusal to overwrite.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import re
import shutil
import socket
import sys
from pathlib import Path

VENDOR_BINARIES = {
    "openai": "codex",
    "codex": "codex",
    "claude": "claude",
    "agy": "agy",
    "cursor": "cursor-agent",
    "grok": "grok",
}

_LIST_ITEM = re.compile(r"^(\s*)-\s")
_KEY_LINE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$")
_VENDOR_KEY = re.compile(r"^\s*(?:-\s+)?vendor:\s*(\S+)")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_blank_or_comment(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _clean(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value.strip().lower()


def available_vendors(explicit: list[str] | None) -> tuple[set[str], dict[str, str]]:
    """Return (kept vendor names, reason per vendor)."""
    reasons: dict[str, str] = {}
    if explicit:
        requested = {v.strip().lower() for v in explicit if v.strip()}
        unknown = sorted(requested - set(VENDOR_BINARIES))
        if unknown:
            raise SystemExit(
                f"init-vendors: unknown --vendor {', '.join(unknown)}; "
                f"known: {', '.join(sorted(VENDOR_BINARIES))}"
            )
        # openai and codex are the same CLI: asking for either keeps both
        # spellings so a sample's `vendor: openai` survives `--vendor codex`.
        binaries = {VENDOR_BINARIES[v] for v in requested}
        keep = {v for v, b in VENDOR_BINARIES.items() if b in binaries}
        for vendor in VENDOR_BINARIES:
            reasons[vendor] = "kept (--vendor)" if vendor in keep else "removed (not in --vendor list)"
        return keep, reasons
    keep: set[str] = set()
    for vendor, binary in VENDOR_BINARIES.items():
        path = shutil.which(binary)
        if path:
            keep.add(vendor)
            reasons[vendor] = f"kept ({binary} at {path})"
        else:
            reasons[vendor] = f"removed ({binary} not on PATH)"
    return keep, reasons


def _section_bounds(lines: list[str], key: str) -> tuple[int, int] | None:
    """Return [start, end) line indexes of a top-level ``key:`` mapping/list."""
    start = None
    for i, line in enumerate(lines):
        if _indent(line) == 0 and re.match(rf"^{re.escape(key)}:\s*(#.*)?$", line):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if not _is_blank_or_comment(line) and _indent(line) == 0:
            end = j
            break
    return start, end


def _list_blocks(lines: list[str], start: int, end: int, item_indent: int) -> list[tuple[int, int]]:
    """Split lines[start:end] into ``- `` item blocks at ``item_indent``.

    Comment lines at the item indent directly above an item belong to it.
    """
    blocks: list[tuple[int, int]] = []
    i = start
    while i < end:
        line = lines[i]
        m = _LIST_ITEM.match(line)
        if m and len(m.group(1)) == item_indent:
            block_start = i
            # absorb comment lines directly above at the same indent
            k = i - 1
            while k >= start and lines[k].strip().startswith("#") and _indent(lines[k]) == item_indent:
                block_start = k
                k -= 1
            j = i + 1
            while j < end:
                nxt = lines[j]
                if _is_blank_or_comment(nxt):
                    j += 1
                    continue
                m2 = _LIST_ITEM.match(nxt)
                if (m2 and len(m2.group(1)) == item_indent) or _indent(nxt) <= item_indent:
                    break
                j += 1
            # Trailing blank/comment lines document whatever comes next (the
            # next item, or the key after the list): never delete them with
            # this item.
            block_end = j
            while block_end > i + 1 and _is_blank_or_comment(lines[block_end - 1]):
                block_end -= 1
            blocks.append((block_start, block_end))
            i = j
        else:
            i += 1
    return blocks


def _block_vendor(lines: list[str], block: tuple[int, int]) -> str | None:
    for line in lines[block[0]:block[1]]:
        if line.strip().startswith("#"):
            continue
        m = _VENDOR_KEY.match(line)
        if m:
            return _clean(m.group(1))
    return None


def _first_item_indent(lines: list[str], start: int, end: int) -> int | None:
    for line in lines[start + 1:end]:
        if _is_blank_or_comment(line):
            continue
        m = _LIST_ITEM.match(line)
        return len(m.group(1)) if m else None
    return None


def prune(lines: list[str], keep: set[str]) -> tuple[list[str], list[str], int]:
    """Return (new lines, notes, surviving panel entry count)."""
    notes: list[str] = []
    panel = _section_bounds(lines, "panel")
    if panel is None:
        raise SystemExit("sample has no top-level `panel:` section")
    p_start, p_end = panel
    item_indent = _first_item_indent(lines, p_start, p_end)
    drop: list[tuple[int, int]] = []
    kept_blocks: list[tuple[int, int]] = []

    if item_indent is not None:
        # Shape 1: panel is a list of `- id:` entries.
        for block in _list_blocks(lines, p_start + 1, p_end, item_indent):
            vendor = _block_vendor(lines, block)
            if vendor is None or vendor in keep:
                kept_blocks.append(block)
            else:
                drop.append(block)
                notes.append(f"panel entry removed: vendor {vendor}")
        min_line = None
    else:
        # Shape 2: panel is a mapping with reviewers: list.
        rev_start = None
        rev_indent = None
        for i in range(p_start + 1, p_end):
            m = _KEY_LINE.match(lines[i])
            if m and m.group(2) == "reviewers" and not lines[i].strip().startswith("#"):
                rev_start, rev_indent = i, len(m.group(1))
                break
        if rev_start is None:
            raise SystemExit("panel mapping has no `reviewers:` list")
        rev_end = p_end
        for j in range(rev_start + 1, p_end):
            if not _is_blank_or_comment(lines[j]) and _indent(lines[j]) <= rev_indent:
                rev_end = j
                break
        r_item_indent = _first_item_indent(lines, rev_start, rev_end)
        if r_item_indent is None:
            raise SystemExit("`reviewers:` is not a list")
        for block in _list_blocks(lines, rev_start + 1, rev_end, r_item_indent):
            vendor = _block_vendor(lines, block)
            if vendor is None or vendor in keep:
                kept_blocks.append(block)
            else:
                drop.append(block)
                notes.append(f"panel reviewer removed: vendor {vendor}")
        min_line = None
        for i in range(p_start + 1, p_end):
            if re.match(r"^\s*min_responding_reviewers:\s*\d+", lines[i]):
                min_line = i
                break

    survivors = len(kept_blocks)
    out = list(lines)
    if min_line is not None:
        current = int(re.search(r"\d+", out[min_line]).group(0))
        clamped = max(1, min(current, survivors))
        if clamped != current:
            out[min_line] = re.sub(r"\d+", str(clamped), out[min_line], count=1)
            notes.append(f"min_responding_reviewers clamped {current} -> {clamped}")

    # Shape 1 synthesis re-pointing (before deleting blocks so indexes hold).
    synth = _section_bounds(out, "synthesis")
    if synth is not None and item_indent is not None:
        s_start, s_end = synth
        s_vendor = None
        for i in range(s_start + 1, s_end):
            m = _VENDOR_KEY.match(out[i])
            if m and not out[i].strip().startswith("#"):
                s_vendor = _clean(m.group(1))
                break
        if s_vendor is not None and s_vendor not in keep and kept_blocks:
            donor = kept_blocks[0]
            donor_vals = {}
            for line in out[donor[0]:donor[1]]:
                m = _KEY_LINE.match(line.replace("- ", "  ", 1))
                if m and not line.strip().startswith("#"):
                    donor_vals[m.group(2)] = m.group(3).strip()
            stale: list[int] = []
            for i in range(s_start + 1, s_end):
                m = _KEY_LINE.match(out[i])
                if not m or out[i].strip().startswith("#"):
                    continue
                key = m.group(2)
                if key not in ("vendor", "model", "effort"):
                    continue
                if key in donor_vals:
                    out[i] = f"{m.group(1)}{key}: {donor_vals[key]}\n"
                else:
                    # e.g. the donor has no `model:` (uses the CLI default);
                    # keeping the old vendor's model here would hand vendor A
                    # a model id that belongs to vendor B.
                    stale.append(i)
            for i in reversed(stale):
                drop.append((i, i + 1))
            notes.append(
                f"synthesis vendor {s_vendor} unavailable; now uses "
                f"{donor_vals.get('vendor', '?')} / {donor_vals.get('model', '?')}"
            )
        elif s_vendor is not None and s_vendor not in keep:
            notes.append(f"WARNING: synthesis vendor {s_vendor} unavailable and no panel entry to borrow from")

    # Other roles: warn only. `synthesizer` lives under `panel:` in the
    # auto-dev-sdk shape, so look for it there as well as at top level.
    role_ranges: list[tuple[str, int, int]] = []
    for key in ("stages", "synthesizer", "probe"):
        sec = _section_bounds(out, key)
        if sec is not None:
            role_ranges.append((key, sec[0] + 1, sec[1]))
    if item_indent is None:
        for i in range(p_start + 1, p_end):
            m = _KEY_LINE.match(out[i])
            if m and m.group(2) == "synthesizer" and not out[i].strip().startswith("#"):
                s_indent = len(m.group(1))
                j = i + 1
                while j < p_end and (_is_blank_or_comment(out[j]) or _indent(out[j]) > s_indent):
                    j += 1
                role_ranges.append(("panel.synthesizer", i + 1, j))
                break
    for key, a, b in role_ranges:
        for i in range(a, b):
            m = _VENDOR_KEY.match(out[i])
            if m and not out[i].strip().startswith("#") and _clean(m.group(1)) not in keep:
                notes.append(f"WARNING: {key} uses vendor {_clean(m.group(1))} which is not available; edit by hand")

    for a, b in sorted(drop, reverse=True):
        del out[a:b]
    return out, notes, survivors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sample", required=True, help="tracked sample-vendors.yaml to prune")
    parser.add_argument("--out", required=True, help="local vendors.yaml to write, or - for stdout")
    parser.add_argument("--vendor", action="append", default=None,
                        help="keep exactly these vendors instead of probing PATH; repeatable")
    parser.add_argument("--force", action="store_true", help="overwrite an existing --out")
    args = parser.parse_args(argv)

    sample = Path(args.sample)
    if not sample.is_file():
        print(f"init-vendors: sample not found: {sample}", file=sys.stderr)
        return 2
    to_stdout = args.out == "-"
    out_path = None if to_stdout else Path(args.out)
    if out_path is not None and out_path.exists() and not args.force:
        print(f"init-vendors: {out_path} exists; pass --force to overwrite", file=sys.stderr)
        return 2

    keep, reasons = available_vendors(args.vendor)
    lines = sample.read_text(encoding="utf-8").splitlines(keepends=True)
    new_lines, notes, survivors = prune(lines, keep)

    kept_list = ", ".join(sorted(v for v in keep if v in VENDOR_BINARIES)) or "none"
    removed = sorted(v for v, r in reasons.items() if r.startswith("removed") and v != "codex")
    header = [
        f"# {sample.name.replace('sample-', '')} — machine-local vendor selection. NOT tracked by git.\n",
        f"# Generated by shared/vendors/scripts/init-vendors.py from {sample.name}\n",
        f"# on {_dt.date.today().isoformat()} for host {socket.gethostname()}.\n",
        f"# Kept: {kept_list}. Removed: {', '.join(removed) or 'none'}.\n",
        "# Edit freely (delete entries that fail the skill's doctor, change models);\n",
        "# regenerate with --force after installing or removing vendor CLIs.\n",
        "# Catalog of vendors and model ids: shared/vendors/sample-vendors.yaml\n",
        "\n",
    ]
    text = "".join(header + new_lines)
    # Status goes to stderr when the document itself goes to stdout, so
    # `--out - > vendors.yaml` yields a clean file.
    status = sys.stderr if to_stdout else sys.stdout
    if to_stdout:
        sys.stdout.write(text)
    else:
        out_path.write_text(text, encoding="utf-8")
        print(f"wrote {out_path}", file=status)
    for vendor in VENDOR_BINARIES:
        if vendor == "codex":
            continue
        print(f"  {vendor:7s} {reasons[vendor]}", file=status)
    for note in notes:
        print(f"  {note}", file=status)
    if survivors < 2:
        print("init-vendors: fewer than two panel entries survive; the panel skills need at least two",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
