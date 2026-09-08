#!/usr/bin/env python3
"""Durable, provider-neutral session state for the shared vendors wrapper.

The shell wrapper owns process launch; this helper owns the small amount of
state needed to turn an opaque caller-provided session key into a provider
conversation id.  State is local-machine runtime state (vendor sessions are
local too), never a repository artifact.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - shared vendors currently targets Unix
    fcntl = None  # type: ignore[assignment]


SCHEMA_VERSION = 1
PREASSIGNABLE_VENDORS = {"claude", "grok"}
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,255}")


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _transport_fingerprint(args: argparse.Namespace) -> str:
    encoded = json.dumps(
        args.transport_arg or [], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _identity_payload(args: argparse.Namespace) -> dict[str, str | int]:
    return {
        "schema_version": SCHEMA_VERSION,
        "key": args.key,
        "vendor": args.vendor.strip().lower(),
        "model": args.model or "",
        "cwd": os.path.realpath(args.cwd) if args.cwd else "",
        # Native arguments can alter model/provider/project/tool context. Keep
        # their ordered fingerprint in the identity without persisting their
        # potentially sensitive raw values.
        "transport_sha256": _transport_fingerprint(args),
    }


def _identity_hash(args: argparse.Namespace) -> str:
    encoded = json.dumps(
        _identity_payload(args), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _state_path(args: argparse.Namespace) -> Path:
    return Path(args.state_dir).expanduser().resolve() / f"{_identity_hash(args)}.json"


def _key_sha256(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _key_guard_path(state_dir: Path, key: str) -> Path:
    """Return a non-record path used to serialize plan/reset for one key."""

    return state_dir / f".key-{_key_sha256(key)}"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _required_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid {label}: {exc}")
    if not isinstance(value, dict):
        raise SystemExit(f"invalid {label}: expected object")
    return value


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise SystemExit(f"cannot hash recovery evidence: {exc}")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def _record_guard(path: Path) -> Iterator[None]:
    """Serialize plan/finalize transactions for one logical session."""

    guard = path.with_suffix(path.suffix + ".guard")
    guard.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with guard.open("a+", encoding="utf-8") as handle:
        try:
            os.chmod(guard, 0o600)
        except OSError:
            pass
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _pid_alive(raw_pid: Any) -> bool:
    try:
        pid = int(raw_pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lease_is_live(record: dict[str, Any]) -> bool:
    token = record.get("lease_token")
    if not token:
        return False
    try:
        expires = float(record.get("lease_expires_epoch", 0))
    except (TypeError, ValueError):
        expires = 0
    # Fail closed across coordinator crashes. A turn remains protected for its
    # lease window even if its owner disappears, and a still-running owner
    # remains protected even if a very long call outlives that window.
    return expires > time.time() or _pid_alive(record.get("lease_owner_pid"))


def _nonnegative_int(value: Any, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def cmd_plan(args: argparse.Namespace) -> int:
    path = _state_path(args)
    identity = _identity_payload(args)
    state_dir = Path(args.state_dir).expanduser().resolve()
    # The key-level guard makes an operator reset atomic with respect to new
    # identities for the same logical conversation. Record guards still
    # serialize plan/finalize for each concrete vendor/model/cwd identity.
    with _record_guard(_key_guard_path(state_dir, args.key)):
        with _record_guard(path):
            record = _read_json(path)
            if record and _lease_is_live(record):
                owner = record.get("lease_owner_pid", "unknown")
                raise SystemExit(
                    f"session key is already in use by live pid {owner}: "
                    f"{_identity_hash(args)[:12]}"
                )
            stored_record = record

            previous_session_id = record.get("session_id")
            if not isinstance(previous_session_id, str) or not previous_session_id:
                previous_session_id = None
            elif not _valid_session_id(previous_session_id):
                raise SystemExit("stored native session id is malformed; refusing resume")
            max_turns = int(args.max_turns)
            if max_turns < 0:
                raise SystemExit("session max turns must be non-negative")
            turn_count = _nonnegative_int(record.get("session_turn_count"))
            if previous_session_id is None:
                turn_count = 0
            auto_reset = bool(
                previous_session_id and max_turns and turn_count >= max_turns
            )
            reset_at = _utc_now() if auto_reset else None
            if auto_reset:
                previous_session_id = None
                turn_count = 0
            mode = "resume" if previous_session_id else "new"
            requested_session_id = previous_session_id
            if mode == "new" and args.vendor.strip().lower() in PREASSIGNABLE_VENDORS:
                requested_session_id = str(uuid.uuid4())

            token = str(uuid.uuid4())
            lease_sec = max(int(args.lease_sec), 60)
            record = {
                "schema_version": SCHEMA_VERSION,
                "identity_hash": _identity_hash(args),
                "key_sha256": _key_sha256(args.key),
                "vendor": identity["vendor"],
                "model": identity["model"],
                "cwd": identity["cwd"],
                "transport_sha256": identity["transport_sha256"],
                "session_id": previous_session_id,
                "session_turn_count": turn_count,
                "session_max_turns": max_turns or None,
                "pending_session_id": requested_session_id,
                "pending_mode": mode,
                "pending_turn_number": turn_count + 1,
                "lease_token": token,
                "lease_owner_pid": int(args.owner_pid),
                "lease_expires_epoch": time.time() + lease_sec,
                "updated_at": _utc_now(),
            }
            for field in ("reset_count", "last_reset_at", "auto_reset_count", "last_auto_reset_at"):
                if field in stored_record:
                    record[field] = stored_record[field]
            if auto_reset:
                record["auto_reset_count"] = (
                    _nonnegative_int(record.get("auto_reset_count")) + 1
                )
                record["last_auto_reset_at"] = reset_at
            plan = {
                "schema_version": SCHEMA_VERSION,
                "record_path": str(path),
                "identity_hash": record["identity_hash"],
                "lease_token": token,
                "mode": mode,
                "session_id": requested_session_id,
                "previous_session_id": previous_session_id,
                "turn_number": turn_count + 1,
                "max_turns": max_turns or None,
                "auto_reset": auto_reset,
            }
            # Publish the exact-token capability before the lease. If this helper
            # is terminated between the two atomic writes, signal cleanup either
            # has the plan needed to release our lease or finds no matching lease.
            _atomic_write_json(Path(args.output), plan)
            _atomic_write_json(path, record)
    return 0


def cmd_field(args: argparse.Namespace) -> int:
    payload = _read_json(Path(args.plan))
    value = payload.get(args.name)
    if value is None:
        return 0
    if isinstance(value, bool):
        print("true" if value else "false")
    elif isinstance(value, (str, int, float)):
        print(value)
    else:
        print(json.dumps(value, separators=(",", ":")))
    return 0


def cmd_finalize(args: argparse.Namespace) -> int:
    plan = _read_json(Path(args.plan))
    raw_path = plan.get("record_path")
    token = plan.get("lease_token")
    if not isinstance(raw_path, str) or not isinstance(token, str):
        raise SystemExit("invalid session plan: missing record_path or lease_token")
    path = Path(raw_path)
    with _record_guard(path):
        record = _read_json(path)
        if record.get("lease_token") != token:
            raise SystemExit("session lease changed before finalize; refusing stale write")

        if args.invalidate:
            record["session_id"] = None
            record["session_turn_count"] = 0
        elif args.success:
            observed = args.observed_session_id.strip()
            planned = plan.get("session_id")
            chosen = observed or (planned if isinstance(planned, str) else "")
            if not chosen:
                raise SystemExit(
                    "cannot finalize a successful keyed call without a native session id"
                )
            if not _valid_session_id(chosen):
                raise SystemExit("refusing malformed native session id")
            turn_number = plan.get("turn_number")
            if (
                isinstance(turn_number, bool)
                or not isinstance(turn_number, int)
                or turn_number <= 0
            ):
                raise SystemExit("invalid session plan: missing positive turn number")
            record["session_id"] = chosen
            record["session_turn_count"] = turn_number

        for field in (
            "pending_session_id", "pending_mode", "pending_turn_number", "lease_token",
            "lease_owner_pid", "lease_expires_epoch",
        ):
            record.pop(field, None)
        record["last_mode"] = plan.get("mode")
        record["last_success"] = bool(args.success)
        record["updated_at"] = _utc_now()
        _atomic_write_json(path, record)
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    """Forget every provider identity mapped from one opaque logical key."""

    state_dir = Path(args.state_dir).expanduser().resolve()
    expected_key_hash = _key_sha256(args.key)
    if not state_dir.is_dir():
        print(0)
        return 0

    with _record_guard(_key_guard_path(state_dir, args.key)):
        matching_paths = sorted(
            path
            for path in state_dir.glob("*.json")
            if _read_json(path).get("key_sha256") == expected_key_hash
        )
        with contextlib.ExitStack() as stack:
            for path in matching_paths:
                stack.enter_context(_record_guard(path))

            records = [(path, _read_json(path)) for path in matching_paths]
            live = [
                (path, record.get("lease_owner_pid", "unknown"))
                for path, record in records
                if _lease_is_live(record)
            ]
            if live:
                owners = ", ".join(f"{path.stem[:12]}:{owner}" for path, owner in live)
                raise SystemExit(
                    f"session key has active lease(s) ({owners}); abort or wait before reset"
                )

            reset_at = _utc_now()
            for path, record in records:
                record["session_id"] = None
                record["session_turn_count"] = 0
                for field in (
                    "pending_session_id", "pending_mode", "pending_turn_number", "lease_token",
                    "lease_owner_pid", "lease_expires_epoch",
                ):
                    record.pop(field, None)
                record["last_reset_at"] = reset_at
                record["reset_count"] = int(record.get("reset_count", 0)) + 1
                record["updated_at"] = reset_at
                _atomic_write_json(path, record)

    print(len(matching_paths))
    return 0


def _process_group_members(pgid: int) -> set[int]:
    """Return non-zombie members of a process group."""

    proc_root = Path("/proc")
    if proc_root.is_dir():
        members: set[int] = set()
        scanned = False
        for stat_path in proc_root.glob("[0-9]*/stat"):
            try:
                raw = stat_path.read_text(encoding="utf-8")
                rest = raw[raw.rfind(")") + 2 :].split()
                state = rest[0]
                process_group = int(rest[2])
                pid = int(stat_path.parent.name)
            except (OSError, IndexError, ValueError):
                continue
            scanned = True
            if process_group == pgid and state not in {"Z", "X"}:
                members.add(pid)
        if scanned:
            return members

    try:
        scanner = subprocess.Popen(
            ["ps", "-axo", "pid=,pgid=,stat="],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, _ = scanner.communicate(timeout=2)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"cannot verify process-group membership: {exc}")
    if scanner.returncode != 0:
        raise SystemExit("cannot verify process-group membership: ps failed")
    members = set()
    for line in stdout.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        try:
            pid = int(fields[0])
            process_group = int(fields[1])
        except ValueError:
            continue
        if (
            process_group == pgid
            and pid != scanner.pid
            and not fields[2].startswith("Z")
        ):
            members.add(pid)
    return members


def _strict_process_identity(pid: int) -> tuple[str, int, str] | None:
    """Read one Linux identity; only a vanished proc entry means absent."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SystemExit(f"cannot verify recovery process {pid}: {exc}")
    try:
        fields = raw[raw.rfind(")") + 2:].split()
        return fields[19], int(fields[2]), fields[0]
    except (IndexError, ValueError) as exc:
        raise SystemExit(f"cannot parse recovery process {pid}: {exc}")


