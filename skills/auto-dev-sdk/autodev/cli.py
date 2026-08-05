"""autodev CLI — verb dispatcher (R6, R10, v2-14).

Exit codes (v2 R11 / §4): 0=done, 1=error, 2=gate pending, 3=lock conflict.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from autodev import exit_codes
from autodev import overrides_api as ov
from autodev.artifacts.failure import FailureReport, write_failure
from autodev.errors import (
    AutodevError, ConfigError, DirtyWorkspace, GateFailed, GatePending,
    LockConflict, PreflightError, QuotaHalt,
)
from autodev.orchestrator import Orchestrator, OrchestratorConfig
from autodev.paths import FeaturePaths, find_repo_root
from autodev.preflight import preflight_feature, preflight_repo_root
from autodev.state.atomic import atomic_write
from autodev.state.lock import read_owner
from autodev.state.log import JsonlLog
from autodev.vendors.config import load_vendors_config


SDK_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VENDORS_YML = SDK_ROOT / "vendors.yml"
VENDORS_YML_ENV = "AUTODEV_VENDORS_YML"


def _load_vendors(path: Path | None, *, repo_root: Path) -> object | None:
    candidates: list[Path] = []
    if path is not None:
        candidates.append(path)
    else:
        import os

        env_path = os.environ.get(VENDORS_YML_ENV)
        if env_path:
            candidates.append(Path(env_path))
        candidates.append(DEFAULT_VENDORS_YML)
        # Compatibility fallback for older checkouts. The harness-owned
        # default should live with auto-dev-sdk, not in each target repo.
        candidates.append(repo_root / "vendors.yml")

    for candidate in candidates:
        resolved = candidate if candidate.is_absolute() else Path.cwd() / candidate
        if resolved.exists():
            return load_vendors_config(resolved)
    return None


def _repo_root(args) -> Path:
    return Path(args.repo_root) if args.repo_root else find_repo_root(Path.cwd())


def _common(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--repo-root", default=None)
    sp.add_argument("--vendors-yml", default=None)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autodev", description="auto-dev-sdk v2 harness")
    sub = p.add_subparsers(dest="verb", required=True)

    for verb, help_text in [
        ("prd", "Create or import prd.md for a feature"),
        ("status", "Show feature status"),
        ("run", "Run pipeline from current state"),
        ("next", "Advance one stage"),
        ("pause", "Write .pause sentinel"),
        ("resume", "Remove .pause sentinel"),
        ("quota-resume", "Conditionally resume a quota-paused feature (only if still "
                         "paused, time reached, quota recovered, repo unchanged)"),
        ("abort", "Kill running subprocess; write interrupted failure.json"),
        ("retry", "Retry failed current stage"),
        ("invalidate", "Invalidate a stage artifact (rollback)"),
        ("update", "Append PRD amendment; advance cycle"),
        ("close", "Close feature"),
        ("explain", "Human-readable state summary"),
        ("prd-lint", "Stage 0: validate prd.md against PRD schema"),
    ]:
        sp = sub.add_parser(verb, help=help_text)
        sp.add_argument("feature")
        _common(sp)

    # verb-specific extras
    prd = sub._name_parser_map["prd"]
    prd.add_argument("--from-file", default=None)

    st = sub._name_parser_map["status"]
    st.add_argument("--json", action="store_true")

    inv = sub._name_parser_map["invalidate"]
    inv.add_argument("stage", help="stage artifact name to invalidate")

    upd = sub._name_parser_map["update"]
    upd.add_argument("--amendment", required=True)

    cl = sub._name_parser_map["close"]
    cl.add_argument("reason", choices=("complete", "retiring", "deferred", "cancelled"))
    cl.add_argument("--yes", action="store_true")
    cl.add_argument("--note", default="")

    # --watch: emit one-line stdout markers for key transitions so an
    # outer agent / Monitor can react without polling. Sets the
    # AUTODEV_WATCH=1 env var consumed by autodev.state.log.
    for verb_name in ("run", "next"):
        sub._name_parser_map[verb_name].add_argument(
            "--watch", action="store_true",
            help="Emit one-line stdout alerts on key state transitions.",
        )

    # --until: bound `run` to a phase. `run --until design` advances
    # through the whole design phase (including design-review and any
    # in-design revision reruns) and stops cleanly before build, instead
    # of either running end-to-end (plain `run`) or stepping one stage at
    # a time (`next`). Exit code 0 with a `stopped-at-boundary` event.
    sub._name_parser_map["run"].add_argument(
        "--until", dest="until", choices=("design", "build", "spec"), default=None,
        help="Advance only through the named phase, then stop "
             "(design=stop before build, build=stop before spec, "
             "spec=run to completion).",
    )

    # skip-gate
    sg = sub.add_parser("skip-gate", help="Override a mandatory gate with reason")
    sg.add_argument("feature")
    sg.add_argument("gate", choices=("design-review", "close-approval"))
    sg.add_argument("--reason", required=True)
    sg.add_argument("--who", default=None)
    sg.add_argument(
        "--severity", choices=("low", "normal", "high"), default="normal",
        help="G16: low=parser-artifact-type, normal=default, high=counts as 2 toward ceiling",
    )
    _common(sg)

    # escalate (G16: snapshot + demand human review when skip-gate ceiling hit)
    es = sub.add_parser(
        "escalate",
        help="G16: snapshot feature state and flag for human review "
             "(used when skip-gate ceiling is reached).",
    )
    es.add_argument("feature")
    es.add_argument("--who", default=None)
    _common(es)

    # acknowledge-dirty
    ad = sub.add_parser("acknowledge-dirty", help="Acknowledge dirty workspace with reason")
    ad.add_argument("feature")
    ad.add_argument("--reason", required=True)
    ad.add_argument("--who", default=None)
    _common(ad)

    return p


# ---------- command handlers ----------------------------------------


def cmd_prd(args) -> int:
    repo_root = _repo_root(args)
    try:
        preflight_repo_root(repo_root)
    except PreflightError as e:
        print(f"preflight: {e}", file=sys.stderr)
        return exit_codes.ERROR
    fp = FeaturePaths(repo_root=repo_root, feature=args.feature)
    status = fp.current_status() or "planned"
    target = fp.status_dir(status)
    target.mkdir(parents=True, exist_ok=True)
    prd = target / "prd.md"
    if args.from_file:
        src = Path(args.from_file)
        if not src.exists():
            print(f"source not found: {src}", file=sys.stderr)
            return exit_codes.ERROR
        # Stage 0 validation — refuse to import malformed PRDs.
        from autodev.prd_intake import validate_prd_file
        v = validate_prd_file(src)
        if not v.ok:
            print(f"prd intake failed for {src}:", file=sys.stderr)
            print(v.format_errors(), file=sys.stderr)
            return exit_codes.ERROR
        atomic_write(prd, src.read_bytes())
        try:
            from autodev.artifacts.workflow_state import bootstrap_workflow_state

            if (target / "architecture.md").exists():
                bootstrap_workflow_state(target)
        except PreflightError as e:
            print(f"workflow-state bootstrap failed: {e}", file=sys.stderr)
            return exit_codes.ERROR
        except Exception as e:
            print(f"workflow-state bootstrap failed: {e}", file=sys.stderr)
            return exit_codes.ERROR
        print(f"imported {src} → {prd} ({len(v.requirement_markers)} requirements)")
        return exit_codes.OK
    print("interactive PRD intake not bundled; use --from-file")
    return exit_codes.ERROR


def cmd_prd_lint(args) -> int:
    """Stage 0 — check that the feature's prd.md conforms to PRD schema."""
    from autodev.prd_intake import validate_prd_file
    repo_root = _repo_root(args)
    fp = FeaturePaths(repo_root=repo_root, feature=args.feature)
    status = fp.current_status()
    if status is None:
        print(f"{args.feature}: not found", file=sys.stderr)
        return exit_codes.ERROR
    prd = fp.status_dir(status) / "prd.md"
    v = validate_prd_file(prd)
    if v.ok:
        print(f"prd-lint ok: {len(v.requirement_markers)} requirements, "
              f"sections {v.sections_found!r}")
        return exit_codes.OK
    print("prd-lint failed:", file=sys.stderr)
    print(v.format_errors(), file=sys.stderr)
    return exit_codes.ERROR


