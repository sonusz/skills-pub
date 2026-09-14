from __future__ import annotations

import json
import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

from autodev import overrides_api as ov
from autodev import ralph
from autodev import __version__ as HARNESS_VERSION
from autodev.artifacts.common import write_markdown_with_hash
from autodev.artifacts.design_packet import write_accepted_design, write_design_packet
from autodev.artifacts.revision_state import RevisionState, load_state, write_state
from autodev.artifacts.scope import Scope, ScopeItem, write_scope
from autodev.artifacts.verdict import PanelVerdict, write_verdict
from autodev.state.cascade import StalenessCascade
from autodev.errors import GatePending, PreflightError, SchemaError
from autodev.state.atomic import atomic_write_json
from autodev.orchestrator import (
    Orchestrator,
    OrchestratorConfig,
)
from autodev.state.hashing import hash_file
from autodev.vendors.config import (
    PanelConfig,
    PanelReviewerSpec,
    PanelSynthesizerSpec,
    ProbeConfig,
    STAGES,
    StageSpec,
    VendorsConfig,
)


def _write_fake_vendor(path: Path) -> Path:
    path.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            from __future__ import annotations

            import datetime
            import hashlib
            import json
            import os
            import re
            import subprocess
            import sys
            from pathlib import Path

            DATE = datetime.date.today().isoformat()

            def extract(prompt: str, key: str) -> Path | None:
                m = re.search(rf"{re.escape(key)}\\s*[:=]\\s*`?([^\\s`\\n]+)`?", prompt)
                return Path(m.group(1)) if m else None

            def write_tmp(target: Path, body: str) -> None:
                tmp = target.with_name(target.name + ".tmp")
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(body, encoding="utf-8")

            def hash_file(path: Path) -> str:
                return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

            def bump(active: Path, stage: str) -> int:
                counter = active / "scratch" / f".{stage}.count"
                counter.parent.mkdir(parents=True, exist_ok=True)
                n = int(counter.read_text() or "0") if counter.exists() else 0
                n += 1
                counter.write_text(str(n), encoding="utf-8")
                return n

            def log_prompt(active: Path, stage: str, n: int, prompt: str) -> None:
                scratch = active / "scratch"
                scratch.mkdir(parents=True, exist_ok=True)
                (scratch / f".{stage}.{n}.prompt").write_text(prompt, encoding="utf-8")

            def render_ralph_review(statuses: dict[str, str], scope_hash: str) -> str:
                # v3-core: ralph-review.json (strict JSON schema)
                counts = {"Fully": 0, "Partial": 0, "Missing": 0, "Deviated": 0, "Deferred": 0}
                for st in statuses.values():
                    counts[st] = counts.get(st, 0) + 1
                classifications = [
                    {"req_id": f"{sid}.r1", "scope_id": sid,
                     "classification": st, "evidence": f"build iter evidence for {sid}"}
                    for sid, st in statuses.items()
                ]
                return json.dumps({
                    "classifications": classifications,
                    "summary": counts,
                    "design_conformance": {
                        "verdict": "Aligned",
                        "findings": [],
                    },
                }, indent=2) + "\\n"

            stdin_prompt = sys.stdin.read()
            if stdin_prompt:
                prompt = stdin_prompt
            elif len(sys.argv) >= 2 and sys.argv[-1] != "-":
                prompt = sys.argv[-1]
            else:
                print("fake vendor: missing prompt", file=sys.stderr)
                sys.exit(2)
            tgt_scope = extract(prompt, "TARGET_ARTIFACT")
            tgt_trace = extract(prompt, "TARGET_TRACE")
            tgt_test_plan = extract(prompt, "TARGET_TEST_PLAN")
            tgt_build = extract(prompt, "TARGET_BUILD_JSON")
            tgt_spec = extract(prompt, "TARGET_SPEC")
            tgt_readme = extract(prompt, "TARGET_README")
            tgt_review = extract(prompt, "TARGET_REVIEW")
            tgt_ralph = extract(prompt, "TARGET_RALPH_REVIEW")

            primary = tgt_ralph or tgt_build or tgt_trace or tgt_scope or tgt_spec or tgt_review
            if primary is None:
                print("fake vendor: no target found", file=sys.stderr)
                sys.exit(2)
            active = primary.parent

            if tgt_build:
                n = bump(active, "build")
                log_prompt(active, "build", n, prompt)
                if (
                    os.environ.get("AUTODEV_PHASE5B_MISSING_BUILD_ONCE") == "1"
                    and n == 1
                ):
                    sys.exit(0)
                if (
                    os.environ.get("AUTODEV_PHASE5B_COMMIT_BASELINE_ONCE") == "1"
                    and n == 1
                ):
                    subprocess.run(
                        ["git", "commit", "-q", "-m", "commit prior residue"],
                        cwd=Path.cwd(), check=True,
                    )
                if os.environ.get("AUTODEV_PHASE5B_DIRTY_ONCE") == "1":
                    changed = Path.cwd() / "src" / "interrupted.py"
                    if n == 1:
                        changed.parent.mkdir(parents=True, exist_ok=True)
                        changed.write_text("VALUE = 1\\n", encoding="utf-8")
                        subprocess.run(
                            ["git", "add", "--", str(changed.relative_to(Path.cwd()))],
                            cwd=Path.cwd(), check=True,
                        )
                    elif n == 2:
                        subprocess.run(
                            ["git", "commit", "-q", "-m", "finish interrupted build"],
                            cwd=Path.cwd(), check=True,
                        )
                if os.environ.get("AUTODEV_PHASE5B_AMEND_BUILD") == "1":
                    changed = Path.cwd() / "src" / "amended.py"
                    changed.parent.mkdir(parents=True, exist_ok=True)
                    changed.write_text(f"VALUE = {n}\\n", encoding="utf-8")
                    subprocess.run(
                        ["git", "add", "--", str(changed.relative_to(Path.cwd()))],
                        cwd=Path.cwd(), check=True,
                    )
                    subprocess.run(
                        ["git", "commit", "-q", "--amend", "--no-edit"],
                        cwd=Path.cwd(), check=True,
                    )
                scope_hash = hash_file(active / "scope.json")
                route_at = int(os.environ.get("AUTODEV_PHASE5B_ROUTE_AT", "0") or "0")
                route_layer = os.environ.get("AUTODEV_PHASE5B_ROUTE_LAYER", "")
                if route_layer and n == route_at:
                    scope_id = os.environ.get("AUTODEV_PHASE5B_SCOPE_ID", "t-1")
                    body = {
                        "source": str(active / "scope.json"),
                        "source_hash": scope_hash,
                        "written": DATE,
                        "test_cmd_run": f"fake-build-{n}",
                        "test_exit_code": 0,
                        "test_results": {"passed": 1, "failed": 0, "skipped": 0},
                        "files_changed": ["src/generated.py"],
                        "lint": {"passed": True, "cmd": "n/a"},
                        "deviations": [{
                            "scope_id": scope_id,
                            "severity": "blocking",
                            "blocking": True,
                            "detail": "upstream defect",
                            "diagnosis": {
                                "defective_layer": route_layer,
                                "evidence": "PRD-backed routing evidence here",
                            },
                        }],
                        "blocking": True,
                        "workspace_dirty_at_stage_end": False,
                    }
                else:
                    body = {
                        "source": str(active / "scope.json"),
                        "source_hash": scope_hash,
                        "written": DATE,
                        "test_cmd_run": f"fake-build-{n}",
                        "test_exit_code": 0,
                        "test_results": {"passed": 1, "failed": 0, "skipped": 0},
                        "files_changed": ["src/generated.py"],
                        "lint": {"passed": True, "cmd": "n/a"},
                        "deviations": [],
                        "blocking": False,
                        "workspace_dirty_at_stage_end": False,
                    }
                write_tmp(tgt_build, json.dumps(body, indent=2) + "\\n")
                sys.exit(0)

            if tgt_ralph:
                n = bump(active, "ralph-review")
                log_prompt(active, "ralph-review", n, prompt)
                malformed_at = int(os.environ.get("AUTODEV_PHASE5B_MALFORMED_AT", "0") or "0")
                if malformed_at and n == malformed_at:
                    # Write invalid JSON to trigger malformed handling
                    write_tmp(tgt_ralph, "not valid json\\n")
                    sys.exit(0)
                seq = json.loads(os.environ.get("AUTODEV_PHASE5B_SEQUENCE", "[]"))
                statuses = seq[min(n - 1, len(seq) - 1)]
                write_tmp(tgt_ralph, render_ralph_review(statuses, hash_file(active / "scope.json")))
                sys.exit(0)

            if tgt_spec:
                index_hash = hash_file(active / "implementation-index.json")
                write_tmp(
                    tgt_spec,
                    f"<!-- source: implementation-index.json -->\\n<!-- source_hash: {index_hash} -->\\n"
                    f"<!-- written: {DATE} -->\\n\\n## 1. Purpose\\nSpec.\\n",
                )
                if tgt_readme:
                    write_tmp(
                        tgt_readme,
                        f"<!-- source: implemented-spec.md -->\\n<!-- source_hash: {index_hash} -->\\n"
                        f"<!-- written: {DATE} -->\\n\\n# README\\n",
                    )
                sys.exit(0)

            if tgt_review:
                scope_hash = hash_file(active / "scope.json")
                write_tmp(
                    tgt_review,
                    f"<!-- source: scope.json -->\\n<!-- source_hash: {scope_hash} -->\\n"
                    f"<!-- written: {DATE} -->\\n\\n# Review\\n",
                )
                sys.exit(0)

            if tgt_trace and tgt_test_plan:
                n = bump(active, "design")
                log_prompt(active, "design", n, prompt)
                # Design artifacts hash against arch-design.md (its canonical
                # cascade upstream), not prd.md.
                arch_hash_match = extract(prompt, "ARCH_DESIGN_HASH")
                arch_hash = str(arch_hash_match) if arch_hash_match else hash_file(active / "arch-design.md")
                write_tmp(
                    tgt_trace,
                    f"<!-- source: arch-design.md -->\\n<!-- source_hash: {arch_hash} -->\\n"
                    f"<!-- written: {DATE} -->\\n\\n| # | Req ID | Scope ID | Requirement | Test(s) | Code Path | Status |\\n"
                    f"|---|---|---|---|---|---|---|\\n| 1 | t-1.r1 | t-1 | x | -- | -- | pending |\\n",
                )
                write_tmp(
                    tgt_test_plan,
                    f"<!-- source: arch-design.md -->\\n<!-- source_hash: {arch_hash} -->\\n"
                    f"<!-- written: {DATE} -->\\n\\n## Test Strategy\\n",
                )
                tgt_changelog = extract(prompt, "TARGET_CHANGELOG")
                if tgt_changelog is not None:
                    landed_changelog = active / "design-changelog.json"
                    entries = []
                    if landed_changelog.exists():
                        entries = json.loads(landed_changelog.read_text()).get("entries", [])
                    next_round = (entries[-1]["round"] + 1) if entries else 1
                    entries.append({
                        "round": next_round,
                        "trigger": "build",
                        "reason": "fake rebuttal",
                        "artifacts_changed": [],
                        "added": [],
                        "removed": [],
                    })
                    write_tmp(
                        tgt_changelog,
                        json.dumps({
                            "kind": "design-changelog",
                            "schema_version": 1,
                            "entries": entries,
                        }, indent=2) + "\\n",
                    )
                sys.exit(0)

            print("fake vendor: unsupported stage", file=sys.stderr)
            sys.exit(3)
            """
        ),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _orch(repo_root: Path, vendor_bin: Path) -> Orchestrator:
    os.environ["AUTODEV_VENDOR_BIN_CLAUDE"] = str(vendor_bin)
    vendors = VendorsConfig(
        path=repo_root / "vendors.yml",
        stages={
            s: StageSpec(stage=s, vendor="claude", model="fake", probe_interval_sec=30)
            for s in STAGES
        },
        panel=PanelConfig(
            reviewers=(
                PanelReviewerSpec(vendor="claude", model="fake-panel-claude"),
                PanelReviewerSpec(vendor="agy", model="fake-panel-agy"),
                PanelReviewerSpec(vendor="codex", model="fake-panel-codex"),
            ),
            synthesizer=PanelSynthesizerSpec(vendor="claude", model="fake-panel-synth"),
        ),
        probe=ProbeConfig(vendor="claude", model="fake-probe"),
    )
    return Orchestrator(OrchestratorConfig(
        repo_root=repo_root, vendors=vendors, session_id="phase5b-test",
    ))


def _seed_feature(active: Path, *, ids: list[str]) -> None:
    active.mkdir(parents=True, exist_ok=True)
    repo_root = active.parents[3]
    (repo_root / "docs").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "architecture-proposal.md").write_text(
        "# Architecture Proposal\n",
        encoding="utf-8",
    )
    (active / "architecture.md").write_text(
        "# Architecture Input\n\n- `docs/architecture-proposal.md`\n",
        encoding="utf-8",
    )
    (active / "prd.md").write_text("# PRD\n\n1. Loop review.\n", encoding="utf-8")
    prd_hash = hash_file(active / "prd.md")
    # core R4: design/scope/trace/test_plan's canonical upstream is now
    # arch-design.md, not prd.md directly — seed a passed initial design
    # + arch-review so the four design-package artifacts anchor to it.
    write_markdown_with_hash(
        active / "arch-design.md",
        "## 1. Goal\nDrive the build/ralph cycle.\n"
        "## 5. PRD coverage\n| R1 | build/ralph loop |\n",
        source=str(active / "prd.md"),
        source_hash=prd_hash,
    )
    arch_design_hash = hash_file(active / "arch-design.md")
    atomic_write_json(active / "arch-review.json", {
        "kind": "arch-review",
        "source": str(active / "arch-design.md"),
        "source_hash": arch_design_hash,
        "prd_hash": prd_hash,
        "written": "2026-04-21T00:00:00Z",
        "verdict": "pass",
        "findings": [],
    })
    write_markdown_with_hash(
        active / "design.md",
        "# Design\n\n## Loop\nDrive the build/ralph cycle from one design packet.\n\n"
        "Validation commands: [\"pytest -q\"]\n",
        source=str(active / "arch-design.md"),
        source_hash=arch_design_hash,
    )
    scope = Scope(
        source=str(active / "arch-design.md"),
        source_hash=arch_design_hash,
        written="2026-04-21",
        feature="demo",
        mode="fresh",
        diff_base="main",
        in_scope=[
            ScopeItem(id=sid, description=f"item {sid}", prd_ref=["§1"],
                      design_ref=["Loop"], status="active")
            for sid in ids
        ],
        excluded=[],
    )
    write_scope(active / "scope.json", scope)
    scope_hash = hash_file(active / "scope.json")
    trace_rows = "\n".join(
        f"| {i} | {sid}.r1 | {sid} | req {sid} | -- | -- | pending |"
        for i, sid in enumerate(ids, 1)
    )
    write_markdown_with_hash(
        active / "trace.md",
        f"| # | Req ID | Scope ID | Requirement | Test(s) | Code Path | Status |\n"
        f"|---|---|---|---|---|---|---|\n{trace_rows}\n",
        source=str(active / "arch-design.md"),
        source_hash=arch_design_hash,
    )
    write_markdown_with_hash(
        active / "test-plan.md",
        "## Test Strategy\nLoop coverage.\n",
        source=str(active / "arch-design.md"),
        source_hash=arch_design_hash,
    )
    packet = write_design_packet(active)
    zero_hash = "sha256:" + "0" * 64
    for gate, source_path in (
        ("design-review", packet),
        ("trace-review", packet),
    ):
        write_verdict(active / f"panel-{gate}.json", PanelVerdict(
            gate=gate,
            verdict="pass",
            findings=[],
            source=str(source_path),
            source_hash=hash_file(source_path),
            prompt_file="prompt.md",
            prompt_hash=zero_hash,
            harness_version=HARNESS_VERSION,
            run_ts="2026-04-21T00:00:00+00:00",
        ))
    write_accepted_design(active)


def _baseline_payload(repo_root: Path) -> dict:
    """The workspace_baseline an iteration context records at prepare time."""
    from autodev.workspace import snapshot

    snap = snapshot(repo_root)
    return {
        "raw": snap.raw,
        "head": snap.head,
        "path_fingerprints": dict(snap.path_fingerprints),
    }


def _count(active: Path, stage: str) -> int:
    p = active / "scratch" / f".{stage}.count"
    return int(p.read_text()) if p.exists() else 0


def test_build_loops_until_all_active_items_fully(git_repo, feature_active, monkeypatch):
    _seed_feature(feature_active, ids=["t-1", "t-2"])
    # decoy: stage-review's output file (v3-core: review.json). Should
    # stay untouched during the ralph loop (ralph writes ralph-review.json).
    (feature_active / "review.json").write_text("close approval decoy\n", encoding="utf-8")
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")

    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    orch = _orch(git_repo, vendor_bin)
    monkeypatch.setenv(
        "AUTODEV_PHASE5B_SEQUENCE",
        json.dumps([
            {"t-1": "Partial", "t-2": "Missing"},
            {"t-1": "Fully", "t-2": "Fully"},
        ]),
    )

    result = orch.advance_one("demo")

    assert result.stage_name == "build"
    assert result.success is True
    assert _count(feature_active, "build") == 2
    assert _count(feature_active, "ralph-review") == 2
    state = ralph.load_ralph_state(feature_active)
    assert state.iter == 2
    assert state.fully_history[-1] == {"t-1", "t-2"}
    assert (feature_active / "ralph-review.json").exists()
    assert (feature_active / "review.json").read_text(encoding="utf-8") == "close approval decoy\n"

    prompt = (
        feature_active / "scratch" / ".ralph-review.1.prompt"
    ).read_text(encoding="utf-8")
    # Ralph gets accepted design + trace, but remains isolated from
    # PRD/build/spec narration.
    assert "DESIGN_PACKET_PATH:" in prompt
    assert "ACCEPTED_DESIGN_PATH:" in prompt
    assert "DESIGN_PATH:" in prompt
    assert "SCOPE_PATH:" in prompt
    assert "TRACE_PATH:" in prompt
    assert "TARGET_RALPH_REVIEW:" in prompt
    assert "BUILD_JSON_PATH:" not in prompt
    assert "SPEC_PATH:" not in prompt
    assert "PRD_PATH:" not in prompt
    assert "TARGET_REVIEW:" not in prompt

    context_path = feature_active / "ralph-iteration-context.json"
    previous_path = feature_active / "ralph-review.previous.json"
    first_review_prompt = prompt
    second_review_prompt = (
        feature_active / "scratch" / ".ralph-review.2.prompt"
    ).read_text(encoding="utf-8")
    assert str(context_path) in first_review_prompt
    assert "- BUILD_BEFORE_REF:" in first_review_prompt
    assert "- BUILD_AFTER_REF:" in first_review_prompt
    assert f"- PREVIOUS_RALPH_REVIEW_PATH: `{previous_path}`" not in first_review_prompt
    assert f"- PREVIOUS_RALPH_REVIEW_PATH: `{previous_path}`" in second_review_prompt
    assert previous_path.exists()
    previous = json.loads(previous_path.read_text(encoding="utf-8"))
    assert {
        item["scope_id"]: item["classification"]
        for item in previous["classifications"]
    } == {"t-1": "Partial", "t-2": "Missing"}
    context = json.loads(context_path.read_text(encoding="utf-8"))
    assert context["iteration"] == 2
    assert context["status"] == "ready_for_review"
    assert context["previous_ralph_review_path"] == str(previous_path)
    assert context["diff"]["available"] is True

    first_build_prompt = (
        feature_active / "scratch" / ".build.1.prompt"
    ).read_text(encoding="utf-8")
    second_build_prompt = (
        feature_active / "scratch" / ".build.2.prompt"
    ).read_text(encoding="utf-8")
    assert str(feature_active / "ralph-review.json") not in first_build_prompt
    assert str(feature_active / "ralph-review.json") in second_build_prompt
    assert str(feature_active / "ralph-state.json") in second_build_prompt


def test_ralph_diff_context_preserves_amended_commit_delta(
    git_repo, feature_active, monkeypatch,
):
    """The captured pre-build SHA remains diffable after build amends HEAD."""
    _seed_feature(feature_active, ids=["t-1"])
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")
    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    orch = _orch(git_repo, vendor_bin)
    before = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(git_repo), text=True,
    ).strip()

    monkeypatch.setenv("AUTODEV_PHASE5B_AMEND_BUILD", "1")
    monkeypatch.setenv(
        "AUTODEV_PHASE5B_SEQUENCE", json.dumps([{"t-1": "Fully"}]),
    )
    result = orch.advance_one("demo")

    assert result.success is True
    context_path = feature_active / "ralph-iteration-context.json"
    context = json.loads(context_path.read_text(encoding="utf-8"))
    after = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(git_repo), text=True,
    ).strip()
    assert before != after
    assert context["before_ref"] == before
    assert context["after_ref"] == after
    assert context["diff"]["patch_command"][-3:] == [before, after, "--"]

    patch_result = subprocess.run(
        context["diff"]["patch_command"],
        cwd=str(git_repo), text=True, capture_output=True, check=True,
    )
    assert "src/amended.py" in patch_result.stdout
    review_prompt = (
        feature_active / "scratch" / ".ralph-review.1.prompt"
    ).read_text(encoding="utf-8")
    assert f"- BUILD_BEFORE_REF: `{before}`" in review_prompt
    assert f"- BUILD_AFTER_REF: `{after}`" in review_prompt
    assert f"- RALPH_ITERATION_CONTEXT_PATH: `{context_path}`" in review_prompt


def test_build_retries_until_product_changes_are_committed(
    git_repo, feature_active, monkeypatch,
):
    """A Build may not hand Ralph a staged tree while its commit is pending."""
    _seed_feature(feature_active, ids=["t-1"])
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")
    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    orch = _orch(git_repo, vendor_bin)
    before = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(git_repo), text=True,
    ).strip()

    monkeypatch.setenv("AUTODEV_PHASE5B_DIRTY_ONCE", "1")
    monkeypatch.setenv(
        "AUTODEV_PHASE5B_SEQUENCE", json.dumps([{"t-1": "Fully"}]),
    )
    result = orch.advance_one("demo")

    assert result.success is True
    assert _count(feature_active, "build") == 2
    assert _count(feature_active, "ralph-review") == 1
    assert not (feature_active / "build-output-rejection.json").exists()
    assert subprocess.check_output(
        ["git", "status", "--short", "--", "src/interrupted.py"],
        cwd=str(git_repo), text=True,
    ) == ""

    context_path = feature_active / "ralph-iteration-context.json"
    context = json.loads(context_path.read_text(encoding="utf-8"))
    assert context["before_ref"] == before
    assert context["before_ref"] != context["after_ref"]
    patch_result = subprocess.run(
        context["diff"]["patch_command"],
        cwd=str(git_repo), text=True, capture_output=True, check=True,
    )
    assert "src/interrupted.py" in patch_result.stdout

    retry_prompt = (
        feature_active / "scratch" / ".build.2.prompt"
    ).read_text(encoding="utf-8")
    assert str(feature_active / "build-output-rejection.json") in retry_prompt
    assert "Create one\nsynchronous `WIP: <feature> iter N` commit" in retry_prompt
    assert any(
        event.get("stage") == "build"
        and event.get("event") == "output-rejected-retrying"
        and event.get("detail", {}).get("kind") == "uncommitted_product_changes"
        for event in _read_log(feature_active)
    )
    assert any(
        event.get("stage") == "build"
        and event.get("event") == "session-reset-after-output-rejection"
        and event.get("detail", {}).get("kind") == "uncommitted_product_changes"
        for event in _read_log(feature_active)
    )


def test_build_retry_refreshes_baseline_after_prior_residue_is_committed(
    git_repo, feature_active, monkeypatch,
):
    """Committing pre-existing dirt is a repair, not uncommitted residue.

    The baseline comparison used to be symmetric, so acknowledged dirt that
    the build committed showed up as ``dirty -> clean`` and rejected the
    attempt; the first attempt now passes outright.
    """
    _seed_feature(feature_active, ids=["t-1"])
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")
    residue = git_repo / "src" / "prior.py"
    residue.parent.mkdir(parents=True, exist_ok=True)
    residue.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "--", "src/prior.py"], cwd=str(git_repo), check=True,
    )

    monkeypatch.setenv("AUTODEV_PHASE5B_COMMIT_BASELINE_ONCE", "1")
    monkeypatch.setenv(
        "AUTODEV_PHASE5B_SEQUENCE", json.dumps([{"t-1": "Fully"}]),
    )
    orch = _orch(git_repo, _write_fake_vendor(git_repo / "fake_vendor.py"))
    result = orch.advance_one("demo")

    assert result.success is True
    assert _count(feature_active, "build") == 1
    assert _count(feature_active, "ralph-review") == 1
    assert not (feature_active / "build-output-rejection.json").exists()
    assert subprocess.check_output(
        ["git", "status", "--short", "--", "src/prior.py"],
        cwd=str(git_repo), text=True,
    ) == ""
    events = _read_log(feature_active)
    assert not any(
        event.get("stage") == "build"
        and event.get("event") == "output-rejected-retrying"
        for event in events
    )
    absorbed = next(
        event for event in events
        if event.get("stage") == "build"
        and event.get("event") == "baseline-dirt-committed"
    )
    assert absorbed["detail"]["entries"] == ["A  src/prior.py"]


def test_build_retry_still_rejects_new_residue_next_to_committed_dirt(
    git_repo, feature_active, monkeypatch,
):
    """Committing baseline dirt must not hide residue the build itself left."""
    _seed_feature(feature_active, ids=["t-1"])
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")
    residue = git_repo / "src" / "prior.py"
    residue.parent.mkdir(parents=True, exist_ok=True)
    residue.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "--", "src/prior.py"], cwd=str(git_repo), check=True,
    )

    monkeypatch.setenv("AUTODEV_PHASE5B_COMMIT_BASELINE_ONCE", "1")
    monkeypatch.setenv("AUTODEV_PHASE5B_DIRTY_ONCE", "1")
    monkeypatch.setenv(
        "AUTODEV_PHASE5B_SEQUENCE", json.dumps([{"t-1": "Fully"}]),
    )
    orch = _orch(git_repo, _write_fake_vendor(git_repo / "fake_vendor.py"))
    result = orch.advance_one("demo")

    assert result.success is True
    assert _count(feature_active, "build") == 2
    assert _count(feature_active, "ralph-review") == 1
    rejection = next(
        event for event in _read_log(feature_active)
        if event.get("stage") == "build"
        and event.get("event") == "output-rejected-retrying"
    )
    assert rejection["detail"]["kind"] == "uncommitted_product_changes"
    assert "src/interrupted.py" in rejection["detail"]["detail"]
    assert "src/prior.py" not in rejection["detail"]["detail"]


def test_build_retries_when_a_successful_turn_writes_no_artifact(
    git_repo, feature_active, monkeypatch,
):
    """Exit 0 without this turn's build.json must not launch an empty Ralph."""
    _seed_feature(feature_active, ids=["t-1"])
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")
    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    orch = _orch(git_repo, vendor_bin)

    monkeypatch.setenv("AUTODEV_PHASE5B_MISSING_BUILD_ONCE", "1")
    monkeypatch.setenv(
        "AUTODEV_PHASE5B_SEQUENCE", json.dumps([{"t-1": "Fully"}]),
    )
    result = orch.advance_one("demo")

    assert result.success is True
    assert _count(feature_active, "build") == 2
    assert _count(feature_active, "ralph-review") == 1
    assert not (feature_active / "build-output-rejection.json").exists()
    retry_prompt = (
        feature_active / "scratch" / ".build.2.prompt"
    ).read_text(encoding="utf-8")
    assert str(feature_active / "build-output-rejection.json") in retry_prompt
    assert any(
        event.get("stage") == "build"
        and event.get("event") == "output-rejected-retrying"
        and event.get("detail", {}).get("kind") == "missing_artifact"
        for event in _read_log(feature_active)
    )


