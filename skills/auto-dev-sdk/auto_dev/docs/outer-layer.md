# Outer-layer contract (PRD R5 / R8 / R12)

auto-dev-sdk runs as a standalone CLI. An "outer layer" is anything that
invokes the CLI on behalf of a user: a Claude Code session, a codex
session, a human in a terminal, or a CI pipeline.

## Hard rules

1. **Writes only via CLI.** The outer layer never writes gate files,
   artifacts, or the lock directly. All state changes go through
   `auto-dev <verb>`.
2. **Reads are free.** `ls docs/features/<feature>/active/`,
   `tail log.jsonl`, `cat scope.json` etc. are fine — they never change
   state.
3. **No vendor-specific API between layers.** The only interface between
   outer and inner is: CLI exit code, stdout/stderr, artifact files,
   `log.jsonl`.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Verb completed |
| 1 | Error (pipeline, config, or unhandled) |
| 2 | Gate pending (blocks waiting on user) |
| 3 | Lock conflict (another process holds `.lock/`) |

## Gates

| Gate | Raised by | Cleared by |
|------|-----------|------------|
| `prd-review` | `implement` after completeness check | `auto-dev approve <feature> prd-review` |
| `close-approval` | `implement` after review stage | `auto-dev approve <feature> close-approval` *then* `close` |

## LLM-outer-layer behaviors (MUST, PRD R5)

An LLM outer layer (Claude Code, codex) MUST implement these behaviors.
Each defends against a different staleness/drift failure mode:

1. **Current-feature tracking.** Track the most recent `<feature>`
   argument across CLI calls as the session's current feature. Re-anchor
   when the user switches features.
2. **Pre-turn passive poll.** Before responding to each user turn, run
   `auto-dev status <feature>` and surface any state changes
   proactively. Defends against stale answers when the pipeline
   advanced between turns.
3. **Pre-write recheck.** Before each `approve` / `abort` / `update`,
   run `auto-dev status <feature>` once more and confirm intent with
   the user against the latest state. Defends against decisions made
   on stale context.
4. **Scheduled wake.** While the pipeline is active, poll via
   `ScheduleWakeup` / `/loop` (default 10–15 min) and surface progress.
   Stop polling on close / abort.
5. **Allow-cmd prediction.** Before launching `implement`, read
   `vendors.yml` + `prompts/` and predict the command set the subagents
   will need. Confirm with the user, pass via `--allow-cmd`. If the
   build halts on an unauthorized command (visible in
   `build.json.deviations`), ask the user whether to extend the allow
   list and `resume`.

## Permission model (PRD R12)

The inner tool executor has its OWN permission system, decoupled from
outer-layer settings.

- Declared ONCE at `implement` start via `--allow-cmd` / `--deny-cmd`
  (comma-separated glob or `re:<regex>` patterns).
- Denied commands are refused by the executor; the subagent records a
  non-blocking deviation and continues.
- No mid-stream permission elevation: the executor never writes a gate,
  never sends IPC, never blocks waiting for permission.
- No `--inherit-permissions` from the outer layer's config. Outer LLMs
  are expected to predict the command set explicitly (see behavior 5).

## Invariants the CLI upholds

- `.lock/` is created via atomic `mkdir`; second concurrent `implement`
  exits with code 3 immediately.
- Artifact writes are atomic (`.tmp` → rename). Orphan `.tmp` files
  older than 1 hour are reported on preflight but never auto-deleted.
- Hashes (`sha256:` + 64 hex) are SHA-256 of raw bytes with no
  normalization. Trailing newlines affect the digest.
- Staleness cascade: mutating a stage's upstream source invalidates all
  downstream artifacts. Fresh detection is driven by comparing each
  artifact's recorded `source_hash` to the current upstream file hash.
- `log.jsonl` is append-only JSONL with one event per line;
  `schema: 1` until a breaking change bumps it.

## Minimum happy-path (no outer Claude)

```bash
# Write a PRD (or place one by hand).
auto-dev prd my-feature --from-file ~/drafts/prd.md

# Start the pipeline; review PRD and approve.
auto-dev implement my-feature --prd-review-mode=none
auto-dev approve my-feature prd-review
auto-dev resume  my-feature   # continues from filesystem state

# Pipeline runs scope / plan / build / spec / review.
# At the close-approval gate:
auto-dev approve my-feature close-approval
auto-dev close   my-feature complete
```

Every artifact is readable with a text editor between steps.
