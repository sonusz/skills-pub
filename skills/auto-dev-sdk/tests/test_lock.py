"""ad-1: single-writer lock."""
from __future__ import annotations

import pytest

from auto_dev.errors import LockConflict
from auto_dev.state.lock import Lock, read_owner


def test_lock_acquire_succeeds_on_empty_dir(tmp_path):
    lock = Lock(tmp_path, session_id="s1", verb="implement")
    lock.acquire()
    assert (tmp_path / ".lock" / "owner.json").exists()
    owner = read_owner(tmp_path)
    assert owner["session_id"] == "s1"
    assert owner["verb"] == "implement"
    lock.release()
    assert not (tmp_path / ".lock").exists()


def test_lock_acquire_fails_on_second_caller(tmp_path):
    lock1 = Lock(tmp_path, session_id="s1", verb="implement")
    lock1.acquire()
    lock2 = Lock(tmp_path, session_id="s2", verb="implement")
    with pytest.raises(LockConflict):
        lock2.acquire()
    lock1.release()


def test_lock_release_on_context_manager_exit(tmp_path):
    with Lock(tmp_path, session_id="s1", verb="implement"):
        assert (tmp_path / ".lock").exists()
    assert not (tmp_path / ".lock").exists()


def test_lock_released_on_exception(tmp_path):
    with pytest.raises(RuntimeError):
        with Lock(tmp_path, session_id="s1", verb="implement"):
            assert (tmp_path / ".lock").exists()
            raise RuntimeError("simulated")
    assert not (tmp_path / ".lock").exists()


def test_force_acquire_overwrites_after_attestation(tmp_path):
    lock1 = Lock(tmp_path, session_id="s1", verb="implement")
    lock1.acquire()
    lock2 = Lock(tmp_path, session_id="s2", verb="implement")
    # Force=True documents user attestation that the prior owner is dead.
    lock2.acquire(force=True)
    owner = read_owner(tmp_path)
    assert owner["session_id"] == "s2"
    lock2.release()