def test_ralph_diff_context_reuses_original_before_ref_on_reentry(
    git_repo, feature_active,
):
    """An interrupted iteration keeps its first pre-build pointer."""
    _seed_feature(feature_active, ids=["t-1"])
    orch = _orch(git_repo, _write_fake_vendor(git_repo / "fake_vendor.py"))
    context_path = orch._prepare_ralph_iteration_context(feature_active)
    original = json.loads(context_path.read_text(encoding="utf-8"))

    changed = git_repo / "src" / "reentered.py"
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", "src/reentered.py"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--amend", "--no-edit"],
        cwd=git_repo, check=True,
    )

    same_path = orch._prepare_ralph_iteration_context(feature_active)
    reentered = json.loads(same_path.read_text(encoding="utf-8"))
    assert same_path == context_path
    assert reentered["iteration"] == 1
    assert reentered["before_ref"] == original["before_ref"]
    assert reentered["after_ref"] is None


def test_malformed_ralph_review_retries_then_amends_and_succeeds(
    git_repo, feature_active, monkeypatch,
):
    """A malformed ralph-review must NOT kill the run. The harness
    re-dispatches the SAME review agent, handing it the prior (rejected)
    review + a rejection note, and the retry produces a valid list."""
    _seed_feature(feature_active, ids=["t-1"])
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")
    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    orch = _orch(git_repo, vendor_bin)

    monkeypatch.setenv("AUTODEV_PHASE5B_SEQUENCE", json.dumps([{"t-1": "Fully"}]))
    # First ralph-review pass emits invalid JSON; the retry (n=2) emits a
    # valid list from the sequence.
    monkeypatch.setenv("AUTODEV_PHASE5B_MALFORMED_AT", "1")

    result = orch.advance_one("demo")

    assert result.success is True
    assert result.stage_name == "build"
    # Build ran once; ralph-review ran twice (reject + amend).
    assert _count(feature_active, "build") == 1
    assert _count(feature_active, "ralph-review") == 2
    state = ralph.load_ralph_state(feature_active)
    # Exactly one iteration is RECORDED — the rejected pass does not count.
    assert state.iter == 1
    assert state.fully_history[-1] == {"t-1"}
    # Rejection note is cleaned up once validation passes.
    assert not (feature_active / "ralph-review-output-rejection.json").exists()

    # The retry prompt must hand the agent the prior review + the
    # structured rejection so it amends rather than reclassifies blind.
    retry_prompt = (
        feature_active / "scratch" / ".ralph-review.2.prompt"
    ).read_text(encoding="utf-8")
    assert "CONTEXT_ARTIFACTS" in retry_prompt
    assert str(feature_active / "ralph-review.json") in retry_prompt
    assert str(feature_active / "ralph-review-output-rejection.json") in retry_prompt


