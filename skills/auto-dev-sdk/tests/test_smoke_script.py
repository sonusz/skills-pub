"""ad-13: smoke_adapters.py skip/fail behavior (no live API calls)."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

from auto_dev.vendors.config import STAGES

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "smoke_adapters.py"


def _write_vendors(tmp_path, vendor="anthropic", model="claude-opus-4-7"):
    p = tmp_path / "vendors.yml"
    body = "\n".join(f"{s}:\n  vendor: {vendor}\n  model: {model}" for s in STAGES)
    p.write_text(body)
    return p


def _run_script(args, monkeypatch, env=None):
    """Invoke the script like a CLI; return exit code."""
    monkeypatch.setattr(sys, "argv", ["smoke_adapters.py", *args])
    if env is not None:
        for k, v in env.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)
    try:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    except SystemExit as e:
        return e.code or 0
    return 0


def test_missing_vendors_yml_returns_1(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "nope.yml"
    code = _run_script(["--vendors-yml", str(missing)], monkeypatch)
    assert code == 1


def test_skip_when_no_credentials(tmp_path, monkeypatch, capsys):
    yml = _write_vendors(tmp_path, vendor="anthropic")
    code = _run_script(
        ["--vendors-yml", str(yml)],
        monkeypatch,
        env={"ANTHROPIC_API_KEY": None},
    )
    # No creds → skip → exit 0.
    assert code == 0
    err = capsys.readouterr().err
    assert "SKIP" in err
    assert "anthropic" in err


def test_credentialed_failure_exits_non_zero(tmp_path, monkeypatch, capsys):
    """When credentials are set but the adapter raises, we exit 1."""
    yml = _write_vendors(tmp_path, vendor="anthropic")

    # Force adapter init failure by pointing the SDK lookup at a stub.
    import auto_dev.vendors.registry as registry
    from auto_dev.vendors.base import VendorAdapter

    class BoomAdapter(VendorAdapter):
        name = "anthropic"

        def run_subagent(self, **_):
            raise RuntimeError("simulated api outage")

    # Pre-register so _load_builtins is skipped for this vendor.
    registry._REGISTRY["anthropic"] = BoomAdapter  # type: ignore[assignment]
    try:
        code = _run_script(
            ["--vendors-yml", str(yml)],
            monkeypatch,
            env={"ANTHROPIC_API_KEY": "sk-test"},
        )
    finally:
        # Restore real registry so later tests don't see the stub.
        registry._REGISTRY.pop("anthropic", None)

    err = capsys.readouterr().err
    assert code == 1
    assert "FAIL" in err
    assert "simulated api outage" in err
