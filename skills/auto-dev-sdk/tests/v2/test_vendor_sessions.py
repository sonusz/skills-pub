"""Persistent session contract across shared/vendors and auto-dev callers."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from autodev.panel import runner as panel_runner
from autodev.panel.runner import _compose_reviewer_prompt, _invoke_reviewer
from autodev.vendors.config import PanelReviewerSpec, StageSpec
from autodev.vendors.shared_call import SharedVendorResult
from autodev.vendors import subprocess_runner
from autodev.vendors.session_keys import (
    BUILD_SESSION_MAX_TURNS,
    DEFAULT_SESSION_MAX_TURNS,
    DESIGN_SESSION_MAX_TURNS,
    RALPH_REVIEW_SESSION_MAX_TURNS,
    feature_session_key,
    reviewer_session_key,
)
from autodev.vendors.subprocess_runner import run_stage_subprocess


SHARED_CALL = Path(__file__).resolve().parents[4] / "shared" / "vendors" / "scripts" / "call.sh"
SESSION_HELPER = SHARED_CALL.with_name("session-state.py")


FAKE_VENDOR = r'''#!{python}
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
provider = {{
    "codex": "openai",
    "claude": "claude",
    "agy": "agy",
    "cursor-agent": "cursor",
    "grok": "grok",
}}[name]
fixed = {{
    "openai": "11111111-1111-4111-8111-111111111111",
    "agy": "22222222-2222-4222-8222-222222222222",
    "cursor": "33333333-3333-4333-8333-333333333333",
}}.get(provider)

def value(flag, default=""):
    if flag in args:
        pos = args.index(flag)
        if pos + 1 < len(args):
            return args[pos + 1]
    return default

if provider in {{"openai", "claude"}}:
    prompt = sys.stdin.read()
elif provider == "agy":
    prompt = value("--print") or value("-p")
elif provider == "cursor":
    prompt = " ".join(args[args.index("--") + 1:]) if "--" in args else ""
else:
    prompt_path = value("--prompt-file")
    prompt = Path(prompt_path).read_text() if prompt_path else ""

if provider == "claude":
    session_id = value("--resume") or value("--session-id")
elif provider == "grok":
    session_id = value("--resume") or value("--session-id")
elif provider == "openai":
    if args and args[0] == "exec" and "resume" in args:
        resume_pos = args.index("resume")
        session_id = args[resume_pos + 1]
    else:
        session_id = fixed
elif provider == "agy":
    session_id = value("--conversation") or fixed
else:
    session_id = value("--resume") or fixed

capture = Path(os.environ["FAKE_SESSION_CAPTURE"])
with capture.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({{
        "provider": provider,
        "args": args,
        "prompt": prompt,
        "session_id": session_id,
    }}) + "\n")

fork_marker = os.environ.get("FAKE_FORK_ON_TERM_MARKER")
if fork_marker:
    def fork_on_term(_signal_number, _frame):
        child = subprocess.Popen([
            sys.executable, "-c", "import time; time.sleep(30)",
        ])
        Path(fork_marker).write_text(str(child.pid), encoding="utf-8")
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, fork_on_term)

delay = float(os.environ.get("FAKE_SESSION_DELAY_SEC", "0"))
if delay:
    time.sleep(delay)

fail_marker = os.environ.get("FAKE_FAIL_RESUME_ONCE")
is_resume = any(flag in args for flag in ("--resume", "--conversation")) or (
    provider == "openai" and args and args[0] == "exec" and "resume" in args
)
if fail_marker and is_resume and not Path(fail_marker).exists():
    Path(fail_marker).touch()
    print("session not found")
    raise SystemExit(7)

response = "OK:" + prompt.strip()
if provider == "openai":
    out = value("--output-last-message")
    Path(out).write_text(response + "\n", encoding="utf-8")
    print(json.dumps({{"type": "thread.started", "thread_id": session_id}}))
    print(json.dumps({{
        "type": "turn.completed",
        "usage": {{"input_tokens": 1, "output_tokens": 1}},
    }}))
elif provider == "claude":
    print(json.dumps({{"type": "system", "subtype": "init", "session_id": session_id}}))
    print(json.dumps({{
        "type": "result", "subtype": "success", "result": response,
        "session_id": session_id,
        "usage": {{"input_tokens": 1, "output_tokens": 1}},
    }}))
elif provider == "agy":
    payload = {{
        "status": "SUCCESS",
        "response": response, "error": "", "duration_seconds": 0.01,
        "num_turns": 1,
        "usage": {{
            "input_tokens": 1, "output_tokens": 1, "thinking_tokens": 0,
            "cache_read_tokens": 0, "total_tokens": 2,
        }},
    }}
    if os.environ.get("FAKE_OMIT_SESSION_ID") != provider:
        payload["conversation_id"] = session_id
    print(json.dumps(payload))
elif provider == "cursor":
    payload = {{
        "type": "result", "subtype": "success", "result": response,
        "usage": {{"inputTokens": 1, "outputTokens": 1}},
    }}
    if os.environ.get("FAKE_OMIT_SESSION_ID") != provider:
        payload["session_id"] = session_id
    print(json.dumps(payload))
else:
    print(json.dumps({{"type": "text", "data": response}}))
    print(json.dumps({{
        "type": "end", "sessionId": session_id,
        "usage": {{"input_tokens": 1, "output_tokens": 1}},
    }}))
'''


@pytest.fixture()
def fake_vendor_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "fake-vendor"
    executable.write_text(FAKE_VENDOR.format(python=sys.executable), encoding="utf-8")
    executable.chmod(0o755)
    for name in ("codex", "claude", "agy", "cursor-agent", "grok"):
        (bin_dir / name).symlink_to(executable)
    capture = tmp_path / "capture.jsonl"
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_SESSION_CAPTURE"] = str(capture)
    env["VENDORS_SESSION_STATE_DIR"] = str(tmp_path / "session-state")
    return env, capture


@pytest.mark.parametrize(
    ("vendor", "output_id", "resume_flag"),
    [
        ("openai", "openai", "resume"),
        ("claude", "claude", "--resume"),
        ("agy", "agy", "--conversation"),
        ("cursor", "cursor", "--resume"),
        ("grok", "grok", "--resume"),
    ],
)
def test_shared_vendor_reuses_native_session_and_delta_prompt(
    tmp_path: Path,
    fake_vendor_env: tuple[dict[str, str], Path],
    vendor: str,
    output_id: str,
    resume_flag: str,
) -> None:
    env, capture = fake_vendor_env
    session_key = f"test:{vendor}:reviewer-0"

    for run_no in (1, 2):
        output_dir = tmp_path / f"run-{vendor}-{run_no}"
        proc = subprocess.run(
            [
                "/bin/bash", str(SHARED_CALL),
                "--vendor", vendor,
                "--model", "fake-model",
                "--id", output_id,
                "--session-key", session_key,
                "--prompt", "INITIAL FULL PROMPT",
                "--resume-prompt", "DELTA PROMPT",
                "--output-dir", str(output_dir),
                "--timeout", "30",
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=45,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        status = (output_dir / output_id / "status").read_text(encoding="utf-8")
        expected_mode = "new" if run_no == 1 else "resume"
        assert f"session_mode={expected_mode}" in status
        assert "session_id=" in status

    events = [json.loads(line) for line in capture.read_text().splitlines()]
    mine = [event for event in events if event["provider"] == output_id]
    assert len(mine) == 2
    # codex prompts carry the shared-module non-interactive preamble
    # (VENDORS_CODEX_PREAMBLE) ahead of the caller's text.
    assert mine[0]["prompt"].strip().endswith("INITIAL FULL PROMPT")
    assert mine[1]["prompt"].strip().endswith("DELTA PROMPT")
    assert "INITIAL FULL PROMPT" not in mine[1]["prompt"]
    assert mine[0]["session_id"] == mine[1]["session_id"]
    assert resume_flag in mine[1]["args"]


def test_shared_vendor_rotates_session_after_successful_turn_limit(
    tmp_path: Path,
    fake_vendor_env: tuple[dict[str, str], Path],
) -> None:
    env, capture = fake_vendor_env
    modes: list[str] = []
    turns: list[str] = []
    auto_resets: list[str] = []

    for run_no in range(1, 8):
        output_dir = tmp_path / f"rotation-{run_no}"
        proc = subprocess.run(
            [
                "/bin/bash", str(SHARED_CALL),
                "--vendor", "claude",
                "--id", "claude",
                "--session-key", "rotation-key",
                "--session-max-turns", "3",
                "--prompt", "INITIAL",
                "--resume-prompt", "DELTA",
                "--output-dir", str(output_dir),
                "--timeout", "30",
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=45,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        status = {}
        for line in (output_dir / "claude" / "status").read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator:
                status[key] = value
        modes.append(status["session_mode"])
        turns.append(status["session_turn"])
        auto_resets.append(status["session_auto_reset"])

    assert modes == ["new", "resume", "resume", "new", "resume", "resume", "new"]
    assert turns == ["1", "2", "3", "1", "2", "3", "1"]
    assert auto_resets == ["false", "false", "false", "true", "false", "false", "true"]
    events = [json.loads(line) for line in capture.read_text().splitlines()]
    session_ids = [event["session_id"] for event in events]
    assert session_ids[0] == session_ids[1] == session_ids[2]
    assert session_ids[3] == session_ids[4] == session_ids[5]
    assert len({session_ids[0], session_ids[3], session_ids[6]}) == 3
    assert [event["prompt"].strip() for event in events] == [
        "INITIAL", "DELTA", "DELTA", "INITIAL", "DELTA", "DELTA", "INITIAL",
    ]


def test_codex_native_arguments_remain_in_exec_scope_on_resume(
    tmp_path: Path,
    fake_vendor_env: tuple[dict[str, str], Path],
) -> None:
    env, capture = fake_vendor_env
    for run_no in (1, 2):
        output_dir = tmp_path / f"codex-native-{run_no}"
        proc = subprocess.run(
            [
                "/bin/bash", str(SHARED_CALL),
                "--vendor", "openai",
                "--session-key", "codex-native-scope",
                "--native-arg", "--oss",
                "--prompt", "INITIAL",
                "--resume-prompt", "DELTA",
                "--output-dir", str(output_dir),
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr

    events = [json.loads(line) for line in capture.read_text().splitlines()]
    resumed_args = events[1]["args"]
    assert resumed_args.index("--oss") < resumed_args.index("resume")


def test_keyed_process_group_bootstrap_preserves_stdin_prompt(
    tmp_path: Path,
    fake_vendor_env: tuple[dict[str, str], Path],
) -> None:
    env, capture = fake_vendor_env
    proc = subprocess.run(
        [
            "/bin/bash", str(SHARED_CALL),
            "--vendor", "agy",
            "--session-key", "stdin-bootstrap",
            "--output-dir", str(tmp_path / "stdin-output"),
        ],
        input="PROMPT FROM STDIN",
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    event = json.loads(capture.read_text(encoding="utf-8").strip())
    assert event["prompt"].strip() == "PROMPT FROM STDIN"


def test_session_keys_are_stable_and_reviewer_slots_are_isolated(tmp_path: Path) -> None:
    active = tmp_path / "repo" / "docs" / "features" / "feature-a" / "active"
    active.mkdir(parents=True)

    assert feature_session_key(active, "design") == feature_session_key(active, "design")
    assert feature_session_key(active, "design") != feature_session_key(active, "build")
    first = reviewer_session_key(
        active, gate="design-review", slot=0,
        configured_vendor="claude", configured_model="opus",
    )
    second = reviewer_session_key(
        active, gate="design-review", slot=1,
        configured_vendor="claude", configured_model="opus",
    )
    trace = reviewer_session_key(
        active, gate="trace-review", slot=0,
        configured_vendor="claude", configured_model="opus",
    )
    assert len({first, second, trace}) == 3


def test_design_and_design_review_sessions_rotate_on_review_type_switch(
    tmp_path: Path,
) -> None:
    active = tmp_path / "repo" / "docs" / "features" / "feature-a" / "active"
    active.mkdir(parents=True)
    log_path = active / "log.jsonl"

    def append_round(round_key: str, round_type: str) -> None:
        row = {
            "event": "panel-review-round",
            "detail": {
                "gate": "design-review",
                "round_key": round_key,
                "round_type": round_type,
            },
        }
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    def reviewer_key(gate: str = "design-review") -> str:
        return reviewer_session_key(
            active, gate=gate, slot=0,
            configured_vendor="claude", configured_model="opus",
        )

    initial_design = feature_session_key(active, "design")
    initial_reviewer = reviewer_key()
    initial_trace = reviewer_key("trace-review")

    append_round("packet-1", "coverage")
    append_round("packet-2", "coverage")
    assert feature_session_key(active, "design") == initial_design
    assert reviewer_key() == initial_reviewer

    append_round("packet-3", "budget")
    budget_design = feature_session_key(active, "design")
    budget_reviewer = reviewer_key()
    assert budget_design != initial_design
    assert budget_reviewer != initial_reviewer

    append_round("packet-3", "budget")  # resumed duplicate: no new epoch
    append_round("packet-4", "budget")
    assert feature_session_key(active, "design") == budget_design
    assert reviewer_key() == budget_reviewer

    append_round("packet-5", "coverage")
    assert feature_session_key(active, "design") not in {
        initial_design, budget_design,
    }
    assert reviewer_key() not in {initial_reviewer, budget_reviewer}
    assert reviewer_key("trace-review") == initial_trace


def test_missing_native_session_is_invalidated_and_next_call_starts_fresh(
    tmp_path: Path,
    fake_vendor_env: tuple[dict[str, str], Path],
) -> None:
    env, _ = fake_vendor_env
    env["FAKE_FAIL_RESUME_ONCE"] = str(tmp_path / "failed-once")
    modes: list[str] = []

    for run_no in (1, 2, 3):
        output_dir = tmp_path / f"recovery-{run_no}"
        proc = subprocess.run(
            [
                "/bin/bash", str(SHARED_CALL),
                "--vendor", "agy",
                "--id", "agy",
                "--session-key", "recovery-key",
                "--prompt", "INITIAL",
                "--resume-prompt", "DELTA",
                "--output-dir", str(output_dir),
                "--timeout", "30",
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=45,
        )
        status = (output_dir / "agy" / "status").read_text(encoding="utf-8")
        mode = next(
            line.partition("=")[2]
            for line in status.splitlines()
            if line.startswith("session_mode=")
        )
        modes.append(mode)
        if run_no == 2:
            assert proc.returncode != 0
            assert "session_invalidated=1" in status
        else:
            assert proc.returncode == 0, proc.stdout + proc.stderr

    assert modes == ["new", "resume", "new"]


def test_session_state_rejects_concurrent_owner_and_never_stores_raw_key(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    first_plan = tmp_path / "first-plan.json"
    second_plan = tmp_path / "second-plan.json"
    common = [
        sys.executable, str(SESSION_HELPER), "plan",
        "--state-dir", str(state_dir),
        "--key", "sensitive-logical-key",
        "--vendor", "openai",
        "--model", "fake-model",
        "--cwd", str(tmp_path),
        "--owner-pid", str(os.getpid()),
        "--lease-sec", "60",
    ]

    first = subprocess.run(
        [*common, "--output", str(first_plan)],
        text=True, capture_output=True, timeout=10,
    )
    second = subprocess.run(
        [*common, "--output", str(second_plan)],
        text=True, capture_output=True, timeout=10,
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode != 0
    assert "already in use" in second.stderr
    state_files = list(state_dir.glob("*.json"))
    assert len(state_files) == 1
    assert "sensitive-logical-key" not in state_files[0].read_text(encoding="utf-8")
    assert state_dir.stat().st_mode & 0o777 == 0o700
    assert state_files[0].stat().st_mode & 0o777 == 0o600

    released = subprocess.run(
        [sys.executable, str(SESSION_HELPER), "finalize", "--plan", str(first_plan)],
        text=True, capture_output=True, timeout=10,
    )
    assert released.returncode == 0, released.stderr
    third = subprocess.run(
        [*common, "--output", str(second_plan)],
        text=True, capture_output=True, timeout=10,
    )
    assert third.returncode == 0, third.stderr
    subprocess.run(
        [sys.executable, str(SESSION_HELPER), "finalize", "--plan", str(second_plan)],
        check=True, text=True, capture_output=True, timeout=10,
    )


def test_session_reset_forgets_all_identities_and_refuses_live_lease(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    key = "feature-design-key"

    def plan(identity: str, output: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable, str(SESSION_HELPER), "plan",
                "--state-dir", str(state_dir),
                "--key", key,
                "--vendor", "claude",
                "--model", identity,
                "--cwd", str(tmp_path),
                "--owner-pid", str(os.getpid()),
                "--lease-sec", "60",
                "--output", str(output),
            ],
            text=True, capture_output=True, timeout=10,
        )

    plans = [tmp_path / "one.json", tmp_path / "two.json"]
    for index, plan_path in enumerate(plans, start=1):
        created = plan(f"model-{index}", plan_path)
        assert created.returncode == 0, created.stderr
        finalized = subprocess.run(
            [
                sys.executable, str(SESSION_HELPER), "finalize",
                "--plan", str(plan_path), "--success",
                "--observed-session-id", f"11111111-1111-4111-8111-11111111111{index}",
            ],
            text=True, capture_output=True, timeout=10,
        )
        assert finalized.returncode == 0, finalized.stderr

    live_plan = tmp_path / "live.json"
    resumed = plan("model-1", live_plan)
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(live_plan.read_text())["mode"] == "resume"
    blocked = subprocess.run(
        [
            sys.executable, str(SESSION_HELPER), "reset",
            "--state-dir", str(state_dir), "--key", key,
        ],
        text=True, capture_output=True, timeout=10,
    )
    assert blocked.returncode != 0
    assert "active lease" in blocked.stderr
    subprocess.run(
        [sys.executable, str(SESSION_HELPER), "finalize", "--plan", str(live_plan)],
        check=True, text=True, capture_output=True, timeout=10,
    )

    reset = subprocess.run(
        [
            sys.executable, str(SESSION_HELPER), "reset",
            "--state-dir", str(state_dir), "--key", key,
        ],
        text=True, capture_output=True, timeout=10,
    )
    assert reset.returncode == 0, reset.stderr
    assert reset.stdout.strip() == "2"

    fresh_plan = tmp_path / "fresh.json"
    fresh = plan("model-1", fresh_plan)
    assert fresh.returncode == 0, fresh.stderr
    assert json.loads(fresh_plan.read_text())["mode"] == "new"
    subprocess.run(
        [sys.executable, str(SESSION_HELPER), "finalize", "--plan", str(fresh_plan)],
        check=True, text=True, capture_output=True, timeout=10,
    )


def test_failed_session_turn_does_not_advance_rotation_counter(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    session_id = "11111111-1111-4111-8111-111111111111"

    def plan(name: str) -> tuple[Path, dict[str, object]]:
        output = tmp_path / f"{name}.json"
        proc = subprocess.run(
            [
                sys.executable, str(SESSION_HELPER), "plan",
                "--state-dir", str(state_dir),
                "--key", "failure-count-key",
                "--vendor", "openai",
                "--model", "fake-model",
                "--cwd", str(tmp_path),
                "--owner-pid", str(os.getpid()),
                "--lease-sec", "60",
                "--max-turns", "2",
                "--output", str(output),
            ],
            text=True, capture_output=True, timeout=10,
        )
        assert proc.returncode == 0, proc.stderr
        return output, json.loads(output.read_text())

    first_path, first = plan("first")
    assert (first["mode"], first["turn_number"], first["auto_reset"]) == (
        "new", 1, False,
    )
    subprocess.run(
        [
            sys.executable, str(SESSION_HELPER), "finalize",
            "--plan", str(first_path), "--success",
            "--observed-session-id", session_id,
        ],
        check=True, text=True, capture_output=True, timeout=10,
    )

    failed_path, failed = plan("failed")
    assert (failed["mode"], failed["turn_number"]) == ("resume", 2)
    subprocess.run(
        [sys.executable, str(SESSION_HELPER), "finalize", "--plan", str(failed_path)],
        check=True, text=True, capture_output=True, timeout=10,
    )

    retry_path, retry = plan("retry")
    assert (retry["mode"], retry["turn_number"], retry["auto_reset"]) == (
        "resume", 2, False,
    )
    subprocess.run(
        [
            sys.executable, str(SESSION_HELPER), "finalize",
            "--plan", str(retry_path), "--success",
            "--observed-session-id", session_id,
        ],
        check=True, text=True, capture_output=True, timeout=10,
    )

    rotated_path, rotated = plan("rotated")
    assert (rotated["mode"], rotated["turn_number"], rotated["auto_reset"]) == (
        "new", 1, True,
    )
    subprocess.run(
        [sys.executable, str(SESSION_HELPER), "finalize", "--plan", str(rotated_path)],
        check=True, text=True, capture_output=True, timeout=10,
    )


def test_unexpired_session_lease_survives_owner_process_exit(tmp_path: Path) -> None:
    owner = subprocess.Popen([sys.executable, "-c", "pass"])
    owner_pid = owner.pid
    assert owner.wait(timeout=10) == 0

    state_dir = tmp_path / "state"
    first_plan = tmp_path / "first-plan.json"
    second_plan = tmp_path / "second-plan.json"
    common = [
        sys.executable, str(SESSION_HELPER), "plan",
        "--state-dir", str(state_dir),
        "--key", "crashed-owner-key",
        "--vendor", "openai",
        "--model", "fake-model",
        "--cwd", str(tmp_path),
        "--owner-pid", str(owner_pid),
        "--lease-sec", "60",
    ]

    first = subprocess.run(
        [*common, "--output", str(first_plan)],
        text=True, capture_output=True, timeout=10,
    )
    second = subprocess.run(
        [*common, "--output", str(second_plan)],
        text=True, capture_output=True, timeout=10,
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode != 0
    assert "already in use" in second.stderr
    subprocess.run(
        [sys.executable, str(SESSION_HELPER), "finalize", "--plan", str(first_plan)],
        check=True, text=True, capture_output=True, timeout=10,
    )


def test_handled_group_termination_releases_keyed_session_lease(
    tmp_path: Path,
    fake_vendor_env: tuple[dict[str, str], Path],
) -> None:
    env, capture = fake_vendor_env
    hanging_env = env.copy()
    hanging_env["FAKE_SESSION_DELAY_SEC"] = "30"
    fork_marker = tmp_path / "forked-child-pid"
    hanging_env["FAKE_FORK_ON_TERM_MARKER"] = str(fork_marker)
    output_dir = tmp_path / "interrupted"
    proc = subprocess.Popen(
        [
            "/bin/bash", str(SHARED_CALL),
            "--vendor", "agy",
            "--session-key", "interrupt-key",
            "--prompt", "HANG",
            "--output-dir", str(output_dir),
        ],
        env=hanging_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    deadline = time.monotonic() + 5
    while (
        (not capture.exists() or not capture.read_text(encoding="utf-8").strip())
        and proc.poll() is None
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)

    try:
        assert capture.exists() and capture.read_text(encoding="utf-8").strip()
        os.killpg(proc.pid, signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate(timeout=10)

    assert proc.returncode == 143, stdout + stderr
    assert fork_marker.exists(), "fake vendor did not exercise the post-TERM fork"
    forked_pid = int(fork_marker.read_text(encoding="utf-8"))
    forked_status = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(forked_pid)],
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert (
        forked_status.returncode != 0
        or not forked_status.stdout.strip()
        or forked_status.stdout.strip().startswith(("Z", "X"))
    )
    assert not (output_dir / ".vendors-call-agy.lock").exists()
    state_files = list((tmp_path / "session-state").glob("*.json"))
    assert len(state_files) == 1
    interrupted_state = json.loads(state_files[0].read_text(encoding="utf-8"))
    assert "lease_token" not in interrupted_state
    assert interrupted_state["last_interrupted"] is True

    retry_output = tmp_path / "interrupt-retry"
    retry = subprocess.run(
        [
            "/bin/bash", str(SHARED_CALL),
            "--vendor", "agy",
            "--session-key", "interrupt-key",
            "--prompt", "RETRY",
            "--output-dir", str(retry_output),
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert retry.returncode == 0, retry.stdout + retry.stderr
    retry_status = (retry_output / "agy" / "status").read_text(encoding="utf-8")
    assert "session_mode=new" in retry_status


def test_interrupted_cleanup_releases_only_exact_plan_tokens(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    stale_plan = tmp_path / "stale-plan.json"
    current_plan = tmp_path / "current-plan.json"
    script = r'''
set -e
py="$1"
helper="$2"
state="$3"
stale="$4"
current="$5"
"$py" "$helper" plan --state-dir "$state" --key stale-key \
  --vendor agy --cwd "$state" --owner-pid "$$" --output "$stale"
"$py" "$helper" plan --state-dir "$state" --key current-key \
  --vendor agy --cwd "$state" --owner-pid "$$" --output "$current"
"$py" "$helper" interrupt --process-group "$$" \
  --coordinator-pid "$$" --plan "$current"
printf 'cleanup-complete\n'
'''
    proc = subprocess.run(
        [
            "/bin/bash", "-c", script, "bash",
            sys.executable, str(SESSION_HELPER), str(state_dir),
            str(stale_plan), str(current_plan),
        ],
        text=True,
        capture_output=True,
        timeout=20,
        start_new_session=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    stale_record = Path(json.loads(stale_plan.read_text())["record_path"])
    current_record = Path(json.loads(current_plan.read_text())["record_path"])
    stale_state = json.loads(stale_record.read_text(encoding="utf-8"))
    current_state = json.loads(current_record.read_text(encoding="utf-8"))
    assert "lease_token" in stale_state
    assert "lease_token" not in current_state
    assert current_state["last_interrupted"] is True

    subprocess.run(
        [sys.executable, str(SESSION_HELPER), "finalize", "--plan", str(stale_plan)],
        check=True,
        text=True,
        capture_output=True,
        timeout=10,
    )


def test_observation_ignores_nested_grok_session_id_decoys(tmp_path: Path) -> None:
    decoy = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    actual = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    transcript = tmp_path / "grok.jsonl"
    transcript.write_text(
        "\n".join([
            json.dumps({
                "type": "tool_call",
                "rawInput": {"sessionId": decoy},
            }),
            json.dumps({"type": "end", "sessionId": actual}),
        ]),
        encoding="utf-8",
    )
    output = tmp_path / "out"
    output.write_text("assistant text", encoding="utf-8")
    observed = tmp_path / "session.json"

    proc = subprocess.run(
        [
            sys.executable, str(SESSION_HELPER), "observe",
            "--vendor", "grok",
            "--output-file", str(output),
            "--transcript-file", str(transcript),
            "--mode", "new",
            "--exit-code", "0",
            "--output", str(observed),
        ],
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(observed.read_text(encoding="utf-8"))
    assert payload["session_id"] == actual
    assert decoy not in observed.read_text(encoding="utf-8")


def test_implicit_working_directory_is_part_of_session_identity(
    tmp_path: Path,
    fake_vendor_env: tuple[dict[str, str], Path],
) -> None:
    env, _ = fake_vendor_env
    cwd_a = tmp_path / "repo-a"
    cwd_b = tmp_path / "repo-b"
    cwd_a.mkdir()
    cwd_b.mkdir()
    statuses: list[str] = []

    for index, cwd in enumerate((cwd_a, cwd_b), start=1):
        output_dir = tmp_path / f"cwd-run-{index}"
        proc = subprocess.run(
            [
                "/bin/bash", str(SHARED_CALL),
                "--vendor", "agy",
                "--session-key", "same-key",
                "--prompt", "INITIAL",
                "--output-dir", str(output_dir),
            ],
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        statuses.append((output_dir / "agy" / "status").read_text(encoding="utf-8"))

    assert all("session_mode=new" in status for status in statuses)
    hashes = {
        next(
            line.partition("=")[2]
            for line in status.splitlines()
            if line.startswith("session_key_hash=")
        )
        for status in statuses
    }
    assert len(hashes) == 2
    assert len(list((tmp_path / "session-state").glob("*.json"))) == 2


def test_concurrent_calls_cannot_share_one_output_id(
    tmp_path: Path,
    fake_vendor_env: tuple[dict[str, str], Path],
) -> None:
    env, capture = fake_vendor_env
    delayed_env = env.copy()
    delayed_env["FAKE_SESSION_DELAY_SEC"] = "1.5"
    output_dir = tmp_path / "shared-output"
    first_command = [
        "/bin/bash", str(SHARED_CALL),
        "--vendor", "agy",
        "--id", "agy",
        "--session-key", "first-key",
        "--prompt", "FIRST",
        "--output-dir", str(output_dir),
    ]
    first = subprocess.Popen(
        first_command,
        env=delayed_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    lock_dir = output_dir / ".vendors-call-agy.lock"
    deadline = time.monotonic() + 5
    while not lock_dir.exists() and first.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)

    try:
        assert lock_dir.is_dir(), "first coordinator never acquired its output lock"
        second = subprocess.run(
            [
                "/bin/bash", str(SHARED_CALL),
                "--vendor", "agy",
                "--id", "agy",
                "--session-key", "second-key",
                "--prompt", "SECOND",
                "--output-dir", str(output_dir),
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
        )
        assert second.returncode == 2
        assert "output id is already in use" in second.stderr
        first_stdout, first_stderr = first.communicate(timeout=10)
    finally:
        if first.poll() is None:
            first.terminate()
            first.communicate(timeout=10)

    assert first.returncode == 0, first_stdout + first_stderr
    assert not lock_dir.exists()
    first_status = (output_dir / "agy" / "status").read_text(encoding="utf-8")
    assert "exit_code=0" in first_status
    assert "session_mode=new" in first_status

    followup_dir = tmp_path / "followup-output"
    followup = subprocess.run(
        [
            "/bin/bash", str(SHARED_CALL),
            "--vendor", "agy",
            "--session-key", "first-key",
            "--prompt", "INITIAL",
            "--resume-prompt", "FOLLOWUP",
            "--output-dir", str(followup_dir),
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert followup.returncode == 0, followup.stdout + followup.stderr
    followup_status = (followup_dir / "agy" / "status").read_text(encoding="utf-8")
    assert "session_mode=resume" in followup_status
    events = [json.loads(line) for line in capture.read_text().splitlines()]
    assert [event["prompt"].strip() for event in events] == ["FIRST", "FOLLOWUP"]


@pytest.mark.parametrize("vendor", ["agy", "cursor"])
def test_success_without_dynamic_native_session_id_fails_closed(
    tmp_path: Path,
    fake_vendor_env: tuple[dict[str, str], Path],
    vendor: str,
) -> None:
    env, _ = fake_vendor_env
    missing_env = env.copy()
    missing_env["FAKE_OMIT_SESSION_ID"] = vendor
    failed_output = tmp_path / f"missing-{vendor}"
    first = subprocess.run(
        [
            "/bin/bash", str(SHARED_CALL),
            "--vendor", vendor,
            "--session-key", f"missing-id-{vendor}",
            "--prompt", "INITIAL",
            "--output-dir", str(failed_output),
        ],
        env=missing_env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    status = (failed_output / vendor / "status").read_text(encoding="utf-8")
    assert first.returncode != 0
    assert "exit_code=70" in status
    assert "session protocol did not yield a native session id" in status

    retry_output = tmp_path / f"retry-{vendor}"
    retry = subprocess.run(
        [
            "/bin/bash", str(SHARED_CALL),
            "--vendor", vendor,
            "--session-key", f"missing-id-{vendor}",
            "--prompt", "INITIAL AGAIN",
            "--output-dir", str(retry_output),
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert retry.returncode == 0, retry.stdout + retry.stderr
    retry_status = (retry_output / vendor / "status").read_text(encoding="utf-8")
    assert "session_mode=new" in retry_status


def test_native_arguments_are_fingerprinted_without_being_persisted(
    tmp_path: Path,
    fake_vendor_env: tuple[dict[str, str], Path],
) -> None:
    env, _ = fake_vendor_env
    raw_args = ("--project=alpha-sensitive", "--project=beta-sensitive")
    modes: list[str] = []

    for index, native_arg in enumerate(raw_args, start=1):
        output_dir = tmp_path / f"native-{index}"
        proc = subprocess.run(
            [
                "/bin/bash", str(SHARED_CALL),
                "--vendor", "agy",
                "--session-key", "native-context-key",
                "--native-arg", native_arg,
                "--prompt", "INITIAL",
                "--output-dir", str(output_dir),
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        status = (output_dir / "agy" / "status").read_text(encoding="utf-8")
        modes.append(status)

    assert all("session_mode=new" in status for status in modes)
    state_files = list((tmp_path / "session-state").glob("*.json"))
    assert len(state_files) == 2
    serialized_state = "\n".join(
        path.read_text(encoding="utf-8") for path in state_files
    )
    assert all(raw_arg not in serialized_state for raw_arg in raw_args)


@pytest.mark.parametrize("output_id", [".", ".."])
def test_output_id_rejects_directory_traversal_aliases(
    tmp_path: Path,
    fake_vendor_env: tuple[dict[str, str], Path],
    output_id: str,
) -> None:
    env, capture = fake_vendor_env
    proc = subprocess.run(
        [
            "/bin/bash", str(SHARED_CALL),
            "--vendor", "agy",
            "--id", output_id,
            "--prompt", "MUST NOT RUN",
            "--output-dir", str(tmp_path / "output"),
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert proc.returncode == 2
    assert "direct child" in proc.stderr
    assert not capture.exists()


def test_output_id_rejects_symlinked_call_directory(
    tmp_path: Path,
    fake_vendor_env: tuple[dict[str, str], Path],
) -> None:
    env, capture = fake_vendor_env
    output_dir = tmp_path / "output"
    target = tmp_path / "shared-target"
    output_dir.mkdir()
    target.mkdir()
    (output_dir / "agy").symlink_to(target, target_is_directory=True)

    proc = subprocess.run(
        [
            "/bin/bash", str(SHARED_CALL),
            "--vendor", "agy",
            "--prompt", "MUST NOT RUN",
            "--output-dir", str(output_dir),
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert proc.returncode == 2
    assert "must not be a symlink" in proc.stderr
    assert not capture.exists()
    assert list(target.iterdir()) == []


@pytest.mark.parametrize(
    ("vendor", "native_arg"),
    [
        ("openai", "--last"),
        ("claude", "--from-pr"),
        ("claude", "-r11111111-1111-4111-8111-111111111111"),
        ("agy", "--output-format=json"),
        ("cursor", "resume"),
        ("grok", "--session-id"),
        ("grok", "-s55555555-5555-4555-8555-555555555555"),
    ],
)
def test_session_key_rejects_native_flags_that_override_session_transport(
    tmp_path: Path,
    fake_vendor_env: tuple[dict[str, str], Path],
    vendor: str,
    native_arg: str,
) -> None:
    env, _ = fake_vendor_env
    proc = subprocess.run(
        [
            "/bin/bash", str(SHARED_CALL),
            "--vendor", vendor,
            "--session-key", "owned-session-key",
            "--native-arg", native_arg,
            "--prompt", "INITIAL",
            "--output-dir", str(tmp_path / "conflict"),
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert proc.returncode == 2
    assert "owns native session transport" in proc.stderr
    assert not (tmp_path / "session-state").exists()


@pytest.mark.parametrize(
    ("stage", "stateful"),
    [
        ("design", True),
        ("build", True),
        ("ralph-review", True),
        ("spec", False),
    ],
)
def test_stage_runner_enables_sessions_only_for_repeating_agents(
    git_repo: Path,
    feature_active: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    stateful: bool,
) -> None:
    captured: dict[str, object] = {}
    artifact = feature_active / f"{stage}.json"

    def fake_call_shared_vendor(**kwargs: object) -> SharedVendorResult:
        captured.update(kwargs)
        artifact.with_name(artifact.name + ".tmp").write_text("{}", encoding="utf-8")
        return SharedVendorResult(
            vendor=str(kwargs["vendor"]),
            output_id=str(kwargs["output_id"]),
            returncode=0,
            output="ok",
            log="",
            status={"exit_code": "0", "session_mode": "new"},
            summary_stdout="",
            summary_stderr="",
            elapsed_sec=0.01,
            output_dir=tmp_path,
        )

    monkeypatch.setattr(
        subprocess_runner, "call_shared_vendor", fake_call_shared_vendor,
    )
    spec = StageSpec(
        stage=stage, vendor="claude", model="fake", probe_interval_sec=30,
    )

    result = run_stage_subprocess(
        stage=stage,
        stage_spec=spec,
        prompt="full prompt",
        resume_prompt="delta prompt",
        artifact_target=artifact,
        feature_active=feature_active,
        allowed_write_paths=[feature_active],
        cwd=git_repo,
    )

    assert result.ok
    if stateful:
        assert captured["session_key"] == feature_session_key(feature_active, stage)
        assert captured["resume_prompt"] == "delta prompt"
        expected_max_turns = {
            "design": DESIGN_SESSION_MAX_TURNS,
            "build": BUILD_SESSION_MAX_TURNS,
            "ralph-review": RALPH_REVIEW_SESSION_MAX_TURNS,
        }.get(stage, DEFAULT_SESSION_MAX_TURNS)
        assert captured["session_max_turns"] == expected_max_turns
    else:
        assert captured["session_key"] is None
        assert captured["resume_prompt"] is None
        assert captured["session_max_turns"] is None


def test_panel_reviewer_forwards_its_session_and_continuation_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_call_shared_vendor(**kwargs: object) -> SharedVendorResult:
        captured.update(kwargs)
        return SharedVendorResult(
            vendor=str(kwargs["vendor"]),
            output_id=str(kwargs["output_id"]),
            returncode=0,
            output="Verdict: pass",
            log="",
            status={"exit_code": "0", "session_mode": "resume"},
            summary_stdout="",
            summary_stderr="",
            elapsed_sec=0.01,
            output_dir=tmp_path,
        )

    monkeypatch.delenv(panel_runner.FAKE_INVOKER_ENV, raising=False)
    monkeypatch.setattr(panel_runner, "call_shared_vendor", fake_call_shared_vendor)
    result = _invoke_reviewer(
        PanelReviewerSpec(vendor="claude", model="fake"),
        "full reviewer contract",
        30,
        cwd=tmp_path,
        session_key="reviewer-slot-key",
        resume_prompt="review current revision",
    )

    assert result.ok
    assert captured["session_key"] == "reviewer-slot-key"
    assert captured["session_max_turns"] == DEFAULT_SESSION_MAX_TURNS
    assert captured["resume_prompt"] == "review current revision"


def test_reviewer_continuation_prompt_reuses_contract_but_refreshes_file_refs(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "design-packet.json"
    artifact.write_text("{}", encoding="utf-8")

    initial = _compose_reviewer_prompt(
        gate="design-review", artifact_path=artifact, consulted_docs=[],
        feature_active=tmp_path,
    )
    continuation = _compose_reviewer_prompt(
        gate="design-review", artifact_path=artifact, consulted_docs=[],
        feature_active=tmp_path, continuation=True,
    )

    assert "Continue the existing `design-review` reviewer session" in continuation
    assert str(artifact) in continuation
    assert "hash=`" in continuation
    assert len(continuation) < len(initial)