def test_output_retry_keeps_previous_accepted_review_file(
    git_repo, feature_active, monkeypatch,
):
    _seed_feature(feature_active, ids=["t-1"])
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")
    prior = {
        "classifications": [{
            "req_id": "t-1.r1", "scope_id": "t-1",
            "classification": "Partial", "evidence": "src/old.py:1",
        }],
        "summary": {
            "Fully": 0, "Partial": 1, "Missing": 0,
            "Deviated": 0, "Deferred": 0,
        },
        "design_conformance": {
            "verdict": "Aligned", "findings": [],
        },
    }
    (feature_active / "ralph-review.json").write_text(
        json.dumps(prior) + "\n", encoding="utf-8",
    )
    ralph.write_ralph_state(feature_active, ralph.RalphState(
        source=str(feature_active / "scope.json"),
        source_hash=hash_file(feature_active / "scope.json"),
        trace_hash=hash_file(feature_active / "trace.md"),
        test_plan_hash=hash_file(feature_active / "test-plan.md"),
        design_hash=hash_file(feature_active / "design.md"),
        iter=1,
        fully_history=[set(), set()],
        statuses_history=[{}, {"t-1": "Partial"}],
    ))
    orch = _orch(git_repo, _write_fake_vendor(git_repo / "fake_vendor.py"))
    monkeypatch.setenv("AUTODEV_PHASE5B_SEQUENCE", json.dumps([{"t-1": "Fully"}]))
    monkeypatch.setenv("AUTODEV_PHASE5B_MALFORMED_AT", "1")

    result = orch.advance_one("demo")

    assert result.success is True
    previous_path = feature_active / "ralph-review.previous.json"
    assert json.loads(previous_path.read_text(encoding="utf-8")) == prior
    retry_prompt = (
        feature_active / "scratch" / ".ralph-review.2.prompt"
    ).read_text(encoding="utf-8")
    assert f"- PREVIOUS_RALPH_REVIEW_PATH: `{previous_path}`" in retry_prompt
    assert str(feature_active / "ralph-review-output-rejection.json") in retry_prompt


