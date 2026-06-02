"""Feature-lock concurrency safety: self-validation, stale-holder reclaim,
and non-clobbering release.

Guards the two failure modes that let two orchestrators run one feature
concurrently: (1) a crashed holder's lock blocking forever, and (2) an
orphaned holder advancing after its lock was freed/reclaimed.
"""
from __future__ import annotations

import json
import os
import socket

import pytest

from autodev.errors import LockConflict
from autodev.state.lock import Lock, read_owner


def _feature_root(tmp_path):
    fr = tmp_path / "docs" / "features" / "demo" / "active"
    fr.mkdir(parents=True)
    return fr


def _write_owner(fr, **fields):
    (fr / ".lock").mkdir(exist_ok=True)
    base = {"token": "X", "pid": os.getpid(), "host": socket.gethostname()}
    base.update(fields)
    (fr / ".lock" / "owner.json").write_text(json.dumps(base), encoding="utf-8")


def test_validate_true_for_holder_false_after_steal(tmp_path):
    fr = _feature_root(tmp_path)
    lk = Lock(fr, session_id="s1", verb="run")
    lk.acquire()
    assert lk.validate() is True
    # Another acquirer reclaims (token changes) → we no longer hold it.
    _write_owner(fr, token="OTHER")
    assert lk.validate() is False
    # Lock deleted entirely → also not held.
    (fr / ".lock" / "owner.json").unlink()
    assert lk.validate() is False


def test_acquire_reclaims_dead_holder(tmp_path):
    fr = _feature_root(tmp_path)
    _write_owner(fr, pid=999999, token="dead")  # pid that does not exist
    # Dead holder → reclaim, no conflict.
    lk = Lock(fr, session_id="s2", verb="run")
    lk.acquire()
    assert lk.validate() is True
    assert read_owner(fr)["session_id"] == "s2"


def test_acquire_conflicts_with_live_holder(tmp_path):
    fr = _feature_root(tmp_path)
    _write_owner(fr, pid=os.getpid(), token="live")  # this process is alive
    with pytest.raises(LockConflict):
        Lock(fr, session_id="s3", verb="run").acquire()


def test_acquire_force_overrides_live_holder(tmp_path):
    fr = _feature_root(tmp_path)
    _write_owner(fr, pid=os.getpid(), token="live")
    lk = Lock(fr, session_id="s4", verb="run")
    lk.acquire(force=True)  # explicit attestation of staleness
    assert read_owner(fr)["session_id"] == "s4"


def test_release_does_not_clobber_other_holder(tmp_path):
    fr = _feature_root(tmp_path)
    lk = Lock(fr, session_id="s5", verb="run")
    lk.acquire()
    # Someone else reclaimed the lock (token changed).
    _write_owner(fr, token="OTHER")
    lk.release()  # our (orphaned) release must NOT delete the new holder's lock
    assert (fr / ".lock").exists()
    assert read_owner(fr)["token"] == "OTHER"


def test_release_force_removes_regardless(tmp_path):
    fr = _feature_root(tmp_path)
    _write_owner(fr, token="SOMEONE_ELSE")
    Lock(fr, session_id="abort", verb="abort").release(force=True)
    assert not (fr / ".lock").exists()


def test_context_manager_acquires_and_releases(tmp_path):
    fr = _feature_root(tmp_path)
    with Lock(fr, session_id="s6", verb="run") as lk:
        assert lk.validate() is True
        assert (fr / ".lock").exists()
    assert not (fr / ".lock").exists()
