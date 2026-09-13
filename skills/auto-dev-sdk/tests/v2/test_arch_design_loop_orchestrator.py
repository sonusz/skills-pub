"""arch-design / arch-review loop -- Stage B orchestrator coverage.

Detail §9.3, tests 3/4/5/6/9. Companion to ``test_arch_design_loop.py``
(Stage A: cascade freshness, arch-review schema, session-key sharing,
credit-turn) -- this file exercises ``Orchestrator._advance_arch_design_loop``
and ``Orchestrator._run_arch_review`` end to end through the real
orchestrator, using a fake vendor subprocess (no live LLM calls).
"""
from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from autodev import overrides_api as ov
from autodev.artifacts.revision_state import load_state
from autodev.errors import GatePending, SchemaError
from autodev.orchestrator import ARCH_REVIEW_MAX_ROUNDS, Orchestrator, OrchestratorConfig
from autodev.state.cascade import StalenessCascade
from autodev.state.log import JsonlLog
from autodev.vendors.config import (
    PanelConfig,
    PanelReviewerSpec,
    PanelSynthesizerSpec,
    ProbeConfig,
    STAGES,
    StageSpec,
    VendorsConfig,
)

FAKE_VENDOR = Path(__file__).resolve().parent / "fakes" / "fake_vendor_cli_auto.py"
FAKE_PANEL = Path(__file__).resolve().parent / "fakes" / "fake_panel_invoker.sh"


def _vendors_fake_everywhere(repo_root: Path) -> VendorsConfig:
    """All coding + review stages -> claude vendor with the autodetecting fake."""
    return VendorsConfig(
        path=repo_root / "vendors.yml",
        stages={
            s: StageSpec(stage=s, vendor="claude", model="fake-model", probe_interval_sec=30)
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


def _write_prd(active: Path) -> Path:
    prd = active / "prd.md"
    prd.write_text(
        "# PRD: demo\n\n"
        "## Problem\nx\n\n## Users\nx\n\n"
        "## Requirements\n### R1: thing\n\n"
        "## Constraints\nx\n\n## Success criteria\nx\n\n## Out of scope\nx\n",
        encoding="utf-8",
    )
    return prd


def _commit(git_repo: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=str(git_repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=str(git_repo), check=True)


def _feature_dir(git_repo: Path, feature: str) -> Path:
    active = git_repo / "docs" / "features" / feature / "active"
    active.mkdir(parents=True)
    return active


def _log_events(active: Path) -> list[dict]:
    log_path = active / "log.jsonl"
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _count_events(active: Path, stage: str, event: str) -> int:
    return sum(
        1 for row in _log_events(active)
        if row.get("stage") == stage and row.get("event") == event
    )


# ---------- test 3: loop pass-first-try path ----

def test_arch_design_loop_dispatches_once_each_on_first_pass(git_repo, monkeypatch):
    feature = "demo"
    active = _feature_dir(git_repo, feature)
    _write_prd(active)
    _commit(git_repo, "seed prd")

    monkeypatch.setenv("AUTODEV_VENDOR_BIN_CLAUDE", str(FAKE_VENDOR))
    monkeypatch.setenv("AUTODEV_FAKE_FEATURE", feature)

    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo, vendors=_vendors_fake_everywhere(git_repo),
        session_id="t3",
    ))
    result = orch.advance_one(feature)

    assert result.success
    assert result.stage_name == "arch_review"
    assert (active / "arch-design.md").exists()
    review = json.loads((active / "arch-review.json").read_text())
    assert review["verdict"] == "pass"

    # One dispatch each -- no wasted rounds when the review passes first try.
    assert _count_events(active, "arch-design", "subprocess-dispatch") == 1
    assert _count_events(active, "arch-review", "stage-complete") == 1

    assert StalenessCascade(active).next_stage() == "design"


# ---------- test 4: needs_revision -> revise -> pass ----

def test_arch_design_loop_rejects_once_then_passes(git_repo, monkeypatch):
    feature = "demo"
    active = _feature_dir(git_repo, feature)
    _write_prd(active)
    _commit(git_repo, "seed prd")

    monkeypatch.setenv("AUTODEV_VENDOR_BIN_CLAUDE", str(FAKE_VENDOR))
    monkeypatch.setenv("AUTODEV_FAKE_FEATURE", feature)
    monkeypatch.setenv("AUTODEV_FAKE_ARCH_REVIEW_REJECT_FIRST", "1")

    import autodev.prompts_loader as prompts_loader
    real_render = prompts_loader.render_stage_prompt
    arch_design_calls: list[dict] = []

    def _spy(*, stage, **kwargs):
        if stage == "arch-design":
            arch_design_calls.append({
                "context_artifacts": list(kwargs.get("context_artifacts") or []),
                "preseeded": kwargs.get("preseeded", False),
            })
        return real_render(stage=stage, **kwargs)

    monkeypatch.setattr(prompts_loader, "render_stage_prompt", _spy)

    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo, vendors=_vendors_fake_everywhere(git_repo),
        session_id="t4",
    ))
    result = orch.advance_one(feature)

    assert result.success
    review = json.loads((active / "arch-review.json").read_text())
    assert review["verdict"] == "pass"

    # arch-design dispatched twice (initial + one in-place revision);
    # arch-review dispatched twice (needs_revision, then pass).
    assert _count_events(active, "arch-design", "subprocess-dispatch") == 2
    assert _count_events(active, "arch-review", "stage-complete") == 2

    # The second arch-design dispatch is preseeded (in-place Edit) and is
    # fed the rejected arch-review.json as CONTEXT_ARTIFACTS.
    assert arch_design_calls, "render_stage_prompt was never spied for arch-design"
    last = arch_design_calls[-1]
    assert last["preseeded"] is True
    assert str(active / "arch-review.json") in last["context_artifacts"]