def test_incomplete_ralph_review_exhausts_retries_then_raises(
    git_repo, feature_active, monkeypatch,
):
    """If the review never covers every active scope item, the harness
    retries up to the cap and only then surfaces the schema error."""
    from autodev.orchestrator import STAGE_OUTPUT_RETRY_MAX

    _seed_feature(feature_active, ids=["t-1", "t-2"])
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")
    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    orch = _orch(git_repo, vendor_bin)

    # Every pass classifies only t-1, so t-2 coverage is always missing.
    monkeypatch.setenv("AUTODEV_PHASE5B_SEQUENCE", json.dumps([{"t-1": "Fully"}]))

    with pytest.raises(SchemaError, match="missing active scope classifications"):
        orch.advance_one("demo")

    assert _count(feature_active, "build") == 1
    assert _count(feature_active, "ralph-review") == STAGE_OUTPUT_RETRY_MAX
    # The rejection note names the uncovered scope item for the operator.
    rej = json.loads(
        (feature_active / "ralph-review-output-rejection.json").read_text(encoding="utf-8")
    )
    assert rej["missing_scope_ids"] == ["t-2"]


def test_run_until_design_stops_before_build(git_repo, feature_active, monkeypatch):
    """`run --until design` advances through the design phase (which is
    already sealed by _seed_feature: accepted-design.json present) and
    stops cleanly before build, rather than entering the Ralph loop."""
    _seed_feature(feature_active, ids=["t-1"])
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")
    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    orch = _orch(git_repo, vendor_bin)

    # Sanity: with the design phase sealed, the next stage IS build.
    assert orch._next_stage_name(feature_active) == "build"

    orch.run("demo", stop_before="build")

    # Build never ran; the loop stopped at the phase boundary.
    assert _count(feature_active, "build") == 0
    assert not (feature_active / "ralph-state.json").exists()
    events = [
        (e.get("stage"), e.get("event")) for e in _read_log(feature_active)
    ]
    assert ("orchestrator", "stopped-at-boundary") in events
    assert ("orchestrator", "pipeline-done") not in events


def _read_log(active: Path) -> list[dict]:
    import json as _json
    p = active / "log.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(_json.loads(line))
    return out


def _seed_routable_build(feature_active: Path) -> None:
    """Seed a reviewed iteration whose next build may route to design."""
    state = ralph.RalphState(
        source=str(feature_active / "scope.json"),
        source_hash=hash_file(feature_active / "scope.json"),
        trace_hash=hash_file(feature_active / "trace.md"),
        test_plan_hash=hash_file(feature_active / "test-plan.md"),
        design_hash=hash_file(feature_active / "design.md"),
        iter=1,
        fully_history=[set(), {"t-1"}],
        statuses_history=[{}, {"t-1": "Fully"}],
    )
    ralph.write_ralph_state(feature_active, state)
    (feature_active / "ralph-review.json").write_text("stale\\n", encoding="utf-8")
    (feature_active / "rework-mode.json").write_text(
        '{"mode": "patch"}', encoding="utf-8",
    )
    (feature_active / "design-changelog.json").write_text(
        json.dumps({
            "kind": "design-changelog",
            "schema_version": 1,
            "entries": [{
                "round": 1,
                "trigger": "initial",
                "reason": "seed",
                "artifacts_changed": [],
                "added": [],
                "removed": [],
            }],
        }, indent=2) + "\n",
        encoding="utf-8",
    )
    # design-packet.json's context_refs pick up design-changelog.json once it
    # exists, so the packet/panel verdicts/accepted-design seeded by
    # _seed_feature (before the changelog existed) must be regenerated here —
    # otherwise the staleness cascade sees a stale design_packet and reroutes
    # to it instead of to build, defeating the route-at-build-n=1 setup below.
    packet = write_design_packet(feature_active)
    zero_hash = "sha256:" + "0" * 64
    for gate, source_path in (
        ("design-review", packet),
        ("trace-review", packet),
    ):
        write_verdict(feature_active / f"panel-{gate}.json", PanelVerdict(
            gate=gate,
            verdict="pass",
            findings=[],
            source=str(source_path),
            source_hash=hash_file(source_path),
            prompt_file="prompt.md",
            prompt_hash=zero_hash,
            harness_version=HARNESS_VERSION,
            run_ts="2026-04-21T00:00:00+00:00",
        ))
    write_accepted_design(feature_active)


def test_build_route_dispatches_design_in_place(git_repo, feature_active, monkeypatch):
    """A build-routed rerun revises design in place: no artifact is deleted,
    design is dispatched within the same advance cycle, and the design
    agent receives build.json + both panel verdicts + its own prior
    artifacts through CONTEXT_ARTIFACTS (the same path a blocking panel
    verdict's LOCAL_REVISE branch already uses)."""
    _seed_feature(feature_active, ids=["t-1"])
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")
    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    orch = _orch(git_repo, vendor_bin)

    _seed_routable_build(feature_active)

    monkeypatch.setenv("AUTODEV_PHASE5B_ROUTE_LAYER", "design")
    monkeypatch.setenv("AUTODEV_PHASE5B_ROUTE_AT", "1")
    monkeypatch.setenv("AUTODEV_PHASE5B_SCOPE_ID", "t-1")

    result = orch.advance_one("demo")

    assert result.success is True
    assert _count(feature_active, "build") == 1
    assert _count(feature_active, "ralph-review") == 0
    assert _count(feature_active, "design") == 1

    # Nothing is deleted: every artifact from the core doc's preserved
    # list that existed by the time routing fired is still on disk.
    for name in (
        "design.md", "scope.json", "trace.md", "test-plan.md",
        "design-packet.json", "panel-design-review.json",
        "panel-trace-review.json", "accepted-design.json", "build.json",
        "ralph-state.json", "ralph-review.json",
        "ralph-iteration-context.json",
    ):
        assert (feature_active / name).exists(), f"{name} should be preserved"

    # The design agent actually produced a new round in place — proof
    # the preseeded in-place-revision path ran to completion, not just
    # that files survived. append-only semantics: the seeded entry is
    # still there, and a new one lands on top of it.
    assert (feature_active / "design-changelog.json").exists()
    changelog = json.loads(
        (feature_active / "design-changelog.json").read_text(encoding="utf-8")
    )
    assert len(changelog["entries"]) == 2
    assert changelog["entries"][-1]["round"] == 2
    assert changelog["entries"][-1]["trigger"] == "build"

    # rework-mode.json is the one documented exception: cleared so a
    # routed rerun falls back to root-cause instead of inheriting a
    # stale patch-mode computed from an already-superseded verdict.
    assert not (feature_active / "rework-mode.json").exists()

    # No route-feedback snapshot/registration mechanism: the feedback IS
    # the file on disk (build.json), read via CONTEXT_ARTIFACTS.
    assert not (feature_active / ".route-feedback.json").exists()
    state_after = load_state(feature_active)
    assert state_after.pending_feedback == {}
    assert state_after.L["design-review"] == 1

    design_prompt = (
        feature_active / "scratch" / ".design.1.prompt"
    ).read_text(encoding="utf-8")
    for expected in (
        "build.json", "panel-design-review.json", "panel-trace-review.json",
        "design.md", "scope.json", "trace.md", "test-plan.md",
        "design-changelog.json",
    ):
        assert str(feature_active / expected) in design_prompt
    assert "PRE-FILLED" in design_prompt

    assert orch._next_stage_name(feature_active) == "design_packet"