def cmd_status(args) -> int:
    repo_root = _repo_root(args)
    fp = FeaturePaths(repo_root=repo_root, feature=args.feature)
    current = fp.current_status()
    report: dict = {"feature": args.feature, "exists": current is not None}
    if current is None:
        if args.json:
            print(json.dumps(report))
        else:
            print(f"{args.feature}: not found")
        return exit_codes.OK
    report["status"] = current
    if current == "active":
        active = fp.active()
        pre = preflight_feature(active, reject_on_lock=False)
        report["next_stage"] = pre.next_stage
        report["stale_artifacts"] = pre.stale_artifacts
        report["lock_owner"] = pre.lock_held_by
        # overrides surfaced always (R4d)
        overrides = ov.load(active)
        report["active_overrides"] = [r.to_dict() for r in overrides.active_records()]
        report["historical_overrides"] = [r.to_dict() for r in overrides.historical_records()]
        log = JsonlLog(active / "log.jsonl")
        report["recent_events"] = log.tail(5)
        # Pause sentinel
        report["paused"] = (active / ".pause").exists()
        # G23: build-blocking halt surface
        build_path = active / "build.json"
        if build_path.exists():
            try:
                from autodev.artifacts.build import load_build
                br = load_build(build_path)
                if br.blocking:
                    report["build_blocking"] = {
                        "blocked": True,
                        "scope_ids": [
                            d.get("scope_id", "?") for d in br.deviations
                            if d.get("blocking") is True
                        ],
                    }
            except Exception:
                pass
        # v3-core R4: revision-loop counters (simplified — L per gate only).
        from autodev.artifacts.revision_state import (
            ALL_PANEL_GATES, L_MAX, load_state,
        )
        s = load_state(active)
        report["revision_state"] = {
            "L": {g: s.L.get(g, 0) for g in ALL_PANEL_GATES},
            "L_remaining": {
                g: max(0, L_MAX - s.L.get(g, 0)) for g in ALL_PANEL_GATES
            },
            "prd_target_streak": {
                g: s.prd_target_streak.get(g, 0) for g in ALL_PANEL_GATES
            },
            "pending_feedback": dict(s.pending_feedback),
        }
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"feature: {report['feature']}")
        print(f"status:  {report['status']}")
        if report.get("paused"):
            print("paused:  yes (run `autodev resume`)")
        if report.get("build_blocking", {}).get("blocked"):
            bb = report["build_blocking"]
            ids = ", ".join(bb["scope_ids"]) if bb["scope_ids"] else "(none listed)"
            print(f"build blocking: {ids} (PRD amendment required)")
        if report.get("next_stage"):
            print(f"next:    {report['next_stage']}")
        if report.get("stale_artifacts"):
            print(f"stale:   {', '.join(report['stale_artifacts'])}")
        if report.get("active_overrides"):
            print("overrides (active):")
            for r in report["active_overrides"]:
                g = f" gate={r.get('gate')}" if r.get("gate") else ""
                print(f"  {r['kind']}{g} by {r['who']}: {r['reason']}")
        if report.get("historical_overrides"):
            print(f"overrides (historical): {len(report['historical_overrides'])}")
        if report.get("lock_owner"):
            print(f"lock:    held by {report['lock_owner'].get('session_id')}")
        if report.get("revision_state"):
            rs = report["revision_state"]
            if (
                any(rs["L"].values())
                or any(rs["prd_target_streak"].values())
                or rs["pending_feedback"]
            ):
                print(
                    "revisions: "
                    + ", ".join(
                        f"{g}={rs['L'][g]}/{rs['L'][g] + rs['L_remaining'][g]}"
                        for g in rs["L"]
                    )
                )
                if any(rs["prd_target_streak"].values()):
                    print(
                        "  prd target streak: "
                        + ", ".join(
                            f"{g}={rs['prd_target_streak'][g]}"
                            for g in rs["prd_target_streak"]
                            if rs["prd_target_streak"][g]
                        )
                    )
                if rs["pending_feedback"]:
                    for stage, paths in rs["pending_feedback"].items():
                        print(f"  pending feedback → {stage}: {paths}")
        if report.get("recent_events"):
            print("recent events:")
            for ev in report["recent_events"]:
                print(f"  [{ev.get('stage')}] {ev.get('event')}")
    return exit_codes.OK


