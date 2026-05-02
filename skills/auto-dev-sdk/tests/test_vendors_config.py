"""ad-5: vendors.yml loader."""
from __future__ import annotations

import pytest

from auto_dev.errors import ConfigError
from auto_dev.vendors.config import STAGES, load_vendors_config


def _write(tmp_path, yaml_text):
    p = tmp_path / "vendors.yml"
    p.write_text(yaml_text)
    return p


def test_load_ok(tmp_path):
    yaml_text = "\n".join(
        f"{s}:\n  vendor: anthropic\n  model: m" for s in STAGES
    )
    cfg = load_vendors_config(_write(tmp_path, yaml_text))
    spec = cfg.resolve("plan")
    assert spec.vendor == "anthropic"
    assert spec.model == "m"


def test_missing_stage(tmp_path):
    yaml_text = "plan:\n  vendor: anthropic\n  model: m\n"
    with pytest.raises(ConfigError):
        load_vendors_config(_write(tmp_path, yaml_text))


def test_delegated_stage_cannot_resolve(tmp_path):
    yaml_text = "\n".join(f"{s}:\n  vendor: anthropic\n  model: m" for s in STAGES)
    cfg = load_vendors_config(_write(tmp_path, yaml_text))
    with pytest.raises(ConfigError):
        cfg.resolve("panel_review")


def test_malformed_stage_entry(tmp_path):
    yaml_text = "\n".join(f"{s}:\n  vendor: anthropic\n  model: m" for s in STAGES if s != "plan")
    yaml_text += "\nplan:\n  vendor: 42\n  model: m\n"
    with pytest.raises(ConfigError):
        load_vendors_config(_write(tmp_path, yaml_text))


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError):
        load_vendors_config(tmp_path / "does-not-exist.yml")
