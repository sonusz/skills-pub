<h1 align="center">skills-pub</h1>

<p align="center">
  <b>Cross-vendor agent skills.</b> Claude, Codex, Gemini, Grok, and Cursor<br>
  build, review, and fix the same feature, and the harness reads their disagreement.
</p>

<p align="center">
  <a href="https://github.com/sonusz/skills-pub/actions/workflows/test.yml"><img alt="tests" src="https://github.com/sonusz/skills-pub/actions/workflows/test.yml/badge.svg"></a>
  <img alt="python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue">
  <img alt="linux | macos" src="https://img.shields.io/badge/platform-linux%20%7C%20macos-lightgrey">
  <a href="LICENSE"><img alt="MIT" src="https://img.shields.io/badge/license-MIT-green">
</p>

---

Seven skills in the open [`SKILL.md`](https://github.com/anthropics/skills) format that take a feature from PRD to merged PR. They load into Claude Code, Codex CLI, and any agent that reads skills from a directory. Everything is shell and Python; there is nothing to deploy.

**The point is the vendors working together, not any one of them.** One adapter drives all five CLIs behind the same interface, so a skill can hand one artifact to several models at once and keep going when one of them runs out of quota.

- **Disagreement is the signal.** Review gates send the same spec, design, or diff to several vendors in parallel and synthesize where they diverge. Consensus among models that share training data proves little; a split vote points at the under-defined clause.
- **Quota-aware switching.** Before every launch the harness reads the vendor's remaining quota and falls back to the next candidate in that role's list, never to the same provider it is meant to check. A run pauses rather than guesses when nothing qualifies.
- **PRD in, reviewed code out.** `auto-dev-lite` for most features, `auto-dev-sdk` when the feature is big enough to want a harness with state on disk and multi-vendor panels at its design, trace, and close gates.
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

## One adapter, five vendors

Every skill that talks to a model goes through `shared/vendors/scripts/call.sh`. It is the only place that knows how to drive each vendor's CLI; skills pass a prompt and a vendor label and get back the same files regardless of vendor: `out`, `status`, `log`, `stream`, and `usage.json` per call.

| Vendor label | CLI it drives | Login | Models this repo has used | Schema output |
|---|---|---|---|---|
| `claude` | `claude` (Claude Code, headless `-p`) | `claude login` | `opus`, `sonnet`, `claude-fable-5`, `claude-sonnet-5` | yes |
| `openai` (alias `codex`) | `codex` (`codex exec`) | `codex login` | `gpt-6-astra`, `gpt-5.6-terra`, `gpt-5.6-sol` | yes |
| `agy` | `agy` (Antigravity / Gemini) | `agy login` | `gemini-3.1-pro-high`, `gemini-3.1-pro` | no |
| `grok` (alias `xai`) | `grok` (Grok Build) | `grok login` | `grok-4.5` | yes |
| `cursor` | `cursor-agent` | `cursor-agent login` | `gemini-3.1-pro`, `claude-sonnet-5-medium` | no |

Only three things are abstracted across vendors, on purpose: **model**, **effort** (`min` to `max`, mapped to each CLI's nearest native setting), and **yolo** (each CLI's no-approval mode). Everything else is uniform mechanics the adapter adds on top:

- **Fan-out.** One call can name several vendors; they run in parallel with a `--min-success` threshold, which is what a panel is.
- **Watchdog with judgment.** A stalled call is killed after a window with no new stream output. If configured, a cheap model first reads the process tree and output tails and decides whether the call is thinking or wedged.
- **Persistent sessions.** A `--session-key` resumes the vendor's native conversation on later calls, with a turn cap after which the session rotates. `auto-dev-sdk` uses this so a design agent keeps its context across gate rounds.
- **Schema-constrained output.** `--schema-file` makes `claude`, `openai`, and `grok` return a JSON object that conforms; panels use it for synthesis.
- **Doctor.** `scripts/doctor.sh` proves each configured call is ready before anything launches, and keeps diagnostics when it is not.

Configuration follows the same shape everywhere: a tracked `sample-vendors.yaml` per skill lists every vendor it can use, a git-ignored `vendors.yaml` beside it is that list pruned to what works on this machine, and `init-vendors.py` generates the local file. See [Configure vendors](#configure-vendors).

## Why several vendors, and what happens when one runs dry

Four of the seven skills send the same artifact to several vendors: `panel-review` directly, `pr-review` and `compact-skill` through it, and `auto-dev-sdk` at its three panel gates. The reasoning, from `panel-review`:

- **Divergence is the signal.** When three models read one spec differently, the spec is under-defined. That shows up before the implementation bug does.
- **Consensus is not validation.** Vendors share training data, cutoffs, and upstream sources, so they can agree on the same mistake. A unanimous concern is actionable; unanimous approval is not proof.
- **Different biases, different failure modes.** For a config or architecture decision no test can verify, reviewer diversity is the best available proxy for ground truth.
- **A failed call is never silently replaced.** Substituting another vendor mid-run would weaken the signal without telling you. A panel needs at least two successful reviewers or it aborts.

Running several vendors means running into several quota limits. `auto-dev-sdk` handles that per role rather than per run:

- **Quota is read before every launch.** Each LLM role in `vendors.yml` (every coding stage, every panel reviewer, the synthesizer, the idle probe) may set `min_quota_pct` and an ordered `fallbacks` list. The harness fetches the vendor's remaining quota (all five vendors have fetchers) and runs the first candidate at or above its floor. A reading it cannot fetch counts as insufficient: the check fails closed.
- **Fallbacks keep the panel honest.** A reviewer's fallback must be a different underlying provider from its primary, so a panel never collapses into one vendor wearing two hats. A synthesizer's fallback must be schema-capable.
- **A lost reviewer is retried, not faked.** A reviewer that times out or returns nothing is retried once with a fresh quota reading, so the retry can land on a fallback. After that the harness force-refreshes quota and drops the reviewer only if exhaustion is positively confirmed. The panel still has to meet `min_responding_reviewers` (default 2).
- **When nothing qualifies, the run pauses instead of guessing.** It records the earliest reset time and a repo fingerprint in `.quota-pause.json`; `autodev quota-resume` continues only when the time has passed, quota has recovered, and the repo has not changed underneath.

Details and code references in [Why this harness](skills/auto-dev-sdk/README.md#why-this-harness).

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