def _orch(args) -> Orchestrator:
    repo_root = _repo_root(args)
    cfg_vendors = _load_vendors(
        Path(args.vendors_yml) if args.vendors_yml else None,
        repo_root=repo_root,
    )
    if cfg_vendors is None:
        raise ConfigError(
            "vendors config not found; create auto-dev-sdk vendors.yml, set "
            f"{VENDORS_YML_ENV}, or pass --vendors-yml"
        )
    cfg = OrchestratorConfig(repo_root=repo_root, vendors=cfg_vendors)
    return Orchestrator(cfg)


def _dispatch_orch(args, call: str) -> int:
    try:
        orch = _orch(args)
    except ConfigError as e:
        print(f"config: {e}", file=sys.stderr)
        return exit_codes.ERROR
    # Stage 0 (DESIGN.md): before any orchestrator action, ensure the
    # feature's prd.md passes the PRD-schema check. Cheap, deterministic,
    # fails before any subprocess dispatch.
    try:
        from autodev.prd_intake import validate_prd_file
        repo_root = _repo_root(args)
        fp = FeaturePaths(repo_root=repo_root, feature=args.feature)
        status = fp.current_status()
        if status:
            prd = fp.status_dir(status) / "prd.md"
            if prd.exists():
                v = validate_prd_file(prd)
                if not v.ok:
                    print(
                        f"prd.md fails Stage 0 schema check for "
                        f"{args.feature!r}:",
                        file=sys.stderr,
                    )
                    print(v.format_errors(), file=sys.stderr)
                    print("  (run `autodev prd-lint <feature>` for details)",
                          file=sys.stderr)
                    return exit_codes.ERROR
    except Exception:  # defensive — never let Stage 0 break dispatch
        pass
    try:
        if call == "run":
            from autodev.orchestrator import PHASE_STOP_BEFORE
            until = getattr(args, "until", None)
            stop_before = PHASE_STOP_BEFORE.get(until) if until else None
            orch.run(args.feature, stop_before=stop_before)
            if until:
                print(f"{args.feature}: run advanced through '{until}' phase")
            else:
                print(f"{args.feature}: run complete")
        elif call == "next":
            orch.advance_one(args.feature)
            print(f"{args.feature}: next complete")
    except LockConflict as e:
        print(f"lock conflict: {e}", file=sys.stderr)
        return exit_codes.LOCK_CONFLICT
    except QuotaHalt as e:
        # All candidate LLMs for a role are below their min remaining quota.
        # Pause cleanly + record the earliest recovery time and a state
        # fingerprint, then exit GATE_PENDING. A later `autodev quota-resume`
        # (scheduled at resume_at) auto-continues only if still paused, time
        # reached, quota recovered, and the repo/feature is unchanged.
        from autodev.quota_pause import write_quota_pause
        active = FeaturePaths(repo_root=_repo_root(args), feature=args.feature).active()
        rec = write_quota_pause(
            active, role=e.role, resume_at=e.resume_at, diagnostics=e.diagnostics
        )
        print(
            f"QUOTA_PAUSE feature={args.feature} role={e.role} "
            f"resume_at={rec['resume_at']}"
        )
        print(
            f"gate pending: quota — all candidates for {e.role!r} below min quota; "
            f"schedule `autodev quota-resume {args.feature}` at {rec['resume_at']}",
            file=sys.stderr,
        )
        return exit_codes.GATE_PENDING
    except GatePending as e:
        print(f"gate pending: {e.gate} — {e.detail}", file=sys.stderr)
        return exit_codes.GATE_PENDING
    except (GateFailed, DirtyWorkspace, PreflightError, AutodevError) as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return exit_codes.ERROR
    return exit_codes.OK


