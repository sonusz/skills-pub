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
from autodev.artifacts.scope import Scope, ScopeItem, write_scope
from autodev.artifacts.verdict import PanelVerdict, write_verdict
from autodev.errors import GatePending, SchemaError
from autodev.orchestrator import Orchestrator, OrchestratorConfig
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
                counter = active / f".{stage}.count"
                n = int(counter.read_text() or "0") if counter.exists() else 0
                n += 1
                counter.write_text(str(n), encoding="utf-8")
                return n

            def log_prompt(active: Path, stage: str, n: int, prompt: str) -> None:
                (active / f".{stage}.{n}.prompt").write_text(prompt, encoding="utf-8")

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
                scope_hash = hash_file(active / "scope.json")
                write_tmp(
                    tgt_trace,
                    f"<!-- source: scope.json -->\\n<!-- source_hash: {scope_hash} -->\\n"
                    f"<!-- written: {DATE} -->\\n\\n| # | Req ID | Scope ID | Requirement | Test(s) | Code Path | Status |\\n"
                    f"|---|---|---|---|---|---|---|\\n| 1 | t-1.r1 | t-1 | x | -- | -- | pending |\\n",
                )
                write_tmp(
                    tgt_test_plan,
                    f"<!-- source: scope.json -->\\n<!-- source_hash: {scope_hash} -->\\n"
                    f"<!-- written: {DATE} -->\\n\\n## Test Strategy\\n",
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
    write_markdown_with_hash(
        active / "design.md",
        "# Design\n\n## Loop\nDrive the build/ralph cycle from one design packet.\n\n"
        "Validation commands: [\"pytest -q\"]\n",
        source=str(active / "prd.md"),
        source_hash=prd_hash,
    )
    scope = Scope(
        source=str(active / "prd.md"),
        source_hash=prd_hash,
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
        source=str(active / "prd.md"),
        source_hash=prd_hash,
    )
    write_markdown_with_hash(
        active / "test-plan.md",
        "## Test Strategy\nLoop coverage.\n",
        source=str(active / "prd.md"),
        source_hash=prd_hash,
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


def _count(active: Path, stage: str) -> int:
    p = active / f".{stage}.count"
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

    prompt = (feature_active / ".ralph-review.1.prompt").read_text(encoding="utf-8")
    # v3-core update: ralph-review is context-isolated (trace+code only).
    # No PRD/scope/build.json/spec in its inputs.
    assert "TRACE_PATH:" in prompt
    assert "TARGET_RALPH_REVIEW:" in prompt
    assert "BUILD_JSON_PATH:" not in prompt
    assert "SCOPE_PATH:" not in prompt
    assert "SPEC_PATH:" not in prompt
    assert "PRD_PATH:" not in prompt
    assert "TARGET_REVIEW:" not in prompt

    first_build_prompt = (feature_active / ".build.1.prompt").read_text(encoding="utf-8")
    second_build_prompt = (feature_active / ".build.2.prompt").read_text(encoding="utf-8")
    assert str(feature_active / "ralph-review.json") not in first_build_prompt
    assert str(feature_active / "ralph-review.json") in second_build_prompt
    assert str(feature_active / "ralph-state.json") in second_build_prompt


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
    retry_prompt = (feature_active / ".ralph-review.2.prompt").read_text(encoding="utf-8")
    assert "CONTEXT_ARTIFACTS" in retry_prompt
    assert str(feature_active / "ralph-review.json") in retry_prompt
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


def test_build_route_skips_ralph_review(git_repo, feature_active, monkeypatch):
    _seed_feature(feature_active, ids=["t-1"])
    ov.record_acknowledge_dirty(feature_active, reason="pytest", who="pytest")
    vendor_bin = _write_fake_vendor(git_repo / "fake_vendor.py")
    orch = _orch(git_repo, vendor_bin)

    state = ralph.RalphState(
        source=str(feature_active / "scope.json"),
        source_hash=hash_file(feature_active / "scope.json"),
        trace_hash=hash_file(feature_active / "trace.md"),
        test_plan_hash=hash_file(feature_active / "test-plan.md"),
        iter=1,
        fully_history=[set(), {"t-1"}],
        statuses_history=[{}, {"t-1": "Fully"}],
    )
    ralph.write_ralph_state(feature_active, state)
    (feature_active / "ralph-review.json").write_text("stale\\n", encoding="utf-8")

    monkeypatch.setenv("AUTODEV_PHASE5B_ROUTE_LAYER", "design")
    monkeypatch.setenv("AUTODEV_PHASE5B_ROUTE_AT", "1")
    monkeypatch.setenv("AUTODEV_PHASE5B_SCOPE_ID", "t-1")

    result = orch.advance_one("demo")

    assert result.success is True
    assert _count(feature_active, "build") == 1
    assert _count(feature_active, "ralph-review") == 0
    assert not (feature_active / "design.md").exists()
    assert not (feature_active / "scope.json").exists()
    assert not (feature_active / "trace.md").exists()
    assert not (feature_active / "test-plan.md").exists()
    assert not (feature_active / "ralph-state.json").exists()
    assert not (feature_active / "ralph-review.json").exists()


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
    }))
    ralph.write_ralph_state(feature_active, ralph.RalphState(
        source=str(feature_active / "scope.json"),
        source_hash=hash_file(feature_active / "scope.json"),
        trace_hash=hash_file(feature_active / "trace.md"),
        test_plan_hash=hash_file(feature_active / "test-plan.md"),
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
    }))
    ralph.write_ralph_state(feature_active, ralph.RalphState(
        source=str(feature_active / "scope.json"),
        source_hash=hash_file(feature_active / "scope.json"),
        trace_hash=hash_file(feature_active / "trace.md"),
        test_plan_hash=hash_file(feature_active / "test-plan.md"),
        iter=2,
        fully_history=[set(), {"t-1"}, {"t-1", "t-2"}],
        statuses_history=[{}, {"t-1": "Fully"}, {"t-1": "Fully", "t-2": "Fully"}],
    ))
    assert orch._next_stage_name(feature_active) == "implementation_index"


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
