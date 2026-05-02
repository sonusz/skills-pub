"""ad-3: build.json schema."""
from __future__ import annotations

import pytest

from auto_dev.artifacts.build import BuildReport, load_build, write_build
from auto_dev.errors import SchemaError


def _mk() -> BuildReport:
    return BuildReport(
        source="scope.json",
        source_hash="sha256:" + "a" * 64,
        written="2026-04-19",
        test_results={"passed": 1, "failed": 0, "skipped": 0},
        lint={"passed": True},
        files_changed=["x.py"],
    )


def test_roundtrip(tmp_path):
    p = tmp_path / "build.json"
    write_build(p, _mk())
    loaded = load_build(p)
    assert loaded.test_results["passed"] == 1
    assert loaded.lint["passed"] is True
    assert loaded.blocking is False


def test_requires_test_result_fields(tmp_path):
    r = _mk()
    r.test_results = {"passed": 1}  # missing failed, skipped
    with pytest.raises(SchemaError):
        write_build(tmp_path / "build.json", r)


def test_requires_lint_passed(tmp_path):
    r = _mk()
    r.lint = {}
    with pytest.raises(SchemaError):
        write_build(tmp_path / "build.json", r)