def test_missing_pending_feedback_fails_closed(git_repo, feature_active):
    orch = _orch(git_repo, _write_fake_vendor(git_repo / "fake_vendor.py"))
    state = RevisionState()
    state.pending_feedback = {"design": ["missing-feedback.json"]}
    write_state(feature_active, state)

    with pytest.raises(PreflightError, match="pending feedback.*missing"):
        orch._context_artifacts_for_stage(
            feature_active,
            "design",
            feature_active / "design.md",
            [
                feature_active / "scope.json",
                feature_active / "trace.md",
                feature_active / "test-plan.md",
                feature_active / "design-changelog.json",
            ],
        )


def test_fresh_design_does_not_rehydrate_discarded_design_context(
    git_repo, feature_active,
):
    """Invalidating design.md is a true rebaseline, not history replay."""
    orch = _orch(git_repo, _write_fake_vendor(git_repo / "fake_vendor.py"))
    extras = [
        feature_active / "scope.json",
        feature_active / "trace.md",
        feature_active / "test-plan.md",
        feature_active / "design-changelog.json",
    ]
    for path in extras:
        path.write_text("discarded prior design\n", encoding="utf-8")
    (feature_active / "panel-design-review.json").write_text(
        "discarded prior verdict\n", encoding="utf-8",
    )

    context = orch._context_artifacts_for_stage(
        feature_active,
        "design",
        feature_active / "design.md",
        extras,
    )

    assert context == []


def test_next_stage_requires_completed_ralph_loop_before_index(git_repo, feature_active):
    _seed_feature(feature_active, ids=["t-1", "t-2"])
    (feature_active / "build.json").write_text(
        json.dumps({
            "source": str(feature_active / "scope.json"),
            "source_hash": hash_file(feature_active / "scope.json"),
            "written": "2026-04-21",
            "test_cmd_run": "pytest",
            "test_exit_code": 0,
            "test_results": {"passed": 1, "failed": 0, "skipped": 0},
            "files_changed": [],
            "lint": {"passed": True, "cmd": "n/a"},
            "deviations": [],
            "blocking": False,
            "workspace_dirty_at_stage_end": False,
        }),
        encoding="utf-8",
    )
    import json as _json
    (feature_active / "ralph-review.json").write_text(_json.dumps({
        "classifications": [
            {"req_id": "t-1.r1", "scope_id": "t-1",
             "classification": "Fully", "evidence": "x"},
        ],
        "summary": {"Fully": 1, "Partial": 0, "Missing": 0, "Deviated": 0, "Deferred": 0},
        "design_conformance": {
            "verdict": "Aligned", "findings": [],
        },
    }))
    ralph.write_ralph_state(feature_active, ralph.RalphState(
        source=str(feature_active / "scope.json"),
        source_hash=hash_file(feature_active / "scope.json"),
        trace_hash=hash_file(feature_active / "trace.md"),
        test_plan_hash=hash_file(feature_active / "test-plan.md"),
        design_hash=hash_file(feature_active / "design.md"),
        iter=1,
        fully_history=[set(), {"t-1"}],
        statuses_history=[{}, {"t-1": "Fully"}],
    ))
    orch = _orch(git_repo, _write_fake_vendor(git_repo / "fake_vendor.py"))
    assert orch._next_stage_name(feature_active) == "build"

    (feature_active / "ralph-review.json").write_text(_json.dumps({
        "classifications": [
            {"req_id": "t-1.r1", "scope_id": "t-1",
             "classification": "Fully", "evidence": "x"},
            {"req_id": "t-2.r1", "scope_id": "t-2",
             "classification": "Fully", "evidence": "x"},
        ],
        "summary": {"Fully": 2, "Partial": 0, "Missing": 0, "Deviated": 0, "Deferred": 0},
        "design_conformance": {
            "verdict": "Aligned", "findings": [],
        },
    }))
    ralph.write_ralph_state(feature_active, ralph.RalphState(
        source=str(feature_active / "scope.json"),
        source_hash=hash_file(feature_active / "scope.json"),
        trace_hash=hash_file(feature_active / "trace.md"),
        test_plan_hash=hash_file(feature_active / "test-plan.md"),
        design_hash=hash_file(feature_active / "design.md"),
        iter=2,
        fully_history=[set(), {"t-1"}, {"t-1", "t-2"}],
        statuses_history=[{}, {"t-1": "Fully"}, {"t-1": "Fully", "t-2": "Fully"}],
    ))
    assert orch._next_stage_name(feature_active) == "implementation_index"

    # A build that re-reports blocking (e.g. after an in-place design
    # dispatch route) must not be treated as a completed Ralph loop even
    # though ralph-state and ralph-review still show full coverage from
    # the prior round.
    (feature_active / "build.json").write_text(_json.dumps({
        "source": str(feature_active / "scope.json"),
        "source_hash": hash_file(feature_active / "scope.json"),
        "written": "2026-04-21",
        "test_cmd_run": "pytest",
        "test_exit_code": 0,
        "test_results": {"passed": 1, "failed": 0, "skipped": 0},
        "files_changed": [],
        "lint": {"passed": True, "cmd": "n/a"},
        "deviations": [
            {"scope_id": "t-1", "severity": "blocking", "blocking": True, "detail": "x"},
        ],
        "blocking": True,
        "workspace_dirty_at_stage_end": False,
    }))
    assert orch._next_stage_name(feature_active) == "build"

    (feature_active / "build.json").write_text(_json.dumps({
        "source": str(feature_active / "scope.json"),
        "source_hash": hash_file(feature_active / "scope.json"),
        "written": "2026-04-21",
        "test_cmd_run": "pytest",
        "test_exit_code": 0,
        "test_results": {"passed": 1, "failed": 0, "skipped": 0},
        "files_changed": [],
        "lint": {"passed": True, "cmd": "n/a"},
        "deviations": [],
        "blocking": False,
        "workspace_dirty_at_stage_end": False,
    }))
    assert orch._next_stage_name(feature_active) == "implementation_index"


def test_restart_reviews_current_build_without_dispatching_build_vendor(
    git_repo, feature_active, monkeypatch,
):
    _seed_feature(feature_active, ids=["t-1"])
    # The fake vendor script lives in the repo root; write it before the
    # baseline is recorded or the predicate rightly reports it as residue.
    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(git_repo), text=True,
    ).strip()
    (feature_active / "build.json").write_text(json.dumps({
        "source": str(feature_active / "scope.json"),
        "source_hash": hash_file(feature_active / "scope.json"),
        "written": "2026-04-21",
        "test_cmd_run": "pytest",
        "test_exit_code": 0,
        "test_results": {"passed": 1, "failed": 0, "skipped": 0},
        "files_changed": ["src/generated.py"],
        "lint": {"passed": True, "cmd": "n/a"},
        "deviations": [],
        "blocking": False,
        "workspace_dirty_at_stage_end": False,
    }), encoding="utf-8")
    (feature_active / "ralph-iteration-context.json").write_text(json.dumps({
        "schema": 1,
        "iteration": 1,
        "status": "build_pending",
        "written_at": "2026-04-21T00:00:00+00:00",
        "repo_root": str(git_repo),
        "trace_hash": hash_file(feature_active / "trace.md"),
        "before_ref": head,
        "after_ref": head,  # recorded when the build subprocess returned
        "build_hash_before": None,
        "workspace_baseline": _baseline_payload(git_repo),
        "previous_ralph_review_path": None,
        "previous_ralph_review_hash": None,
        "diff": None,
    }), encoding="utf-8")
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")

    orch = _orch(git_repo, vendor_bin)
    monkeypatch.setenv(
        "AUTODEV_PHASE5B_SEQUENCE", json.dumps([{"t-1": "Fully"}]),
    )

    assert StalenessCascade(feature_active).next_stage() == "implementation_index"
    result = orch.advance_one("demo")

    assert result.success is True
    assert _count(feature_active, "build") == 0
    assert _count(feature_active, "ralph-review") == 1
    context = json.loads(
        (feature_active / "ralph-iteration-context.json").read_text(encoding="utf-8")
    )
    assert context["status"] == "ready_for_review"
    assert context["before_ref"] == head
    assert context["after_ref"] == head


def test_resume_reruns_partially_persisted_iteration(git_repo, feature_active, monkeypatch):
    _seed_feature(feature_active, ids=["t-1"])
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")
    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    orch = _orch(git_repo, vendor_bin)

    ralph.write_ralph_state(feature_active, ralph.RalphState(
        source=str(feature_active / "scope.json"),
        source_hash=hash_file(feature_active / "scope.json"),
        trace_hash=hash_file(feature_active / "trace.md"),
        test_plan_hash=hash_file(feature_active / "test-plan.md"),
        design_hash=hash_file(feature_active / "design.md"),
        iter=1,
        fully_history=[set(), set()],
        statuses_history=[{}, {"t-1": "Missing"}],
    ))
    (feature_active / "build.json").write_text("stale build\n", encoding="utf-8")
    (feature_active / "ralph-review.json").write_text("stale review\n", encoding="utf-8")

    monkeypatch.setenv("AUTODEV_PHASE5B_SEQUENCE", json.dumps([{"t-1": "Fully"}]))
    result = orch.advance_one("demo")

    assert result.success is True
    assert _count(feature_active, "build") == 1
    assert _count(feature_active, "ralph-review") == 1
    state = ralph.load_ralph_state(feature_active)
    assert state.iter == 2
    assert state.fully_history[-1] == {"t-1"}


