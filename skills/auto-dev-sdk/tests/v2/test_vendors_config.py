"""v2-5: vendors.yml loader + per-vendor allowlist (R3 / Codex R4 fix)."""
from __future__ import annotations

import pytest

from autodev.errors import ConfigError, VendorNotAllowed
from autodev.vendors.config import STAGES, load_vendors_config
from autodev.vendors.shared_call import split_common_vendor_flags


def _write(tmp_path, body):
    p = tmp_path / "vendors.yml"
    p.write_text(body)
    return p


def _happy_stages():
    return {
        "design": {"vendor": "claude", "model": "fake-design"},
        "build":  {"vendor": "claude", "model": "fake-build"},
        "spec":   {"vendor": "codex",  "model": "fake-spec"},
        "review": {"vendor": "claude", "model": "fake-review"},
    }


def _happy_panel():
    return {
        "reviewers": [
            {"vendor": "claude", "model": "fake-panel-claude"},
            {"vendor": "gemini", "model": "fake-panel-gemini"},
            {"vendor": "codex", "model": "fake-panel-codex"},
        ],
        "synthesizer": {"vendor": "claude", "model": "fake-panel-synth"},
    }


def _happy_probe():
    return {"vendor": "claude", "model": "fake-probe", "timeout_sec": 60}


def _happy_doc(*, stages=None, panel=None, probe=None):
    return {
        "stages": stages if stages is not None else _happy_stages(),
        "panel": panel if panel is not None else _happy_panel(),
        "probe": probe if probe is not None else _happy_probe(),
    }


def _yaml_dump(obj):
    import yaml
    return yaml.safe_dump(obj)


def test_load_happy_path(tmp_path):
    body = _yaml_dump(_happy_doc())
    cfg = load_vendors_config(_write(tmp_path, body))
    s = cfg.resolve("build")
    assert s.vendor == "claude"
    assert s.probe_interval_sec > 0
    assert cfg.probe.vendor == "claude"
    assert cfg.panel.synthesizer.model == "fake-panel-synth"


def test_vendor_labels_are_case_insensitive(tmp_path):
    stages = _happy_stages()
    stages["design"]["vendor"] = "Claude"
    panel = _happy_panel()
    panel["reviewers"][2]["vendor"] = "OpenAI"
    probe = {"vendor": "OPENAI", "model": "fake-probe", "timeout_sec": 60}
    cfg = load_vendors_config(
        _write(tmp_path, _yaml_dump(_happy_doc(stages=stages, panel=panel, probe=probe)))
    )
    assert cfg.resolve("design").vendor == "claude"
    assert cfg.panel.reviewers[2].vendor == "openai"
    assert cfg.probe.vendor == "openai"


def test_missing_stages_key(tmp_path):
    body = _yaml_dump({"design": {"vendor": "claude", "model": "m"}})
    with pytest.raises(ConfigError):
        load_vendors_config(_write(tmp_path, body))


def test_missing_one_stage(tmp_path):
    stages = _happy_stages()
    del stages["review"]
    with pytest.raises(ConfigError):
        load_vendors_config(_write(tmp_path, _yaml_dump(_happy_doc(stages=stages))))


def test_unknown_vendor_rejected(tmp_path):
    stages = _happy_stages()
    stages["design"]["vendor"] = "gemini"  # not allowed for coding
    with pytest.raises(ConfigError):
        load_vendors_config(_write(tmp_path, _yaml_dump(_happy_doc(stages=stages))))


def test_flags_allowlisted_accepted(tmp_path):
    stages = _happy_stages()
    stages["build"]["flags"] = ["--effort", "high"]  # claude allowlist
    cfg = load_vendors_config(_write(tmp_path, _yaml_dump(_happy_doc(stages=stages))))
    assert cfg.resolve("build").flags == ("--effort", "high")


def test_effort_fields_loaded_and_normalized(tmp_path):
    stages = _happy_stages()
    stages["design"]["effort"] = "MAX"
    panel = _happy_panel()
    panel["reviewers"][0]["effort"] = "x-high"
    panel["synthesizer"]["effort"] = "high"
    probe = {"vendor": "claude", "model": "fake-probe", "effort": "minimum"}

    cfg = load_vendors_config(
        _write(tmp_path, _yaml_dump(_happy_doc(stages=stages, panel=panel, probe=probe)))
    )

    assert cfg.resolve("design").effort == "max"
    assert cfg.panel.reviewers[0].effort == "xhigh"
    assert cfg.panel.synthesizer.effort == "high"
    assert cfg.probe.effort == "min"


def test_stage_probe_interval_loaded(tmp_path):
    stages = _happy_stages()
    stages["build"]["probe_interval_sec"] = 123
    cfg = load_vendors_config(_write(tmp_path, _yaml_dump(_happy_doc(stages=stages))))
    assert cfg.resolve("build").probe_interval_sec == 123


