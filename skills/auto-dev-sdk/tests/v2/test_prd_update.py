"""`autodev update --from-file` — replace-in-place + independent history.

Covers detail doc §7.1: successful replace (cycle advance, history dir,
log detail), empty-diff / lint-failure rejection, R-numbering stability
(retired-number reuse, non-monotonic new numbers, renumbering by title or
body swap), implicit deletion, history-dir contents, missing/removed CLI
flags, `autodev prd`'s refusal to overwrite an existing `prd.md`, `status`
surfacing prd-history, and back-compat `## Amendment` lint warnings.
"""
from __future__ import annotations

import json

import pytest

from autodev import exit_codes
from autodev.cli import main
from autodev.prd_intake import validate_prd_text


def _reqs(items: dict[int, tuple[str, str]]) -> str:
    parts = []
    for n in sorted(items):
        title, body = items[n]
        parts.append(f"### R{n}: {title}\n{body}\n")
    return "\n".join(parts) + "\n"


def _prd(items: dict[int, tuple[str, str]], *, extra: str = "") -> str:
    return (
        "# PRD: demo\n\n"
        "## Problem\np\n\n"
        "## Users\nu\n\n"
        "## Requirements\n\n"
        f"{_reqs(items)}"
        "## Constraints\nc\n\n"
        "## Success criteria\ns\n\n"
        "## Out of scope\no\n"
        f"{extra}"
    )


BASE_REQS = {
    1: ("Alpha", "alpha body"),
    2: ("Beta", "beta body"),
    3: ("Gamma", "gamma body"),
    4: ("Delta", "delta body"),
}
BASE_PRD = _prd(BASE_REQS)


def _write_base(feature_active, reqs=None):
    (feature_active / "prd.md").write_text(_prd(reqs or BASE_REQS), encoding="utf-8")


def _write_source(tmp_path, name, items, *, extra=""):
    p = tmp_path / name
    p.write_text(_prd(items, extra=extra), encoding="utf-8")
    return p


# ---------------------------------------------------------------------
# Successful replace
# ---------------------------------------------------------------------


def test_update_replaces_prd_and_advances_cycle(git_repo, feature_active, tmp_path):
    _write_base(feature_active)
    (feature_active / "diagnosis.json").write_text("{}", encoding="utf-8")
    (feature_active / "rework-mode.json").write_text("{}", encoding="utf-8")

    from autodev.artifacts.revision_state import ALL_PANEL_GATES, load_state, write_state
    s = load_state(feature_active)
    s.L = {g: 3 for g in ALL_PANEL_GATES}
    write_state(feature_active, s)

    new_reqs = dict(BASE_REQS)
    new_reqs[2] = ("Beta", "beta body v2 — reworded")
    src = _write_source(tmp_path, "new.md", new_reqs)

    code = main([
        "update", "demo", "--from-file", str(src), "--repo-root", str(git_repo),
    ])
    assert code == exit_codes.OK

    assert (feature_active / "prd.md").read_text(encoding="utf-8") == src.read_text(
        encoding="utf-8"
    )

    from autodev import overrides_api as ov
    assert ov.load(feature_active).current_cycle == 2

    s2 = load_state(feature_active)
    assert all(v == 0 for v in s2.L.values())

    assert not (feature_active / "diagnosis.json").exists()
    assert not (feature_active / "rework-mode.json").exists()

    log_path = feature_active / "log.jsonl"
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    last = json.loads(lines[-1])
    assert last["event"] == "prd-amended"
    detail = last["detail"]
    assert detail["removed"] == []
    assert detail["added"] == []
    assert detail["changed"] == ["R2"]
    assert "summary" in detail and "changed R2" in detail["summary"]
    assert "snapshot" in detail and "diff" in detail
    assert detail["history_dir"] == "docs/features/demo/active/prd-history"