def test_upstream_change_resets_only_ralph_state_and_restarts_iter_one(
    git_repo, feature_active, monkeypatch,
):
    _seed_feature(feature_active, ids=["t-1"])
    src_dir = git_repo / "src"
    src_dir.mkdir()
    keep = src_dir / "keep.py"
    keep.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(git_repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed workspace"], cwd=str(git_repo), check=True)
    head_before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(git_repo), text=True).strip()

    ralph.write_ralph_state(feature_active, ralph.RalphState(
        source=str(feature_active / "scope.json"),
        source_hash=hash_file(feature_active / "scope.json"),
        trace_hash=hash_file(feature_active / "trace.md"),
        test_plan_hash=hash_file(feature_active / "test-plan.md"),
        design_hash=hash_file(feature_active / "design.md"),
        iter=2,
        fully_history=[set(), set(), set()],
        statuses_history=[{}, {"t-1": "Missing"}, {"t-1": "Partial"}],
    ))
    (feature_active / "trace.md").write_text(
        (feature_active / "trace.md").read_text(encoding="utf-8") + "\n<!-- changed -->\n",
        encoding="utf-8",
    )
    packet = write_design_packet(feature_active)
    zero_hash = "sha256:" + "0" * 64
    write_verdict(feature_active / "panel-design-review.json", PanelVerdict(
        gate="design-review",
        verdict="pass",
        findings=[],
        source=str(packet),
        source_hash=hash_file(packet),
        prompt_file="prompt.md",
        prompt_hash=zero_hash,
        harness_version=HARNESS_VERSION,
        run_ts="2026-04-21T00:00:00+00:00",
    ))
    write_verdict(feature_active / "panel-trace-review.json", PanelVerdict(
        gate="trace-review",
        verdict="pass",
        findings=[],
        source=str(packet),
        source_hash=hash_file(packet),
        prompt_file="prompt.md",
        prompt_hash=zero_hash,
        harness_version=HARNESS_VERSION,
        run_ts="2026-04-21T00:00:00+00:00",
    ))
    write_accepted_design(feature_active)
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")
    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    orch = _orch(git_repo, vendor_bin)
    monkeypatch.setenv("AUTODEV_PHASE5B_SEQUENCE", json.dumps([{"t-1": "Fully"}]))

    result = orch.advance_one("demo")

    assert result.success is True
    state = ralph.load_ralph_state(feature_active)
    assert state.iter == 1
    assert keep.read_text(encoding="utf-8") == "VALUE = 1\n"
    head_after = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(git_repo), text=True).strip()
    assert head_after == head_before


def test_loop_has_no_hard_cap_and_does_not_call_detect_stall(
    git_repo, feature_active, monkeypatch,
):
    _seed_feature(feature_active, ids=["t-1", "t-2"])
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")
    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    orch = _orch(git_repo, vendor_bin)

    sequence = [{"t-1": "Fully", "t-2": "Partial"} for _ in range(31)]
    sequence.append({"t-1": "Fully", "t-2": "Fully"})
    monkeypatch.setenv("AUTODEV_PHASE5B_SEQUENCE", json.dumps(sequence))

    def _forbidden(*args, **kwargs):
        raise AssertionError("detect_stall should not be called in phase-5b loop")

    monkeypatch.setattr(ralph, "detect_stall", _forbidden)

    result = orch.advance_one("demo")

    assert result.success is True
    assert _count(feature_active, "build") == 32
    assert _count(feature_active, "ralph-review") == 32
    assert ralph.load_ralph_state(feature_active).iter == 32


def test_restart_after_partial_review_runs_next_build_not_empty_review(
    git_repo, feature_active, monkeypatch,
):
    """Pause between review N and build N+1, then restart.

    build.json is still "fresh" by input hashes, but the current build was
    already reviewed (Partial). A restart must run the rework build, not
    re-review the landed build against an empty diff — an empty-diff
    "Fully" would end the loop with the Partial findings never acted on.
    """
    _seed_feature(feature_active, ids=["t-1"])
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")
    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    orch = _orch(git_repo, vendor_bin)
    monkeypatch.setenv(
        "AUTODEV_PHASE5B_SEQUENCE",
        json.dumps([{"t-1": "Partial"}, {"t-1": "Fully"}]),
    )
    # Trip the in-loop pause checkpoint as soon as one iteration is recorded.
    monkeypatch.setattr(
        orch, "_check_pause_sentinel",
        lambda active: ralph.load_ralph_state(active).iter >= 1,
    )
    with pytest.raises(GatePending, match="pause"):
        orch.advance_one("demo")
    assert _count(feature_active, "build") == 1
    assert _count(feature_active, "ralph-review") == 1
    assert ralph.load_ralph_state(feature_active).iter == 1
    assert StalenessCascade(feature_active).next_stage() != "build"

    resumed = _orch(git_repo, vendor_bin)
    result = resumed.advance_one("demo")

    assert result.success is True
    assert _count(feature_active, "build") == 2
    assert _count(feature_active, "ralph-review") == 2
    state = ralph.load_ralph_state(feature_active)
    assert state.iter == 2
    assert state.fully_history[-1] == {"t-1"}
    assert not any(
        event.get("event") == "ralph-resumed-from-current-build"
        for event in _read_log(feature_active)
    )


def test_restart_after_reviewer_crash_reviews_landed_build_without_rebuilding(
    git_repo, feature_active, monkeypatch,
):
    """A build that landed but was never reviewed is reviewed on restart."""
    _seed_feature(feature_active, ids=["t-1"])
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")
    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    orch = _orch(git_repo, vendor_bin)
    monkeypatch.setenv("AUTODEV_PHASE5B_SEQUENCE", json.dumps([{"t-1": "Fully"}]))

    def crash(*args, **kwargs):
        raise RuntimeError("reviewer process died")

    monkeypatch.setattr(orch, "_run_ralph_review", crash)
    with pytest.raises(RuntimeError, match="reviewer process died"):
        orch.advance_one("demo")
    assert _count(feature_active, "build") == 1
    assert _count(feature_active, "ralph-review") == 0
    context = json.loads(
        (feature_active / "ralph-iteration-context.json").read_text(encoding="utf-8")
    )
    assert context["status"] == "ready_for_review"
    assert context["iteration"] == 1

    resumed = _orch(git_repo, vendor_bin)
    result = resumed.advance_one("demo")

    assert result.success is True
    assert _count(feature_active, "build") == 1
    assert _count(feature_active, "ralph-review") == 1
    assert ralph.load_ralph_state(feature_active).iter == 1
    assert any(
        event.get("event") == "ralph-resumed-from-current-build"
        and event.get("detail", {}).get("iteration") == 1
        for event in _read_log(feature_active)
    )


def test_resumed_blocking_build_dispatches_design_instead_of_only_routing(
    git_repo, feature_active, monkeypatch,
):
    """The resume path must dispatch the routed design rerun like the main loop.

    Returning a bare "routed" result would leave build.json blocking, so
    every subsequent advance re-enters the resume path, routes again, and
    burns an L slot until L_MAX halts the run without design ever running.
    """
    _seed_feature(feature_active, ids=["t-1"])
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")
    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    _seed_routable_build(feature_active)
    monkeypatch.setenv("AUTODEV_PHASE5B_ROUTE_LAYER", "design")
    monkeypatch.setenv("AUTODEV_PHASE5B_ROUTE_AT", "1")
    monkeypatch.setenv("AUTODEV_PHASE5B_SCOPE_ID", "t-1")

    orch = _orch(git_repo, vendor_bin)

    def crash(*args, **kwargs):
        raise RuntimeError("orchestrator died after build landed")

    monkeypatch.setattr(orch, "_enforce_build_blocking", crash)
    with pytest.raises(RuntimeError, match="after build landed"):
        orch.advance_one("demo")
    assert _count(feature_active, "build") == 1
    assert _count(feature_active, "design") == 0

    resumed = _orch(git_repo, vendor_bin)
    result = resumed.advance_one("demo")

    assert result.success is True
    assert _count(feature_active, "build") == 1
    assert _count(feature_active, "ralph-review") == 0
    assert _count(feature_active, "design") == 1
    assert load_state(feature_active).L["design-review"] == 1
    assert any(
        event.get("event") == "ralph-resumed-from-current-build"
        for event in _read_log(feature_active)
    )


def test_build_retry_survives_session_reset_helper_failure(
    git_repo, feature_active, monkeypatch,
):
    """A failing session reset must not turn a retryable rejection into a crash."""
    _seed_feature(feature_active, ids=["t-1"])
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")
    monkeypatch.setenv("AUTODEV_PHASE5B_DIRTY_ONCE", "1")
    monkeypatch.setenv(
        "AUTODEV_PHASE5B_SEQUENCE", json.dumps([{"t-1": "Fully"}]),
    )

    def broken_reset(active, role):
        raise RuntimeError("session helper exited 1")

    monkeypatch.setattr("autodev.orchestrator.reset_feature_session", broken_reset)
    orch = _orch(git_repo, _write_fake_vendor(git_repo / "fake_vendor.py"))
    result = orch.advance_one("demo")

    assert result.success is True
    assert _count(feature_active, "build") == 2
    assert _count(feature_active, "ralph-review") == 1
    events = _read_log(feature_active)
    assert any(
        event.get("stage") == "build"
        and event.get("event") == "session-reset-failed"
        and "session helper exited 1" in event.get("detail", {}).get("error", "")
        for event in events
    )
    assert any(
        event.get("stage") == "build"
        and event.get("event") == "output-rejected-retrying"
        for event in events
    )


def test_pending_context_with_unchanged_build_json_reruns_build(
    git_repo, feature_active, monkeypatch,
):
    """A pending iteration whose build never returned is built, not reviewed."""
    _seed_feature(feature_active, ids=["t-1"])
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(git_repo), text=True,
    ).strip()
    build_path = feature_active / "build.json"
    build_path.write_text(json.dumps({
        "source": str(feature_active / "scope.json"),
        "source_hash": hash_file(feature_active / "scope.json"),
        "written": "2026-04-21",
        "test_cmd_run": "pytest",
        "test_exit_code": 0,
        "test_results": {"passed": 1, "failed": 0, "skipped": 0},
        "files_changed": ["src/generated.py"],
        "lint": {"passed": True, "cmd": "n/a"},
        "deviations": [],
        "blocking": False,
        "workspace_dirty_at_stage_end": False,
    }), encoding="utf-8")
    (feature_active / "ralph-iteration-context.json").write_text(json.dumps({
        "schema": 1,
        "iteration": 1,
        "status": "build_pending",
        "written_at": "2026-04-21T00:00:00+00:00",
        "repo_root": str(git_repo),
        "trace_hash": hash_file(feature_active / "trace.md"),
        "before_ref": head,
        "after_ref": None,
        # build.json is exactly what it was when the iteration started.
        "build_hash_before": hash_file(build_path),
        "previous_ralph_review_path": None,
        "previous_ralph_review_hash": None,
        "diff": None,
    }), encoding="utf-8")
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")

    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    orch = _orch(git_repo, vendor_bin)
    monkeypatch.setenv(
        "AUTODEV_PHASE5B_SEQUENCE", json.dumps([{"t-1": "Fully"}]),
    )

    assert StalenessCascade(feature_active).next_stage() == "implementation_index"
    result = orch.advance_one("demo")

    assert result.success is True
    assert _count(feature_active, "build") == 1
    assert _count(feature_active, "ralph-review") == 1
    context = json.loads(
        (feature_active / "ralph-iteration-context.json").read_text(encoding="utf-8")
    )
    assert context["iteration"] == 1
    assert context["before_ref"] == head
    assert not any(
        event.get("event") == "ralph-resumed-from-current-build"
        for event in _read_log(feature_active)
    )