def cmd_run(args) -> int:
    if getattr(args, "watch", False):
        os.environ["AUTODEV_WATCH"] = "1"
    return _dispatch_orch(args, "run")


def cmd_next(args) -> int:
    if getattr(args, "watch", False):
        os.environ["AUTODEV_WATCH"] = "1"
    return _dispatch_orch(args, "next")


def cmd_pause(args) -> int:
    fp = FeaturePaths(repo_root=_repo_root(args), feature=args.feature)
    active = fp.active()
    if not active.is_dir():
        print(f"{args.feature} not active", file=sys.stderr)
        return exit_codes.ERROR
    (active / ".pause").write_text("paused\n", encoding="utf-8")
    print(f"paused {args.feature}")
    return exit_codes.OK


def cmd_resume(args) -> int:
    fp = FeaturePaths(repo_root=_repo_root(args), feature=args.feature)
    active = fp.active()
    sent = active / ".pause"
    if sent.exists():
        sent.unlink()
    print(f"resumed {args.feature}; run `autodev run` or `autodev next`")
    return exit_codes.OK


def cmd_quota_resume(args) -> int:
    """Conditionally auto-resume a quota-paused feature.

    Auto-continues ONLY if ALL guards hold; otherwise it does nothing and never
    overrides a human/other process: (1) still quota-paused, (2) resume_at
    reached, (3) repo/feature fingerprint unchanged, (4) quota actually
    recovered. If quota has not recovered, reschedule to the new earliest reset
    (preserving the original fingerprint)."""
    from datetime import datetime, timezone

    from autodev.state.atomic import atomic_write_json
    from autodev.quota_pause import (
        QUOTA_PAUSE_FILE,
        clear_quota_pause,
        compute_fingerprint,
        is_quota_paused,
        read_quota_pause,
        recheck_recovery,
    )

    fp = FeaturePaths(repo_root=_repo_root(args), feature=args.feature)
    active = fp.active()
    rec = read_quota_pause(active)
    if rec is None:
        print(f"{args.feature}: no quota-pause record; nothing to do")
        return exit_codes.OK

    # Guard 1: still quota-paused (not manually resumed / aborted / superseded).
    if not is_quota_paused(active):
        try:
            (active / QUOTA_PAUSE_FILE).unlink()
        except OSError:
            pass
        print(
            f"{args.feature}: no longer quota-paused (resumed/aborted elsewhere); "
            f"not auto-resuming"
        )
        return exit_codes.OK

    # Guard 2: resume_at reached.
    resume_at = rec.get("resume_at")
    if resume_at:
        try:
            ra = datetime.fromisoformat(resume_at)
            if ra.tzinfo is None:
                ra = ra.replace(tzinfo=timezone.utc)
        except ValueError:
            ra = None
        if ra is not None and datetime.now(timezone.utc) < ra:
            print(
                f"{args.feature}: quota window not reached yet "
                f"(resume_at={resume_at}); staying paused"
            )
            return exit_codes.OK

    # Guard 3: repo/feature unchanged since pause (any commit/edit/abort cancels).
    if compute_fingerprint(active) != rec.get("fingerprint"):
        print(
            f"{args.feature}: repo changed since quota pause; not auto-resuming — "
            f"run `autodev resume` manually if this is intended"
        )
        return exit_codes.OK

    # Guard 4: quota actually recovered for at least one recorded candidate.
    recovered, earliest = recheck_recovery(rec)
    if not recovered:
        rec["resume_at"] = earliest.isoformat() if earliest else resume_at
        atomic_write_json(active / QUOTA_PAUSE_FILE, rec)  # keep original fingerprint
        print(
            f"{args.feature}: quota still below min; rescheduled "
            f"resume_at={rec['resume_at']}"
        )
        return exit_codes.OK

    # All guards pass → resume.
    clear_quota_pause(active)
    print(
        f"{args.feature}: quota recovered and repo unchanged; resumed. "
        f"Run `autodev run` or `autodev next`."
    )
    return exit_codes.OK


