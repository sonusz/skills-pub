"""Panel-review skill bridge (PRD R11 `panel` mode).

Invokes the external `panel-review` skill. The skill owns its own vendor
selection (PRD R3: vendors.yml does NOT override panel-review). Produces
`prd-panel-review.md`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from auto_dev.artifacts.common import write_markdown_with_hash
from auto_dev.state.hashing import hash_file


PANEL_REVIEW_BIN_ENV = "AUTODEV_PANEL_REVIEW_BIN"
"""Env var pointing at the panel-review executable.

If unset, we look for a script on PATH named `panel-review`. If that's also
absent, we write a placeholder `prd-panel-review.md` explaining how to
install the skill — the pipeline continues since this mode is non-blocking.
"""


def invoke_panel_review(
    *,
    feature: str,
    feature_root: Path,
    prd_path: Path,
) -> dict[str, Any]:
    prd_hash = hash_file(prd_path)
    out_path = feature_root / "prd-panel-review.md"

    bin_ = os.environ.get(PANEL_REVIEW_BIN_ENV) or shutil.which("panel-review")
    if bin_ and Path(bin_).exists():
        proc = subprocess.run(
            [bin_, "--prd", str(prd_path), "--out", str(out_path)],
            capture_output=True,
            text=True,
            timeout=1800,
        )
        if proc.returncode == 0:
            # Subprocess writes `out_path` directly. Ensure hash provenance exists.
            if not _has_source_hash(out_path):
                body = out_path.read_text(encoding="utf-8") if out_path.exists() else proc.stdout
                write_markdown_with_hash(
                    out_path, body, source=str(prd_path), source_hash=prd_hash
                )
            return {
                "prd_panel_review_path": str(out_path),
                "vendor_note": "selected by panel-review skill (not vendors.yml)",
                "output": proc.stdout,
            }
        # Fall through to placeholder on error.
        body = f"# panel-review failed\n\nexit={proc.returncode}\n\n```\n{proc.stderr}\n```\n"
    else:
        body = (
            "# panel-review skill not installed\n\n"
            "Install the `panel-review` skill or set "
            f"`{PANEL_REVIEW_BIN_ENV}` to its executable path.\n\n"
            "This gate is non-blocking; pipeline continues.\n"
        )

    write_markdown_with_hash(out_path, body, source=str(prd_path), source_hash=prd_hash)
    return {
        "prd_panel_review_path": str(out_path),
        "vendor_note": "panel-review skill not available — placeholder written",
        "output": "",
    }


def _has_source_hash(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        head = path.read_text(encoding="utf-8").splitlines()[:5]
    except OSError:
        return False
    return any("source_hash:" in line for line in head)
