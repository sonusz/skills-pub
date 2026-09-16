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

The sole outer-layer helper exception is the mandatory, read-only PRD semantic-intent check below. That subagent may read the PRD and its source references, but may not edit files or invoke the pipeline.

If asked to code directly in a repo covered by this skill, decline and ask the user to confirm leaving the pipeline.

## What the CLI does

`autodev` is the state machine:

- Dispatches all harness-owned LLM calls through the packaged
  `shared/vendors/scripts/call.sh` interface per repo-root `vendors.yml`.
- Runs a single-agent `arch-design`/`arch-review` loop before design: `arch-design` drafts the architecture from the PRD, `arch-review` checks PRD coverage/invention/redundancy/reuse; repeat until `pass` sends it into `design`.
- Runs `design-review` over the unified design packet and `close-approval` over `implemented-spec.md` + PRD + judgment-free PRD checklist.
- Uses configured `claude` + `grok` + `agy` + `codex`/`openai` panel reviewers
  for review diversity.
- Requires two responding panel reviewers by default. A failed reviewer is
  retried once and may be omitted only after a forced quota refresh positively
  confirms exhaustion; unknown and non-quota failures still block.
- Preserves each reviewer finding, clusters semantically equivalent findings
  into one ticket, and enforces the PRD's `Release threshold: P0|P1|P2`.
- Runs build in the Ralph loop: build writes code + `build.json`; `ralph-review` checks coverage; repeat until complete, stalled, or routed.
- Writes bridge artifacts: `design-packet.json`, `accepted-design.json`, `implementation-index.json`, `prd-checklist.json`.
- Owns a background-watch protocol: start marker, periodic heartbeat, and one terminal marker for every `run --watch` / `next --watch` outcome.
- Blocks on missing/stale artifacts, failed gates, dirty workspace without `acknowledge-dirty`, or locks.
- Owns bundled stage/gate prompts under `autodev/prompts/` and `autodev/panel/prompts/`; neither this skill nor the outer agent can modify them at runtime.

## PRD pre-flight (cold-start authoring)

Before invoking `autodev prd <feature> --from-file ...`, check whether the user has a rigorous PRD ready:

- If they hand over a structured `prd.md` aligned with the v2 schema (`## Problem / Users / Architectural principles / Requirements / Constraints / Success Criteria / Out of Scope`), skip drafting and continue to the mandatory semantic-intent check below before import.
- If they only have an idea, a screenshot, a PDF, a reference document, or a draft `prd.md` that has not passed `autodev prd-lint`, drive PRD authoring first by following [references/prd-authoring.md](references/prd-authoring.md). That guide bundles the canonical schema, the interrogation checklist that surfaces gaps (concurrency floor / ceiling, failure modes, recovery, operator surface, implementation discipline, library reuse, platform compat), the bullet-classification rubric, the lessons-learned carryover, the design-stage POC clause (when a design premise needs proof beyond code/docs/measurements), and a worked example. Do NOT invent a PRD silently; walk the checklist with the user so gaps surface explicitly.

### Mandatory semantic-intent check

Before `autodev prd`, spawn a fresh read-only subagent. Give it only the PRD and its source references; ask it to restate the requirements and flag plausible alternate readings. Do not supply the intended interpretation or prior conclusions.

Compare its independent reading with the user's confirmed intent. If they differ materially, surface the mismatch, make only the smallest user-approved clarification, and repeat with a fresh pass until they align. Do not proceed merely because the subagent says the PRD is acceptable, and do not let it invent requirements or replace user approval. Apply the same check after a material PRD amendment and before resuming the pipeline; clarify an active PRD only through `autodev update`.

Trigger the pre-flight when the user says "write a PRD", "draft requirements", "spec out a new feature", or hands over an unstructured idea and asks to start auto-dev. For `update`, skip only the cold-start authoring steps; a material amendment still requires the semantic-intent check before resume. Skip the full pre-flight for `close`, `pause`, `resume`, `abort`, `retry`, `invalidate`, `grant-rerun`, `skip-gate`, and `acknowledge-dirty`.

## CLI install check

Before invoking any `autodev` verb, run `autodev --help` or `command -v autodev`. If unavailable or broken, read [references/install.md](references/install.md) and install the Python package from this skill root. Use the installed `autodev` path immediately; do not invent `vendors.yml` model choices for target repos. If the SDK-root `vendors.yml` is missing, ask the user whether to generate it from `sample-vendors.yml` (`python3 shared/vendors/scripts/init-vendors.py --sample sample-vendors.yml --out vendors.yml`) or pass `AUTODEV_VENDORS_YML`.

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
| `autodev grant-rerun <f> <gate> --reason "..."` | After a gate exhausts `L_MAX`, authorize one auditable producer correction. This neither passes nor skips the gate; the next blocking verdict halts again. |
| `autodev skip-gate <f> <gate> --reason "..."` | Override a mandatory gate |
| `autodev acknowledge-dirty <f> --reason "..."` | Override dirty-workspace block |
| `autodev abort <f>` | Hard-stop the run: write `.pause` sentinel (so orchestrator can't dispatch next stage) + kill running vendor subprocess + write interrupted failure. Run `autodev resume` before next `run`. |
| `autodev reset-session <f> design\|build\|ralph-review\|arch-review` | Forget one paused feature agent's provider-native conversation so its next turn starts fresh. Pause first; an active session lease blocks reset. |
| `autodev restore-design <f> [--package package-NNN]` | Restore a paused feature's latest (or named) hash-verified design-package snapshot after an interrupted or mistaken invalidation. |
| `autodev retry <f>` | Retry last failed stage |
| `autodev invalidate <f> <stage>` | Rollback a stage artifact |
| `autodev update <f> --amendment "..."` | Amend PRD; start new cycle |
| `autodev close <f> <reason> [--yes]` | Close feature |
| `autodev explain <f>` | Human-readable state |
| `autodev prd-lint <f>` | Validate the imported PRD against the v2 schema |

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

Quota lookup is implemented for Claude, Codex/OpenAI, Cursor, Agy, and Grok.
Agy uses its prompt-free loopback quota server and Grok uses ACP billing; neither
lookup spends model quota. Unreadable quota fails closed.

For panel transport, the default response quorum is
`panel.min_responding_reviewers: 2`. Each failed reviewer gets one immediate
retry, then a forced quota refresh. The synthesizer may proceed without that
reviewer only when the refresh positively confirms exhaustion. If fewer than
the configured quorum respond, the harness quota-pauses; if quota is unknown or
the failure is non-quota, the panel stays pending.

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
3. Before any write-like verb (`grant-rerun`, `skip-gate`, `acknowledge-dirty`, `abort`, `reset-session`, `restore-design`, `invalidate`, `update`, `close`), rerun `autodev status`.
4. For every background `run`/`next`, use `--watch` and attach one generic Monitor that implements [references/watch.md](references/watch.md). Do not invent shell sleep loops, cron polling, or per-feature heartbeat logic.
   - Treat `started` as the advertised heartbeat contract and reset the silence deadline on every watch marker.
   - Heartbeats are health signals; do not relay routine ones to the user.
   - Two missed heartbeat intervals require an immediate `autodev status` and a surfaced alert.
   - A non-success `terminal` marker requires the same immediate status/alert; `terminal outcome=complete` ends monitoring cleanly.
5. Do not construct or inject panel-review prompts; harness calls panel-review.

### Push alerts via `--watch`

`autodev run <feature> --watch` emits transition alerts plus harness-owned heartbeat and terminal markers. Run it in the background and attach the required generic Monitor; do not add a second polling loop. Full protocol in [references/watch.md](references/watch.md).

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
