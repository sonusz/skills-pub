"""v2-1: state primitives (atomic, lock, hash, cascade, log)."""
from __future__ import annotations

import json
import re

import pytest

from autodev.errors import LockConflict
from autodev.state import (
    HASH_PREFIX, Lock, StalenessCascade,
    atomic_write, atomic_write_json, hash_bytes, hash_file,
    parse_markdown_source_hash, read_owner,
)
from autodev.state.log import JsonlLog


def test_atomic_write_renames_on_success(tmp_path):
    p = tmp_path / "x.txt"
    atomic_write(p, "hello")
    assert p.read_text() == "hello"
    assert not (tmp_path / "x.txt.tmp").exists()


def test_atomic_write_json_roundtrip(tmp_path):
    p = tmp_path / "x.json"
    atomic_write_json(p, {"a": 1})
    assert json.loads(p.read_text()) == {"a": 1}


def test_hash_format_prefix():
    assert re.match(r"^sha256:[0-9a-f]{64}$", hash_bytes(b"x"))


def test_hash_includes_trailing_newline(tmp_path):
    p1, p2 = tmp_path / "a", tmp_path / "b"
    p1.write_bytes(b"x")
    p2.write_bytes(b"x\n")
    assert hash_file(p1) != hash_file(p2)


def test_markdown_source_hash_parse(tmp_path):
    p = tmp_path / "m.md"
    h = "sha256:" + "a" * 64
    p.write_text(f"<!-- source: foo -->\n<!-- source_hash: {h} -->\n<!-- written: now -->\n\n")
    assert parse_markdown_source_hash(p) == h


def test_lock_exclusive(tmp_path):
    l1 = Lock(tmp_path, session_id="s1", verb="run")
    l1.acquire()
    assert (tmp_path / ".lock").is_dir()
    assert read_owner(tmp_path)["session_id"] == "s1"
    l2 = Lock(tmp_path, session_id="s2", verb="run")
    with pytest.raises(LockConflict):
        l2.acquire()
    l1.release()
    assert not (tmp_path / ".lock").exists()


def test_lock_context_manager_releases(tmp_path):
    with Lock(tmp_path, session_id="s", verb="run"):
        assert (tmp_path / ".lock").exists()
    assert not (tmp_path / ".lock").exists()


def test_lock_released_on_exception(tmp_path):
    with pytest.raises(RuntimeError):
        with Lock(tmp_path, session_id="s", verb="run"):
            raise RuntimeError("boom")
    assert not (tmp_path / ".lock").exists()


def test_jsonl_log_emit_and_tail(tmp_path):
    log = JsonlLog(tmp_path / "log.jsonl")
    for i in range(5):
        log.emit(stage="x", event=f"e{i}", feature="foo", detail={"n": i})
    events = log.tail(3)
    assert [e["event"] for e in events] == ["e2", "e3", "e4"]
    assert events[0]["schema"] == 2  # v2 bumped


def test_cascade_prd_only_is_fresh(feature_active):
    (feature_active / "prd.md").write_text("# PRD\n")
    c = StalenessCascade(feature_active)
    fresh = c.fresh()
    assert fresh["prd"] is True
    assert fresh["design"] is False
    assert fresh["scope"] is False
    assert c.next_stage() == "design"


def test_cascade_prd_change_invalidates_scope(feature_active):
    from autodev.artifacts.scope import Scope, ScopeItem, write_scope
    (feature_active / "prd.md").write_text("# v1\n")
    prd_h = hash_file(feature_active / "prd.md")
    # panel-design-review verdict
    from autodev.artifacts.verdict import PanelVerdict, write_verdict
    (feature_active / "design.md").write_text(
        f"<!-- source: {feature_active / 'prd.md'} -->\n"
        f"<!-- source_hash: {prd_h} -->\n"
        "<!-- written: 2026-04-20 -->\n\n## 1. Context\nx\n",
        encoding="utf-8",
    )
    v = PanelVerdict(
        gate="design-review", verdict="pass", findings=[],
        source=str(feature_active / "design.md"), source_hash=hash_file(feature_active / "design.md"),
        prompt_file="x", prompt_hash="sha256:" + "0" * 64,
        harness_version="t", run_ts="2026-04-20T00:00:00Z",
    )
    write_verdict(feature_active / "panel-design-review.json", v)
    write_scope(feature_active / "scope.json", Scope(
        source=str(feature_active / "prd.md"), source_hash=prd_h,
        written="2026-04-20", feature="demo", mode="fresh", diff_base="main",
        in_scope=[ScopeItem(id="s-1", description="x", prd_ref=["§1"], design_ref=["§1"])],
    ))
    c = StalenessCascade(feature_active)
    assert c.fresh()["scope"] is True
    # Mutate PRD
    (feature_active / "prd.md").write_text("# v2\n")
    c2 = StalenessCascade(feature_active)
    assert c2.fresh()["design"] is False
    assert c2.fresh()["scope"] is False
    assert c2.fresh()["panel_design_review"] is False
