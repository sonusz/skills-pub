"""Budget accounting: metering from log.jsonl, prompt lines, police rotation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodev.budget import (
    BUDGET_TARGETS_FILENAME,
    POLICE_STATE_FILENAME,
    compute_spent,
    format_budget_lines,
    load_targets,
    police_banner,
    select_budget_police,
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


def _pick(feature_active, vendors, *, round_key, unavailable=None):
    """Unwrap PoliceSelection to its vendor for rotation-order tests."""
    sel = select_budget_police(
        feature_active, vendors, round_key=round_key, unavailable=unavailable,
    )
    return sel.vendor if sel is not None else None


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


class TestPoliceRotation:
    VENDORS = ["claude", "codex", "agy", "grok"]

    def test_same_round_key_is_stable(self, feature_active: Path) -> None:
        first = _pick(
            feature_active, self.VENDORS, round_key="r1",
        )
        again = _pick(
            feature_active, self.VENDORS, round_key="r1",
        )
        assert first == again == "claude"

    def test_new_round_advances_in_fixed_order(
        self, feature_active: Path,
    ) -> None:
        picks = [
            _pick(feature_active, self.VENDORS, round_key=k)
            for k in ("r1", "r2", "r3", "r4", "r5")
        ]
        assert picks == ["claude", "codex", "agy", "grok", "claude"]

    def test_unavailable_vendor_is_passed_over(
        self, feature_active: Path,
    ) -> None:
        first = _pick(
            feature_active, self.VENDORS, round_key="r1",
            unavailable={"claude", "codex"},
        )
        assert first == "agy"
        # Pointer advanced past the selected vendor, not past the skipped.
        second = _pick(
            feature_active, self.VENDORS, round_key="r2",
        )
        assert second == "grok"

    def test_all_unavailable_selects_none_and_persists_nothing(
        self, feature_active: Path,
    ) -> None:
        pick = _pick(
            feature_active, self.VENDORS, round_key="r1",
            unavailable=set(self.VENDORS),
        )
        assert pick is None
        assert not (feature_active / POLICE_STATE_FILENAME).exists()

    def test_corrupt_state_recovers(self, feature_active: Path) -> None:
        (feature_active / POLICE_STATE_FILENAME).write_text(
            "{broken", encoding="utf-8",
        )
        assert _pick(
            feature_active, self.VENDORS, round_key="r1",
        ) == "claude"

    def test_resume_pins_vendor_even_when_now_unavailable(
        self, feature_active: Path,
    ) -> None:
        # Production sequence: select on empty unavailable, the vendor
        # quota-skips, the round resumes with it marked unavailable. The
        # round pin must win — reassignment mid-round would double-audit.
        first = _pick(
            feature_active, self.VENDORS, round_key="r1",
        )
        assert first == "claude"
        resumed = _pick(
            feature_active, self.VENDORS, round_key="r1",
            unavailable={"claude"},
        )
        assert resumed == "claude"
        # The pin must not have consumed a second rotation slot.
        state = json.loads(
            (feature_active / POLICE_STATE_FILENAME).read_text(encoding="utf-8"),
        )
        assert len(state["history"]) == 1

    def test_vendor_removed_mid_round_consumes_no_second_slot(
        self, feature_active: Path,
    ) -> None:
        assert _pick(
            feature_active, self.VENDORS, round_key="r1",
        ) == "claude"
        # claude removed from vendors.yml; same round resumes: no police,
        # no second slot charged to this round.
        shrunk = ["codex", "agy", "grok"]
        assert _pick(
            feature_active, shrunk, round_key="r1",
        ) is None
        state = json.loads(
            (feature_active / POLICE_STATE_FILENAME).read_text(encoding="utf-8"),
        )
        assert len(state["history"]) == 1
        # The next round proceeds normally from the stored pointer.
        assert _pick(
            feature_active, self.VENDORS, round_key="r2",
        ) == "codex"


class TestBudgetPoliceSuffix:
    """Runner-side seat resolution: pin precedence, eligibility, audit."""

    @staticmethod
    def _config() -> "PanelConfig":
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
    def _settled(vendor: str) -> "ReviewerResult":
        from autodev.panel.runner import ReviewerResult
        return ReviewerResult(
            vendor=vendor, model="fake", ok=True, output="reviewed",
            elapsed_sec=1.0,
        )

    def _resolve(self, feature_active: Path, **kwargs):
        from autodev.panel.runner import _budget_police_suffix
        defaults = dict(
            feature_active=feature_active,
            panel_config=self._config(),
            reviewer_results=[],
            cache_extras={},
            round_key="rk-1",
            gate_label="design-review",
            log_emit=None,
        )
        defaults.update(kwargs)
        return _budget_police_suffix(**defaults)

    def test_fresh_round_banners_first_vendor_and_records_pin(
        self, feature_active: Path,
    ) -> None:
        events: list[dict] = []
        suffix, record = self._resolve(
            feature_active, log_emit=events.append,
        )
        assert suffix is not None and set(suffix) == {"claude"}
        assert "BUDGET POLICE" in suffix["claude"]
        assert record is not None
        assert record["vendor"] == "claude"
        assert record["round_key"] == "rk-1"
        # The banner is frozen into the pin so resumed sessions see the
        # same account as the original dispatch.
        assert record["banner"] == suffix["claude"]
        assert [(e["event"], e["source"]) for e in events] == [
            ("budget-police-selected", "rotation"),
        ]

    def test_cache_pin_wins_over_rotation_state(
        self, feature_active: Path,
    ) -> None:
        # budget-police.json lost mid-round; the reviewer-cache pin must
        # keep the role with the original vendor AND reuse its frozen
        # banner instead of regenerating a drifted account.
        events: list[dict] = []
        suffix, record = self._resolve(
            feature_active,
            cache_extras={
                "budget_police": {
                    "vendor": "grok", "round_key": "rk-1",
                    "banner": "FROZEN-BANNER",
                },
            },
            log_emit=events.append,
        )
        assert suffix == {"grok": "FROZEN-BANNER"}
        assert record is not None and record["vendor"] == "grok"
        assert record["banner"] == "FROZEN-BANNER"
        assert [(e["event"], e["source"]) for e in events] == [
            ("budget-police-selected", "cache-pin"),
        ]

    def test_pinned_settled_vendor_gets_no_second_banner(
        self, feature_active: Path,
    ) -> None:
        events: list[dict] = []
        suffix, record = self._resolve(
            feature_active,
            reviewer_results=[self._settled("grok")],
            cache_extras={
                "budget_police": {"vendor": "grok", "round_key": "rk-1"},
            },
            log_emit=events.append,
        )
        assert suffix is None
        assert record is not None and record["vendor"] == "grok"
        assert events == []

    def test_pinned_quota_skipped_vendor_is_not_rebannered(
        self, feature_active: Path,
    ) -> None:
        # The pinned vendor positively quota-skipped this round: its slot
        # is retried (so it is in to_run), but the seat stays spent — the
        # banner must not be re-attached, and the role must not move.
        events: list[dict] = []
        suffix, record = self._resolve(
            feature_active,
            cache_extras={
                "budget_police": {"vendor": "grok", "round_key": "rk-1"},
                "quota_skipped": {"grok": {"vendor": "grok"}},
            },
            log_emit=events.append,
        )
        assert suffix is None
        assert record is not None and record["vendor"] == "grok"
        assert events == []
        # No second rotation slot may have been charged.
        assert not (feature_active / POLICE_STATE_FILENAME).exists()

    def test_settled_and_quota_skipped_vendors_passed_over(
        self, feature_active: Path,
    ) -> None:
        suffix, record = self._resolve(
            feature_active,
            reviewer_results=[self._settled("claude")],
            cache_extras={"quota_skipped": {"codex": {"vendor": "codex"}}},
        )
        assert suffix is not None and set(suffix) == {"grok"}
        assert record is not None and record["vendor"] == "grok"

    def test_all_slots_settled_selects_nobody(
        self, feature_active: Path,
    ) -> None:
        events: list[dict] = []
        suffix, record = self._resolve(
            feature_active,
            reviewer_results=[
                self._settled("claude"), self._settled("codex"),
                self._settled("grok"),
            ],
            log_emit=events.append,
        )
        assert suffix is None
        assert record is None
        assert events == []
        assert not (feature_active / POLICE_STATE_FILENAME).exists()

    def test_state_pin_survives_cache_loss_with_frozen_banner(
        self, feature_active: Path,
    ) -> None:
        # Taint recovery deletes the reviewer cache (and its pin) but
        # leaves budget-police.json. The resumed round must reuse the
        # banner frozen in the rotation state — not regenerate a drifted
        # one — and audit the re-attach as state-pin, not rotation.
        first_suffix, first_record = self._resolve(feature_active)
        assert first_suffix is not None and set(first_suffix) == {"claude"}
        frozen = first_record["banner"]
        # Log grows between attempts; a regenerated banner would differ.
        _write_log(feature_active, [
            _row("2026-08-06T00:00:00+00:00", "subprocess-end", "build",
                 {"elapsed_sec": 3600.0}),
        ])
        events: list[dict] = []
        suffix, record = self._resolve(
            feature_active, cache_extras={}, log_emit=events.append,
        )
        assert suffix == {"claude": frozen}
        assert record["banner"] == frozen
        assert [(e["event"], e["source"]) for e in events] == [
            ("budget-police-selected", "state-pin"),
        ]

    def test_prior_round_quota_outage_is_passed_over(
        self, feature_active: Path,
    ) -> None:
        # A fresh round has no matching cache, so the previous round's
        # raw cache is the only cross-round outage signal.
        (feature_active / "panel-design-review.reviewers.json").write_text(
            json.dumps({
                "gate": "stale-round",
                "quota_skipped": {"claude": {"vendor": "claude"}},
            }),
            encoding="utf-8",
        )
        suffix, record = self._resolve(feature_active)
        assert suffix is not None and set(suffix) == {"codex"}
        assert record is not None and record["vendor"] == "codex"

    def test_state_write_registry_records_round(
        self, feature_active: Path,
    ) -> None:
        from autodev.budget import police_state_write_round
        assert police_state_write_round(feature_active) is None
        self._resolve(feature_active)
        assert police_state_write_round(feature_active) == "rk-1"

    def test_log_emit_failure_does_not_lose_banner(
        self, feature_active: Path,
    ) -> None:
        def _boom(_: dict) -> None:
            raise OSError("log append failed")

        suffix, record = self._resolve(feature_active, log_emit=_boom)
        assert suffix is not None and set(suffix) == {"claude"}
        assert record is not None and record["vendor"] == "claude"
        assert record["round_key"] == "rk-1"


class TestPoliceBanner:
    def test_banner_contains_duty_and_account(
        self, feature_active: Path,
    ) -> None:
        _write_log(feature_active, [
            _row("2026-08-06T00:00:00+00:00", "subprocess-end", "build",
                 {"elapsed_sec": 3600.0}),
        ])
        banner = police_banner(feature_active)
        assert "BUDGET POLICE" in banner
        assert "[budget]" in banner
        assert "BUDGET_SPENT" in banner
        assert "Zero `[budget]` findings is a valid" in banner
