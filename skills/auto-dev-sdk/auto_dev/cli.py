"""CLI entry point — `auto-dev <verb> ...`.

Subcommands (PRD R4 / R9):
  prd <feature> [--from-file PATH]
  status <feature>
  approve <feature> <gate>
  abort <feature>
  resume <feature>
  update <feature> --amendment TEXT
  close <feature> <reason> [--note TEXT]
  implement <feature> [--prd-review-mode ...] [--yes]
                     [--allow-cmd GLOB,GLOB] [--deny-cmd GLOB]
                     [--from-file PATH]

Exit codes (PRD R10): 0=done, 1=error, 2=gate pending, 3=lock conflict.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from auto_dev import exit_codes
from auto_dev.errors import (
    AutoDevError,
    ConfigError,
    GatePending,
    LockConflict,
    PipelineError,
)
from auto_dev.orchestrator import Orchestrator, OrchestratorConfig, abort, status
from auto_dev.stages import gates
from auto_dev.stages.paths import FeaturePaths
from auto_dev.vendors import load_vendors_config


DEFAULT_VENDORS_YML = Path("vendors.yml")


def _find_repo_root(start: Path) -> Path:
    """Walk up looking for `docs/features/`. Fall back to cwd."""
    p = Path(start).resolve()
    for candidate in [p, *p.parents]:
        if (candidate / "docs" / "features").is_dir():
            return candidate
    return Path(start).resolve()


def _load_vendors(path: Path | None, needed: bool) -> "object | None":
    if path is None:
        path = DEFAULT_VENDORS_YML
    path = Path(path)
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        if needed:
            raise ConfigError(
                f"vendors.yml not found at {path}; pass --vendors-yml or create one"
            )
        return None
    return load_vendors_config(path)


def _make_orchestrator(args) -> Orchestrator:
    repo_root = Path(args.repo_root) if args.repo_root else _find_repo_root(Path.cwd())
    vendors = _load_vendors(Path(args.vendors_yml) if args.vendors_yml else None, needed=True)
    cfg = OrchestratorConfig(
        repo_root=repo_root,
        vendors=vendors,
        prd_review_mode=getattr(args, "prd_review_mode", "none"),
        yes=getattr(args, "yes", False),
        allow_cmd=_split_csv(getattr(args, "allow_cmd", None)),
        deny_cmd=_split_csv(getattr(args, "deny_cmd", None)),
    )
    return Orchestrator(cfg)


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", default=None, help="Repo root (defaults to nearest ancestor with docs/features/)")
    parser.add_argument("--vendors-yml", default=None, help="Path to vendors.yml (default: ./vendors.yml)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="auto-dev", description="auto-dev SDK harness.")
    sub = p.add_subparsers(dest="verb", required=True)

    # prd
    sp = sub.add_parser("prd", help="Create or import prd.md for a feature")
    sp.add_argument("feature")
    sp.add_argument("--from-file", default=None, help="Copy this file into planned/prd.md instead of interviewing")
    _common(sp)

    # status
    sp = sub.add_parser("status", help="Show feature status")
    sp.add_argument("feature")
    sp.add_argument("--json", action="store_true")
    _common(sp)

    # approve
    sp = sub.add_parser("approve", help="Approve a pending gate")
    sp.add_argument("feature")
    sp.add_argument("gate", choices=list(gates.VALID_GATES))
    _common(sp)

    # abort
    sp = sub.add_parser("abort", help="Record interrupted.json; release lock")
    sp.add_argument("feature")
    _common(sp)

    # resume
    sp = sub.add_parser("resume", help="Re-enter pipeline from filesystem state")
    sp.add_argument("feature")
    _add_implement_flags(sp)
    _common(sp)

    # update
    sp = sub.add_parser("update", help="Append a PRD amendment and re-run affected stages")
    sp.add_argument("feature")
    sp.add_argument("--amendment", required=True, help="Text of the amendment (markdown)")
    _common(sp)

    # close
    sp = sub.add_parser("close", help="Close a feature")
    sp.add_argument("feature")
    sp.add_argument("reason", choices=("complete", "retiring", "deferred", "cancelled"))
    sp.add_argument("--note", default="", help="Cancellation note (used when reason=cancelled)")
    sp.add_argument("--yes", action="store_true", help="Skip close-approval gate check")
    _common(sp)

    # implement
    sp = sub.add_parser("implement", help="Run the implement pipeline end-to-end")
    sp.add_argument("feature")
    sp.add_argument("--from-file", default=None)
    _add_implement_flags(sp)
    _common(sp)

    return p


def _add_implement_flags(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--prd-review-mode", choices=("none", "subagent", "panel"), default="none")
    sp.add_argument("--yes", action="store_true", help="Non-interactive; gates still raise exit 2")
    sp.add_argument("--allow-cmd", default="", help="Comma-separated command allow-list (glob or re:regex)")
    sp.add_argument("--deny-cmd", default="", help="Comma-separated command deny-list")


# ---------------------------------------------------------------------------
# Verbs


def cmd_prd(args) -> int:
    repo_root = Path(args.repo_root) if args.repo_root else _find_repo_root(Path.cwd())
    fp = FeaturePaths(repo_root=repo_root, feature=args.feature)
    fp.base.mkdir(parents=True, exist_ok=True)
    # Default to planned/ for a fresh PRD.
    target_status = fp.current_status() or "planned"
    target_dir = fp.status_dir(target_status)
    target_dir.mkdir(parents=True, exist_ok=True)
    prd = target_dir / "prd.md"
    if args.from_file:
        src = Path(args.from_file)
        if not src.exists():
            print(f"source not found: {src}", file=sys.stderr)
            return exit_codes.ERROR
        from auto_dev.state.atomic import atomic_write

        atomic_write(prd, src.read_bytes())
        print(f"imported {src} → {prd}")
        return exit_codes.OK
    if not sys.stdin.isatty():
        print(
            "prd intake requires a TTY for interview. Use --from-file to import.",
            file=sys.stderr,
        )
        return exit_codes.ERROR
    print("Interactive PRD intake not bundled in v0.1. Use --from-file for now.")
    return exit_codes.ERROR


def cmd_status(args) -> int:
    repo_root = Path(args.repo_root) if args.repo_root else _find_repo_root(Path.cwd())
    report = status(repo_root, args.feature)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        if not report.get("exists"):
            print(f"{args.feature}: not found")
            return exit_codes.OK
        print(f"feature: {report['feature']}")
        print(f"status: {report['status']}")
        print(f"next stage: {report['next_stage']}")
        if report.get("pending_gates"):
            print(f"pending gates: {', '.join(report['pending_gates'])}")
        if report.get("stale_artifacts"):
            print(f"stale: {', '.join(report['stale_artifacts'])}")
        if report.get("lock_owner"):
            print(f"lock: held by {report['lock_owner'].get('session_id')}")
        if report.get("recent_events"):
            print("recent events:")
            for ev in report["recent_events"]:
                print(f"  [{ev.get('stage')}] {ev.get('event')}")
    return exit_codes.OK


def cmd_approve(args) -> int:
    repo_root = Path(args.repo_root) if args.repo_root else _find_repo_root(Path.cwd())
    fp = FeaturePaths(repo_root=repo_root, feature=args.feature)
    active = fp.active()
    if not active.is_dir():
        print(f"feature {args.feature!r} not active", file=sys.stderr)
        return exit_codes.ERROR
    gates.approve(active, args.gate)
    print(f"approved {args.gate} for {args.feature}")
    return exit_codes.OK


def cmd_abort(args) -> int:
    repo_root = Path(args.repo_root) if args.repo_root else _find_repo_root(Path.cwd())
    result = abort(repo_root, args.feature)
    print(json.dumps(result))
    return exit_codes.OK


def cmd_implement(args, *, resume: bool = False) -> int:
    orch = _make_orchestrator(args)
    try:
        result = orch.implement(
            args.feature,
            prd_from_file=Path(args.from_file) if getattr(args, "from_file", None) else None,
        )
    except LockConflict as e:
        print(f"lock conflict: {e}", file=sys.stderr)
        return exit_codes.LOCK_CONFLICT
    except GatePending as e:
        print(f"gate pending: {e.gate} — {e.detail}", file=sys.stderr)
        return exit_codes.GATE_PENDING
    except (PipelineError, AutoDevError) as e:
        print(f"pipeline error: {e}", file=sys.stderr)
        return exit_codes.ERROR
    print(json.dumps(result, indent=2))
    return exit_codes.OK


def cmd_resume(args) -> int:
    # Resume is structurally identical — filesystem state + cascade decide what runs.
    return cmd_implement(args, resume=True)


def cmd_update(args) -> int:
    orch = _make_orchestrator(args)
    try:
        result = orch.update(args.feature, amendment_text=args.amendment)
    except (PipelineError, AutoDevError) as e:
        print(f"update failed: {e}", file=sys.stderr)
        return exit_codes.ERROR
    print(json.dumps(result, indent=2))
    return exit_codes.OK


def cmd_close(args) -> int:
    orch = _make_orchestrator(args)
    orch.cfg.yes = args.yes
    try:
        result = orch.close(args.feature, args.reason, cancel_note=args.note)
    except GatePending as e:
        print(f"gate pending: {e}", file=sys.stderr)
        return exit_codes.GATE_PENDING
    except (PipelineError, AutoDevError, ValueError, FileExistsError, FileNotFoundError) as e:
        print(f"close failed: {e}", file=sys.stderr)
        return exit_codes.ERROR
    print(json.dumps(result, indent=2))
    return exit_codes.OK


# ---------------------------------------------------------------------------

_DISPATCH = {
    "prd": cmd_prd,
    "status": cmd_status,
    "approve": cmd_approve,
    "abort": cmd_abort,
    "resume": cmd_resume,
    "update": cmd_update,
    "close": cmd_close,
    "implement": cmd_implement,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    fn = _DISPATCH[args.verb]
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
