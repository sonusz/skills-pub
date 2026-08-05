from __future__ import annotations

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from autodev.state.process_registry import (
    process_group_alive,
    read_processes,
    register_process,
    terminate_registered_processes,
    unregister_process,
)
from autodev.vendors import shared_call
from autodev.vendors.shared_call import call_shared_vendor

FAKE_CLI = Path(__file__).resolve().parent / "fakes" / "fake_vendor_cli.sh"


def _wait_until(predicate, *, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def test_registry_tracks_parallel_process_groups_and_terminates_them(tmp_path):
    registry = tmp_path / ".running-pids.json"
    children = [
        subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        for _ in range(2)
    ]
    try:
        for i, child in enumerate(children):
            register_process(registry, pid=child.pid, label=f"reviewer:{i}")

        entries = read_processes(registry)
        assert {entry["pid"] for entry in entries} == {
            child.pid for child in children
        }
        summary = terminate_registered_processes(
            registry,
            owner_pid=os.getpid(),
            grace_sec=1.0,
        )
        assert set(summary.pids) == {child.pid for child in children}
        assert len(summary.pgids) == 2
        assert summary.all_stopped
        assert not registry.exists()
        for child in children:
            child.wait(timeout=2)
            assert child.returncode in {-15, -9}
    finally:
        for child in children:
            if child.poll() is None:
                try:
                    os.killpg(child.pid, 9)
                except ProcessLookupError:
                    pass
                child.wait(timeout=2)


def test_registry_owner_filter_does_not_kill_unrelated_process(tmp_path):
    registry = tmp_path / ".running-pids.json"
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        register_process(registry, pid=child.pid, label="build")
        summary = terminate_registered_processes(
            registry,
            owner_pid=os.getpid() + 1,
            grace_sec=0.0,
        )
        assert summary.pids == ()
        assert child.poll() is None
    finally:
        unregister_process(registry, pid=child.pid)
        if child.poll() is None:
            os.killpg(child.pid, 9)
            child.wait(timeout=2)


def test_shared_vendor_registers_while_running_and_clears_on_exit(
    git_repo, tmp_path, monkeypatch,
):
    monkeypatch.setenv("AUTODEV_VENDOR_BIN_CLAUDE", str(FAKE_CLI))
    monkeypatch.setenv("AUTODEV_FAKE_BEHAVIOR", "timeout")
    registry = tmp_path / ".running-pids.json"

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            call_shared_vendor,
            vendor="claude",
            model="fake",
            prompt="registry test",
            output_id="registry-test",
            timeout_sec=1,
            cwd=git_repo,
            output_dir=tmp_path / "vendor-out",
            process_registry=registry,
            process_label="panel-reviewer:claude",
        )
        assert _wait_until(lambda: bool(read_processes(registry)))
        entries = read_processes(registry)
        assert entries[0]["label"] == "panel-reviewer:claude"
        result = future.result(timeout=5)

    assert result.returncode != 0
    assert not registry.exists()


def test_shared_vendor_leaves_no_timer_process_group(
    git_repo, tmp_path, monkeypatch,
):
    """A quick vendor exit must also reap call.sh's long timeout watchdog."""
    monkeypatch.setenv("AUTODEV_VENDOR_BIN_CLAUDE", str(FAKE_CLI))
    monkeypatch.setenv("AUTODEV_FAKE_BEHAVIOR", "exit_nonzero")
    captured_pgids: list[int] = []
    real_register = shared_call.register_process

    def capture_register(path, *, pid, label):
        captured_pgids.append(os.getpgid(pid))
        real_register(path, pid=pid, label=label)

    monkeypatch.setattr(shared_call, "register_process", capture_register)
    result = call_shared_vendor(
        vendor="claude",
        model="fake",
        prompt="watchdog cleanup test",
        output_id="watchdog-cleanup",
        timeout_sec=60,
        cwd=git_repo,
        output_dir=tmp_path / "vendor-out",
        process_registry=tmp_path / ".running-pids.json",
    )

    assert result.returncode != 0
    assert len(captured_pgids) == 1
    assert not process_group_alive(captured_pgids[0])