def test_update_empty_diff_rejected(git_repo, feature_active, tmp_path):
    _write_base(feature_active)
    src = _write_source(tmp_path, "same.md", BASE_REQS)
    before = (feature_active / "prd.md").read_text(encoding="utf-8")

    from autodev import overrides_api as ov
    before_cycle = ov.load(feature_active).current_cycle

    code = main([
        "update", "demo", "--from-file", str(src), "--repo-root", str(git_repo),
    ])
    assert code == exit_codes.ERROR
    assert (feature_active / "prd.md").read_text(encoding="utf-8") == before
    assert ov.load(feature_active).current_cycle == before_cycle
    assert not (feature_active / "prd-history").exists()
    assert not (feature_active / "log.jsonl").exists()


def test_update_lint_failure_rejected(git_repo, feature_active, tmp_path):
    _write_base(feature_active)
    before = (feature_active / "prd.md").read_text(encoding="utf-8")
    src = tmp_path / "broken.md"
    src.write_text("# PRD\n## Problem\np\n", encoding="utf-8")  # missing sections

    code = main([
        "update", "demo", "--from-file", str(src), "--repo-root", str(git_repo),
    ])
    assert code == exit_codes.ERROR
    assert (feature_active / "prd.md").read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------
# R-numbering stability
# ---------------------------------------------------------------------


def test_update_reuses_retired_number_rejected(
    git_repo, feature_active, tmp_path, capsys,
):
    _write_base(feature_active)
    # Step 1: legitimately remove R3.
    step1_reqs = {k: v for k, v in BASE_REQS.items() if k != 3}
    src1 = _write_source(tmp_path, "step1.md", step1_reqs)
    assert main([
        "update", "demo", "--from-file", str(src1), "--repo-root", str(git_repo),
    ]) == exit_codes.OK
    capsys.readouterr()

    # Step 2: try to reintroduce R3 with brand-new content.
    step2_reqs = dict(step1_reqs)
    step2_reqs[3] = ("New Gamma", "totally different content")
    src2 = _write_source(tmp_path, "step2.md", step2_reqs)
    before = (feature_active / "prd.md").read_text(encoding="utf-8")

    code = main([
        "update", "demo", "--from-file", str(src2), "--repo-root", str(git_repo),
    ])
    assert code == exit_codes.ERROR
    assert (feature_active / "prd.md").read_text(encoding="utf-8") == before
    captured = capsys.readouterr()
    assert "retired" in captured.err


def test_update_non_monotonic_new_number_rejected(git_repo, feature_active, tmp_path, capsys):
    reqs = {1: ("Alpha", "a"), 2: ("Beta", "b"), 4: ("Delta", "d")}
    _write_base(feature_active, reqs)
    new_reqs = dict(reqs)
    new_reqs[3] = ("Newcomer", "brand new")
    src = _write_source(tmp_path, "new.md", new_reqs)
    before = (feature_active / "prd.md").read_text(encoding="utf-8")

    code = main([
        "update", "demo", "--from-file", str(src), "--repo-root", str(git_repo),
    ])
    assert code == exit_codes.ERROR
    assert (feature_active / "prd.md").read_text(encoding="utf-8") == before
    captured = capsys.readouterr()
    assert "numbered above" in captured.err


def test_update_renumber_title_swap_rejected(git_repo, feature_active, tmp_path, capsys):
    _write_base(feature_active)
    new_reqs = dict(BASE_REQS)
    new_reqs[2] = ("Gamma", "beta body")  # R2's title now equals old R3's title
    src = _write_source(tmp_path, "new.md", new_reqs)
    before = (feature_active / "prd.md").read_text(encoding="utf-8")

    code = main([
        "update", "demo", "--from-file", str(src), "--repo-root", str(git_repo),
    ])
    assert code == exit_codes.ERROR
    assert (feature_active / "prd.md").read_text(encoding="utf-8") == before
    captured = capsys.readouterr()
    assert "renumbering is not allowed" in captured.err