def _require_absent_identity(pid: int, start_id: str, label: str) -> None:
    current = _strict_process_identity(pid)
    if current is not None:
        relation = "same identity" if current[0] == start_id else "reused identity"
        raise SystemExit(f"{label} pid has {relation}: refusing recovery")


def _require_empty_process_group(pgid: int) -> None:
    proc = Path("/proc")
    if not proc.is_dir():
        raise SystemExit("cannot verify recovery process group without /proc")
    if _strict_process_identity(pgid) is not None:
        raise SystemExit("recovery process-group leader pid was reused")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        identity = _strict_process_identity(int(entry.name))
        if identity is not None and identity[1] == pgid and identity[2] not in {"Z", "X"}:
            raise SystemExit("recovery process group still has live members")


def _signal_members(pids: set[int], signal_number: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal_number)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            raise SystemExit(f"cannot signal process-group member {pid}: {exc}")


def _stop_dedicated_process_group(
    *, pgid: int, coordinator_pid: int, grace_sec: float,
) -> None:
    if pgid <= 0 or coordinator_pid <= 0 or pgid != coordinator_pid:
        raise SystemExit("handled session cleanup requires a dedicated process group")
    try:
        actual_group = os.getpgid(coordinator_pid)
    except ProcessLookupError:
        raise SystemExit("session coordinator disappeared before cleanup")
    if actual_group != pgid or os.getpgrp() != pgid or os.getppid() != coordinator_pid:
        raise SystemExit("refusing cleanup outside the coordinator process group")

    allowed = {coordinator_pid, os.getpid()}
    term_deadline = time.monotonic() + max(grace_sec, 0.0)
    while True:
        others = _process_group_members(pgid) - allowed
        if not others:
            return
        _signal_members(others, signal.SIGTERM)
        if time.monotonic() >= term_deadline:
            break
        time.sleep(0.02)

    kill_deadline = time.monotonic() + 1.0
    while True:
        others = _process_group_members(pgid) - allowed
        if not others:
            return
        _signal_members(others, signal.SIGKILL)
        if time.monotonic() >= kill_deadline:
            break
        time.sleep(0.02)
    remaining = sorted(_process_group_members(pgid) - allowed)
    if remaining:
        raise SystemExit(
            f"process group {pgid} still has live members after cleanup: {remaining}"
        )


