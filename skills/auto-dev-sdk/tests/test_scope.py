"""ad-3: scope.json read/write + schema."""
from __future__ import annotations

import pytest

from auto_dev.artifacts.scope import Scope, ScopeItem, ExcludedItem, load_scope, write_scope
from auto_dev.errors import SchemaError


def _mk_scope(**overrides) -> Scope:
    base = Scope(
        source="prd.md",
        source_hash="sha256:" + "a" * 64,
        written="2026-04-19",
        feature="foo",
        mode="fresh",
        diff_base="main",
        in_scope=[ScopeItem(id="foo-1", description="x", prd_ref="§1", status="active")],
        excluded=[ExcludedItem(id="foo-x1", description="y", reason="z")],
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def test_roundtrip(tmp_path):
    p = tmp_path / "scope.json"
    write_scope(p, _mk_scope())
    loaded = load_scope(p)
    assert loaded.feature == "foo"
    assert loaded.in_scope[0].id == "foo-1"
    assert loaded.excluded[0].id == "foo-x1"


def test_active_items_filter(tmp_path):
    s = _mk_scope(in_scope=[
        ScopeItem(id="a", description="", prd_ref="§1", status="active"),
        ScopeItem(id="b", description="", prd_ref="§1", status="removed"),
        ScopeItem(id="c", description="", prd_ref="§1", status="superseded", superseded_by="a"),
    ])
    p = tmp_path / "scope.json"
    write_scope(p, s)
    loaded = load_scope(p)
    assert [i.id for i in loaded.active_items()] == ["a"]


def test_rejects_missing_source_hash(tmp_path):
    s = _mk_scope(source_hash="")
    with pytest.raises(SchemaError):
        write_scope(tmp_path / "scope.json", s)


def test_rejects_invalid_status(tmp_path):
    s = _mk_scope(in_scope=[ScopeItem(id="a", description="", prd_ref="§1", status="bogus")])  # type: ignore
    with pytest.raises(SchemaError):
        write_scope(tmp_path / "scope.json", s)


def test_superseded_requires_ref(tmp_path):
    s = _mk_scope(in_scope=[ScopeItem(id="a", description="", prd_ref="§1", status="superseded")])
    with pytest.raises(SchemaError):
        write_scope(tmp_path / "scope.json", s)


def test_duplicate_ids_rejected(tmp_path):
    s = _mk_scope(in_scope=[
        ScopeItem(id="a", description="", prd_ref="§1", status="active"),
        ScopeItem(id="a", description="", prd_ref="§1", status="active"),
    ])
    with pytest.raises(SchemaError):
        write_scope(tmp_path / "scope.json", s)