def test_update_delete_then_reintroduce_as_new_number_rejected(
    git_repo, feature_active, tmp_path, capsys,
):
    _write_base(feature_active)
    new_reqs = {k: v for k, v in BASE_REQS.items() if k != 2}
    new_reqs[9] = BASE_REQS[2]  # R2's exact old content, reborn as R9
    src = _write_source(tmp_path, "new.md", new_reqs)
    before = (feature_active / "prd.md").read_text(encoding="utf-8")

    code = main([
        "update", "demo", "--from-file", str(src), "--repo-root", str(git_repo),
    ])
    assert code == exit_codes.ERROR
    assert (feature_active / "prd.md").read_text(encoding="utf-8") == before
    captured = capsys.readouterr()
    assert "renumbering is not allowed" in captured.err


def test_update_implicit_deletion_allowed(git_repo, feature_active, tmp_path):
    _write_base(feature_active)
    new_reqs = {k: v for k, v in BASE_REQS.items() if k != 4}
    src = _write_source(tmp_path, "new.md", new_reqs)

    code = main([
        "update", "demo", "--from-file", str(src), "--repo-root", str(git_repo),
    ])
    assert code == exit_codes.OK

    log_path = feature_active / "log.jsonl"
    last = json.loads(log_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert last["detail"]["removed"] == ["R4"]


# ---------------------------------------------------------------------
# History directory contents
# ---------------------------------------------------------------------


def test_update_history_dir_contents(git_repo, feature_active, tmp_path):
    _write_base(feature_active)
    old_text = (feature_active / "prd.md").read_text(encoding="utf-8")
    new_reqs = {k: v for k, v in BASE_REQS.items() if k != 4}
    src = _write_source(tmp_path, "new.md", new_reqs)

    code = main([
        "update", "demo", "--from-file", str(src), "--repo-root", str(git_repo),
    ])
    assert code == exit_codes.OK

    hist_dir = feature_active / "prd-history"
    assert hist_dir.is_dir()
    json_files = sorted(hist_dir.glob("prd.*.json"))
    assert len(json_files) == 1
    meta = json.loads(json_files[0].read_text(encoding="utf-8"))

    snapshot = hist_dir / meta["snapshot"]
    assert snapshot.read_text(encoding="utf-8") == old_text

    diff_text = (hist_dir / meta["diff"]).read_text(encoding="utf-8")
    assert "-### R4:" in diff_text

    assert meta["removed"] == ["R4"]
    assert meta["added"] == []
    assert meta["changed"] == []
    assert sorted(meta["r_numbers"]) == [1, 2, 3]
    assert meta["old_hash"].startswith("sha256:")
    assert meta["new_hash"].startswith("sha256:")


# ---------------------------------------------------------------------
# CLI flag surface
# ---------------------------------------------------------------------


def test_update_missing_from_file_is_usage_error(git_repo, feature_active):
    with pytest.raises(SystemExit) as exc:
        main(["update", "demo", "--repo-root", str(git_repo)])
    assert exc.value.code == 2


def test_update_amendment_flag_no_longer_accepted(git_repo, feature_active, tmp_path):
    src = _write_source(tmp_path, "new.md", BASE_REQS)
    with pytest.raises(SystemExit) as exc:
        main([
            "update", "demo", "--amendment", "changed something",
            "--repo-root", str(git_repo),
        ])
    assert exc.value.code == 2


# ---------------------------------------------------------------------
# `autodev prd` refuses to overwrite an existing prd.md
# ---------------------------------------------------------------------


def test_prd_rejects_existing_prd_md_in_planned(git_repo, tmp_path, capsys):
    planned = git_repo / "docs" / "features" / "demo" / "planned"
    planned.mkdir(parents=True)
    existing = "# already here\n"
    (planned / "prd.md").write_text(existing, encoding="utf-8")
    src = _write_source(tmp_path, "new.md", BASE_REQS)

    code = main([
        "prd", "demo", "--from-file", str(src), "--repo-root", str(git_repo),
    ])
    assert code == exit_codes.ERROR
    captured = capsys.readouterr()
    assert "autodev update" in captured.err
    assert (planned / "prd.md").read_text(encoding="utf-8") == existing


def test_prd_rejects_existing_prd_md_in_active(git_repo, feature_active, tmp_path, capsys):
    _write_base(feature_active)
    existing = (feature_active / "prd.md").read_text(encoding="utf-8")
    src = _write_source(tmp_path, "new.md", BASE_REQS)

    code = main([
        "prd", "demo", "--from-file", str(src), "--repo-root", str(git_repo),
    ])
    assert code == exit_codes.ERROR
    captured = capsys.readouterr()
    assert "autodev update" in captured.err
    assert (feature_active / "prd.md").read_text(encoding="utf-8") == existing


# ---------------------------------------------------------------------
# `status` surfaces prd-history
# ---------------------------------------------------------------------


def test_status_shows_prd_version_text_and_json(git_repo, feature_active, tmp_path, capsys):
    _write_base(feature_active)

    code = main(["status", "demo", "--repo-root", str(git_repo)])
    assert code == exit_codes.OK
    out = capsys.readouterr().out
    assert "prd:     version 1 (no recorded updates)" in out

    new_reqs = {k: v for k, v in BASE_REQS.items() if k != 4}
    src = _write_source(tmp_path, "new.md", new_reqs)
    assert main([
        "update", "demo", "--from-file", str(src), "--repo-root", str(git_repo),
    ]) == exit_codes.OK

    code = main(["status", "demo", "--repo-root", str(git_repo)])
    assert code == exit_codes.OK
    out = capsys.readouterr().out
    assert "prd:     version 2" in out

    code = main(["status", "demo", "--json", "--repo-root", str(git_repo)])
    assert code == exit_codes.OK
    out = capsys.readouterr().out
    report = json.loads(out)
    assert report["prd_history"]["versions"] == 2
    assert report["prd_history"]["last_update"]["summary"]


# ---------------------------------------------------------------------
# Back-compat `## Amendment` section: lint warning, not error
# ---------------------------------------------------------------------


PRD_WITH_AMENDMENT = BASE_PRD + (
    "\n## Amendment 2026-08-05\n\n### R5: Epsilon\nepsilon body\n"
    "\nAssurance: R1 core -> strict\n"
)


def test_prd_lint_amendment_section_warns_but_passes(git_repo, tmp_path, capsys):
    active = git_repo / "docs" / "features" / "demo" / "active"
    active.mkdir(parents=True)
    (active / "prd.md").write_text(PRD_WITH_AMENDMENT, encoding="utf-8")

    code = main(["prd-lint", "demo", "--repo-root", str(git_repo)])
    assert code == exit_codes.OK
    out = capsys.readouterr().out
    assert "prd-lint warning" in out
    assert "Amendment" in out


def test_validate_prd_text_warnings_do_not_change_parsed_results():
    plain = validate_prd_text(BASE_PRD)
    with_amendment = validate_prd_text(PRD_WITH_AMENDMENT)

    assert plain.warnings == []
    assert with_amendment.warnings != []

    assert with_amendment.ok is True
    # The Amendment section legitimately introduces R5; everything else
    # about parsing (sections, other markers) is unaffected by the warning.
    assert set(with_amendment.requirement_markers) == set(plain.requirement_markers) | {
        "R5"
    }

    from autodev.assurance import parse_assurance
    m_plain, errs_plain = parse_assurance(
        BASE_PRD, known_rs=set(plain.requirement_markers)
    )
    m_amend, errs_amend = parse_assurance(
        PRD_WITH_AMENDMENT, known_rs=set(with_amendment.requirement_markers)
    )
    assert errs_plain == errs_amend == []
    assert m_plain.default == m_amend.default
    assert m_plain.release_threshold == m_amend.release_threshold

    # The Amendment's `Assurance: R1 core -> strict` override line is still
    # applied: R1 is not otherwise present in the plain PRD's per-R map (no
    # `## Assurance` section), so its presence in `m_amend.per_r` shows the
    # override line, not just the default, is what put it there.
    assert "R1" not in m_plain.per_r
    assert m_amend.per_r.get("R1") == "strict"