def test_stage_legacy_timeout_auto_migrates(tmp_path, capsys):
    """Legacy `timeout_sec` is auto-migrated to `probe_interval_sec` with a
    stderr warning. Run continues — users can rename at their convenience."""
    stages = _happy_stages()
    stages["build"]["timeout_sec"] = 123
    cfg = load_vendors_config(_write(tmp_path, _yaml_dump(_happy_doc(stages=stages))))
    assert cfg.resolve("build").probe_interval_sec == 123
    err = capsys.readouterr().err
    assert "stages.build.timeout_sec" in err
    assert "deprecated" in err.lower()


def test_panel_probe_intervals_loaded(tmp_path):
    panel = _happy_panel()
    panel["reviewer_probe_interval_sec"] = 321
    panel["synthesizer_probe_interval_sec"] = 123
    cfg = load_vendors_config(_write(tmp_path, _yaml_dump(_happy_doc(panel=panel))))
    assert cfg.panel.reviewer_probe_interval_sec == 321
    assert cfg.panel.synthesizer_probe_interval_sec == 123


def test_panel_legacy_timeout_auto_migrates(tmp_path, capsys):
    """Legacy `reviewer_timeout_sec` / `synthesizer_timeout_sec` are
    auto-migrated to their probe_interval_sec equivalents with a warning."""
    panel = _happy_panel()
    panel["reviewer_timeout_sec"] = 321
    panel["synthesizer_timeout_sec"] = 222
    cfg = load_vendors_config(_write(tmp_path, _yaml_dump(_happy_doc(panel=panel))))
    assert cfg.panel.reviewer_probe_interval_sec == 321
    assert cfg.panel.synthesizer_probe_interval_sec == 222
    err = capsys.readouterr().err
    assert "panel.reviewer_timeout_sec" in err
    assert "panel.synthesizer_timeout_sec" in err


def test_invalid_effort_rejected(tmp_path):
    stages = _happy_stages()
    stages["design"]["effort"] = "heroic"
    with pytest.raises(ConfigError, match="effort"):
        load_vendors_config(_write(tmp_path, _yaml_dump(_happy_doc(stages=stages))))


def test_effort_field_conflicts_with_legacy_effort_flag(tmp_path):
    stages = _happy_stages()
    stages["build"]["effort"] = "high"
    stages["build"]["flags"] = ["--effort", "max"]
    with pytest.raises(ConfigError, match="both `effort`"):
        load_vendors_config(_write(tmp_path, _yaml_dump(_happy_doc(stages=stages))))


def test_legacy_effort_flags_preserve_full_shared_scale():
    effort, model, native = split_common_vendor_flags(["--effort", "x-high"])
    assert effort == "xhigh"
    assert model is None
    assert native == []


def test_flags_non_allowlisted_rejected(tmp_path):
    stages = _happy_stages()
    # --allowedTools is harness-owned; users cannot override
    stages["build"]["flags"] = ["--allowedTools", "Bash,Network"]
    with pytest.raises(VendorNotAllowed):
        load_vendors_config(_write(tmp_path, _yaml_dump(_happy_doc(stages=stages))))


def test_flags_widening_attempt_rejected(tmp_path):
    stages = _happy_stages()
    # --add-dir is harness-owned (write scope control)
    stages["design"]["flags"] = ["--add-dir", "/etc"]
    with pytest.raises(VendorNotAllowed):
        load_vendors_config(_write(tmp_path, _yaml_dump(_happy_doc(stages=stages))))


def test_flags_codex_allowlist(tmp_path):
    stages = _happy_stages()
    stages["spec"]["flags"] = ["--disable", "some-feature"]  # codex allowlist
    cfg = load_vendors_config(_write(tmp_path, _yaml_dump(_happy_doc(stages=stages))))
    assert cfg.resolve("spec").flags[0] == "--disable"


def test_probe_config_loaded_from_top_level(tmp_path):
    body = _yaml_dump(_happy_doc(
        probe={
            "vendor": "codex",
            "model": "fake-probe-codex",
            "timeout_sec": 45,
            "flags": ["-c", "reasoning_effort=\"low\""],
        },
    ))
    cfg = load_vendors_config(_write(tmp_path, body))
    assert cfg.probe.vendor == "codex"
    assert cfg.probe.model == "fake-probe-codex"
    assert cfg.probe.timeout_sec == 45
    assert cfg.probe.flags == ("-c", "reasoning_effort=\"low\"")


def test_probe_unknown_vendor_rejected(tmp_path):
    body = _yaml_dump(_happy_doc(
        probe={"vendor": "gemini", "model": "fake-probe-gemini"},
    ))
    with pytest.raises(ConfigError):
        load_vendors_config(_write(tmp_path, body))


def test_missing_panel_rejected(tmp_path):
    body = _yaml_dump({"stages": _happy_stages(), "probe": _happy_probe()})
    with pytest.raises(ConfigError, match="panel"):
        load_vendors_config(_write(tmp_path, body))


def test_missing_probe_rejected(tmp_path):
    body = _yaml_dump({"stages": _happy_stages(), "panel": _happy_panel()})
    with pytest.raises(ConfigError, match="probe"):
        load_vendors_config(_write(tmp_path, body))