def _finalize_interrupted_plan(plan_path: Path) -> bool:
    plan = _read_json(plan_path)
    raw_path = plan.get("record_path")
    token = plan.get("lease_token")
    if not isinstance(raw_path, str) or not isinstance(token, str):
        raise SystemExit(f"invalid interrupted session plan: {plan_path}")
    path = Path(raw_path)
    with _record_guard(path):
        record = _read_json(path)
        # A different token belongs to another process incarnation. Never
        # release it, even if a numeric PID happens to have been reused.
        if record.get("lease_token") != token:
            return False
        pending_mode = record.get("pending_mode")
        for field in (
            "pending_session_id", "pending_mode", "pending_turn_number", "lease_token",
            "lease_owner_pid", "lease_expires_epoch",
        ):
            record.pop(field, None)
        record["last_mode"] = pending_mode
        record["last_success"] = False
        record["last_interrupted"] = True
        record["updated_at"] = _utc_now()
        _atomic_write_json(path, record)
    return True


def cmd_interrupt(args: argparse.Namespace) -> int:
    """Stop one dedicated call group, then release only its exact leases."""

    _stop_dedicated_process_group(
        pgid=args.process_group,
        coordinator_pid=args.coordinator_pid,
        grace_sec=args.grace_sec,
    )
    released = sum(
        _finalize_interrupted_plan(Path(raw_plan))
        for raw_plan in args.plan
    )
    print(released)
    return 0


