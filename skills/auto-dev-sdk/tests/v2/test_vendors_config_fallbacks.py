"""vendors.yml quota-gate schema: min_quota_pct + fallbacks on all roles."""
from __future__ import annotations

import tempfile
import textwrap
from pathlib import Path

import pytest

from autodev.errors import ConfigError
from autodev.vendors.config import load_vendors_config


def _load(yml: str):
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
        f.write(textwrap.dedent(yml))
        path = f.name
    return load_vendors_config(Path(path))


_GOOD = """
stages:
  design: {vendor: claude, model: claude-fable-5, effort: max, min_quota_pct: 20,
           fallbacks: [{vendor: cursor, model: gpt-5.6-sol-xhigh, effort: max, min_quota_pct: 15},
                       {vendor: codex, model: gpt-5.6-sol, min_quota_pct: 10}]}
  build: {vendor: cursor, model: gpt-5.6-sol-xhigh, effort: max}
  review: {vendor: claude, model: claude-fable-5, effort: high}
  spec: {vendor: cursor, model: gpt-5.6-sol-xhigh, effort: medium}
panel:
  reviewers:
    - {vendor: claude, model: claude-fable-5, effort: max, min_quota_pct: 25,
       fallbacks: [{vendor: cursor, model: gemini-3.1-pro, min_quota_pct: 10}]}
    - {vendor: cursor, model: gemini-3.1-pro, effort: max}
  synthesizer: {vendor: claude, model: claude-sonnet-5, effort: high, min_quota_pct: 30,
                fallbacks: [{vendor: codex, model: gpt-5.6-sol, min_quota_pct: 10}]}
probe: {vendor: claude, model: claude-haiku-4-5, effort: low, min_quota_pct: 5,
        fallbacks: [{vendor: codex, model: gpt-5.6-luna, min_quota_pct: 5}]}
"""


def test_parses_all_roles():
    c = _load(_GOOD)
    d = c.stages["design"]
    assert d.min_quota_pct == 20.0
    assert [(f.vendor, f.min_quota_pct) for f in d.fallbacks] == [("cursor", 15.0), ("codex", 10.0)]
    assert c.panel.reviewers[0].min_quota_pct == 25.0
    assert [f.vendor for f in c.panel.reviewers[0].fallbacks] == ["cursor"]
    assert c.panel.synthesizer.min_quota_pct == 30.0
    assert c.panel.min_responding_reviewers == 2
    assert [f.vendor for f in c.panel.synthesizer.fallbacks] == ["codex"]
    assert c.probe.min_quota_pct == 5.0
    assert [f.vendor for f in c.probe.fallbacks] == ["codex"]


def test_backward_compatible_without_quota_fields():
    c = _load(
        """
        stages:
          design: {vendor: claude, model: m}
          build: {vendor: claude, model: m}
          review: {vendor: claude, model: m}
          spec: {vendor: claude, model: m}
        panel:
          reviewers:
            - {vendor: claude, model: m}
            - {vendor: agy, model: m}
          synthesizer: {vendor: claude, model: m}
        probe: {vendor: claude, model: m}
        """
    )
    assert c.stages["design"].min_quota_pct is None
    assert c.stages["design"].fallbacks == ()
    assert c.panel.min_responding_reviewers == 2


@pytest.mark.parametrize("value", [0, 1, 3, True])
def test_rejects_invalid_panel_response_quorum(value):
    with pytest.raises(ConfigError, match="min_responding_reviewers"):
        _load(
            _GOOD.replace(
                "panel:\n",
                f"panel:\n  min_responding_reviewers: {str(value).lower()}\n",
                1,
            )
        )


_BASE = (
    "stages:\n"
    "  design: {{vendor: claude, model: m{design}}}\n"
    "  build: {{vendor: claude, model: m}}\n"
    "  review: {{vendor: claude, model: m}}\n"
    "  spec: {{vendor: claude, model: m}}\n"
    "panel:\n"
    "  reviewers:\n"
    "    - {{vendor: claude, model: m{rev}}}\n"
    "    - {{vendor: agy, model: m}}\n"
    "  synthesizer: {{vendor: claude, model: m{syn}}}\n"
    "probe: {{vendor: claude, model: m}}\n"
)


@pytest.mark.parametrize(
    "design,rev,syn,why",
    [
        (", min_quota_pct: 150", "", "", "min_quota>100"),
        (", min_quota_pct: -1", "", "", "min_quota<0"),
        (", fallbacks: [{vendor: agy, model: g}]", "", "", "stage fallback agy (panel-only)"),
        ("", ", fallbacks: [{vendor: claude, model: m2}]", "", "reviewer same-provider fallback"),
        ("", "", ", fallbacks: [{vendor: agy, model: g}]", "synth fallback not schema-capable"),
    ],
)
def test_rejections(design, rev, syn, why):
    with pytest.raises(ConfigError):
        _load(_BASE.format(design=design, rev=rev, syn=syn))