def test_rejected_build_with_pending_context_is_retried_not_reviewed(
    git_repo, feature_active, monkeypatch,
):
    """build.json is promoted before the residue check rejects the attempt.

    If the harness dies before the retry lands, the pending context and a
    moved build.json look like a landed build; the residue still on disk
    says otherwise, so the restart must run build again, not review it.
    """
    _seed_feature(feature_active, ids=["t-1"])
    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    _seed_landed_pending_iteration(git_repo, feature_active)
    (feature_active / "build-output-rejection.json").write_text(json.dumps({
        "stage": "build", "attempt": 1, "kind": "uncommitted_product_changes",
        "detail": "left src/x.py staged",
    }), encoding="utf-8")
    leftover = git_repo / "src" / "x.py"
    leftover.parent.mkdir(parents=True, exist_ok=True)
    leftover.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", "src/x.py"], cwd=str(git_repo), check=True)
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")

    orch = _orch(git_repo, vendor_bin)
    monkeypatch.setenv(
        "AUTODEV_PHASE5B_SEQUENCE", json.dumps([{"t-1": "Fully"}]),
    )
    assert StalenessCascade(feature_active).next_stage() == "implementation_index"

    result = orch.advance_one("demo")

    assert result.success is True
    assert _count(feature_active, "build") == 1
    assert _count(feature_active, "ralph-review") == 1
    assert not (feature_active / "build-output-rejection.json").exists()
    assert not any(
        event.get("event") == "ralph-resumed-from-current-build"
        for event in _read_log(feature_active)
    )


def test_stale_rejection_note_does_not_block_resume_of_clean_landed_build(
    git_repo, feature_active, monkeypatch,
):
    """Attempt 2 fixed attempt 1's residue and landed; the harness died
    before unlinking the note. The clean landed build is reviewed, not
    rebuilt against a stale amendment request."""
    _seed_feature(feature_active, ids=["t-1"])
    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    _seed_landed_pending_iteration(git_repo, feature_active)
    note = feature_active / "build-output-rejection.json"
    note.write_text(json.dumps({
        "stage": "build", "attempt": 1, "kind": "uncommitted_product_changes",
        "detail": "left src/x.py staged",
    }), encoding="utf-8")
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")

    orch = _orch(git_repo, vendor_bin)
    monkeypatch.setenv(
        "AUTODEV_PHASE5B_SEQUENCE", json.dumps([{"t-1": "Fully"}]),
    )
    result = orch.advance_one("demo")

    assert result.success is True
    assert _count(feature_active, "build") == 0
    assert _count(feature_active, "ralph-review") == 1
    assert not note.exists()
    assert any(
        event.get("event") == "ralph-resumed-from-current-build"
        for event in _read_log(feature_active)
    )


def test_pending_build_without_recorded_post_build_ref_is_rebuilt(
    git_repo, feature_active, monkeypatch,
):
    """No post-build ref means the harness died mid-build: nothing vouches
    for build.json, so it is rebuilt even though its hash moved."""
    _seed_feature(feature_active, ids=["t-1"])
    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    _seed_landed_pending_iteration(git_repo, feature_active)
    context_path = feature_active / "ralph-iteration-context.json"
    context = json.loads(context_path.read_text(encoding="utf-8"))
    context["after_ref"] = None
    context_path.write_text(json.dumps(context), encoding="utf-8")
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")

    orch = _orch(git_repo, vendor_bin)
    monkeypatch.setenv(
        "AUTODEV_PHASE5B_SEQUENCE", json.dumps([{"t-1": "Fully"}]),
    )
    result = orch.advance_one("demo")

    assert result.success is True
    assert _count(feature_active, "build") == 1
    assert not any(
        event.get("event") == "ralph-resumed-from-current-build"
        for event in _read_log(feature_active)
    )


def test_unreachable_baseline_head_rebuilds_instead_of_crashing(
    git_repo, feature_active, monkeypatch,
):
    """History rewritten under a pending iteration must not wedge the run."""
    _seed_feature(feature_active, ids=["t-1"])
    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    _seed_landed_pending_iteration(git_repo, feature_active)
    context_path = feature_active / "ralph-iteration-context.json"
    context = json.loads(context_path.read_text(encoding="utf-8"))
    context["workspace_baseline"]["head"] = "0" * 40
    context_path.write_text(json.dumps(context), encoding="utf-8")
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")

    orch = _orch(git_repo, vendor_bin)
    monkeypatch.setenv(
        "AUTODEV_PHASE5B_SEQUENCE", json.dumps([{"t-1": "Fully"}]),
    )
    assert orch._current_build_awaits_review(feature_active) is False
    result = orch.advance_one("demo")

    assert result.success is True
    assert _count(feature_active, "build") == 1
    assert _count(feature_active, "ralph-review") == 1


def test_interrupted_seal_resumes_with_recorded_post_build_ref(
    git_repo, feature_active, monkeypatch,
):
    """The harness dies after the build returned but before the iteration
    is sealed; the operator commits on the branch; the resumed review must
    cover the build's own delta, not the operator's commit."""
    _seed_feature(feature_active, ids=["t-1"])
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")
    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    monkeypatch.setenv("AUTODEV_PHASE5B_AMEND_BUILD", "1")
    monkeypatch.setenv("AUTODEV_PHASE5B_SEQUENCE", json.dumps([{"t-1": "Fully"}]))
    orch = _orch(git_repo, vendor_bin)

    def crash(context_path):
        raise RuntimeError("harness died before sealing")

    monkeypatch.setattr(orch, "_finalize_ralph_iteration_context", crash)
    with pytest.raises(RuntimeError, match="before sealing"):
        orch.advance_one("demo")
    context_path = feature_active / "ralph-iteration-context.json"
    pending = json.loads(context_path.read_text(encoding="utf-8"))
    assert pending["status"] == "build_pending"
    build_head = pending["after_ref"]
    assert build_head == subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(git_repo), text=True,
    ).strip()

    unrelated = git_repo / "docs" / "operator-note.md"
    unrelated.write_text("hotfix while the harness was down\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", "docs/operator-note.md"], cwd=str(git_repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "operator hotfix"], cwd=str(git_repo), check=True)
    operator_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(git_repo), text=True,
    ).strip()
    assert operator_head != build_head

    resumed = _orch(git_repo, vendor_bin)
    result = resumed.advance_one("demo")

    assert result.success is True
    assert _count(feature_active, "build") == 1
    assert _count(feature_active, "ralph-review") == 1
    sealed = json.loads(context_path.read_text(encoding="utf-8"))
    assert sealed["status"] == "ready_for_review"
    assert sealed["after_ref"] == build_head
    assert operator_head not in sealed["diff"]["patch_command"]
    from autodev.artifacts.build import load_build
    assert load_build(feature_active / "build.json").sealed_ref == build_head


def test_pending_context_with_unchanged_build_json_reruns_build(
    git_repo, feature_active, monkeypatch,
):
    """A pending iteration whose build never returned is built, not reviewed."""
    _seed_feature(feature_active, ids=["t-1"])
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(git_repo), text=True,
    ).strip()
    build_path = feature_active / "build.json"
    build_path.write_text(json.dumps({
        "source": str(feature_active / "scope.json"),
        "source_hash": hash_file(feature_active / "scope.json"),
        "written": "2026-04-21",
        "test_cmd_run": "pytest",
        "test_exit_code": 0,
        "test_results": {"passed": 1, "failed": 0, "skipped": 0},
        "files_changed": ["src/generated.py"],
        "lint": {"passed": True, "cmd": "n/a"},
        "deviations": [],
        "blocking": False,
        "workspace_dirty_at_stage_end": False,
    }), encoding="utf-8")
    (feature_active / "ralph-iteration-context.json").write_text(json.dumps({
        "schema": 1,
        "iteration": 1,
        "status": "build_pending",
        "written_at": "2026-04-21T00:00:00+00:00",
        "repo_root": str(git_repo),
        "trace_hash": hash_file(feature_active / "trace.md"),
        "before_ref": head,
        "after_ref": None,
        # build.json is exactly what it was when the iteration started.
        "build_hash_before": hash_file(build_path),
        "previous_ralph_review_path": None,
        "previous_ralph_review_hash": None,
        "diff": None,
    }), encoding="utf-8")
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")

    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    orch = _orch(git_repo, vendor_bin)
    monkeypatch.setenv(
        "AUTODEV_PHASE5B_SEQUENCE", json.dumps([{"t-1": "Fully"}]),
    )

    assert StalenessCascade(feature_active).next_stage() == "implementation_index"
    result = orch.advance_one("demo")

    assert result.success is True
    assert _count(feature_active, "build") == 1
    assert _count(feature_active, "ralph-review") == 1
    context = json.loads(
        (feature_active / "ralph-iteration-context.json").read_text(encoding="utf-8")
    )
    assert context["iteration"] == 1
    assert context["before_ref"] == head
    assert not any(
        event.get("event") == "ralph-resumed-from-current-build"
        for event in _read_log(feature_active)
    )


