from __future__ import annotations

import os
import subprocess
import sys
import time
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from autodev.state import hostos, process_registry
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


def test_ps_process_group_probe_ignores_zombie_members(monkeypatch):
    monkeypatch.setattr(process_registry.shutil, "which", lambda name: "/bin/ps")
    monkeypatch.setattr(
        process_registry.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="101 S+\n202 Z\n202 Z+\n303 Ss\n",
        ),
    )

    assert process_registry._process_group_alive_via_ps(101)
    assert not process_registry._process_group_alive_via_ps(202)
    assert not process_registry._process_group_alive_via_ps(404)


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


# --- explicit OS branching -------------------------------------------------
#
# process_registry detects the host OS and runs that OS's command; these
# tests drive both branches on any host by patching ``_host_os``. The Linux
# branch reads a fake /proc tree; the macOS branch sees a fake ``ps``.

_LINUX_STAT_LINE = (
    "123 (python3) S 1 123 123 0 -1 4194560 0 0 0 0 0 0 0 0 20 0 1 0 987654 0 0\n"
)
_DARWIN_LSTART = "Wed Sep 16 11:00:33 2026"
_PS_LSTART_FORMAT = "%a %b %d %H:%M:%S %Y"


def _epoch_for(lstart: str) -> str:
    return str(int(time.mktime(time.strptime(lstart, _PS_LSTART_FORMAT))))


def _fake_proc(tmp_path: Path, entries: dict[int, str]) -> Path:
    root = tmp_path / "proc"
    for pid, line in entries.items():
        (root / str(pid)).mkdir(parents=True)
        (root / str(pid) / "stat").write_text(line, encoding="utf-8")
    return root


def _forbid(name: str):
    def boom(*args, **kwargs):
        raise AssertionError(f"{name} must not be consulted on this branch")
    return boom


def _fake_ps(monkeypatch, stdout: str, *, returncode: int = 0) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(process_registry.shutil, "which", lambda name: "/bin/ps")
    monkeypatch.setattr(process_registry.subprocess, "run", fake_run)
    return calls


def test_host_os_maps_platform_families(monkeypatch):
    for platform, expected in (
        ("linux", "linux"),
        ("linux2", "linux"),
        ("darwin", "darwin"),
        ("freebsd14", "freebsd14"),
        ("win32", "win32"),
    ):
        monkeypatch.setattr(hostos.sys, "platform", platform)
        assert hostos._host_os() == expected
    assert process_registry._host_os is hostos._host_os


def test_process_start_id_linux_reads_proc_starttime(tmp_path, monkeypatch):
    monkeypatch.setattr(process_registry, "_host_os", lambda: "linux")
    monkeypatch.setattr(
        process_registry, "_PROC_ROOT",
        _fake_proc(tmp_path, {123: _LINUX_STAT_LINE}),
    )
    monkeypatch.setattr(process_registry.subprocess, "run", _forbid("subprocess.run"))

    assert process_registry._process_start_id(123) == "987654"
    assert process_registry._process_start_id(124) is None


def test_process_start_id_darwin_uses_ps_lstart(monkeypatch):
    monkeypatch.setattr(process_registry, "_host_os", lambda: "darwin")
    calls = _fake_ps(monkeypatch, f"{_DARWIN_LSTART}\n")

    start_id = process_registry._process_start_id(123)

    assert start_id == _epoch_for(_DARWIN_LSTART)
    assert start_id.isdigit()
    assert calls == [["/bin/ps", "-o", "lstart=", "-p", "123"]]


def test_process_start_id_darwin_tolerates_ps_column_padding(monkeypatch):
    """macOS pads single-digit days and appends trailing blanks."""
    monkeypatch.setattr(process_registry, "_host_os", lambda: "darwin")
    padded = "Sun Sep  6 22:34:57 2026"
    _fake_ps(monkeypatch, f"{padded}    \n")

    assert process_registry._process_start_id(7) == _epoch_for(padded)


def test_process_start_id_darwin_unknown_when_ps_fails(monkeypatch):
    monkeypatch.setattr(process_registry, "_host_os", lambda: "darwin")

    _fake_ps(monkeypatch, "", returncode=1)  # no such pid
    assert process_registry._process_start_id(123) is None

    _fake_ps(monkeypatch, "not a timestamp\n")
    assert process_registry._process_start_id(123) is None

    monkeypatch.setattr(process_registry.subprocess, "run", _forbid("subprocess.run"))
    monkeypatch.setattr(process_registry.shutil, "which", lambda name: None)
    assert process_registry._process_start_id(123) is None