def cmd_abort(args) -> int:
    """Hard-stop the active run.

    Order of operations:
    1. Write the `.pause` sentinel so the orchestrator's `for` loop
       cannot dispatch another stage after the current vendor dies
       (without this, the orchestrator treats the kill as a stage
       failure and revision-loop-dispatches the next producer).
    2. SIGTERM the running vendor subprocess (5s grace, then SIGKILL).
    3. Write `<stage>-failure.json` with kind=interrupted.

    `autodev resume` clears the sentinel before the next `run`.
    """
    import os
    import signal
    import socket
    import time as _time

    fp = FeaturePaths(repo_root=_repo_root(args), feature=args.feature)
    active = fp.active()
    if not active.is_dir():
        return exit_codes.ERROR

    # Step 1: write .pause sentinel so the orchestrator does NOT
    # treat the upcoming vendor kill as a normal stage failure and
    # dispatch the next producer. The sentinel is checked at the
    # start of every `_advance_one`; users clear it with `autodev
    # resume`. This is what makes abort actually stop the run.
    (active / ".pause").write_text("paused-by-abort\n", encoding="utf-8")

    # Step 2: try to kill the running subprocess by its recorded PID.
    pid_file = active / ".running.pid"
    killed_pid = None
    killed_stage = "unknown"
    reaped = False
    if pid_file.exists():
        try:
            content = pid_file.read_text().strip().splitlines()
            pid = int(content[0])
            killed_stage = content[1] if len(content) > 1 else "unknown"
            # Send SIGTERM to the whole process group
            try:
                os.killpg(pid, signal.SIGTERM)
                killed_pid = pid
            except ProcessLookupError:
                killed_pid = None  # process already exited
            # 5s grace
            if killed_pid is not None:
                for _ in range(50):
                    _time.sleep(0.1)
                    try:
                        os.killpg(pid, 0)
                    except ProcessLookupError:
                        break
                else:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                        reaped = True
                    except ProcessLookupError:
                        pass
            pid_file.unlink(missing_ok=True)
        except (OSError, ValueError) as e:
            print(f"warning: couldn't parse {pid_file}: {e}", file=sys.stderr)

    # Unified interrupted failure artifact (named by stage if known)
    failure_name = f"{killed_stage}-failure.json" if killed_stage != "unknown" else "abort-failure.json"
    fr = FailureReport(
        stage=killed_stage, kind="interrupted",
        detail=f"abort via autodev abort (pid={killed_pid})",
        subprocess_exit=None, stderr_tail="",
        ts="", subprocess_reaped=reaped,
    )
    write_failure(active / failure_name, fr)

    # Step 3: stop the ORCHESTRATOR itself (the `autodev run` process holding
    # the feature lock) — not just its vendor child. An orchestrator that has
    # not yet seen `.pause` would otherwise keep advancing the pipeline after
    # we free its lock, concurrent with the next run. (Defense in depth: the
    # orchestrator also self-terminates on its next lock heartbeat.)
    from autodev.state.lock import Lock, read_owner
    owner = read_owner(active) or {}
    orch_pid = owner.get("pid")
    orch_host = owner.get("host")
    if (
        isinstance(orch_pid, int)
        and orch_pid != os.getpid()
        and (not orch_host or orch_host == socket.gethostname())
    ):
        try:
            os.kill(orch_pid, signal.SIGTERM)
            for _ in range(50):
                _time.sleep(0.1)
                try:
                    os.kill(orch_pid, 0)
                except ProcessLookupError:
                    break
            else:
                try:
                    os.kill(orch_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            print(f"  stopped orchestrator pid {orch_pid}")
        except (ProcessLookupError, PermissionError):
            pass

    # Release the lock (force: we just stopped the holder, so reclaim it even
    # though abort's own token doesn't match the orchestrator's).
    Lock(active, session_id="abort", verb="abort").release(force=True)
    print(
        f"aborted {args.feature}"
        + (f" (killed pid {killed_pid})" if killed_pid else "")
        + "; .pause sentinel written. Run `autodev resume` before next `autodev run`."
    )
    return exit_codes.OK


def cmd_retry(args) -> int:
    # Retry = run; orchestrator picks up from cascade next_stage
    return _dispatch_orch(args, "next")


def cmd_invalidate(args) -> int:
    fp = FeaturePaths(repo_root=_repo_root(args), feature=args.feature)
    active = fp.active()
    # Map stage name to cascade artifact file
    from autodev.state.cascade import ARTIFACTS
    by_name = {a.name: a for a in ARTIFACTS}
    stage = args.stage
    if stage not in by_name:
        print(f"unknown stage: {stage!r}; valid: {list(by_name)}", file=sys.stderr)
        return exit_codes.ERROR
    target = active / by_name[stage].path_fragment
    if target.exists():
        target.unlink()
        print(f"invalidated {target}")
    # If stage is a gate artifact, also clear same-cycle skip-gate (R4d).
    gate_map = {
        "panel_design_review": "design-review",
        "panel_close_approval": "close-approval",
    }
    if stage in gate_map:
        ov.invalidate_clears_skip_gate(active, gate_map[stage])
        print(f"cleared same-cycle skip-gate for {gate_map[stage]} (if present)")
    return exit_codes.OK


def cmd_update(args) -> int:
    fp = FeaturePaths(repo_root=_repo_root(args), feature=args.feature)
    active = fp.active()
    prd = active / "prd.md"
    if not prd.exists():
        print("prd.md not found", file=sys.stderr)
        return exit_codes.ERROR
    from datetime import date
    stamped = f"\n\n## Amendment {date.today().isoformat()}\n\n{args.amendment.strip()}\n"
    prd.write_text(prd.read_text(encoding="utf-8") + stamped, encoding="utf-8")
    ov.advance_cycle(active)
    from autodev.artifacts.revision_state import reset_on_amendment
    reset_on_amendment(active)
    # Mechanism 2 (rigor-tier): a new PRD cycle starts fingerprint-clean;
    # stale diagnosis / rework-mode artifacts must not leak across cycles.
    from autodev.artifacts.fingerprint_history import clear_history
    clear_history(active)
    (active / "diagnosis.json").unlink(missing_ok=True)
    (active / "rework-mode.json").unlink(missing_ok=True)
    # Emit a structured log event so the iteration-history manifest in
    # downstream stage prompts shows the amendment as a clear cycle
    # boundary. Anything in the manifest before this row was produced
    # under the previous PRD.
    from autodev.state.log import JsonlLog
    JsonlLog(active / "log.jsonl").emit(
        stage="orchestrator", event="prd-amended", feature=args.feature,
        detail={"amendment_first_line": args.amendment.strip().splitlines()[0][:200]},
    )
    print(f"amended {prd}; cycle advanced; revision-state L reset to 0")
    return exit_codes.OK


def cmd_close(args) -> int:
    """Close feature. --yes bypasses interactive confirm; does NOT bypass gates."""
    fp = FeaturePaths(repo_root=_repo_root(args), feature=args.feature)
    active = fp.active()
    if not active.is_dir():
        print(f"{args.feature} not active", file=sys.stderr)
        return exit_codes.ERROR
    # Mandatory gate check: panel-close-approval must exist + verdict pass.
    # (OR close-approval skip-gate override active.)
    from autodev.artifacts.verdict import load_verdict
    from autodev.artifacts.overrides import CEILING_REFUSE_AT, CEILING_WARNING_AT
    v_path = active / "panel-close-approval.json"
    overrides = ov.load(active)

    # G16: skip-gate ceiling. Applies BEFORE verdict check so that a
    # skip-gate accumulation failure surfaces even when close-approval
    # skip itself is what tipped the scale.
    weight = overrides.active_skip_gate_weight()
    if weight >= CEILING_REFUSE_AT:
        print(
            f"close refused: skip-gate weight {weight} >= ceiling "
            f"{CEILING_REFUSE_AT} for cycle {overrides.current_cycle}. "
            f"Likely systemic issue. Run `autodev escalate {args.feature}` "
            f"to snapshot for human review, OR `autodev update {args.feature} "
            f"--amendment '...'` to advance the cycle (resets counters).",
            file=sys.stderr,
        )
        return exit_codes.ERROR
    if weight >= CEILING_WARNING_AT and not args.yes:
        print(
            f"close WARNING: skip-gate weight {weight} (warn at "
            f"{CEILING_WARNING_AT}, refuse at {CEILING_REFUSE_AT}). "
            f"Re-run with --yes to proceed.",
            file=sys.stderr,
        )
        return exit_codes.ERROR

    if not overrides.has_active_skip_gate("close-approval"):
        if not v_path.exists():
            print("close-approval gate not run; use `autodev run` first", file=sys.stderr)
            return exit_codes.GATE_PENDING
        v = load_verdict(v_path)
        if v.effectively_blocks():
            print(f"close-approval: verdict={v.verdict}; not passed", file=sys.stderr)
            return exit_codes.GATE_PENDING

    if not args.yes:
        reply = input(f"close {args.feature} as {args.reason}? [y/N] ")
        if reply.strip().lower() != "y":
            print("aborted")
            return exit_codes.ERROR

    import shutil
    dest = fp.status_dir(args.reason)
    if dest.exists():
        print(f"{dest} already exists", file=sys.stderr)
        return exit_codes.ERROR
    # revision-state.json does not survive the close → re-open loop;
    # L counters start fresh when the feature reopens.
    from autodev.artifacts.revision_state import clear_state
    clear_state(active)
    # Remove lock before moving (G6: clean single call)
    import shutil as _shutil
    _shutil.rmtree(active / ".lock", ignore_errors=True)
    shutil.move(str(active), str(dest))
    print(f"closed {args.feature} → {dest}")
    return exit_codes.OK


def cmd_explain(args) -> int:
    return cmd_status(args)  # alias for now


def cmd_skip_gate(args) -> int:
    fp = FeaturePaths(repo_root=_repo_root(args), feature=args.feature)
    active = fp.active()
    if not active.is_dir():
        print(f"{args.feature} not active", file=sys.stderr)
        return exit_codes.ERROR
    try:
        overrides = ov.record_skip_gate(
            active, gate=args.gate, reason=args.reason, who=args.who,
            severity=args.severity,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return exit_codes.ERROR
    # Mechanism 2 (rigor-tier): skipping a gate while a rigor-pivotal
    # diagnosis is pending IS the "keep the tolerance" answer — record
    # the declined (fingerprint, R) pairs so the same re-audit question
    # is never asked again for this stall.
    from autodev.diagnosis import record_skip_as_declined
    declined = record_skip_as_declined(active, args.gate)
    if declined:
        print(
            f"recorded {declined} declined re-audit pair(s) — the pending "
            f"rigor re-audit for {args.gate} is answered as 'keep the "
            f"tolerance' and will not be re-asked"
        )
    # G16: warn if ceiling is approaching.
    from autodev.artifacts.overrides import (
        CEILING_REFUSE_AT, CEILING_WARNING_AT,
    )
    weight = overrides.active_skip_gate_weight()
    print(f"skipped gate {args.gate} (severity={args.severity}) for {args.feature}")
    if weight >= CEILING_REFUSE_AT:
        print(
            f"WARNING: skip-gate weight for this cycle is now {weight} "
            f"(ceiling {CEILING_REFUSE_AT}). `autodev close` will REFUSE. "
            f"Run `autodev escalate {args.feature}` or advance the cycle via "
            f"`autodev update {args.feature} --amendment '...'`.",
            file=sys.stderr,
        )
    elif weight >= CEILING_WARNING_AT:
        print(
            f"WARNING: skip-gate weight is now {weight} (warn at "
            f"{CEILING_WARNING_AT}, refuse at {CEILING_REFUSE_AT}). "
            f"One more normal skip-gate — or a single high-severity one — "
            f"will block `autodev close`.",
            file=sys.stderr,
        )
    return exit_codes.OK


def cmd_escalate(args) -> int:
    """G16: snapshot feature state into escalation.json for human review."""
    fp = FeaturePaths(repo_root=_repo_root(args), feature=args.feature)
    active = fp.active()
    if not active.is_dir():
        print(f"{args.feature} not active", file=sys.stderr)
        return exit_codes.ERROR
    overrides = ov.load(active)
    from datetime import datetime, timezone
    from autodev.artifacts.overrides import SEVERITY_WEIGHT
    snapshot = {
        "feature": args.feature,
        "escalated_at": datetime.now(timezone.utc).isoformat(),
        "escalated_by": ov.resolve_who(args.who),
        "current_cycle": overrides.current_cycle,
        "active_skip_gate_weight": overrides.active_skip_gate_weight(),
        "active_skip_gates": [
            {
                "gate": r.gate, "severity": r.severity,
                "severity_weight": SEVERITY_WEIGHT.get(r.severity, 1),
                "reason": r.reason, "who": r.who, "ts": r.ts,
            }
            for r in overrides.records
            if r.active and r.kind == "skip_gate"
            and r.skipped_in_cycle == overrides.current_cycle
        ],
        "panel_verdicts_present": sorted(
            p.name for p in active.glob("panel-*.json")
        ),
    }
    esc_path = active / "escalation.json"
    atomic_write(esc_path, json.dumps(snapshot, indent=2))
    print(f"wrote {esc_path}")
    print(
        f"\nEscalation snapshot written. This feature has hit the skip-gate "
        f"ceiling (weight={snapshot['active_skip_gate_weight']}).\n"
        f"Next steps — pick ONE:\n"
        f"  (a) AMEND the PRD to enter a new cycle (resets counters):\n"
        f"      autodev update {args.feature} --amendment '<what changed>'\n"
        f"  (b) CLOSE as deferred honestly:\n"
        f"      autodev close {args.feature} deferred --yes\n"
        f"  (c) DOWNGRADE one or more skip-gates to severity=low if they\n"
        f"      are genuinely parser/tooling artifacts (edit overrides.json\n"
        f"      by hand — each downgrade is an audit-logged decision).\n"
        f"Re-read panel-*.json, overrides.json, and escalation.json before\n"
        f"deciding. Do NOT add more skip-gate overrides.",
        file=sys.stderr,
    )
    return exit_codes.OK


def cmd_acknowledge_dirty(args) -> int:
    fp = FeaturePaths(repo_root=_repo_root(args), feature=args.feature)
    active = fp.active()
    if not active.is_dir():
        print(f"{args.feature} not active", file=sys.stderr)
        return exit_codes.ERROR
    try:
        ov.record_acknowledge_dirty(active, reason=args.reason, who=args.who)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return exit_codes.ERROR
    print(f"acknowledged dirty workspace for {args.feature}")
    return exit_codes.OK


_DISPATCH = {
    "prd": cmd_prd,
    "prd-lint": cmd_prd_lint,
    "status": cmd_status,
    "run": cmd_run,
    "next": cmd_next,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "quota-resume": cmd_quota_resume,
    "abort": cmd_abort,
    "retry": cmd_retry,
    "invalidate": cmd_invalidate,
    "update": cmd_update,
    "close": cmd_close,
    "explain": cmd_explain,
    "skip-gate": cmd_skip_gate,
    "acknowledge-dirty": cmd_acknowledge_dirty,
    "escalate": cmd_escalate,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    fn = _DISPATCH[args.verb]
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
