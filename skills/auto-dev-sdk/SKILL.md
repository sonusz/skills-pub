---
name: auto-dev
description: >
  Auxiliary skill for auto-dev-sdk. Thin dispatcher: parses user intent
  into `autodev` CLI calls; DOES NOT code; DOES NOT write artifacts; DOES
  NOT inject prompts into panel-review subprocesses. The harness (the
  `autodev` CLI) owns all state and enforcement; this skill only
  translates natural language to verbs + surfaces harness output.

  TRIGGER aggressively on non-trivial feature work in a repo. Phrases:
  "implement X", "build X", "add X
  feature", "update X", "close X", "pause X", "resume X".

  Skip for single-line fixes, typos, config tweaks — those are not
  pipeline material.

  The harness uses SDK-root `vendors.yml` by default. Do NOT invent
  vendor/model choices for target repos.
---

# auto-dev (auxiliary skill)

## Role

Thin dispatcher only. I do not write feature code, edit source files, create artifacts, mutate `.lock/`, `.gates/`, `overrides.json`, or `panel-*.json`, or inject panel prompts. All state mutation goes through `autodev`. Reads are allowed: `autodev status`, `autodev explain`, `cat` artifacts, `tail log.jsonl`.

If asked to code directly in a repo covered by this skill, decline and ask the user to confirm leaving the pipeline.

## What the CLI does

`autodev` is the state machine:

- Dispatches all harness-owned LLM calls through the packaged
  `shared/vendors/scripts/call.sh` interface per repo-root `vendors.yml`.
- Runs `design-review` over the unified design packet and `close-approval` over `implemented-spec.md` + PRD + judgment-free PRD checklist.
- Uses configured Agy-Claude + `grok` + `codex`/`openai` panel reviewers
  for review diversity.
- Runs build in the Ralph loop: build writes code + `build.json`; `ralph-review` checks coverage; repeat until complete, stalled, or routed.
- Writes bridge artifacts: `design-packet.json`, `accepted-design.json`, `implementation-index.json`, `prd-checklist.json`.
- Blocks on missing/stale artifacts, failed gates, dirty workspace without `acknowledge-dirty`, or locks.
- Owns bundled stage/gate prompts under `autodev/prompts/` and `autodev/panel/prompts/`; neither this skill nor the outer agent can modify them at runtime.

## PRD pre-flight (cold-start authoring)

Before invoking `autodev prd <feature> --from-file ...`, check whether the user has a rigorous PRD ready:

- If they hand over a structured `prd.md` aligned with the v2 schema (`## Problem / Users / Architectural principles / Requirements / Constraints / Success Criteria / Out of Scope`), proceed straight to import.
- If they only have an idea, a screenshot, a PDF, a reference document, or a draft `prd.md` that has not passed `autodev prd-lint`, drive PRD authoring first by following [references/prd-authoring.md](references/prd-authoring.md). That guide bundles the canonical schema, the interrogation checklist that surfaces gaps (concurrency floor / ceiling, failure modes, recovery, operator surface, implementation discipline, library reuse, platform compat), the bullet-classification rubric, the lessons-learned carryover, and a worked example. Do NOT invent a PRD silently; walk the checklist with the user so gaps surface explicitly.

Trigger the pre-flight when the user says "write a PRD", "draft requirements", "spec out a new feature", or hands over an unstructured idea and asks to start auto-dev. Skip the pre-flight for `update`, `close`, `pause`, `resume`, `abort`, `retry`, `invalidate`, `skip-gate`, `acknowledge-dirty` — those operate on features that already have a PRD.

## CLI install check

Before invoking any `autodev` verb, run `autodev --help` or `command -v autodev`. If unavailable or broken, read [references/install.md](references/install.md) and install the Python package from this skill root. Use the installed `autodev` path immediately; do not invent `vendors.yml` model choices for target repos. If the SDK-root `vendors.yml` is missing, ask the user whether to copy `vendors.yml.example` or pass `AUTODEV_VENDORS_YML`.

In the whitelist below, `autodev` means either the PATH command or the resolved installed binary from the install reference.

## Verb whitelist (my allowed CLI calls)

I may only invoke these:

