# skills-pub

Agent skills for PRD-driven feature development, multi-vendor review, and
autonomous PR upkeep, plus the shared modules they depend on. Skills follow the open `SKILL.md` format and work in Claude
Code, Codex CLI, and other agents that load skills from a directory.

| Skill | What it is | Pick it when |
|---|---|---|
| [`skills/auto-dev-sdk`](skills/auto-dev-sdk/) | A Python harness (`autodev` CLI) that owns pipeline state and enforcement: design packet → multi-vendor design review → build loop → implementation index → PRD checklist → close approval. The skill itself is a thin dispatcher from natural language to CLI verbs. | You want the pipeline enforced by code, with review gates that call several LLM vendors and a resumable filesystem state machine. |
| [`skills/auto-dev-lite`](skills/auto-dev-lite/) | A prompt-only skill. You own a core requirements document; the orchestrator owns a detail spec; fresh subagents do a design dry run, scoped implementation, and reviews. | You want document-driven development without external vendor CLIs or a harness install. |
| [`skills/panel-review`](skills/panel-review/) | Runs one prompt through several LLM vendors in parallel and synthesizes where they agree and, more importantly, where they diverge. `auto-dev-sdk` uses the same mechanism for its review gates. | You want a cross-vendor second opinion on a spec, config, or architecture decision that tests cannot verify. |
| [`skills/pr-review`](skills/pr-review/) | Two-phase review of a local branch diff or a GitHub PR: first whether the code delivers what its docs (plan, PRD, spec) require, then a bug hunt. In GitHub mode it can post inline review threads. | You want a structured pre-merge review, not a one-line sanity check. |
| [`skills/auto-fix`](skills/auto-fix/) | Takes a failure signal (CI log or review comment) plus the affected scope, reads the project's feature docs for design intent, and either applies a minimum fix that matches the documented intent or escalates with a conflict report. | You want autonomous fixes that refuse to reverse design decisions. |
| [`skills/pr-watch-auto`](skills/pr-watch-auto/) | Watches a pushed PR's CI and review comments in one loop, delegates fixes to `auto-fix`, squash-commits and pushes, and resolves or escalates each thread. | You want a PR babysat after pushing it. |

`shared/` holds the modules the skills link to: `shared/vendors` (one adapter
for every supported LLM CLI, with idle probing and doctor checks),
`shared/github-ops` (GitHub API primitives for the PR skills), `shared/os`
(host OS detection), `shared/secrets` (redaction and secret scanning), and
`shared/doctor` (the shared output format for `doctor.sh` scripts).

`auto-fix`, `pr-review`, and `pr-watch-auto` read a project's `docs/features/<X>/`
folder for design intent when one exists; `auto-dev-sdk` produces that folder.

## Requirements

- Linux or macOS. Windows needs WSL: the scripts assume a POSIX shell and
  `/proc` or BSD `ps`.
- Python 3.11+ for `auto-dev-sdk`; bash 3.2+ for the shell scripts (macOS's
  system bash is fine).
- For `auto-dev-sdk`, `panel-review`, and `pr-review`: at least one supported
  LLM CLI on `PATH` and logged in. Supported: `claude`, `codex`, `agy`, `grok`,
  `cursor-agent`. See [`shared/vendors/sample-vendors.yaml`](shared/vendors/sample-vendors.yaml)
  for binaries, login commands, and model ids. `auto-dev-lite` needs none.
- For `pr-review`, `auto-fix`, and `pr-watch-auto` in GitHub mode: the `gh` CLI,
  logged in.

## Install a skill

Symlink the skill directory into your agent's skills directory. Symlinks keep
the `shared/` links resolving and let `git pull` update everything:

```bash
git clone https://github.com/sonusz/skills-pub.git
REPO="$PWD/skills-pub"

ln -sfn "$REPO/skills/auto-dev-lite" ~/.claude/skills/auto-dev-lite   # Claude Code
ln -sfn "$REPO/skills/auto-dev-sdk"  ~/.claude/skills/auto-dev-sdk
ln -sfn "$REPO/skills/panel-review"  ~/.claude/skills/panel-review
# pr-watch-auto depends on auto-fix: link both
ln -sfn "$REPO/skills/auto-fix"      ~/.claude/skills/auto-fix
ln -sfn "$REPO/skills/pr-watch-auto" ~/.claude/skills/pr-watch-auto
# Codex: use ~/.codex/skills/ instead
```

If you must copy instead of link, replace each `shared/*` symlink inside the
copied skill with a real copy of that module.

## panel-review: pick your vendors

```bash
cd skills-pub/skills/panel-review
python3 ../../shared/vendors/scripts/init-vendors.py \
  --sample sample-vendors.yaml --out vendors.yaml   # keeps vendors whose CLI is on PATH
bash scripts/doctor.sh                                # then delete entries that fail
```

## auto-dev-sdk: install the CLI

```bash
cd skills-pub/skills/auto-dev-sdk
python3 -m pip install --user .          # provides the `autodev` command
autodev --help

# Tell the harness which vendors exist on this machine
python3 ../../shared/vendors/scripts/init-vendors.py \
  --sample sample-vendors.yml --out vendors.yml
```

`vendors.yml` is machine-local and git-ignored; `sample-vendors.yml` is the
tracked catalog. Full details, including venv installs and a first feature
walkthrough, are in [`skills/auto-dev-sdk/README.md`](skills/auto-dev-sdk/README.md)
and [`skills/auto-dev-sdk/references/install.md`](skills/auto-dev-sdk/references/install.md).

## Run the tests

```bash
cd skills/auto-dev-sdk
python3 -m pip install --user -e ".[test]"
python3 -m pytest -q            # ~4 minutes; live-vendor tests are opt-in via -m live
```

## Contributing

This repository is a read-only mirror of a larger private repository. Pull
requests are welcome and are applied upstream as patches, so the commit that
lands will not be the PR's own commit. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
