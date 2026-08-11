"""Built-in stdout watch protocol for background autodev runs.

The harness emits a start marker, periodic heartbeats, and exactly one terminal
marker. A generic outer Monitor can therefore alert on either a non-success
terminal outcome or two missed heartbeat intervals without inventing polling
logic per feature.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable

DEFAULT_HEARTBEAT_SEC = 60.0
HEARTBEAT_ENV = "AUTODEV_WATCH_HEARTBEAT_SEC"

_OUTPUT_LOCK = threading.Lock()


def heartbeat_interval() -> float:
    """Return the configured heartbeat interval, failing safe to default."""

    raw = os.environ.get(HEARTBEAT_ENV, "")
    if not raw:
        return DEFAULT_HEARTBEAT_SEC
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_HEARTBEAT_SEC
    return value if value > 0 else DEFAULT_HEARTBEAT_SEC


def _emit(event: str, **fields: object) -> None:
    parts = ["[autodev:watch]", event, "protocol=1"]
    parts.extend(f"{key}={value}" for key, value in fields.items())
    try:
        with _OUTPUT_LOCK:
            sys.stdout.write(" ".join(parts) + "\n")
            sys.stdout.flush()
    except (OSError, ValueError):
        # Watch output is observability only and must never break the run.
        pass


def _outcome(exit_code: int) -> str:
    return {
        0: "complete",
        1: "error",
        2: "gate_pending",
        3: "lock_conflict",
    }.get(exit_code, "error")


class WatchSession:
    """Lifecycle markers and heartbeat thread for one watched CLI command."""

    def __init__(
        self,
        *,
        feature: str,
        verb: str,
        interval_sec: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.feature = feature
        self.verb = verb
        configured = heartbeat_interval() if interval_sec is None else interval_sec
        self.interval_sec = configured if configured > 0 else heartbeat_interval()
        self._monotonic = monotonic
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None
        self._finished = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._started_at = self._monotonic()
        _emit(
            "started",
            feature=self.feature,
            verb=self.verb,
            heartbeat_sec=f"{self.interval_sec:g}",
        )
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"autodev-watch-{self.feature}",
            daemon=True,
        )
        self._thread.start()

    def _elapsed_sec(self) -> int:
        if self._started_at is None:
            return 0
        return max(0, int(self._monotonic() - self._started_at))

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.interval_sec):
            _emit(
                "heartbeat",
                feature=self.feature,
                verb=self.verb,
                elapsed_sec=self._elapsed_sec(),
            )

    def finish(self, exit_code: int) -> None:
        if self._finished:
            return
        self._finished = True
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        _emit(
            "terminal",
            feature=self.feature,
            verb=self.verb,
            outcome=_outcome(exit_code),
            exit_code=exit_code,
            elapsed_sec=self._elapsed_sec(),
        )
