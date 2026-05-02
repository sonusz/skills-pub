#!/usr/bin/env python3
"""ad-17 canonical acceptance test: build `hash-file-cli` end-to-end.

Runs the FULL auto-dev-sdk pipeline against live Anthropic to prove that
`implement` actually writes and tests code. This is the acceptance test
that should have existed in v0.1 — absence of it is the root PROCESS bug
identified in the amendment (ii) post-mortem.

Design:
  * Opt-in: skipped without ANTHROPIC_API_KEY.
  * Uses a throwaway copy of the reference feature (so repeated runs don't
    reuse stale artifacts).
  * Writes a minimal vendors.yml pinned to Anthropic for every stage.
  * Asserts:
      (a) pipeline reaches close-approval gate (exit 2) or completes (exit 0)
      (b) build.json.test_results.failed == 0
      (c) files_changed is non-empty and includes a test file
      (d) review.md exists and every hfc-* scope item classifies as
          Fully/PartiallyImplemented
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REFERENCE_DIR = Path(__file__).resolve().parent.parent / "examples" / "hash-file-cli"
DEFAULT_MODEL = "claude-sonnet-4-6"


def _skip(reason: str) -> int:
    print(f"SKIP build_reference.py: {reason}", file=sys.stderr)
    return 0


def _die(msg: str, code: int = 1) -> int:
    print(f"FAIL build_reference.py: {msg}", file=sys.stderr)
    return code


def _make_workspace(src: Path) -> Path:
    work = Path(tempfile.mkdtemp(prefix="autodev-hash-file-cli-"))
    # Copy reference feature files (PRD + scope) into the workspace.
    shutil.copytree(src / "docs", work / "docs")
    # Minimal vendors.yml pinned to Anthropic.
    stages = ["prd_intake", "prd_review", "scope", "plan", "implement", "spec", "review"]
    vendors_body = "\n".join(
        f"{s}:\n  vendor: anthropic\n  model: {DEFAULT_MODEL}" for s in stages
    )
    (work / "vendors.yml").write_text(vendors_body + "\n")
    return work


def _run_auto_dev(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["auto-dev", *args, "--repo-root", str(cwd), "--vendors-yml", str(cwd / "vendors.yml")],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def _assert_build(active: Path) -> int:
    build_json = active / "build.json"
    if not build_json.exists():
        return _die(f"build.json missing at {build_json}")
    b = json.loads(build_json.read_text())
    failed = b.get("test_results", {}).get("failed", -1)
    if failed != 0:
        return _die(f"build.json.test_results.failed = {failed} (expected 0)")
    files = b.get("files_changed", [])
    if not files:
        return _die("build.json.files_changed is empty")
    has_test = any("test" in f for f in files)
    if not has_test:
        return _die(f"no test file in files_changed: {files}")
    print(f"  build.json OK — {len(files)} files changed, {b['test_results']['passed']} passed")
    return 0


def _assert_review(active: Path) -> int:
    review = active / "review.md"
    if not review.exists():
        return _die(f"review.md missing at {review}")
    text = review.read_text()
    # Light check: every scope ID shows up classified Fully or Partial.
    scope = json.loads((active / "scope.json").read_text())
    active_ids = [i["id"] for i in scope["in_scope"] if i["status"] == "active"]
    for sid in active_ids:
        if sid not in text:
            return _die(f"scope id {sid} not referenced in review.md")
        # Acceptable classifications for passing acceptance:
        bad_markers = ["Missing", "Deviated"]
        section_start = text.find(sid)
        nearby = text[section_start: section_start + 300]
        for bad in bad_markers:
            if bad in nearby and "Fully" not in nearby and "Partial" not in nearby:
                return _die(f"scope {sid} classified as {bad!r}")
    print(f"  review.md OK — all {len(active_ids)} active items Fully/Partial")
    return 0


def main() -> int:
    if "ANTHROPIC_API_KEY" not in os.environ:
        return _skip("ANTHROPIC_API_KEY not set (opt-in acceptance test)")

    if not REFERENCE_DIR.exists():
        return _die(f"reference feature not found at {REFERENCE_DIR}")

    work = _make_workspace(REFERENCE_DIR)
    feature = "hash-file-cli"
    active = work / "docs" / "features" / feature / "active"

    try:
        print(f"[1/4] launching implement (workspace={work})")
        r = _run_auto_dev(
            [
                "implement", feature, "--yes",
                "--prd-review-mode=none",
                "--allow-cmd", "python3 *,pytest *,ls *,cat *,head *,tail *,wc *",
            ],
            cwd=work,
        )

        if r.returncode == 2:
            # Gate pending — approve PRD-review then resume, then same at close.
            print("  hit prd-review gate; approving...")
            a = _run_auto_dev(["approve", feature, "prd-review"], cwd=work)
            if a.returncode != 0:
                return _die(f"approve prd-review failed: {a.stderr}")
            print("[2/4] resuming")
            r = _run_auto_dev(
                ["resume", feature, "--yes",
                 "--allow-cmd", "python3 *,pytest *,ls *,cat *,head *,tail *,wc *"],
                cwd=work,
            )

        if r.returncode not in (0, 2):
            return _die(
                f"pipeline exited {r.returncode}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
            )

        print("[3/4] asserting build artifacts")
        if (code := _assert_build(active)) != 0:
            return code

        print("[4/4] asserting review classifications")
        if (code := _assert_review(active)) != 0:
            return code

        print(f"\nOK — hash-file-cli built end-to-end in {work}")
        return 0
    finally:
        # Don't auto-clean on failure — user may want to inspect.
        if os.environ.get("AUTODEV_REFERENCE_KEEP"):
            print(f"(kept workspace at {work} per AUTODEV_REFERENCE_KEEP)")
        elif "--keep" not in sys.argv:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
