---
name: auto-dev-lite
description: >
  Lightweight document-driven development from a user-owned core requirements
  document and an orchestrator-owned detail spec. Uses fresh subagents for a
  design dry run, scoped implementation, and stage/final reviews; checks both
  requirement conformance and unnecessary or redundant design/code mechanisms.
  Unresolved requirements and core changes return to the user. Triggers on
  "auto-dev-lite", "doc-driven development", "develop against this design
  doc", "implement per this document", or requests for a lighter alternative
  to the full auto-dev-sdk pipeline. Skip single-line fixes, typos, and changes
  with no design surface.
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
  see the conversation — including any guidance the user gives in chat
  mid-run, which reaches a review only as findings merged into its reply
  (§6).

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
- For each mechanism the detail doc introduces, prefer reuse or the simplest
  design that still satisfies every core requirement and constraint. A
  mechanism can be redundant even when it traces to a requirement if removing
  it or using one existing mechanism preserves the same required behavior.

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
2. Compare that reply against the intended direction — together with any
   user feedback attached to this round (§6), classified into the same
   four kinds. Four failure kinds:
   - **Drift** — the plan does things the documents never meant, or misses
     things they require.
   - **Invention** — the plan fills gaps with its own guesses.
   - **Divergence** — the subagent (or your own reading of its plan) shows
     the detail doc no longer faithfully elaborates the core doc.
   - **Excess or redundancy** — the subagent identifies a specific mechanism
     that can be removed, reused, or simplified while preserving all core
     requirements and constraints, or an unmotivated strict rule that makes
     the design less robust (robustness principle; ambiguous/security-relevant
     rejections excepted).
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
   divergence or evidenced excess/redundancy. If after **3 rounds** the same
   disagreement persists, that is not a wording problem — it's an unresolved
   requirement. Stop and put the question to the user (§5).

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
- Dev subagents prefer existing suitable mechanisms and the simplest
  implementation sufficient for the documents. If the detail doc prescribes
  a mechanism that appears unnecessary or redundant, they report its exact
  location and a supported simpler alternative instead of silently omitting
  it. The orchestrator corrects the detail doc and performs a fresh targeted
  Stage 2 check before redispatching code work; complexity required by the
  core doc cannot be removed without the user's decision (§5).
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

The reviewer answers five questions:

1. **Conformance** — does every change trace to the documents?
2. **Completeness** — is anything the documents require for this stage
   missing?
3. **Overreach** — does any change add behavior the documents never
   discussed?
4. **Fidelity** — do the changes honor the *core* document, not just the
   detail doc? A change that matches the detail doc but strays from the
   core requirements is a divergence finding, not a pass.
5. **Proportionality** — can a specific changed-code or affected-design
   mechanism be removed, replaced with an existing mechanism, or simplified
   while preserving every affected requirement and constraint, or does an
   unmotivated rule, check, or hard stop make the system less robust per the
   robustness principle (ambiguous/security-relevant rejections excepted)?

Route the findings — the reviewer's and any user feedback attached to this
round (§6), classified into the same five kinds:

- Conformance/completeness defects → dispatch fix subagents (doc-bound, same
  rules), then re-review the fixed area.
- Evidenced excess/redundancy → treat it as an actionable fix, dispatch code
  fixes through dev subagents, and re-review. If the detail doc prescribes
  the excess, correct it first and run a fresh targeted Stage 2 check before
  code rework. A required core-doc change or unresolved requirement goes to
  the user (§5).
- Overreach, doc-gap, or fidelity findings → §5 (or a detail-doc correction
  plus targeted rework, when the core doc clearly settles the question). Do
  not "keep it since it's written" — undocumented work is a stop signal even
  when the code looks good.

After the final stage, run one closing review over the full change set using
the same five checks. Neither a stage nor closing review passes with an
unresolved evidenced excess/redundancy finding. Then report to the user: what
was built, mapped section-by-section to the core document, plus every
detail-doc edit made along the way.

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

## §6 — User feedback at review points

Design guidance the user gives in chat mid-run ("use X instead of Y",
"that's over-engineered", "you missed Z") is not ignored, not improvised
on, and not written into a document outside the rules below. It enters the
pipeline as **one more reviewer's findings** at a review point: a Stage 2
dry-run reply or a Stage 4 stage/closing review reply.

1. **Output, never input.** The review subagent's prompt and inputs stay
   exactly what the template inlines. The user's feedback is not added to
   the prompt, the documents, the diff, or the scope for that review; the
   reviewer must not see or react to it.
2. **Same vocabulary.** When the reply returns, express each feedback item
   as a finding of that review point — Stage 2: drift, invention,
   divergence, or excess/redundancy; Stage 4: conformance, completeness,
   overreach, fidelity, or proportionality, with the same evidence pointer
   (file, document section) as far as the feedback supplies one. An item
   that fits no kind is a doc gap and routes as one (§5).
3. **Conflict check before merging.** Only once the reply is back — never
   before dispatch, or the reviewer would see the feedback through an
   edited document — read each item against the two documents in their
   existing roles:
   - Conflicts with the **core document** → do not merge. This is §5:
     quote the core-doc sentence and the feedback side by side; the user
     decides which one changes.
   - Conflicts only with the **detail document** → the detail doc misread
     the core doc. Fix the detail doc (the normal Stage 2 power: notify,
     don't wait), then merge.
   - No conflict → merge.
4. **Same routing.** Merged findings go through the routing that review
   point already has — Stage 2 steps 3–5, Stage 4 "Route the findings". No
   weighting, no veto: the user is one reviewer, and a fix subagent
   receives a user-originated finding exactly as it would any other.
5. **Timing.** Feedback that arrives while a review round is in flight is
   held and merged when that round's reply returns. Feedback that arrives
   between rounds — including while dev or fix subagents are running —
   attaches to the next review round dispatched (for running dev work, the
   stage review or re-review that follows it). Tell the user which round it
   attached to. A finding is consumed by exactly one round: once routed, it
   is not merged again.

## What this skill is not

- Not the full `auto-dev-sdk` pipeline — no `docs/features/` folder contract,
  no separate feature-documentation stage, no external vendor CLIs. Two docs, in-harness
  subagents, done.
- Not a general code-review skill — reviews here check *doc conformance and
  proportionality*. Use a dedicated code-review skill for bug hunts and
  style review.

## Files

| Template | Purpose |
| -------- | ------- |
| `prompts/doc-dry-run.md` | Stage 2: dry-run plan, divergence, and design proportionality check |
| `prompts/dev-task.md` | Stage 3: scoped, doc-bound implementation using the simplest sufficient mechanisms |
| `prompts/stage-review.md` | Stage 4: context-free conformance and proportionality review of a stage diff |
