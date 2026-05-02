---
name: auto-dev
description: >
  Auxiliary skill for auto-dev-sdk. Thin dispatcher: parses user intent
  into `autodev` CLI calls; DOES NOT code; DOES NOT write artifacts; DOES
  NOT inject prompts into panel-review subprocesses. The harness (the
  `autodev` CLI) owns all state and enforcement; this skill only
  translates natural language to verbs + surfaces harness output.

  TRIGGER aggressively on non-trivial feature work in a repo that has
  `vendors.yml` at its root. Phrases: "implement X", "build X", "add X
  feature", "update X", "close X", "pause X", "resume X".

  Skip for single-line fixes, typos, config tweaks — those are not
  pipeline material.

  If the target repo does not have `vendors.yml` at root, stop and ask
  the user to create one (copy `vendors.yml.example` from the
  auto-dev-sdk repo). Do NOT invent vendor/model choices.
---

# auto-dev (auxiliary skill)

## Hard rule: I do not code through this skill

When I activate this skill, I **do not** write feature code, edit
source files, or create pipeline artifacts. Everything that mutates
pipeline state goes through the `autodev` CLI. Reads are free
(`autodev status`, `autodev explain`, `cat` artifacts, `tail
log.jsonl`) — those don't change state.

If you ask me to "just write the code directly" in a repo covered by
this skill, I will decline and ask you to confirm leaving the pipeline.
Going around the harness is the exact failure mode this skill exists
to prevent.

## What the CLI does

The `autodev` CLI is a pure state machine. It:
- Drives vendor CLIs (`claude -p`, `codex exec`) via subprocess to do
  actual coding work, per `vendors.yml`.
- Enforces three mandatory panel-review gates: `prd-review`
  (covers PRD↔scope coverage + scope↔architecture fit in one run),
  `test-plan-review` (trace + test-plan after the plan stage), and
  `close-approval` (spec + review.json at the end). Gates use the
  `panel-review` skill, which runs claude + gemini + codex for
  review diversity.
- Refuses to advance on missing/stale artifacts, failed gates, or dirty
  workspace (without explicit `acknowledge-dirty` override).
- Owns the gate prompts — they're bundled in `autodev/prompts/panel-*.md`
  and cannot be modified at runtime by this skill or the outer agent.

## Verb whitelist (my allowed CLI calls)

I may only invoke these:

| Verb | Purpose |
|---|---|
| `autodev prd <f> [--from-file PATH]` | Create/import PRD |
| `autodev status <f>` | Read state |
| `autodev run <f>` | Advance pipeline through all reachable stages |
| `autodev next <f>` | Advance exactly one stage |
| `autodev pause <f>` | Write `.pause` sentinel |
| `autodev resume <f>` | Remove `.pause` sentinel |
| `autodev skip-gate <f> <gate> --reason "..."` | Override a mandatory gate |
| `autodev acknowledge-dirty <f> --reason "..."` | Override dirty-workspace block |
| `autodev abort <f>` | Kill running subprocess |
| `autodev retry <f>` | Retry last failed stage |
| `autodev invalidate <f> <stage>` | Rollback a stage artifact |
| `autodev update <f> --amendment "..."` | Amend PRD; start new cycle |
| `autodev close <f> <reason> [--yes]` | Close feature |
| `autodev explain <f>` | Human-readable state |

I **do not**:
- Edit `.gates/`, `overrides.json`, or any `panel-*.json` file directly.
- Skip `autodev next` by creating downstream artifacts by hand.
- Pass prompts into `panel-review` subprocess calls (harness owns the
  prompts).

## Exit codes

| Code | Meaning | What I do |
|---|---|---|
| 0 | Done | Proceed / report to user |
| 1 | Error | Read stderr; surface to user; don't retry blindly |
| 2 | Gate pending | Run `autodev status` to see which gate; surface to user |
| 3 | Lock conflict | Another process holds `.lock/`; surface with owner info |

## Outer-layer behaviors (MUST)

Mirror of the harness's R5 contract:

1. **Current-feature tracking**: track the most recent `<feature>` arg
   across turns as the session's current feature.
2. **Pre-turn passive poll**: before responding to each user turn in an
   active auto-dev session, run `autodev status <current-feature>`
   and surface state changes proactively.
3. **Pre-write recheck**: before any `skip-gate`, `acknowledge-dirty`,
   `abort`, `invalidate`, `update`, `close` — run `autodev status`
   again to confirm intent against fresh state.
4. **Scheduled wake**: while a pipeline is `run`-ing (not paused, not
   failed), poll via `ScheduleWakeup` every 10-15 min.
5. **No panel prompt injection**: the `panel-review` subprocess is
   called by the harness. I do not construct or inject prompts into
   that subprocess.

## Confirmation gate

Every invocation except feature-folder-enforced routing starts with a
single line:

> "This will run the autodev v2 pipeline on `<feature>`. Proceed, or
> is this smaller than that?"

If you say no, I exit this skill entirely.

## Feature-folder enforcement

If your request touches a path covered by `docs/features/<X>/` (any
status), the change MUST go through `autodev update <X>`. I do not let
ad-hoc edits through.

## Disambiguation from other skills

- **`auto-dev` (original SKILL.md)**: I take precedence when `vendors.yml`
  (v2 schema) is present. If there's only a v0.1-style `vendors.yml`
  (with top-level stage keys instead of a `stages:` map), or no
  `vendors.yml`, the original skill is the right one.
- **`auto-dev-sdk` (v0.1)**: deferred; do not use.

## Never

- **Never commit or push.**
- **Never rewrite `prd.md`.** Use `autodev update` (append-only amendments).
- **Never write `.lock/`, `.gates/`, `overrides.json`, `panel-*.json`,
  or artifact files directly.** Use CLI verbs.
- **Never skip the pre-turn status poll.**
- **Never claim a stage is complete from memory — read the filesystem.**
- **Never inject custom prompts into panel-review invocations.**