def _host_boot() -> tuple[str, str]:
    host = socket.gethostname().strip()
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError as exc:
        raise SystemExit(f"cannot verify local boot identity: {exc}")
    if not host or not boot:
        raise SystemExit("cannot verify local host and boot identity")
    return host, boot


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise SystemExit(f"invalid {label}")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise SystemExit(f"invalid {label}")
    if result <= 0:
        raise SystemExit(f"invalid {label}")
    return result


def _identity(value: Any, label: str) -> tuple[int, str, int, str]:
    if not isinstance(value, dict):
        raise SystemExit(f"missing {label} identity")
    pid = _positive_int(value.get("pid"), f"{label} pid")
    pgid = _positive_int(value.get("pgid"), f"{label} pgid")
    start = value.get("start_id")
    token_hash = value.get("run_token_sha256")
    if not isinstance(start, str) or not start.isdigit():
        raise SystemExit(f"invalid {label} start identity")
    if not isinstance(token_hash, str) or re.fullmatch(r"[0-9a-f]{64}", token_hash) is None:
        raise SystemExit(f"invalid {label} run token digest")
    return pid, start, pgid, token_hash


def _recovery_evidence(
    *, plan_path: Path, plan: dict[str, Any], owner_path: Path,
    identities_path: Path, result_path: Path,
) -> tuple[int, str, int, int, str, str]:
    owner_doc = _required_object(owner_path, "guard owner evidence")
    identities_doc = _required_object(identities_path, "guard identities evidence")
    result = _required_object(result_path, "guard result evidence")
    host, boot = _host_boot()
    for label, doc in (("owner", owner_doc), ("identities", identities_doc), ("result", result)):
        if doc.get("hostname") != host or doc.get("boot_id") != boot:
            raise SystemExit(f"guard {label} evidence is from another host or boot")
    guard = _identity(owner_doc.get("owner"), "guard owner")
    if _identity(identities_doc.get("guard_owner"), "guard owner") != guard \
            or _identity(result.get("guard_owner"), "guard owner") != guard:
        raise SystemExit("guard identity documents disagree")
    if result.get("guard_owner_sha256") != _sha256_file(owner_path) \
            or result.get("guard_identities_sha256") != _sha256_file(identities_path):
        raise SystemExit("guard result does not bind its identity evidence")
    if result.get("terminal") is not True or result.get("reason") not in {"quota", "deadline"}:
        raise SystemExit("guard result is not a recoverable terminal")
    returncode = result.get("returncode")
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        raise SystemExit("guard result lacks a terminal return code")
    try:
        empty_scans = int(result.get("consecutive_empty_scans", 0))
    except (TypeError, ValueError):
        empty_scans = 0
    if result.get("survivors") != [] or empty_scans < 2:
        raise SystemExit("guard result does not prove an empty run")

    raw_record = plan.get("record_path")
    token = plan.get("lease_token")
    if not isinstance(raw_record, str) or not isinstance(token, str) or not token:
        raise SystemExit("invalid session plan")
    bindings = result.get("session_leases")
    if not isinstance(bindings, list):
        raise SystemExit("guard result lacks session lease bindings")
    binding = next((row for row in bindings if isinstance(row, dict)
                    and row.get("plan_path") == str(plan_path)
                    and row.get("plan_sha256") == _sha256_file(plan_path)), None)
    if binding is None:
        raise SystemExit("guard result does not bind the session plan")
    if binding.get("record_path") != str(Path(raw_record).expanduser().resolve()) \
            or binding.get("lease_token_sha256") != hashlib.sha256(token.encode()).hexdigest():
        raise SystemExit("guard result does not bind the exact session lease")
    lease = _identity({
        "pid": binding.get("owner_pid"), "start_id": binding.get("owner_start_id"),
        "pgid": binding.get("owner_pgid"), "run_token_sha256": guard[3],
    }, "lease owner")
    identities = identities_doc.get("identities")
    if not isinstance(identities, list) or not any(
        isinstance(row, dict) and _identity(row, "run") == lease for row in identities
    ):
        raise SystemExit("lease owner is absent from the guarded identity inventory")
    events = result.get("events")
    if not isinstance(events, list) or not any(
        isinstance(event, dict) and event.get("pid") == lease[0]
        and event.get("start_id") == lease[1] and event.get("signal") == signal.SIGKILL
        for event in events
    ):
        raise SystemExit("guard result does not prove lease-owner termination")
    _require_absent_identity(guard[0], guard[1], "guard owner")
    _require_absent_identity(lease[0], lease[1], "lease owner")
    _require_empty_process_group(lease[2])
    return lease[0], lease[1], lease[2], guard[0], guard[1], _sha256_file(result_path)


