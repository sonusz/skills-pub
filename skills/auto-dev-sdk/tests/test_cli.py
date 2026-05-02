"""ad-10, ad-11: CLI smoke tests."""
from __future__ import annotations

from auto_dev import exit_codes
from auto_dev.cli import main
from auto_dev.stages.paths import FeaturePaths
from auto_dev.vendors.config import STAGES


def _write_vendors_yml(repo_root, path=None):
    path = path or (repo_root / "vendors.yml")
    body = "\n".join(f"{s}:\n  vendor: fake\n  model: fake-{s}" for s in STAGES)
    path.write_text(body)
    return path


def test_cli_status_missing_feature(tmp_path, capsys):
    (tmp_path / "docs" / "features").mkdir(parents=True)
    code = main(["status", "ghost", "--repo-root", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == exit_codes.OK
    assert "not found" in out


def test_cli_prd_from_file_imports(tmp_path, capsys):
    (tmp_path / "docs" / "features").mkdir(parents=True)
    src = tmp_path / "draft.md"
    src.write_text("# PRD\n\n## Problem\np\n\n## Users\nu\n\n## Requirements\nr\n\n## Constraints\nc\n\n## Success\ns\n\n## Out of scope\no\n")
    code = main(["prd", "demo", "--from-file", str(src), "--repo-root", str(tmp_path)])
    assert code == exit_codes.OK
    planned = tmp_path / "docs" / "features" / "demo" / "planned" / "prd.md"
    assert planned.exists()


def test_cli_approve(tmp_path):
    (tmp_path / "docs" / "features").mkdir(parents=True)
    fp = FeaturePaths(repo_root=tmp_path, feature="demo")
    active = fp.active()
    active.mkdir(parents=True)
    from auto_dev.stages import gates

    gates.request(active, "prd-review")
    _write_vendors_yml(tmp_path)
    code = main(["approve", "demo", "prd-review", "--repo-root", str(tmp_path)])
    assert code == exit_codes.OK
    assert gates.is_approved(active, "prd-review")


def test_cli_abort_records_interrupted(tmp_path):
    (tmp_path / "docs" / "features").mkdir(parents=True)
    fp = FeaturePaths(repo_root=tmp_path, feature="demo")
    active = fp.active()
    active.mkdir(parents=True)
    code = main(["abort", "demo", "--repo-root", str(tmp_path)])
    assert code == exit_codes.OK
    assert (active / "interrupted.json").exists()