# ---------- Task 1 (detail §3.3 second bullet): PRD amendment after a
# passing arch-review must not stall the loop ----

def test_arch_design_loop_revises_when_prd_amended_after_pass(git_repo, monkeypatch):
    """A ``pass`` verdict already on disk must not short-circuit the loop
    once ``prd.md`` changes underneath it: `arch-design.md`'s recorded
    header hash no longer matches the current PRD hash, so the
    ``arch_design`` cascade node goes stale and the loop must revise
    (and re-review) before doing anything else. Before the fix, the loop
    only checked "file missing" or "verdict == needs_revision" and would
    return the stale "arch-review already passed" no-op forever, leaving
    `run()` to spin with nothing dispatched."""
    feature = "demo"
    active = _feature_dir(git_repo, feature)
    prd = _write_prd(active)
    _commit(git_repo, "seed prd")

    monkeypatch.setenv("AUTODEV_VENDOR_BIN_CLAUDE", str(FAKE_VENDOR))
    monkeypatch.setenv("AUTODEV_FAKE_FEATURE", feature)

    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo, vendors=_vendors_fake_everywhere(git_repo),
        session_id="t1-stall",
    ))
    first = orch.advance_one(feature)
    assert first.success
    review = json.loads((active / "arch-review.json").read_text())
    assert review["verdict"] == "pass"

    dispatches_before = _count_events(active, "arch-design", "subprocess-dispatch")
    reviews_before = _count_events(active, "arch-review", "stage-complete")
    assert dispatches_before == 1
    assert reviews_before == 1

    # Amend the PRD after arch-design.md has already passed review.
    with prd.open("a", encoding="utf-8") as f:
        f.write("\n### R2: another thing\n")
    _commit(git_repo, "amend prd")

    second = orch.advance_one(feature)

    assert (
        _count_events(active, "arch-design", "subprocess-dispatch")
        == dispatches_before + 1
    ), "arch-design must be re-dispatched once the PRD changed underneath it"
    assert (
        _count_events(active, "arch-review", "stage-complete")
        == reviews_before + 1
    ), "arch-review must re-run against the revised arch-design"

    from autodev.state.hashing import hash_file, parse_markdown_source_hash

    new_prd_hash = hash_file(prd)
    assert parse_markdown_source_hash(active / "arch-design.md") == new_prd_hash

    new_review = json.loads((active / "arch-review.json").read_text())
    assert new_review["verdict"] == "pass"
    assert new_review["source_hash"] == hash_file(active / "arch-design.md")

    assert second.success
    assert second.detail != "arch-review already passed"


# ---------- test 5: ARCH_REVIEW_MAX_ROUNDS halt ----

