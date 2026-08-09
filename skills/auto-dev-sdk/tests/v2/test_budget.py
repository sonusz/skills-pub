"""Budget accounting: metering from log.jsonl, prompt lines, shrink rounds."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodev.budget import (
    BUDGET_TARGETS_FILENAME,
    compute_spent,
    format_budget_lines,
    load_targets,
    minimality_review_body,
)


def _write_log(feature_active: Path, rows: list[dict]) -> None:
    lines = [json.dumps(r) for r in rows]
    (feature_active / "log.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8",
    )


def _row(ts: str, event: str, stage: str = "build", detail: dict | None = None) -> dict:
    return {
        "ts": ts, "schema": 2, "stage": stage, "event": event,
        "feature": "f", "detail": detail or {},
    }


@pytest.fixture()
def feature_active(tmp_path: Path) -> Path:
    active = tmp_path / "active"
    active.mkdir()
    return active


class TestComputeSpent:
    def test_missing_log_is_zero(self, feature_active: Path) -> None:
        spent = compute_spent(feature_active)
        assert spent.vendor_hours_total == 0
        assert spent.iterations == 0
        assert spent.design_review_rounds == 0
        assert spent.wall_hours == 0

    def test_aggregates_stages_iterations_rounds_and_span(
        self, feature_active: Path,
    ) -> None:
        _write_log(feature_active, [
            _row("2026-08-06T00:00:00+00:00", "subprocess-end", "build",
                 {"elapsed_sec": 3600.0}),
            _row("2026-08-06T01:00:00+00:00", "subprocess-end", "build",
                 {"elapsed_sec": 1800.0}),
            _row("2026-08-06T02:00:00+00:00", "subprocess-end", "design",
                 {"elapsed_sec": 900.0}),
            _row("2026-08-06T03:00:00+00:00", "iteration-recorded", "ralph-review",
                 {"iter": 1}),
            _row("2026-08-06T04:00:00+00:00", "iteration-recorded", "ralph-review",
                 {"iter": 2}),
            _row("2026-08-06T05:00:00+00:00", "panel-review-round", "gate",
                 {"gate": "design-review", "round_key": "sha256:aa"}),
            _row("2026-08-06T05:10:00+00:00", "panel-vendor-elapsed", "gate",
                 {"gate": "design-review", "role": "reviewer",
                  "vendor": "codex", "elapsed_sec": 720.0}),
            _row("2026-08-06T05:20:00+00:00", "panel-vendor-elapsed", "gate",
                 {"role": "synthesizer", "vendor": "codex",
                  "elapsed_sec": 180.0}),
            _row("2026-08-06T05:30:00+00:00", "panel-review-round", "gate",
                 {"gate": "trace-review", "round_key": "sha256:aa"}),
            _row("2026-08-06T06:00:00+00:00", "stage-complete", "build"),
        ])
        spent = compute_spent(feature_active)
        assert spent.vendor_hours_by_stage["build"] == pytest.approx(1.5)
        assert spent.vendor_hours_by_stage["design"] == pytest.approx(0.25)
        assert spent.vendor_hours_by_stage["panel"] == pytest.approx(0.25)
        assert spent.vendor_hours_total == pytest.approx(2.0)
        assert spent.iterations == 2
        # trace-review rounds must not count as design-review rounds.
        assert spent.design_review_rounds == 1
        assert spent.wall_hours == pytest.approx(6.0)

    def test_rounds_dedup_on_round_key_and_ignore_other_events(
        self, feature_active: Path,
    ) -> None:
        # Resumed attempts of one logical round share the round_key and
        # count once; panel-start (fires on precheck refusals too) and
        # panel-quorum (fires only on quota skips) are both ignored.
        _write_log(feature_active, [
            _row("2026-08-06T00:00:00+00:00", "panel-review-round", "gate",
                 {"gate": "design-review", "round_key": "sha256:aa"}),
            _row("2026-08-06T01:00:00+00:00", "panel-review-round", "gate",
                 {"gate": "design-review", "round_key": "sha256:aa"}),
            _row("2026-08-06T02:00:00+00:00", "panel-review-round", "gate",
                 {"gate": "design-review", "round_key": "sha256:bb"}),
            _row("2026-08-06T03:00:00+00:00", "panel-start", "gate",
                 {"gate": "design-review"}),
            _row("2026-08-06T04:00:00+00:00", "panel-quorum", "gate",
                 {"gate": "design-review"}),
        ])
        assert compute_spent(feature_active).design_review_rounds == 2

    def test_out_of_order_timestamps_still_span_min_to_max(
        self, feature_active: Path,
    ) -> None:
        _write_log(feature_active, [
            _row("2026-08-06T05:00:00+00:00", "iteration-recorded"),
            _row("2026-08-06T01:00:00+00:00", "iteration-recorded"),
            _row("2026-08-06T03:00:00+00:00", "iteration-recorded"),
        ])
        assert compute_spent(feature_active).wall_hours == pytest.approx(4.0)

    def test_mixed_naive_and_aware_timestamps_survive(
        self, feature_active: Path,
    ) -> None:
        _write_log(feature_active, [
            _row("2026-08-06T00:00:00+00:00", "subprocess-end", "build",
                 {"elapsed_sec": 3600.0}),
            _row("2026-08-06T02:00:00", "iteration-recorded"),
            _row("2026-08-06T03:00:00+00:00", "stage-complete", "build"),
        ])
        spent = compute_spent(feature_active)
        assert spent.vendor_hours_total == pytest.approx(1.0)
        assert spent.wall_hours == pytest.approx(3.0)
        assert format_budget_lines(feature_active) != []

    def test_malformed_lines_are_skipped(self, feature_active: Path) -> None:
        (feature_active / "log.jsonl").write_text(
            'not-json\n[]\n'
            + json.dumps(_row("2026-08-06T00:00:00+00:00", "subprocess-end",
                              "build", {"elapsed_sec": 3600.0}))
            + "\n",
            encoding="utf-8",
        )
        spent = compute_spent(feature_active)
        assert spent.vendor_hours_total == pytest.approx(1.0)


class TestTargetsAndLines:
    def test_load_targets_filters_unknown_and_invalid(
        self, feature_active: Path,
    ) -> None:
        (feature_active / BUDGET_TARGETS_FILENAME).write_text(json.dumps({
            "targets": {
                "vendor_hours": 24, "iterations": -3,
                "made_up": 9, "wall_hours": "soon",
            },
        }), encoding="utf-8")
        assert load_targets(feature_active) == {"vendor_hours": 24.0}

    def test_no_spend_yields_no_lines(self, feature_active: Path) -> None:
        assert format_budget_lines(feature_active) == []

    def test_lines_flag_over_target(self, feature_active: Path) -> None:
        _write_log(feature_active, [
            _row("2026-08-06T00:00:00+00:00", "subprocess-end", "build",
                 {"elapsed_sec": 7200.0}),
            _row("2026-08-06T01:00:00+00:00", "iteration-recorded"),
        ])
        (feature_active / BUDGET_TARGETS_FILENAME).write_text(json.dumps({
            "targets": {"vendor_hours": 1},
        }), encoding="utf-8")
        lines = format_budget_lines(feature_active)
        joined = "\n".join(lines)
        assert "BUDGET_SPENT" in joined
        assert "**OVER**" in joined
        assert "BUDGET_RULE" in joined

    def test_lines_mention_missing_targets(self, feature_active: Path) -> None:
        _write_log(feature_active, [
            _row("2026-08-06T00:00:00+00:00", "iteration-recorded"),
        ])
        joined = "\n".join(format_budget_lines(feature_active))
        assert "BUDGET_TARGETS: none set" in joined

    def test_rounds_only_spend_is_not_reported_as_nothing(
        self, feature_active: Path,
    ) -> None:
        # Rounds consumed with zero vendor-hours (quota-halted dispatches)
        # must surface the rounds-vs-target comparison, not the
        # nothing-recorded branch.
        _write_log(feature_active, [
            _row("2026-08-06T00:00:00+00:00", "panel-review-round", "gate",
                 {"gate": "design-review", "round_key": "sha256:aa"}),
        ])
        (feature_active / BUDGET_TARGETS_FILENAME).write_text(json.dumps({
            "targets": {"design_review_rounds": 1},
        }), encoding="utf-8")
        joined = "\n".join(format_budget_lines(feature_active))
        assert "nothing recorded" not in joined
        assert "design-review rounds" in joined

    def test_targets_shown_before_any_spend(self, feature_active: Path) -> None:
        # The initial design prompt renders before any spend is recorded;
        # operator ceilings must still reach it.
        (feature_active / BUDGET_TARGETS_FILENAME).write_text(json.dumps({
            "targets": {"vendor_hours": 24, "iterations": 30},
        }), encoding="utf-8")
        joined = "\n".join(format_budget_lines(feature_active))
        assert "nothing recorded yet" in joined
        assert "iterations 30" in joined
        assert "vendor_hours 24" in joined
        assert "BUDGET_RULE" in joined


class TestShrinkRound:
    """Cadence: three coverage rounds, then two whole-panel shrink rounds."""

    @staticmethod
    def _config():
        from autodev.vendors.config import (
            PanelConfig, PanelReviewerSpec, PanelSynthesizerSpec,
        )
        return PanelConfig(
            reviewers=(
                PanelReviewerSpec(vendor="claude", model="fake"),
                PanelReviewerSpec(vendor="codex", model="fake"),
                PanelReviewerSpec(vendor="grok", model="fake"),
            ),
            synthesizer=PanelSynthesizerSpec(vendor="codex", model="fake"),
        )

    @staticmethod
    def _log_prior_rounds(feature_active: Path, keys: list[str]) -> None:
        _write_log(feature_active, [
            _row(f"2026-08-06T0{i}:00:00+00:00", "panel-review-round", "gate",
                 {"gate": "design-review", "round_key": key})
            for i, key in enumerate(keys)
        ])

    def _resolve(self, feature_active: Path, *, round_key="rk-cur", **kwargs):
        from autodev.panel.runner import _shrink_round_prompts
        defaults = dict(
            feature_active=feature_active,
            panel_config=self._config(),
            round_key=round_key,
            base_prompt=(
                "COVERAGE BODY\n\n---\n\n## Orchestrator context\n\n- X"
            ),
            base_resume_prompt="CONTINUATION PREAMBLE",
            gate_label="design-review",
            log_emit=None,
        )
        defaults.update(kwargs)
        return _shrink_round_prompts(**defaults)

    def test_first_three_rounds_are_coverage(self, feature_active: Path) -> None:
        for n in (0, 1, 2):
            self._log_prior_rounds(feature_active, [f"rk-{i}" for i in range(n)])
            assert self._resolve(feature_active) is None

    def test_rounds_four_and_five_are_shrink(self, feature_active: Path) -> None:
        for n in (3, 4):
            self._log_prior_rounds(feature_active, [f"rk-{i}" for i in range(n)])
            prompts = self._resolve(feature_active)
            assert prompts is not None
            assert set(prompts) == {"claude", "codex", "grok"}
            initial, resume = prompts["claude"]
            # Coverage body replaced; shared context kept.
            assert "minimality review" in initial
            assert "COVERAGE BODY" not in initial
            assert "## Orchestrator context" in initial
            # Resume keeps the role-agnostic preamble plus the body.
            assert resume.startswith("CONTINUATION PREAMBLE")
            assert "minimality review" in resume

    def test_cycle_repeats(self, feature_active: Path) -> None:
        self._log_prior_rounds(feature_active, [f"rk-{i}" for i in range(5)])
        assert self._resolve(feature_active) is None
        self._log_prior_rounds(feature_active, [f"rk-{i}" for i in range(8)])
        assert self._resolve(feature_active) is not None

    def test_resume_of_same_round_is_stable(self, feature_active: Path) -> None:
        # The current round's own event may already be in the log from a
        # prior attempt; the ordinal must not shift.
        self._log_prior_rounds(
            feature_active, ["rk-0", "rk-1", "rk-2", "rk-cur"],
        )
        assert self._resolve(feature_active, round_key="rk-cur") is not None
        self._log_prior_rounds(feature_active, ["rk-0", "rk-1", "rk-cur"])
        assert self._resolve(feature_active, round_key="rk-cur") is None

    def test_shrink_round_emits_audit_event(self, feature_active: Path) -> None:
        events: list[dict] = []
        self._log_prior_rounds(feature_active, ["rk-0", "rk-1", "rk-2"])
        self._resolve(feature_active, log_emit=events.append)
        assert [(e["event"], e["ordinal"]) for e in events] == [
            ("shrink-round", 3),
        ]
        events.clear()
        self._log_prior_rounds(feature_active, ["rk-0"])
        self._resolve(feature_active, log_emit=events.append)
        assert events == []


class TestMinimalityBody:
    def test_banner_contains_duty_and_account(
        self, feature_active: Path,
    ) -> None:
        _write_log(feature_active, [
            _row("2026-08-06T00:00:00+00:00", "subprocess-end", "build",
                 {"elapsed_sec": 3600.0}),
        ])
        body = minimality_review_body(feature_active)
        assert "Shrink round: minimality review" in body
        assert "[budget]" in body
        assert "BUDGET_SPENT" in body
        assert "Zero findings is then the correct report" in body
        # Derived-requirements audit: amendment-only prd_refs are the
        # prime suspects, and rolling a clause back is the operator's call.
        assert "cites only amendment text" in body
        assert "operator's call" in body
        assert "in addition to" not in body
