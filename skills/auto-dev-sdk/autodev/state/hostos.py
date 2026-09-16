"""Host operating-system detection for OS-specific process primitives.

Callers branch explicitly on the result (``linux`` / ``darwin`` / other)
and run that OS's command, instead of probing for ``/proc`` and falling
through. The one definition is ``shared/os/hostos.py``, reached through the
``shared/os`` link at the SDK root; ``shared/vendors/scripts/session-state.py``
delegates to the same file. A copy installed without ``shared/os`` keeps the
inline fallback in ``_host_os``, which implements the identical contract.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

SDK_ROOT = Path(__file__).resolve().parents[2]
_SHARED_HOSTOS = SDK_ROOT / "shared" / "os" / "hostos.py"
_shared_hostos: ModuleType | None = None
_shared_hostos_loaded = False


def _shared_hostos_module() -> ModuleType | None:
    """Load shared/os/hostos.py by path once; None when the link is absent."""
    global _shared_hostos, _shared_hostos_loaded
    if not _shared_hostos_loaded:
        _shared_hostos_loaded = True
        if _SHARED_HOSTOS.is_file():
            spec = importlib.util.spec_from_file_location("autodev_shared_hostos", _SHARED_HOSTOS)
            if spec is not None and spec.loader is not None:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                _shared_hostos = module
    return _shared_hostos


def _host_os() -> str:
    """'linux', 'darwin', or the raw sys.platform for anything else."""
    shared = _shared_hostos_module()
    if shared is not None:
        return shared.host_os()
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    return sys.platform