_ALWAYS_REJECT_SCRIPT = textwrap.dedent(r"""    #!/usr/bin/env python3
    import hashlib
    import re
    import sys
    from pathlib import Path

    def _hash(p):
        return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()

    def _extract(prompt, key):
        m = re.search(
            rf"\*?\*?{key}\*?\*?\s*[:=]\s*`?([^\s`\n]+)`?", prompt,
        )
        return Path(m.group(1)) if m else None

    def main():
        prompt = sys.stdin.read()
        tgt_design = _extract(prompt, "TARGET_ARCH_DESIGN")
        tgt_review = _extract(prompt, "TARGET_ARCH_REVIEW")
        if tgt_design:
            prd = tgt_design.parent / "prd.md"
            body = (
                "<!-- source: " + str(prd) + " -->\n"
                "<!-- source_hash: " + _hash(prd) + " -->\n"
                "<!-- written: 2026-04-20 -->\n\n"
                "## 1. Goal\nx\n## 5. PRD coverage\n| R1 | x |\n"
            )
            tgt_design.with_name(tgt_design.name + ".tmp").write_text(
                body, encoding="utf-8",
            )
            return 0
        if tgt_review:
            active = tgt_review.parent
            arch_design = active / "arch-design.md"
            prd = active / "prd.md"
            payload = (
                '{"kind":"arch-review","source":"' + str(arch_design) + '",'
                '"source_hash":"' + _hash(arch_design) + '",'
                '"prd_hash":"' + _hash(prd) + '",'
                '"written":"2026-04-20T00:00:00Z","verdict":"needs_revision",'
                '"findings":[{"category":"redundant","prd_ref":null,'
                '"evidence":"arch-design.md","problem":"always reject probe",'
                '"correction":"n/a"}]}'
            )
            tgt_review.with_name(tgt_review.name + ".tmp").write_text(
                payload, encoding="utf-8",
            )
            return 0
        print("fake_always_reject: no target found", file=sys.stderr)
        return 3

    if __name__ == "__main__":
        sys.exit(main())
""")


def test_arch_design_loop_halts_after_max_rejected_rounds(git_repo, monkeypatch, tmp_path):
    feature = "demo"
    active = _feature_dir(git_repo, feature)
    _write_prd(active)
    _commit(git_repo, "seed prd")

    fake = tmp_path / "fake_always_reject.py"
    fake.write_text(_ALWAYS_REJECT_SCRIPT, encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("AUTODEV_VENDOR_BIN_CLAUDE", str(fake))
    # `tmp_path` is `git_repo` itself here, so the fake script we just
    # dropped in the repo root is untracked -- acknowledge it rather than
    # committing (the whole point of this fixture is to be repo-external
    # tooling, not feature output).
    ov.record_acknowledge_dirty(active, reason="test fixture", who="pytest")

    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo, vendors=_vendors_fake_everywhere(git_repo),
        session_id="t5",
    ))
    with pytest.raises(GatePending) as excinfo:
        orch.advance_one(feature)
    assert excinfo.value.gate == "arch-review"

    # Exactly ARCH_REVIEW_MAX_ROUNDS revision rounds were dispatched before
    # halting -- the initial pass does not count against the cap, only the
    # rounds rejected by arch-review do (core §7.1).
    assert (
        _count_events(active, "arch-design", "subprocess-dispatch")
        == ARCH_REVIEW_MAX_ROUNDS
    )
    assert (
        _count_events(active, "arch-review", "stage-complete")
        == ARCH_REVIEW_MAX_ROUNDS
    )


# ---------- test 6: panel block routes to arch-design ----