| Verb | Purpose |
|---|---|
| `autodev prd <f> [--from-file PATH]` | Create/import PRD |
| `autodev status <f>` | Read state |
| `autodev run <f> [--watch] [--until design\|build\|spec]` | Advance pipeline through all reachable stages. `--until design` runs the whole design phase (including the design-review gate and any in-design revision reruns) then stops before build; `--until build` stops before spec; omit (or `--until spec`) to run to completion. |
| `autodev next <f> [--watch]` | Advance exactly one stage |
| `autodev pause <f>` | Write `.pause` sentinel |
| `autodev resume <f>` | Remove `.pause` sentinel |
| `autodev quota-resume <f>` | Conditionally resume a quota-paused feature (auto-continues only if still quota-paused, resume_at reached, quota recovered, and repo unchanged; else no-op) |
| `autodev skip-gate <f> <gate> --reason "..."` | Override a mandatory gate |
| `autodev acknowledge-dirty <f> --reason "..."` | Override dirty-workspace block |
| `autodev abort <f>` | Hard-stop the run: write `.pause` sentinel (so orchestrator can't dispatch next stage) + kill running vendor subprocess + write interrupted failure. Run `autodev resume` before next `run`. |
| `autodev retry <f>` | Retry last failed stage |
| `autodev invalidate <f> <stage>` | Rollback a stage artifact |
| `autodev update <f> --amendment "..."` | Amend PRD; start new cycle |
| `autodev close <f> <reason> [--yes]` | Close feature |
| `autodev explain <f>` | Human-readable state |

I **do not**:
- Skip `autodev next` by creating downstream artifacts by hand.
- Pass prompts into panel-review subprocesses.

## Exit codes

| Code | Meaning | What I do |
|---|---|---|
| 0 | Done | Proceed / report to user |
| 1 | Error | Read stderr; surface to user; don't retry blindly |
| 2 | Gate pending | Run `autodev status` to see which gate; surface to user |
| 3 | Lock conflict | Another process holds `.lock/`; surface with owner info |

## Quota fallback & pause

`vendors.yml` may give any LLM (stage / panel reviewer / synthesizer / probe) a
`min_quota_pct` (minimum *remaining* quota %) plus ordered `fallbacks`. Before each
call the harness checks the vendor's remaining quota and uses the primary, else the
first fallback with enough quota. The harness owns all of this — I do **not** edit
`vendors.yml` or pick vendors.

When **every** candidate for a role is below its minimum, `autodev run`/`next`:
- exits **2 (GATE_PENDING)**,
- prints a machine line `QUOTA_PAUSE feature=<f> role=<r> resume_at=<ISO8601>`,
- writes `.pause` + `.quota-pause.json` (earliest recovery time + a repo/feature
  fingerprint).

**My duty on a `QUOTA_PAUSE` exit:**
1. Surface it to the user (which role, when quota resets).
2. Schedule `autodev quota-resume <f>` at `resume_at` (use the `/schedule` skill;
   or, headless, a `cron`/`launchd`/`at` entry). `quota-resume` is self-guarding
   and idempotent — it auto-continues **only if** the feature is still
   quota-paused, the time has been reached, quota has recovered, and the repo is
   **unchanged**; otherwise it does nothing. So scheduling it early/often is safe.
3. Do **not** hand-edit the repo or run other stages while quota-paused — any
   change cancels the automatic resume (by design), and the user would then have
   to `autodev resume` manually.

If quota still hasn't recovered at `resume_at`, `quota-resume` reschedules itself
(updates `resume_at`); re-schedule the wake to the new time.

## Outer-layer duties

Mirror the harness R5 contract:

1. Track the latest `<feature>` arg as current feature across turns.
2. Before each user turn in an active auto-dev session, run `autodev status <current-feature>` and surface state changes.
3. Before any write-like verb (`skip-gate`, `acknowledge-dirty`, `abort`, `invalidate`, `update`, `close`), rerun `autodev status`.
4. While `run` is active and not paused/failed, treat the wakeup as a **10-minute sliding deadman**, not a fixed cadence:
   - Schedule the next `ScheduleWakeup` 10 min out from the most recent activity.
   - **Every time you receive a signal — a `<task-notification>`, a `--watch` stdout alert, or a user message — reset the wakeup to 10 min from now.** Drop the previously-scheduled wakeup; only one is active at a time.
   - If 10 min passes with no signal at all, the wakeup fires; do a `autodev status` check and either resume the deadman or surface a stall.
   - Faster is fine if a stage transition is clearly imminent.
5. Do not construct or inject panel-review prompts; harness calls panel-review.

### Push alerts via `--watch`

`autodev run <feature> --watch` emits one-line stdout markers on key state transitions so the outer agent can react without polling. Run it via `Bash(run_in_background: true)`; optionally attach a `Monitor` task to wake on each alert. Full whitelist, format, and Monitor pattern in [references/watch.md](references/watch.md).

## Confirmation gate

Every invocation except feature-folder-enforced routing starts with this single line:

> "This will run the autodev pipeline on `<feature>`. Proceed, or
> is this smaller than that?"

If the user says no, exit this skill entirely.

## Feature-folder enforcement

If the request touches a path covered by `docs/features/<X>/` in any status, route through `autodev update <X>`. Do not allow ad-hoc edits.

## Disambiguation from other skills

- **`auto-dev` original skill**: this skill takes precedence when SDK-root or explicit vendor config uses v2 schema (`stages:` map). If the available config is v0.1-style (top-level stage keys), use the original skill instead.
- **`auto-dev-sdk` (v0.1)**: deferred; do not use.

## Never

- Never commit or push.
- Never rewrite `prd.md`; use append-only `autodev update`.
- Never write `.lock/`, `.gates/`, `overrides.json`, `panel-*.json`, or artifact files directly.
- Never skip the pre-turn status poll.
- Never claim a stage is complete from memory; read filesystem state.
- Never inject custom prompts into panel-review invocations.