def cmd_recover_interrupted(args: argparse.Namespace) -> int:
    """Release one exact orphan lease proven dead by a same-boot guard."""
    state_dir = Path(args.state_dir).expanduser().resolve()
    plan_path = Path(args.plan).expanduser().resolve()
    plan = _required_object(plan_path, "session plan")
    record_path = Path(str(plan.get("record_path", ""))).expanduser().resolve()
    if record_path.parent != state_dir or record_path.suffix != ".json" \
            or record_path.stem != plan.get("identity_hash"):
        raise SystemExit("session record is outside the canonical state directory")
    owner_path = Path(args.guard_owner).expanduser().resolve()
    identities_path = Path(args.guard_identities).expanduser().resolve()
    result_path = Path(args.guard_result).expanduser().resolve()
    evidence = _recovery_evidence(
        plan_path=plan_path, plan=plan, owner_path=owner_path,
        identities_path=identities_path, result_path=result_path,
    )
    owner_pid, owner_start, owner_pgid, guard_pid, guard_start, result_hash = evidence
    token = str(plan.get("lease_token"))
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with _record_guard(record_path):
        if _required_object(plan_path, "session plan") != plan:
            raise SystemExit("session plan changed during recovery")
        if _recovery_evidence(
            plan_path=plan_path, plan=plan, owner_path=owner_path,
            identities_path=identities_path, result_path=result_path,
        ) != evidence:
            raise SystemExit("guard evidence changed during recovery")
        record = _required_object(record_path, "session record")
        current = record.get("lease_token")
        if current is None and record.get("last_recovered_lease_sha256") == token_hash \
                and record.get("last_recovery_guard_sha256") == result_hash:
            print(0)
            return 0
        if record.get("identity_hash") != plan.get("identity_hash") \
                or current != token or record.get("lease_owner_pid") != owner_pid:
            raise SystemExit("session lease changed before recovery")
        if record.get("pending_mode") != plan.get("mode") \
                or record.get("pending_session_id") != plan.get("session_id") \
                or record.get("pending_turn_number") != plan.get("turn_number"):
            raise SystemExit("session pending state does not match its plan")
        _require_absent_identity(guard_pid, guard_start, "guard owner")
        _require_absent_identity(owner_pid, owner_start, "lease owner")
        _require_empty_process_group(owner_pgid)
        pending_mode = record.get("pending_mode")
        for field in ("pending_session_id", "pending_mode", "pending_turn_number",
                      "lease_token", "lease_owner_pid", "lease_expires_epoch"):
            record.pop(field, None)
        record.update(last_mode=pending_mode, last_success=False, last_interrupted=True,
                      last_recovered_lease_sha256=token_hash,
                      last_recovery_guard_sha256=result_hash, updated_at=_utc_now())
        _atomic_write_json(record_path, record)
    print(1)
    return 0