def _drive_to_design_packet_ready(
    git_repo, feature: str, monkeypatch,
) -> tuple[Path, Orchestrator]:
    """Seed a PRD and drive the fake pipeline through arch-design/
    arch-review (pass) and design/scope/trace/test_plan, stopping right
    before the design-review panel runs. The panel is configured to
    return a blocking (canonical retry_design) verdict the first time it
    runs, via ``fake_panel_invoker.sh``'s ``reviewers_two_fail``."""
    active = _feature_dir(git_repo, feature)
    _write_prd(active)
    # design's workflow-state bootstrap requires a feature-local
    # architecture.md pointing at a discretionary-read arch doc.
    (git_repo / "docs" / "architecture-proposal.md").write_text(
        "# Architecture Proposal\n", encoding="utf-8",
    )
    (active / "architecture.md").write_text(
        "# Architecture Input\n\n- `docs/architecture-proposal.md`\n",
        encoding="utf-8",
    )
    _commit(git_repo, f"seed prd {feature}")

    monkeypatch.setenv("AUTODEV_VENDOR_BIN_CLAUDE", str(FAKE_VENDOR))
    monkeypatch.setenv("AUTODEV_FAKE_FEATURE", feature)
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_INVOKER", str(FAKE_PANEL))
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "reviewers_two_fail")

    ov.record_acknowledge_dirty(active, reason="test setup", who="pytest")

    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo, vendors=_vendors_fake_everywhere(git_repo),
        session_id=f"seed-{feature}",
    ))
    # 1) arch_design + arch_review (compound single call, passes).
    # 2) design (+ scope/trace/test_plan, single manifest-driven call).
    # 3) design_packet (harness-authored artifact).
    for _ in range(3):
        result = orch.advance_one(feature)
        assert result.success, result.detail

    assert (active / "design-packet.json").exists()
    assert not (active / "panel-design-review.json").exists()
    review = json.loads((active / "arch-review.json").read_text())
    assert review["verdict"] == "pass"
    return active, orch


def test_panel_block_routes_to_arch_design_via_advance_one(git_repo, monkeypatch):
    feature = "demo-panel-a"
    active, orch = _drive_to_design_packet_ready(git_repo, feature, monkeypatch)

    dispatches_before = _count_events(active, "arch-design", "subprocess-dispatch")
    reviews_before = _count_events(active, "arch-review", "stage-complete")
    assert dispatches_before == 1 and reviews_before == 1

    result = orch.advance_one(feature)
    assert result.success

    triggered = [
        r for r in _log_events(active) if r.get("event") == "revision-loop-triggered"
    ]
    assert triggered, "no revision-loop-triggered event logged"
    assert triggered[-1]["detail"]["stage_to_rerun"] == "arch-design"

    assert load_state(active).L["design-review"] == 1

    # Forced revision happened even though the on-disk arch-review verdict
    # was already "pass" (core R5: the panel finding overrides the stale
    # pass, so both stages are re-dispatched exactly once more).
    assert (
        _count_events(active, "arch-design", "subprocess-dispatch")
        == dispatches_before + 1
    )
    assert (
        _count_events(active, "arch-review", "stage-complete")
        == reviews_before + 1
    )


def test_panel_block_routes_to_arch_design_via_run_until_build(git_repo, monkeypatch):
    feature = "demo-panel-b"
    active, orch = _drive_to_design_packet_ready(git_repo, feature, monkeypatch)

    dispatches_before = _count_events(active, "arch-design", "subprocess-dispatch")

    # A single-iteration `run(stop_before="build")` must reach the same
    # enforce-first dispatch as advance_one -- the "arch-design" stage
    # name must normalize to the "arch_design" cascade node for the
    # boundary comparison, or this incorrectly stops at the boundary
    # instead of dispatching (detail §3.2).
    orch.run(feature, stop_before="build", max_stages=1)

    boundary_events = [
        r for r in _log_events(active) if r.get("event") == "stopped-at-boundary"
    ]
    assert boundary_events == [], (
        "run(stop_before='build') incorrectly treated the arch-design "
        "rerun as at/after the build boundary"
    )

    assert load_state(active).L["design-review"] == 1
    assert (
        _count_events(active, "arch-design", "subprocess-dispatch")
        == dispatches_before + 1
    )


# ---------- test 9: _run_arch_review output-validation retry ----

_ALWAYS_INVALID_REVIEW_SCRIPT = textwrap.dedent(r"""    #!/usr/bin/env python3
    import re
    import sys
    from pathlib import Path

    def _extract(prompt, key):
        m = re.search(
            rf"\*?\*?{key}\*?\*?\s*[:=]\s*`?([^\s`\n]+)`?", prompt,
        )
        return Path(m.group(1)) if m else None

    def main():
        prompt = sys.stdin.read()
        tgt_review = _extract(prompt, "TARGET_ARCH_REVIEW")
        if not tgt_review:
            print("fake_always_invalid: no TARGET_ARCH_REVIEW", file=sys.stderr)
            return 3
        # verdict says pass but findings is non-empty -- schema-invalid
        # regardless of hash correctness (core R2 / detail §2).
        payload = (
            '{"kind":"arch-review","source":"x","source_hash":"sha256:'
            + ("0" * 64) + '","prd_hash":"sha256:' + ("0" * 64) + '",'
            '"written":"2026-04-20T00:00:00Z","verdict":"pass",'
            '"findings":[{"category":"redundant","prd_ref":null,'
            '"evidence":"x","problem":"x","correction":"x"}]}'
        )
        tgt_review.with_name(tgt_review.name + ".tmp").write_text(
            payload, encoding="utf-8",
        )
        return 0

    if __name__ == "__main__":
        sys.exit(main())
""")