def test_rejected_build_with_pending_context_is_retried_not_reviewed(
    git_repo, feature_active, monkeypatch,
):
    """build.json is promoted before the residue check rejects the attempt.

    If the harness dies before the retry lands, the pending context and a
    moved build.json look like a landed build; the rejection note on disk
    says otherwise, so the restart must run build again, not review it.
    """
    _seed_feature(feature_active, ids=["t-1"])
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(git_repo), text=True,
    ).strip()
    build_path = feature_active / "build.json"
    build_path.write_text(json.dumps({
        "source": str(feature_active / "scope.json"),
        "source_hash": hash_file(feature_active / "scope.json"),
        "written": "2026-04-21",
        "test_cmd_run": "pytest",
        "test_exit_code": 0,
        "test_results": {"passed": 1, "failed": 0, "skipped": 0},
        "files_changed": ["src/generated.py"],
        "lint": {"passed": True, "cmd": "n/a"},
        "deviations": [],
        "blocking": False,
        "workspace_dirty_at_stage_end": False,
    }), encoding="utf-8")
    (feature_active / "ralph-iteration-context.json").write_text(json.dumps({
        "schema": 1,
        "iteration": 1,
        "status": "build_pending",
        "written_at": "2026-04-21T00:00:00+00:00",
        "repo_root": str(git_repo),
        "trace_hash": hash_file(feature_active / "trace.md"),
        "before_ref": head,
        "after_ref": None,
        "build_hash_before": None,
        "previous_ralph_review_path": None,
        "previous_ralph_review_hash": None,
        "diff": None,
    }), encoding="utf-8")
    (feature_active / "build-output-rejection.json").write_text(json.dumps({
        "stage": "build", "attempt": 1, "kind": "uncommitted_product_changes",
        "detail": "left src/x.py staged",
    }), encoding="utf-8")
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")

    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    orch = _orch(git_repo, vendor_bin)
    monkeypatch.setenv(
        "AUTODEV_PHASE5B_SEQUENCE", json.dumps([{"t-1": "Fully"}]),
    )
    assert StalenessCascade(feature_active).next_stage() == "implementation_index"

    result = orch.advance_one("demo")

    assert result.success is True
    assert _count(feature_active, "build") == 1
    assert _count(feature_active, "ralph-review") == 1
    assert not (feature_active / "build-output-rejection.json").exists()
    assert not any(
        event.get("event") == "ralph-resumed-from-current-build"
        for event in _read_log(feature_active)
    )


def _seed_landed_pending_iteration(git_repo: Path, feature_active: Path) -> str:
    """build.json landed under a still-pending context; returns HEAD."""
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(git_repo), text=True,
    ).strip()
    baseline = _baseline_payload(git_repo)
    (feature_active / "build.json").write_text(json.dumps({
        "source": str(feature_active / "scope.json"),
        "source_hash": hash_file(feature_active / "scope.json"),
        "written": "2026-04-21",
        "test_cmd_run": "pytest",
        "test_exit_code": 0,
        "test_results": {"passed": 1, "failed": 0, "skipped": 0},
        "files_changed": ["src/generated.py"],
        "lint": {"passed": True, "cmd": "n/a"},
        "deviations": [],
        "blocking": False,
        "workspace_dirty_at_stage_end": False,
    }), encoding="utf-8")
    (feature_active / "ralph-iteration-context.json").write_text(json.dumps({
        "schema": 1,
        "iteration": 1,
        "status": "build_pending",
        "written_at": "2026-04-21T00:00:00+00:00",
        "repo_root": str(git_repo),
        "trace_hash": hash_file(feature_active / "trace.md"),
        "before_ref": head,
        "after_ref": head,  # recorded when the build subprocess returned
        "build_hash_before": None,
        "workspace_baseline": baseline,
        "previous_ralph_review_path": None,
        "previous_ralph_review_hash": None,
        "diff": None,
    }), encoding="utf-8")
    return head


def test_pending_build_with_uncommitted_residue_is_rebuilt_not_reviewed(
    git_repo, feature_active, monkeypatch,
):
    """The harness died after build.json landed but before the residue
    check: the landed build left product changes uncommitted, so it must be
    rebuilt, never reviewed. The rebuild is held to the iteration's
    persisted baseline, so it has to commit what the interrupted attempt
    left behind (here the fake vendor commits the staged leftover)."""
    _seed_feature(feature_active, ids=["t-1"])
    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    _seed_landed_pending_iteration(git_repo, feature_active)
    leftover = git_repo / "src" / "leftover.py"
    leftover.parent.mkdir(parents=True, exist_ok=True)
    leftover.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", "src/leftover.py"], cwd=str(git_repo), check=True)
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")

    orch = _orch(git_repo, vendor_bin)
    assert orch._current_build_awaits_review(feature_active) is False
    monkeypatch.setenv("AUTODEV_PHASE5B_COMMIT_BASELINE_ONCE", "1")
    monkeypatch.setenv(
        "AUTODEV_PHASE5B_SEQUENCE", json.dumps([{"t-1": "Fully"}]),
    )
    assert StalenessCascade(feature_active).next_stage() == "implementation_index"

    result = orch.advance_one("demo")

    assert result.success is True
    assert _count(feature_active, "build") == 1
    assert _count(feature_active, "ralph-review") == 1
    assert subprocess.check_output(
        ["git", "status", "--short", "--", "src/leftover.py"],
        cwd=str(git_repo), text=True,
    ) == ""
    assert not any(
        event.get("event") == "ralph-resumed-from-current-build"
        for event in _read_log(feature_active)
    )


def test_pending_build_with_out_of_scope_write_is_rebuilt_not_reviewed(
    git_repo, feature_active, monkeypatch,
):
    """A landed build that escaped its write contract must not be reviewed."""
    _seed_feature(feature_active, ids=["t-1"])
    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    _seed_landed_pending_iteration(git_repo, feature_active)
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")
    orch = _orch(git_repo, vendor_bin)
    monkeypatch.setenv(
        "AUTODEV_PHASE5B_SEQUENCE", json.dumps([{"t-1": "Fully"}]),
    )

    # Clean tree: the landed build is resumable as-is ...
    assert orch._current_build_awaits_review(feature_active) is True

    # ... unless the write-contract check reports an escape for it.
    monkeypatch.setattr(
        "autodev.orchestrator.detect_out_of_scope_writes",
        lambda *args, **kwargs: ["P  docs/features/demo/active/prd.md"],
    )
    assert orch._current_build_awaits_review(feature_active) is False

    result = orch.advance_one("demo")

    assert result.success is True
    assert _count(feature_active, "build") == 1
    assert not any(
        event.get("event") == "ralph-resumed-from-current-build"
        for event in _read_log(feature_active)
    )


def test_restart_after_reviewer_crash_keeps_recorded_after_ref(
    git_repo, feature_active, monkeypatch,
):
    """A finalized context is reviewed as recorded even if the operator
    committed on the branch before resuming."""
    _seed_feature(feature_active, ids=["t-1"])
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")
    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    orch = _orch(git_repo, vendor_bin)
    monkeypatch.setenv("AUTODEV_PHASE5B_SEQUENCE", json.dumps([{"t-1": "Fully"}]))

    def crash(*args, **kwargs):
        raise RuntimeError("reviewer process died")

    monkeypatch.setattr(orch, "_run_ralph_review", crash)
    with pytest.raises(RuntimeError):
        orch.advance_one("demo")
    context_path = feature_active / "ralph-iteration-context.json"
    sealed = json.loads(context_path.read_text(encoding="utf-8"))
    assert sealed["status"] == "ready_for_review"

    unrelated = git_repo / "docs" / "operator-note.md"
    unrelated.write_text("operator commit while paused\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", "docs/operator-note.md"], cwd=str(git_repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "operator note"], cwd=str(git_repo), check=True)
    new_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(git_repo), text=True,
    ).strip()
    assert new_head != sealed["after_ref"]

    resumed = _orch(git_repo, vendor_bin)
    result = resumed.advance_one("demo")

    assert result.success is True
    assert _count(feature_active, "build") == 1
    assert _count(feature_active, "ralph-review") == 1
    reviewed = json.loads(context_path.read_text(encoding="utf-8"))
    assert reviewed["after_ref"] == sealed["after_ref"]
    assert reviewed["before_ref"] == sealed["before_ref"]
    event = next(
        e for e in _read_log(feature_active)
        if e.get("event") == "ralph-resumed-from-current-build"
    )
    assert event["detail"]["after_ref"] == sealed["after_ref"]


def test_rejected_pending_build_clears_pinned_post_build_ref(
    git_repo, feature_active, monkeypatch,
):
    """When the resume predicate rejects a landed attempt, the ref it pinned
    must go with it: a rebuild killed after committing would otherwise be
    sealed against this earlier commit on the next restart."""
    _seed_feature(feature_active, ids=["t-1"])
    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    _seed_landed_pending_iteration(git_repo, feature_active)
    leftover = git_repo / "src" / "leftover.py"
    leftover.parent.mkdir(parents=True, exist_ok=True)
    leftover.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", "src/leftover.py"], cwd=str(git_repo), check=True)
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")
    orch = _orch(git_repo, vendor_bin)
    context_path = feature_active / "ralph-iteration-context.json"
    assert json.loads(context_path.read_text(encoding="utf-8"))["after_ref"]

    assert orch._current_build_awaits_review(feature_active) is False

    assert json.loads(context_path.read_text(encoding="utf-8"))["after_ref"] is None


def test_operator_edits_during_pause_are_not_build_residue(
    git_repo, feature_active, monkeypatch,
):
    """A pending iteration is re-entered after the operator left an
    uncommitted file in the tree; the rebuild is judged against the tree at
    loop entry, so that file is theirs to keep, not residue."""
    _seed_feature(feature_active, ids=["t-1"])
    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    _seed_landed_pending_iteration(git_repo, feature_active)
    context_path = feature_active / "ralph-iteration-context.json"
    context = json.loads(context_path.read_text(encoding="utf-8"))
    context["after_ref"] = None  # the build never returned
    context_path.write_text(json.dumps(context), encoding="utf-8")
    notes = git_repo / "src" / "notes.py"
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text("# operator scratch\n", encoding="utf-8")
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")

    orch = _orch(git_repo, vendor_bin)
    monkeypatch.setenv(
        "AUTODEV_PHASE5B_SEQUENCE", json.dumps([{"t-1": "Fully"}]),
    )
    result = orch.advance_one("demo")

    assert result.success is True
    assert _count(feature_active, "build") == 1
    assert _count(feature_active, "ralph-review") == 1
    assert notes.exists()
    assert not any(
        event.get("event") == "output-rejected-retrying"
        for event in _read_log(feature_active)
    )