def _json_values(text: str) -> list[Any]:
    values: list[Any] = []
    stripped = text.strip()
    if stripped:
        try:
            values.append(json.loads(stripped))
        except json.JSONDecodeError:
            pass
    for line in text.splitlines():
        candidate = line.strip()
        if not (candidate.startswith("{") or candidate.startswith("[")):
            continue
        try:
            values.append(json.loads(candidate))
        except json.JSONDecodeError:
            continue
    return values


def _valid_session_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and SESSION_ID_PATTERN.fullmatch(value.strip()) is not None
    )


def _trusted_event_session_id(vendor: str, value: Any) -> str | None:
    """Extract only provider protocol metadata, never nested model/tool data."""

    if not isinstance(value, dict):
        return None
    event_type = value.get("type")
    subtype = value.get("subtype")
    candidate: Any = None
    if vendor == "openai" and event_type == "thread.started":
        candidate = value.get("thread_id")
    elif vendor == "claude" and (
        (event_type == "system" and subtype == "init") or event_type == "result"
    ):
        candidate = value.get("session_id")
    elif vendor == "agy" and event_type in (None, "result"):
        candidate = value.get("conversation_id")
    elif vendor == "cursor" and event_type == "result":
        candidate = value.get("session_id")
    elif vendor == "grok" and event_type == "end":
        candidate = value.get("sessionId")
    if _valid_session_id(candidate):
        return candidate.strip()
    return None


