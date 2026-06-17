"""`autodev quota-resume` guards: still-paused, time, fingerprint, recovery."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import autodev.quota_pause as qp
import autodev.vendors.quota as quota_pkg
from autodev.cli import cmd_quota_resume
from autodev.vendors.quota.base import QuotaResult, now_utc

DIAG = [
    {"vendor": "claude", "model": "m", "min_quota_pct": 20,
     "remaining_pct": 5, "resets_at": None, "error": None}
]


def _args(active):
    return SimpleNamespace(feature="demo", repo_root=str(active.parents[3]), vendors_yml=None)


def _stub_quota(monkeypatch, remaining, reset=None):
    monkeypatch.setattr(
        quota_pkg, "get_remaining",
        lambda v, m=None, force=False: QuotaResult(v, remaining, reset, now_utc()),
    )


def test_before_resume_at_is_noop(feature_active, capsys):
    qp.write_quota_pause(feature_active, role="design", resume_at=now_utc() + timedelta(hours=1), diagnostics=DIAG)
    assert cmd_quota_resume(_args(feature_active)) == 0
    assert (feature_active / ".pause").exists()  # stays paused
    assert "not reached yet" in capsys.readouterr().out


def test_repo_changed_refuses(feature_active, capsys):
    qp.write_quota_pause(feature_active, role="design", resume_at=now_utc() - timedelta(hours=1), diagnostics=DIAG)
    (feature_active / "design.md").write_text("touched")  # mutate repo after pause
    assert cmd_quota_resume(_args(feature_active)) == 0
    assert (feature_active / ".pause").exists()  # stays paused
    assert "repo changed" in capsys.readouterr().out


def test_recovered_and_unchanged_resumes(feature_active, monkeypatch, capsys):
    qp.write_quota_pause(feature_active, role="design", resume_at=now_utc() - timedelta(hours=1), diagnostics=DIAG)
    _stub_quota(monkeypatch, 99)
    assert cmd_quota_resume(_args(feature_active)) == 0
    assert not (feature_active / ".pause").exists()  # resumed
    assert not (feature_active / ".quota-pause.json").exists()
    assert "resumed" in capsys.readouterr().out


def test_still_low_reschedules(feature_active, monkeypatch):
    new_reset = now_utc() + timedelta(hours=3)
    qp.write_quota_pause(feature_active, role="design", resume_at=now_utc() - timedelta(hours=1), diagnostics=DIAG)
    fp_before = qp.read_quota_pause(feature_active)["fingerprint"]
    _stub_quota(monkeypatch, 1, new_reset)
    assert cmd_quota_resume(_args(feature_active)) == 0
    assert (feature_active / ".pause").exists()  # still paused
    rec = qp.read_quota_pause(feature_active)
    assert rec["resume_at"][:13] == new_reset.isoformat()[:13]
    assert rec["fingerprint"] == fp_before  # reschedule preserves original fingerprint


def test_manual_resume_cancels_auto_resume(feature_active, capsys):
    qp.write_quota_pause(feature_active, role="design", resume_at=now_utc() - timedelta(hours=1), diagnostics=DIAG)
    (feature_active / ".pause").unlink()  # simulate `autodev resume`
    assert cmd_quota_resume(_args(feature_active)) == 0
    assert not (feature_active / ".quota-pause.json").exists()  # stale record cleaned
    assert "no longer quota-paused" in capsys.readouterr().out


def test_no_record_is_noop(feature_active, capsys):
    assert cmd_quota_resume(_args(feature_active)) == 0
    assert "nothing to do" in capsys.readouterr().out
