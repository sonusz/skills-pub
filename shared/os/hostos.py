"""Host operating-system detection: the one Python definition for this repo.

``host_os()`` returns ``"linux"``, ``"darwin"``, or the raw ``sys.platform``
for anything else. Callers branch explicitly on the result and run that OS's
command; they never probe for ``/proc`` and never fall through from one OS's
method to another's.

Consumers load this file by path with ``importlib`` through their skill's
``shared/os`` link and keep an identical inline copy as the fallback for a
materialized install shipped without ``shared/os``:
``shared/vendors/scripts/session-state.py`` (``_host_os``) and
``skills/auto-dev-sdk/autodev/state/hostos.py`` (``_host_os``).
The shell twin is ``host-os.sh`` in this directory.
"""
from __future__ import annotations

import sys


def host_os() -> str:
    """'linux', 'darwin', or the raw sys.platform for anything else."""
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    return sys.platform


if __name__ == "__main__":
    print(host_os())