def test_run_arch_review_retries_then_raises_on_invalid_verdict(
    git_repo, monkeypatch, tmp_path,
):
    feature = "demo"
    active = _feature_dir(git_repo, feature)
    prd = _write_prd(active)

    from autodev.artifacts.common import write_markdown_with_hash
    from autodev.state.hashing import hash_file

    prd_hash = hash_file(prd)
    write_markdown_with_hash(
        active / "arch-design.md",
        "## 1. Goal\nx\n## 5. PRD coverage\n| R1 | x |\n",
        source=str(prd), source_hash=prd_hash,
    )
    _commit(git_repo, "seed prd + arch-design")

    fake = tmp_path / "fake_always_invalid.py"
    fake.write_text(_ALWAYS_INVALID_REVIEW_SCRIPT, encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("AUTODEV_VENDOR_BIN_CLAUDE", str(fake))

    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo, vendors=_vendors_fake_everywhere(git_repo),
        session_id="t9",
    ))
    logger = JsonlLog(active / "log.jsonl")

    with pytest.raises(SchemaError):
        orch._run_arch_review(feature, active, logger)

    rejection = active / "arch-review-output-rejection.json"
    assert rejection.exists()
    payload = json.loads(rejection.read_text())
    assert payload["stage"] == "arch-review"

    exhausted = [
        r for r in _log_events(active)
        if r.get("stage") == "arch-review" and r.get("event") == "output-rejected-exhausted"
    ]
    assert exhausted
    assert exhausted[-1]["detail"]["attempts"] == 3


# ---------- Task 2 (detail §2 first bullet, §3.5): malformed (not even
# valid JSON) arch-review.json goes through the same repair-retry path as
# a schema-invalid verdict, modeled on test 9 above ----

_MALFORMED_THEN_VALID_REVIEW_SCRIPT = textwrap.dedent(r"""    #!/usr/bin/env python3
    import hashlib
    import re
    import sys
    from pathlib import Path

    def _hash(p):
        return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()

    def _extract(prompt, key):
        m = re.search(
            rf"\*?\*?{key}\*?\*?\s*[:=]\s*`?([^\s`\n]+)`?", prompt,
        )
        return Path(m.group(1)) if m else None

    def main():
        prompt = sys.stdin.read()
        tgt_review = _extract(prompt, "TARGET_ARCH_REVIEW")
        if not tgt_review:
            print("fake_malformed_then_valid: no TARGET_ARCH_REVIEW", file=sys.stderr)
            return 3
        active = tgt_review.parent
        arch_design = active / "arch-design.md"
        prd = active / "prd.md"
        if not tgt_review.exists():
            # First attempt: land literally unparseable JSON through the
            # same .tmp -> rename path a real artifact would use, so this
            # exercises json.JSONDecodeError inside load_arch_review
            # rather than a runner-side rejection.
            tgt_review.with_name(tgt_review.name + ".tmp").write_text(
                "{not json", encoding="utf-8",
            )
            return 0
        # Second attempt (rejection feedback fed back as CONTEXT_ARTIFACTS):
        # a valid pass verdict.
        payload = (
            '{"kind":"arch-review","source":"' + str(arch_design) + '",'
            '"source_hash":"' + _hash(arch_design) + '",'
            '"prd_hash":"' + _hash(prd) + '",'
            '"written":"2026-04-20T00:00:00Z","verdict":"pass","findings":[]}'
        )
        tgt_review.with_name(tgt_review.name + ".tmp").write_text(
            payload, encoding="utf-8",
        )
        return 0

    if __name__ == "__main__":
        sys.exit(main())
""")