def _observe_session_id(
    *, vendor: str, output_text: str, transcript_text: str,
) -> tuple[str | None, str | None]:
    # Prefer the protocol transcript: normalized output may intentionally
    # contain only the assistant's prose.
    for source, text in (("transcript", transcript_text), ("output", output_text)):
        for value in _json_values(text):
            found = _trusted_event_session_id(vendor, value)
            if found:
                return found, f"{vendor}_{source}_json"
    return None, None


def cmd_observe(args: argparse.Namespace) -> int:
    output_path = Path(args.output_file)
    transcript_path = Path(args.transcript_file)
    try:
        output_text = output_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        output_text = ""
    try:
        transcript_text = transcript_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        transcript_text = ""

    session_id, source = _observe_session_id(
        vendor=args.vendor.strip().lower(),
        output_text=output_text,
        transcript_text=transcript_text,
    )
    requested_session_id = args.requested_session_id.strip()
    if (
        session_id
        and requested_session_id
        and session_id != requested_session_id
    ):
        raise SystemExit(
            "provider returned a native session id different from the requested id"
        )
    if (
        not session_id
        and args.exit_code == 0
        and _valid_session_id(requested_session_id)
    ):
        session_id = requested_session_id
        source = "caller_preassigned"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "provider": args.vendor.strip().lower(),
        "available": bool(session_id),
        "session_id": session_id,
        "requested_mode": args.mode or None,
        "source": source,
    }
    _atomic_write_json(Path(args.output), payload)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan")
    plan.add_argument("--state-dir", required=True)
    plan.add_argument("--key", required=True)
    plan.add_argument("--vendor", required=True)
    plan.add_argument("--model", default="")
    plan.add_argument("--cwd", default="")
    plan.add_argument("--transport-arg", action="append", default=[])
    plan.add_argument("--owner-pid", type=int, required=True)
    plan.add_argument("--lease-sec", type=int, default=28860)
    plan.add_argument("--max-turns", type=int, default=0)
    plan.add_argument("--output", required=True)
    plan.set_defaults(func=cmd_plan)

    field = sub.add_parser("field")
    field.add_argument("--plan", required=True)
    field.add_argument("--name", required=True)
    field.set_defaults(func=cmd_field)

    finalize = sub.add_parser("finalize")
    finalize.add_argument("--plan", required=True)
    finalize.add_argument("--success", action="store_true")
    finalize.add_argument("--invalidate", action="store_true")
    finalize.add_argument("--observed-session-id", default="")
    finalize.set_defaults(func=cmd_finalize)

    reset = sub.add_parser("reset")
    reset.add_argument("--state-dir", required=True)
    reset.add_argument("--key", required=True)
    reset.set_defaults(func=cmd_reset)

    interrupt = sub.add_parser("interrupt")
    interrupt.add_argument("--process-group", type=int, required=True)
    interrupt.add_argument("--coordinator-pid", type=int, required=True)
    interrupt.add_argument("--grace-sec", type=float, default=1.0)
    interrupt.add_argument("--plan", action="append", default=[])
    interrupt.set_defaults(func=cmd_interrupt)

    recover = sub.add_parser("recover-interrupted")
    recover.add_argument("--state-dir", required=True)
    recover.add_argument("--plan", required=True)
    recover.add_argument("--guard-owner", required=True)
    recover.add_argument("--guard-identities", required=True)
    recover.add_argument("--guard-result", required=True)
    recover.set_defaults(func=cmd_recover_interrupted)

    observe = sub.add_parser("observe")
    observe.add_argument("--vendor", required=True)
    observe.add_argument("--output-file", required=True)
    observe.add_argument("--transcript-file", required=True)
    observe.add_argument("--requested-session-id", default="")
    observe.add_argument("--mode", default="")
    observe.add_argument("--exit-code", type=int, required=True)
    observe.add_argument("--output", required=True)
    observe.set_defaults(func=cmd_observe)
    return parser


def main() -> int:
    args = _parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
