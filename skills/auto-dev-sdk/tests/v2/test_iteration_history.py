"""Tests for iteration_history.build_iteration_history + render."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodev.iteration_history import (
    HistoryEntry,
    build_iteration_history,
    render_iteration_history,
)


def _emit(active: Path, ts: str, stage: str, event: str, detail: dict | None = None) -> None:
    """Append a synthetic log.jsonl line."""
    rec = {
        "ts": ts, "schema": 2, "stage": stage, "event": event,
        "feature": "demo", "detail": detail or {},
    }
    with (active / "log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


@pytest.fixture
def active(tmp_path: Path) -> Path:
    a = tmp_path / "active"
    a.mkdir()
    return a


def test_no_log_returns_empty(active):
    assert build_iteration_history(active) == []


def test_filters_to_relevant_events(active):
    _emit(active, "2026-04-29T06:00:00Z", "design", "subprocess-dispatch")
    _emit(active, "2026-04-29T06:00:01Z", "design", "subprocess-start")
    _emit(active, "2026-04-29T06:01:55Z", "design", "subprocess-end", {"ok": True})
    _emit(active, "2026-04-29T06:01:55Z", "design", "stage-complete")
    history = build_iteration_history(active)
    # Only stage-complete is in the relevant set; the dispatch/start/end
    # rows are progress noise and must be filtered out.
    assert len(history) == 1
    assert history[0].label == "design stage-complete"


def test_orders_oldest_first(active):
    _emit(active, "2026-04-29T07:00:00Z", "gate", "panel-done",
          {"gate": "design-review", "verdict": "pass"})
    _emit(active, "2026-04-29T06:00:00Z", "design", "stage-complete")
    history = build_iteration_history(active)
    assert [e.ts for e in history] == [
        "2026-04-29T06:00:00Z",
        "2026-04-29T07:00:00Z",
    ]


def test_amendment_label(active):
    _emit(active, "2026-04-29T11:30:00Z", "orchestrator", "prd-amended",
          {"summary": "removed R4; changed R2, R7; +12/-8 lines"})
    history = build_iteration_history(active)
    assert len(history) == 1
    assert "UPDATED" in history[0].label
    assert "removed R4" in history[0].label
    assert history[0].artifact == "prd.md"


def test_amendment_label_legacy_format(active):
    # Log rows written before this change carry only
    # amendment_first_line, with no summary field.
    _emit(active, "2026-04-29T11:30:00Z", "orchestrator", "prd-amended",
          {"amendment_first_line": "R8 must auto-trigger handoff"})
    history = build_iteration_history(active)
    assert len(history) == 1
    assert "AMENDED" in history[0].label
    assert history[0].artifact == "prd.md"


def test_panel_done_label_includes_verdict(active):
    _emit(active, "2026-04-29T07:00:00Z", "gate", "panel-done",
          {"gate": "close-approval", "verdict": "needs_revision"})
    history = build_iteration_history(active)
    assert "panel-close-approval" in history[0].label
    assert "needs_revision" in history[0].label


def test_revision_loop_triggered_label(active):
    _emit(active, "2026-04-29T07:00:00Z", "gate", "revision-loop-triggered",
          {"gate": "design-review", "would_rerun": "design"})
    history = build_iteration_history(active)
    assert "re-dispatch design" in history[0].label


def test_attaches_current_hash_when_artifact_exists(active):
    (active / "design.md").write_text("# Design\nbody\n")
    _emit(active, "2026-04-29T06:00:00Z", "design", "stage-complete")
    history = build_iteration_history(active)
    assert history[0].current_hash is not None
    assert history[0].current_hash.startswith("sha256:")
    assert history[0].artifact == "design.md"


def test_artifact_missing_yields_no_hash(active):
    _emit(active, "2026-04-29T06:00:00Z", "design", "stage-complete")
    # design.md never written
    history = build_iteration_history(active)
    assert history[0].artifact == "design.md"
    assert history[0].current_hash is None


def test_render_empty_returns_empty_string():
    assert render_iteration_history([]) == ""


def test_render_includes_table_headers_and_rows():
    entries = [
        HistoryEntry(ts="2026-04-29T06:00:00Z", label="design stage-complete",
                     artifact="design.md",
                     current_hash="sha256:" + "a" * 64),
        HistoryEntry(ts="2026-04-29T11:30:00Z",
                     label="prd.md UPDATED",
                     artifact="prd.md",
                     current_hash="sha256:" + "b" * 64),
    ]
    rendered = render_iteration_history(entries)
    assert "Time (UTC)" in rendered
    assert "Event" in rendered
    assert "Current hash" in rendered
    assert "design stage-complete" in rendered
    assert "UPDATED" in rendered
    # Both entries appear
    assert rendered.count("|") >= 12  # 2 header rows + 2 data rows × 6 cells


def test_skips_malformed_jsonl_lines(active):
    (active / "log.jsonl").write_text(
        "this is not json\n"
        + json.dumps({"ts": "2026-04-29T06:00:00Z", "stage": "design",
                      "event": "stage-complete", "detail": {}}) + "\n"
        + "{partial json\n"
    )
    history = build_iteration_history(active)
    assert len(history) == 1