def test_run_arch_review_retries_on_malformed_json_then_passes(
    git_repo, monkeypatch, tmp_path,
):
    feature = "demo"
    active = _feature_dir(git_repo, feature)
    prd = _write_prd(active)

    from autodev.artifacts.common import write_markdown_with_hash
    from autodev.state.hashing import hash_file

    prd_hash = hash_file(prd)
    write_markdown_with_hash(
        active / "arch-design.md",
        "## 1. Goal\nx\n## 5. PRD coverage\n| R1 | x |\n",
        source=str(prd), source_hash=prd_hash,
    )
    _commit(git_repo, "seed prd + arch-design")

    fake = tmp_path / "fake_malformed_then_valid.py"
    fake.write_text(_MALFORMED_THEN_VALID_REVIEW_SCRIPT, encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("AUTODEV_VENDOR_BIN_CLAUDE", str(fake))

    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo, vendors=_vendors_fake_everywhere(git_repo),
        session_id="t2-malformed",
    ))
    logger = JsonlLog(active / "log.jsonl")

    review = orch._run_arch_review(feature, active, logger)

    assert review.verdict == "pass"

    retrying = [
        r for r in _log_events(active)
        if r.get("stage") == "arch-review" and r.get("event") == "output-rejected-retrying"
    ]
    assert retrying
    assert retrying[-1]["detail"]["attempt"] == 1

    # The rejection feedback file is cleaned up once the retry succeeds.
    assert not (active / "arch-review-output-rejection.json").exists()


# ---------- test 10: orchestrator -> session credit-back wiring ----

def test_arch_design_loop_credits_design_session_once_after_pass(git_repo, monkeypatch):
    """Detail §9.3 test 10 / core §6.4: the design-session turn is
    credited back exactly once per ``_advance_arch_design_loop`` call,
    only after the review that actually passes -- a rejected round must
    not trigger a credit-back."""
    feature = "demo"
    active = _feature_dir(git_repo, feature)
    _write_prd(active)
    _commit(git_repo, "seed prd")

    monkeypatch.setenv("AUTODEV_VENDOR_BIN_CLAUDE", str(FAKE_VENDOR))
    monkeypatch.setenv("AUTODEV_FAKE_FEATURE", feature)
    monkeypatch.setenv("AUTODEV_FAKE_ARCH_REVIEW_REJECT_FIRST", "1")

    import autodev.vendors.session_control as session_control

    calls: list[tuple[Path, str]] = []
    reviews_at_call: list[int] = []

    def _fake_credit(feature_active: Path, role: str) -> int:
        calls.append((feature_active, role))
        reviews_at_call.append(_count_events(active, "arch-review", "stage-complete"))
        return 1

    monkeypatch.setattr(session_control, "credit_feature_session_turn", _fake_credit)

    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo, vendors=_vendors_fake_everywhere(git_repo),
        session_id="t10",
    ))
    result = orch.advance_one(feature)

    assert result.success
    review = json.loads((active / "arch-review.json").read_text())
    assert review["verdict"] == "pass"

    # arch-review was dispatched twice (needs_revision, then pass) but the
    # credit-back happened exactly once -- the reject-first round must
    # not have triggered it.
    assert _count_events(active, "arch-review", "stage-complete") == 2
    assert len(calls) == 1
    assert calls[0][1] == "design"

    # The single credit-back call happened only after both arch-review
    # dispatches (the rejected round and the passing round) had already
    # completed -- i.e. after the pass, not eagerly after the reject.
    assert reviews_at_call == [2]


def test_arch_design_loop_survives_session_credit_failure(git_repo, monkeypatch):
    """Detail §9.3 test 10 (second case): if credit_feature_session_turn
    raises, the loop still completes successfully and the failure is
    logged as ``session-credit-failed`` with stage ``arch-review``."""
    feature = "demo"
    active = _feature_dir(git_repo, feature)
    _write_prd(active)
    _commit(git_repo, "seed prd")

    monkeypatch.setenv("AUTODEV_VENDOR_BIN_CLAUDE", str(FAKE_VENDOR))
    monkeypatch.setenv("AUTODEV_FAKE_FEATURE", feature)

    import autodev.vendors.session_control as session_control

    def _raising_credit(feature_active: Path, role: str) -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(session_control, "credit_feature_session_turn", _raising_credit)

    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo, vendors=_vendors_fake_everywhere(git_repo),
        session_id="t10b",
    ))
    result = orch.advance_one(feature)

    assert result.success
    review = json.loads((active / "arch-review.json").read_text())
    assert review["verdict"] == "pass"

    failed = [
        r for r in _log_events(active)
        if r.get("event") == "session-credit-failed"
    ]
    assert failed
    assert failed[-1]["stage"] == "arch-review"
