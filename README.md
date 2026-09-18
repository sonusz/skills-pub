<h1 align="center">skills-pub</h1>

<p align="center">
  Agent skills that take a feature from PRD to merged PR,<br>
  with several LLM vendors checking each other's work.
</p>

<p align="center">
  <a href="https://github.com/sonusz/skills-pub/actions/workflows/test.yml"><img alt="tests" src="https://github.com/sonusz/skills-pub/actions/workflows/test.yml/badge.svg"></a>
  <img alt="python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue">
  <img alt="linux | macos" src="https://img.shields.io/badge/platform-linux%20%7C%20macos-lightgrey">
  <a href="LICENSE"><img alt="MIT" src="https://img.shields.io/badge/license-MIT-green"></a>
</p>

---

Seven skills in the open [`SKILL.md`](https://github.com/anthropics/skills) format. They load into Claude Code, Codex CLI, and any agent that reads skills from a directory. Everything is shell and Python; there is nothing to deploy.

- **PRD in, reviewed code out.** `auto-dev-lite` for most features, `auto-dev-sdk` when the feature is big enough to want a harness with state on disk.
- **Disagreement as a signal.** Review gates send the same artifact to several vendors and synthesize where they diverge, because consensus among models that share training data proves little.
- **Quota-aware.** The harness reads each vendor's remaining quota before a launch and falls back to the next candidate instead of stalling.
- **PRs that look after themselves.** Watch CI and review threads, fix only what documented design intent allows, escalate the rest.
- **Nothing hidden from you.** Every fix, push, and thread resolution needs a confirmation bound to a snapshot of what it will touch.

## Quick start

```bash
git clone https://github.com/sonusz/skills-pub.git
REPO="$PWD/skills-pub"

# Claude Code (use ~/.codex/skills for Codex CLI)
for s in auto-dev-lite panel-review pr-review auto-fix pr-watch-auto compact-skill auto-dev-sdk; do
  ln -sfn "$REPO/skills/$s" ~/.claude/skills/$s
done
```

`auto-dev-lite` and `auto-fix` work right away; the others need vendors configured (below). Then, in your agent:

> implement per this document docs/my-feature.md

and `auto-dev-lite` takes it from there. For the skills that call other vendors, see [Configure vendors](#configure-vendors).

## Skills

| Skill | What it does | Needs |
|---|---|---|
| [**auto-dev-lite**](skills/auto-dev-lite/) | Document-driven development for small and medium features. You own a core requirements doc, the agent owns a detail spec, fresh subagents build and review against both. | nothing extra |
| [**auto-dev-sdk**](skills/auto-dev-sdk/) | The same idea for large features and codebases. A Python harness (`autodev`) owns pipeline state and gates; the skill just turns your request into CLI verbs. | Python 3.11+, vendor CLIs |
| [**panel-review**](skills/panel-review/) | One prompt to several vendors in parallel, then a synthesis of where they agree and where they diverge. | vendor CLIs |
| [**pr-review**](skills/pr-review/) | Two-phase review of a branch diff or a GitHub PR: does the code deliver what its docs require, then a bug hunt. Can post inline threads. | panel-review, GitHub token for PR mode |
| [**auto-fix**](skills/auto-fix/) | Given a CI log or review comment, apply the smallest fix that matches documented design intent, or escalate with a conflict report. | nothing extra |
| [**pr-watch-auto**](skills/pr-watch-auto/) | Babysit a pushed PR: watch CI and review threads in one loop, hand each to auto-fix, push, resolve or escalate. | auto-fix, GitHub token |
| [**compact-skill**](skills/compact-skill/) | Shrink a `SKILL.md` without losing behavior, then have a vendor panel confirm nothing was lost. | panel-review |

Every skill has its own README with requirements, usage, and how it works.

### Which auto-dev?

Start with **auto-dev-lite**. It is prompt-only, needs no install beyond the symlink, and fits most features. Move to **auto-dev-sdk** when a feature is large enough that you want the pipeline state on disk, resumable across sessions, with multi-vendor review gates enforced by code rather than by prompt. Its README has [the two loops drawn out](skills/auto-dev-sdk/README.md#the-two-loops).

## How they fit together

```text
                    PRD
                     │
                     ▼
  panel-review ◄── auto-dev-sdk ──► code + docs/features/<feature>/
       ▲                                        ▲            │
       │ both phases                            │ reads      │ push
       │                                        │            ▼
   pr-review ◄── before merge ── PR ──► pr-watch-auto ──► auto-fix
```

`auto-dev-sdk` leaves a `docs/features/<feature>/` folder next to the code it writes. `auto-fix` reads that folder to decide whether a fix matches the documented intent. `pr-review` checks a diff against whatever plan, PRD, or spec documents the diff itself touches. `panel-review` is both a standalone skill and the mechanism behind `auto-dev-sdk`'s three panel gates. Every skill also works alone; the arrows are conventions, not requirements.

## Why multiple vendors

Four of the seven skills send the same artifact to several vendors: `panel-review` directly, `pr-review` and `compact-skill` through it, and `auto-dev-sdk` at its panel gates. The reasoning, from `panel-review`:

- **Divergence is the signal.** When three models read one spec differently, the spec is under-defined. That shows up before the implementation bug does.
- **Consensus is not validation.** Vendors share training data, cutoffs, and upstream sources, so they can agree on the same mistake. A unanimous concern is actionable; unanimous approval is not proof.
- **Different biases, different failure modes.** For a config or architecture decision no test can verify, reviewer diversity is the best available proxy for ground truth.
- **A failed call is never silently replaced.** Substituting another vendor mid-run would weaken the signal without telling you. A panel needs at least two successful reviewers or it aborts.

`auto-dev-sdk` adds quota awareness on top: before each launch it reads the vendor's remaining quota and, below the configured threshold, moves to the next candidate in that role's fallback list. A reviewer that fails is retried once with a fresh quota reading. Details in [Why this harness](skills/auto-dev-sdk/README.md#why-this-harness).

## Requirements

| | |
|---|---|
| OS | Linux or macOS. Windows via WSL. Scripts detect the OS and use `/proc` or BSD `ps` accordingly. |
| Shell | bash 3.2 or newer. macOS's system bash is enough. |
| Python | 3.11 or newer, for `auto-dev-sdk` only. |
| Vendor CLIs | At least one of `claude`, `codex`, `agy`, `grok`, `cursor-agent` on `PATH` and logged in, for the skills marked above. Binaries, login commands, and model ids: [`shared/vendors/sample-vendors.yaml`](shared/vendors/sample-vendors.yaml). |
| GitHub token | A fine-grained PAT reachable through `git credential fill` for `github.com`. The PR skills call the API directly and do not use `gh`. Read on Pull requests, Actions, Contents, Issues; add Pull requests: Write to post or resolve threads. |

## Configure vendors

Skills that call vendors ship a tracked `sample-vendors.yaml` (or `.yml`) listing every vendor they can use. Each machine keeps a git-ignored `vendors.yaml` beside it, pruned to what actually works there. Generate it once per skill, run the doctor, delete what fails:

```bash
cd "$REPO/skills/panel-review"
python3 ../../shared/vendors/scripts/init-vendors.py \
  --sample sample-vendors.yaml --out vendors.yaml   # keeps vendors whose CLI is on PATH
bash scripts/doctor.sh
```

`init-vendors.py --vendor claude --vendor codex` keeps an explicit list instead of probing `PATH`. `auto-dev-sdk` uses the same flow with `sample-vendors.yml` / `vendors.yml`, and needs its CLI installed first:

```bash
cd "$REPO/skills/auto-dev-sdk" && python3 -m pip install --user . && autodev --help
```

Model names in the samples are the ones this repository has used; change them freely.

## Install notes

Symlinks are not a shortcut, they are the install. Skills reach shared modules through relative links (`skills/<skill>/shared/vendors -> ../../../shared/vendors`), `pr-watch-auto` reaches `auto-fix` the same way, and `pr-review` and `compact-skill` reach `panel-review`. A symlinked install keeps every link valid and updates with `git pull`. If you must copy, replace each `shared/*` and `skills/*` link inside the copy with a real copy of its target.

<details>
<summary><b>Repository layout</b></summary>

```text
skills/<skill>/        one skill: SKILL.md, README.md, scripts/, prompts/
skills/<skill>/shared/ relative links into ../../../shared/
skills/<skill>/skills/ relative links to sibling skills this one depends on
shared/vendors/        one adapter for every vendor CLI (call.sh, doctor.sh)
shared/github-ops/     GitHub API primitives used by the PR skills
shared/secrets/        redact.sh and scan.sh for anything sent out or posted
shared/os/             host OS detection for shell and Python
shared/doctor/         shared output format for the doctor.sh scripts
```

</details>

<details>
<summary><b>Running the tests</b></summary>

```bash
cd "$REPO/skills/auto-dev-sdk"
python3 -m pip install --user -e ".[test]"
python3 -m pytest -q          # about 4 minutes; live-vendor tests are opt-in via -m live
bash ../panel-review/tests/smoke.sh    # panel-review against fake vendor CLIs
```

The pytest suite runs on every push here, on Linux and macOS.

</details>

## Contributing

This repository is a read-only mirror of a larger private repository. `main` is regenerated from upstream on every change and a ruleset blocks direct pushes. Pull requests are welcome and land upstream as patches with your authorship preserved, so the commit that ships will not be the PR's own. Details in [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