def test_process_start_id_other_os_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(process_registry, "_host_os", lambda: "freebsd14")
    monkeypatch.setattr(
        process_registry, "_PROC_ROOT",
        _fake_proc(tmp_path, {123: _LINUX_STAT_LINE}),
    )
    monkeypatch.setattr(process_registry.subprocess, "run", _forbid("subprocess.run"))

    assert process_registry._process_start_id(123) is None


def test_process_group_alive_darwin_zombie_only_group_is_dead(monkeypatch):
    """A successful ps scan is authoritative on macOS: killpg would report
    EPERM for the zombie-only group and be misread as alive."""
    monkeypatch.setattr(process_registry, "_host_os", lambda: "darwin")
    _fake_ps(monkeypatch, "101 S+\n202 Z\n202 Z+\n303 Ss\n")
    monkeypatch.setattr(process_registry.os, "killpg", _forbid("os.killpg"))

    assert process_group_alive(202) is False
    assert process_group_alive(101) is True
    assert process_group_alive(404) is False


def test_process_group_alive_darwin_killpg_is_last_resort_only(monkeypatch):
    monkeypatch.setattr(process_registry, "_host_os", lambda: "darwin")
    monkeypatch.setattr(process_registry.shutil, "which", lambda name: None)
    monkeypatch.setattr(process_registry.subprocess, "run", _forbid("subprocess.run"))

    def raising(exc):
        def killpg(pgid, sig):
            raise exc
        return killpg

    monkeypatch.setattr(process_registry.os, "killpg", raising(ProcessLookupError()))
    assert process_group_alive(202) is False
    monkeypatch.setattr(process_registry.os, "killpg", raising(PermissionError()))
    assert process_group_alive(202) is True
    monkeypatch.setattr(process_registry.os, "killpg", lambda pgid, sig: None)
    assert process_group_alive(202) is True


def test_process_group_alive_linux_scans_proc(tmp_path, monkeypatch):
    monkeypatch.setattr(process_registry, "_host_os", lambda: "linux")
    monkeypatch.setattr(process_registry, "_PROC_ROOT", _fake_proc(tmp_path, {
        100: "100 (a b) S 1 100 100 0 -1 4194560 0 0 0 0 0 0 0 0 20 0 1 0 5 0 0\n",
        200: "200 (z) Z 1 200 200 0 -1 4194560 0 0 0 0 0 0 0 0 20 0 1 0 6 0 0\n",
        201: "201 (z2) Z 1 200 200 0 -1 4194560 0 0 0 0 0 0 0 0 20 0 1 0 7 0 0\n",
        300: "garbage\n",
    }))
    monkeypatch.setattr(process_registry.subprocess, "run", _forbid("subprocess.run"))
    monkeypatch.setattr(process_registry.os, "killpg", _forbid("os.killpg"))

    assert process_group_alive(100) is True
    assert process_group_alive(200) is False
    assert process_group_alive(999) is False


def test_process_group_alive_linux_without_proc_falls_back_to_ps(tmp_path, monkeypatch):
    monkeypatch.setattr(process_registry, "_host_os", lambda: "linux")
    monkeypatch.setattr(process_registry, "_PROC_ROOT", tmp_path / "absent")
    calls = _fake_ps(monkeypatch, "101 S\n")
    monkeypatch.setattr(process_registry.os, "killpg", _forbid("os.killpg"))

    assert process_group_alive(101) is True
    assert process_group_alive(5) is False
    assert calls and calls[0] == ["/bin/ps", "-axo", "pgid=,stat="]


@pytest.mark.skipif(
    process_registry._host_os() not in {"linux", "darwin"},
    reason="start identity is only implemented for linux and darwin",
)
def test_registry_records_start_identity_and_detects_pid_reuse(tmp_path):
    """End-to-end on the real host: the PID-reuse guard must be armed."""
    registry = tmp_path / ".running-pids.json"
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        register_process(registry, pid=child.pid, label="reviewer")
        (entry,) = read_processes(registry)
        assert entry["process_start_id"]
        assert process_registry._entry_is_same_live_process(entry)
        recycled = dict(entry, process_start_id="1")
        assert not process_registry._entry_is_same_live_process(recycled)
    finally:
        unregister_process(registry, pid=child.pid)
        if child.poll() is None:
            os.killpg(child.pid, 9)
            child.wait(timeout=2)
