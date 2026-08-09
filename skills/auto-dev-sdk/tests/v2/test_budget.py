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

    def test_coverage_and_budget_on_one_packet_are_two_rounds(
        self, feature_active: Path,
    ) -> None:
        # The alternation runs both round types on one unchanged packet;
        # the hash alone would under-count design-review spend by 2x.
        _write_log(feature_active, [
            _row("2026-08-06T00:00:00+00:00", "panel-review-round", "gate",
                 {"gate": "design-review", "round_key": "sha256:aa",
                  "round_type": "coverage"}),
            _row("2026-08-06T01:00:00+00:00", "panel-review-round", "gate",
                 {"gate": "design-review", "round_key": "sha256:aa",
                  "round_type": "budget"}),
            _row("2026-08-06T02:00:00+00:00", "panel-review-round", "gate",
                 {"gate": "design-review", "round_key": "sha256:aa",
                  "round_type": "budget"}),
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


def _outcome_row(i: int, key: str, round_type: str, passed: bool) -> dict:
    return _row(
        f"2026-08-06T{i:02d}:00:00+00:00", "design-round-outcome", "gate",
        {"gate": "design-review", "round_key": key,
         "round_type": round_type, "passed": passed},
    )


class TestDesignPhase:
    """Coverage/budget alternation replayed from design-round-outcome events."""

    def _phase(self, feature_active: Path, outcomes, round_key="P"):
        from autodev.budget import design_phase
        _write_log(feature_active, [
            _outcome_row(i, k, t, p) for i, (k, t, p) in enumerate(outcomes)
        ] or [_row("2026-08-06T00:00:00+00:00", "noise")])
        return design_phase(feature_active, round_key)

    def test_empty_log_starts_with_coverage(self, feature_active: Path) -> None:
        assert self._phase(feature_active, []) == "coverage"

    def test_alternates_regardless_of_outcome(self, feature_active: Path) -> None:
        # C fail -> budget next; C fail, B fail -> coverage next.
        assert self._phase(
            feature_active, [("P0", "coverage", False)], round_key="P1",
        ) == "budget"
        assert self._phase(
            feature_active,
            [("P0", "coverage", False), ("P1", "budget", False)],
            round_key="P2",
        ) == "coverage"

    def test_coverage_pass_leads_to_budget_on_same_packet(
        self, feature_active: Path,
    ) -> None:
        assert self._phase(
            feature_active, [("P", "coverage", True)], round_key="P",
        ) == "budget"

    def test_both_passed_on_same_packet_is_complete(
        self, feature_active: Path,
    ) -> None:
        assert self._phase(
            feature_active,
            [("P", "coverage", True), ("P", "budget", True)],
            round_key="P",
        ) == "complete"

    def test_pass_pair_on_different_packets_is_not_complete(
        self, feature_active: Path,
    ) -> None:
        # Budget pass on P1, coverage pass later on P2: P2 still owes budget.
        assert self._phase(
            feature_active,
            [("P1", "coverage", False), ("P2", "budget", True),
             ("P2", "coverage", True)],
            round_key="P2",
        ) == "complete"
        assert self._phase(
            feature_active,
            [("P1", "coverage", False), ("P1", "budget", True),
             ("P2", "coverage", True)],
            round_key="P2",
        ) == "budget"

    def test_recorded_pass_is_never_redispatched(
        self, feature_active: Path,
    ) -> None:
        # Parity says coverage, but coverage already passed this packet:
        # flip to budget instead of re-reviewing a settled type.
        assert self._phase(
            feature_active,
            [("P", "coverage", True), ("P", "budget", False)],
            round_key="P",
        ) == "budget"
        assert self._phase(
            feature_active,
            [("Q", "coverage", False), ("P", "coverage", True)],
            round_key="P",
        ) == "budget"

    def test_gate_satisfied_requires_pair_once_events_exist(
        self, feature_active: Path,
    ) -> None:
        from autodev.budget import design_gate_satisfied
        _write_log(feature_active, [
            _outcome_row(0, "P", "coverage", True),
        ])
        assert design_gate_satisfied(feature_active, "P") is False
        _write_log(feature_active, [
            _outcome_row(0, "P", "coverage", True),
            _outcome_row(1, "P", "budget", True),
        ])
        assert design_gate_satisfied(feature_active, "P") is True

    def test_gate_satisfied_legacy_escape_without_events(
        self, feature_active: Path,
    ) -> None:
        # In-flight features from before the alternation have passing
        # verdicts but no outcome events; they keep single-round
        # semantics instead of being retroactively blocked.
        from autodev.budget import design_gate_satisfied
        assert design_gate_satisfied(feature_active, "P") is True

    def test_duplicate_events_do_not_shift_parity(
        self, feature_active: Path,
    ) -> None:
        # The orchestrator may re-emit an outcome on a cached revisit.
        assert self._phase(
            feature_active,
            [("P", "coverage", True), ("P", "coverage", True)],
            round_key="P",
        ) == "budget"


class TestDesignRoundPlan:
    """Runner-side plan: type selection + whole-panel budget prompts."""

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

    def _plan(self, feature_active: Path, *, round_key="P", **kwargs):
        from autodev.panel.runner import _design_round_plan
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
        return defaults, __import__("autodev.panel.runner", fromlist=["x"])._design_round_plan(**defaults)

    def test_first_round_is_coverage_with_shared_prompts(
        self, feature_active: Path,
    ) -> None:
        _, (round_type, prompts) = self._plan(feature_active)
        assert round_type == "coverage"
        assert prompts is None

    def test_budget_round_overrides_all_vendors(
        self, feature_active: Path,
    ) -> None:
        _write_log(feature_active, [
            _outcome_row(0, "P", "coverage", True),
        ])
        events: list[dict] = []
        _, (round_type, prompts) = self._plan(
            feature_active, log_emit=events.append,
        )
        assert round_type == "budget"
        assert prompts is not None
        assert set(prompts) == {"claude", "codex", "grok"}
        initial, resume = prompts["claude"]
        assert "minimality review" in initial
        assert "COVERAGE BODY" not in initial
        assert "## Orchestrator context" in initial
        assert resume.startswith("CONTINUATION PREAMBLE")
        assert "minimality review" in resume
        assert [(e["event"], e["round_key"]) for e in events] == [
            ("budget-round", "P"),
        ]


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
