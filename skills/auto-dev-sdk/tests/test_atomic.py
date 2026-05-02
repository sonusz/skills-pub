"""ad-1: atomic write primitives."""
from __future__ import annotations

import pytest

from auto_dev.state.atomic import atomic_write, atomic_write_json


def test_atomic_write_renames_on_success(tmp_path):
    path = tmp_path / "out.txt"
    atomic_write(path, "hello")
    assert path.read_text() == "hello"
    assert not (tmp_path / "out.txt.tmp").exists()


def test_atomic_write_preserves_existing_on_error(tmp_path, monkeypatch):
    path = tmp_path / "out.txt"
    atomic_write(path, "original")

    import os

    real_replace = os.replace

    def boom(*a, **k):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write(path, "new content")

    # Original intact; tmp cleaned up.
    assert path.read_text() == "original"
    assert not (tmp_path / "out.txt.tmp").exists()
    monkeypatch.setattr(os, "replace", real_replace)


def test_atomic_write_json_roundtrip(tmp_path):
    path = tmp_path / "data.json"
    atomic_write_json(path, {"a": 1, "b": [2, 3]})
    import json
    assert json.loads(path.read_text()) == {"a": 1, "b": [2, 3]}


def test_atomic_write_creates_parents(tmp_path):
    path = tmp_path / "a" / "b" / "c.txt"
    atomic_write(path, "x")
    assert path.read_text() == "x"
