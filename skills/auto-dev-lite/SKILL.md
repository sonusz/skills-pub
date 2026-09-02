---
name: auto-dev-lite
description: >
  Lightweight document-driven development orchestrator with a two-document
  contract. The user's core requirements document is the immutable source of
  intent (changed only after discussing with the user); from it the
  orchestrator derives a slightly more detailed working spec that it alone
  may edit. Flow: obtain the core doc (co-author it with the user if it
  doesn't exist) → derive the detail doc → comprehension gate: a context-free
  subagent on an everyday-tier model (Sonnet-class, medium effort) reads both
  documents and describes what it WOULD do — no execution — and flags
  divergence between them; the orchestrator repairs gaps by editing ONLY the
  detail doc until a fresh reader's plan matches intent → development: the
  orchestrator fans out medium-effort dev subagents sized to the workload but
  never edits code itself → after each stage, context-free review subagents
  check every change against both documents. Anything the documents don't
  cover — or a divergence unfixable without touching the core doc — stops
  the pipeline for user confirmation. Triggers on "auto-dev-lite",
  "doc-driven development", "develop against this design doc", "implement
  per this document", or when the user wants a lighter alternative to the
  full auto-dev pipeline. Skip for single-line fixes, typos, or changes with
  no design surface.
allowed-tools: Task, Read, Grep, Glob, Bash
---

# Auto-Dev-Lite

Document-driven development with a strict division of labor: **documents
control direction, subagents write code, the orchestrator only dispatches.**

Three invariants hold for the entire run:

1. **The orchestrator never edits code.** No Edit/Write on source files, no
   "quick fixes while you're there". Every code change — including one-line
   follow-ups to review findings — goes through a dev subagent.
2. **Two documents, two ownerships.**
   - The **core document** — provided by the user, or co-authored with them
     in Stage 0 — is the source of intent. The orchestrator **never modifies
     it unilaterally**: any change to it happens only after discussing with
     the user and getting their decision (§5).
   - The **detail document** — derived by the orchestrator in Stage 1 — is
     the operational spec, and the *only* document the orchestrator may edit
     on its own (notifying the user of each edit, without waiting).
3. **The documents bound all work.** Every change must trace to what the
   documents say — the core doc governs intent, the detail doc governs
   specifics. Work neither document covers is never improvised — it triggers
   the stop-and-discuss rule (§5).

## Subagent settings (applies to every spawn)

- **Model**: an everyday-tier model — `model: "sonnet"` on Claude Code (or
  the platform's equivalent workhorse tier). Do not use the orchestrator's
  own top-tier model for subagents; the point of the comprehension gate is
  to test the documents against an ordinary reader.
- **Effort**: medium. Where the harness exposes reasoning effort, set it to
  medium; otherwise state in the prompt that a straightforward, non-exhaustive
  pass is expected.
- **Context-free means fresh**: reviewer subagents are new `general-purpose`
  agents — never `fork` (a fork inherits the conversation and defeats the
  purpose). They receive only what their prompt template inlines: the two
  documents, the diff, the repo root. They may read the repo; they must not
  see the conversation.

Prompt templates live in `prompts/`; substitute every `<variable>` before
dispatch (placeholders are documented at the top of each template).

## Stage 0 — Obtain the core document

The skill requires a core requirements document as input.

- **User provided a path** → read it, confirm it actually describes the work
  (goal, scope, expected behavior). Thin or stale → treat as missing.
- **No document** → build one with the user before anything else. Interview
  for: the goal and why, in-scope / out-of-scope, expected behavior and
  interfaces, constraints (compatibility, performance, style), and what
  "done" looks like. Draft the doc, show it, and iterate until the user
  approves. Default location `docs/<feature>-design.md` in the target repo
  unless the user names one.

Either way, from this point the core document is **user-owned**: the
orchestrator reads it, quotes it, derives from it — but edits it only
through §5.

## Stage 1 — Derive the detail document

Before any review or code, expand the core document into a slightly more
detailed working spec — the detail document. Default location: next to the
core doc as `<core-doc-basename>.detail.md`, unless the user names one.

- **Elaborate, don't extend.** Everything in the detail doc must be
  derivable from the core doc: name the components and files involved,
  pin down interfaces and behaviors the core doc implies, spell out
  verification steps and staging. Adding scope the core doc never asked for
  is invention, not elaboration — the comprehension gate exists to catch
  exactly that.
- Where the core doc leaves a genuinely open decision, either make the
  conservative, reversible choice and record it explicitly in the detail doc
  (so reviewers can see it was a choice), or — if a user could reasonably
  want a say — take it to the user now rather than burying it.
- Keep it proportionate: "slightly more detailed" means an ordinary reader
  can execute it without guessing, not a design novel.

## Stage 2 — Comprehension gate (dry-run loop)

Before any code is written, verify the two documents survive contact with a
reader who has none of your context.

1. Spawn a fresh context-free subagent with `prompts/doc-dry-run.md`
   (everyday model, medium effort). It reads **both documents** — and the
   repo, for grounding — and replies with (a) the concrete plan it *would*
   execute, (b) its assumptions, and (c) any **divergence it sees between
   the core doc and the detail doc**: places where the detail doc drifts
   from, contradicts, or silently extends the core requirements.
   **It must not execute anything.**
2. Compare that reply against the intended direction. Three failure kinds:
   - **Drift** — the plan does things the documents never meant, or misses
     things they require.
   - **Invention** — the plan fills gaps with its own guesses.
   - **Divergence** — the subagent (or your own reading of its plan) shows
     the detail doc no longer faithfully elaborates the core doc.
3. On any failure: **edit only the detail document** to close the specific
   gap — the core document is off-limits here — then tell the user in one
   short message what changed and why (*do not wait for a reply*), and loop
   back to step 1 with a brand-new subagent (never reuse the previous one;
   its corrected understanding is exactly the context you're testing
   against).
4. **If the fix would require changing the core document** — the divergence
   cannot be repaired while keeping the core requirements as written — that
   is not yours to decide. Stop and discuss with the user (§5).
5. Exit the loop when a fresh reader's plan matches intent and reports no
   divergence. If after **3 rounds** the same disagreement persists, that is
   not a wording problem — it's an unresolved requirement. Stop and put the
   question to the user (§5).

## Stage 3 — Development fan-out

Derive stages and tasks from the detail document, then dispatch.

- **Partitioning is your judgment call.** Split by the detail doc's own
  structure where possible (components, endpoints, phases). Independent
  tasks run as parallel subagents in one message; dependent work is
  sequenced into stages. A small change may be one subagent and one stage —
  do not manufacture parallelism.
- Each dev subagent gets `prompts/dev-task.md` with **both documents**
  inlined (core doc = intent authority, detail doc = operational spec), an
  explicit task scope, and the standing order: *implement only what's in
  scope; if you hit anything the documents don't answer — or the two
  documents disagree — stop and report back, do not improvise.*
- When a dev subagent reports a doc gap or a core/detail conflict, route it
  through §5 (or a detail-doc fix + quick Stage 2 recheck, if the core doc
  already answers it) before dispatching anyone else into that area.
- The orchestrator integrates results only by dispatching further subagents
  (e.g., "reconcile these two branches of work"), never by editing directly.

## Stage 4 — Stage review

After each stage's code lands, spawn one or more **fresh context-free**
review subagents (everyday model, medium effort) with
`prompts/stage-review.md`: both documents, the stage's diff, and the stage
scope — no conversation context, so the review tests what the code *is*,
not what the orchestrator believes it is.

The reviewer answers four questions:

1. **Conformance** — does every change trace to the documents?
2. **Completeness** — is anything the documents require for this stage
   missing?
3. **Overreach** — does any change add behavior the documents never
   discussed?
4. **Fidelity** — do the changes honor the *core* document, not just the
   detail doc? A change that matches the detail doc but strays from the
   core requirements is a divergence finding, not a pass.

Route the findings:

- Conformance/completeness defects → dispatch fix subagents (doc-bound, same
  rules), then re-review the fixed area.
- Overreach, doc-gap, or fidelity findings → §5 (or a detail-doc correction
  plus targeted rework, when the core doc clearly settles the question). Do
  not "keep it since it's written" — undocumented work is a stop signal even
  when the code looks good.

After the final stage, run one closing review over the full change set, then
report to the user: what was built, mapped section-by-section to the core
document, plus every detail-doc edit made along the way.

## §5 — Stop-and-discuss rule

Stop the pipeline and consult the user whenever:

- development uncovers a problem neither document ever discussed;
- realizing the requirement turns out to need **substantial work the
  documents don't cover** (new dependency, schema change, cross-cutting
  refactor);
- a dev subagent has already produced significant out-of-scope work;
- a divergence between the core and detail documents **cannot be fixed
  without changing the core document**;
- the Stage 2 loop hits its 3-round limit on the same disagreement;
- any other situation that would require editing the core document.

Present: what was found, why the documents don't answer it, and the options
(usually: amend the core document to include it / explicitly rule it
out-of-scope / rethink the approach). **Wait for the user's decision.** Only
after that decision may the core document change; fold the outcome into both
documents, and — if the change is material to already-dispatched work — pass
the updated pair through a quick Stage 2 check before resuming.

This is the one place the pipeline blocks on the user. Detail-doc edits in
Stage 2 notify without waiting; core-doc changes and scope decisions always
wait.

## What this skill is not

- Not the full `auto-dev` pipeline — no `docs/features/` folder contract, no
  feature-spec stage, no external vendor CLIs. Two docs, in-harness
  subagents, done.
- Not a code-review skill — reviews here check *doc conformance*, not
  general code quality. Use `pr-review` / `multi-lens-review` for bug hunts.

## Files

| Template | Purpose |
| -------- | ------- |
| `prompts/doc-dry-run.md` | Stage 2: context-free reader plans against both docs — without executing — and flags core/detail divergence |
| `prompts/dev-task.md` | Stage 3: scoped implementation task, doc-bound, stop-on-gap and stop-on-conflict |
| `prompts/stage-review.md` | Stage 4: context-free conformance/completeness/overreach/fidelity review of a stage diff |
