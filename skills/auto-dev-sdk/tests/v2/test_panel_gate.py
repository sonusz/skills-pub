"""v2-9/10/11 + G15: panel gate driver — verdict produced via internal
runner + optional standalone-binary escape hatch."""
from __future__ import annotations

import os
from pathlib import Path

from autodev.panel import prompt_path, run_panel_gate


def test_prompt_paths_exist():
    for g in ("design-review", "close-approval"):
        p = prompt_path(g)
        assert p.exists(), f"prompt for {g} missing at {p}"


def test_panel_with_fake_standalone_binary(git_repo, feature_active, tmp_path, monkeypatch):
    """AUTODEV_PANEL_REVIEW_BIN escape hatch: if a standalone binary emits
    a valid verdict, prefer it (upstream-conformance path)."""
    fake = tmp_path / "panel-review-fake.sh"
    fake.write_text(
        "#!/bin/bash\n"
        "out=\"\"\n"
        "while [[ $# -gt 0 ]]; do\n"
        "  if [[ \"$1\" == \"--out\" ]]; then out=\"$2\"; shift 2;\n"
        "  elif [[ \"$1\" == \"--artifact\" ]]; then artifact=\"$2\"; shift 2;\n"
        "  elif [[ \"$1\" == \"--gate\" ]]; then gate=\"$2\"; shift 2;\n"
        "  elif [[ \"$1\" == \"--prompt-file\" ]]; then prompt=\"$2\"; shift 2;\n"
        "  else shift; fi\n"
        "done\n"
        # sha256sum is GNU coreutils; stock macOS only ships shasum.
        "hash_file() {\n"
        "  case \"$(uname -s)\" in\n"
        "    Darwin) shasum -a 256 \"$1\" | awk '{print $1}';;\n"
        "    *) sha256sum \"$1\" | awk '{print $1}';;\n"
        "  esac\n"
        "}\n"
        "hash_src=$(hash_file \"$artifact\")\n"
        "hash_prompt=$(hash_file \"$prompt\")\n"
        "cat > \"$out\" <<EOF\n"
        "{\"gate\":\"$gate\",\"verdict\":\"pass\",\"findings\":[],"
        "\"source\":\"$artifact\",\"source_hash\":\"sha256:$hash_src\","
        "\"prompt_file\":\"$prompt\",\"prompt_hash\":\"sha256:$hash_prompt\","
        "\"harness_version\":\"test\",\"run_ts\":\"2026-04-20T00:00:00Z\"}\n"
        "EOF\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("AUTODEV_PANEL_REVIEW_BIN", str(fake))

    design = feature_active / "design.md"
    design.write_text("# design content\n")
    v = run_panel_gate(
        gate="design-review",
        feature_active=feature_active,
        repo_root=git_repo,
        feature="demo",
        primary_artifact=design,
    )
    assert v.verdict == "pass"
    assert not v.effectively_blocks()
