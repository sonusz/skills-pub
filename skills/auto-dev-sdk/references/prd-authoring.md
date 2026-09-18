# PRD authoring (cold-start guide)

Use this guide when the user has an idea / requirement / reference
document but does NOT yet have a rigorous `prd.md` ready for
`autodev prd <feature> --from-file ...`. The goal of this guide is
to drive the PRD draft to a state where `autodev prd-lint` passes
and the PRD captures only what auto-dev-sdk's stages can act on.

This guide is for cold-start authoring. To **document existing
code**, use a code-to-docs tool instead; that is the inverse
direction (code → docs), not (idea → spec).

---

## When to invoke this guide

Trigger this workflow before any `autodev prd` invocation when ANY
of these is true:

- The user asked to "write a PRD", "draft requirements", "spec out
  a new feature", "turn this idea into something auto-dev can run".
- The user has an idea, a screenshot, a PDF, a reference doc — but
  no `prd.md` aligned with the v2 schema (sections below).
- The user's draft `prd.md` exists but `autodev prd-lint` fails or
  the user has not run it yet.

Skip this guide when the user already produced a structured PRD and
explicitly wants you to import it as-is.

---

## Canonical PRD structure (v2 schema)

The harness's Stage-0 lint checks for these section headings. Every
PRD MUST have:

```
# PRD: <feature-name>

## Problem            ← what is broken / missing today; why now
## Users              ← primary + future users; non-users explicit
## Architectural principles (binding)   ← optional but recommended; A1..An
## Requirements       ← R1, R2, ... (### R<n>: Title blocks)
## Assurance          ← optional; per-R rigor levels (strict/core/loose)
## Constraints        ← runtime, libraries, language, platform
## Success Criteria   ← observable verifications, not vibes
## Out of Scope       ← what this feature explicitly does NOT do
```

Plus optional `## Amendment <date>` blocks appended via
`autodev update <feature> --amendment "..."` after a feature is
underway. An amendment may introduce a new, uniquely numbered
`### R<n>: <Title>` requirement without rewriting the original section.

Every requirement is a `### R<n>: <Title>` block followed by 1-3
short paragraphs and optional bullets. Number sequentially without
gaps; renumber when inserting / deleting.

---

## Workflow

### 1. Bootstrap context (don't draft from blank)

Before writing any PRD bullet, read what already exists in the
target repo:

- `docs/features/*/complete/prd.md` — prior PRDs that might already
  cover related ground. Don't rebuild what's done.
- `docs/features/*/complete/implemented-spec.md` — the implemented
  primitives that the new feature must reuse, not rebuild.
- `docs/features/*/reference/*.md` — preserved reference / design
  documents that motivate the work.
- `docs/features/*/active/` — anything currently in flight that the
  new feature must coordinate with.
- `~/Downloads/skills/shared/library-registry.md` — already-adopted
  libraries the new feature should reuse.

Identify the partition:

| | Description |
|---|---|
| **Built** | Already in `complete/` — do NOT redo |
| **Missing** | Conceptually mandated but not yet implemented — primary scope |
| **Out of scope** | Mentioned in references but explicitly deferred |

The new PRD should cover only the **Missing** column.

### 2. Draft the skeleton

Write the seven canonical sections empty, then start filling them.
Do NOT write Requirements first — that pulls implementation
thinking before scope is clear. Order:

1. Problem (what gap?)
2. Users (who benefits?)
3. Out of Scope (what we're NOT doing — this is the strongest
   anti-bloat tool; write it early)
4. Architectural principles (binding constraints that override
   specific Rs)
5. Requirements (R1..Rn — capabilities only)
6. Constraints (runtime, libs, platform)
7. Success Criteria (observable verifications)

### 3. Assign per-requirement rigor (`## Assurance`)

Each `R<n>` carries a rigor level that mechanically decides which
panel findings block during the pipeline (see
`docs/proposals/rigor-tier.md`):

- `strict` — everything blocks, corner cases included (24×7 path).
- `core` — main-path correctness blocks; edge findings don't.
- `loose` — failures are cheap to discover and fix; only
  contradictions, over-design, and too-coarse sizing block.

Section shape (absent section ⇒ every R is `strict`):

```markdown
## Assurance

Default: loose
Release threshold: P1

| Req | Rigor | Rationale |
|---|---|---|
| R1 | strict | Core algorithm under test — the reason this PoC exists |
| R5 | loose  | Log formatting; failures visible immediately, trivially fixed |
```

Rows are needed only for Rs deviating from the default. Rationale is
required — it calibrates reviewers and is re-asked verbatim at
graduation or stall re-audits. Amendments override levels with
`Assurance: R3 core -> strict` lines (latest wins).

`Release threshold` is separate from rigor and severity. It accepts `P0`,
`P1`, or `P2`; `P1` is the default and preserves historical behavior. Use
`P0` for a time-critical release where P1/P2 findings must be retained as
deferred work but must not trigger another producer/design round. A later
amendment may set `Release threshold: P0` (last declaration wins).

**Elicitation protocol — never ask for a level by name.** Users have
no stable intuition for the labels but do for "can you accept this
failure?". Every question is consequence-acceptance:

1. Global default first, in consequence form: "if this breaks
   overnight with nobody watching and gets fixed next morning, is
   that fine?" → sets `Default:`.
2. Propose a level for every R with a one-line consequence sentence;
   show the full table for at-a-glance confirmation. No silent
   assignment.
3. For uncertain rows, pick the question form by your prior:
   - narrowed to two adjacent levels → ONE binary boundary question
     ("R5's cache corrupts under a rare concurrent write and may go
     unnoticed for days — acceptable?" yes → core, no → strict);
   - no prior → a 3-option tolerance card ordered loose → strict so
     the user stops at the first acceptable rung.
   Batch independent questions into one AskUserQuestion call. Do NOT
   run full sequential ladders (doubles round trips, injects
   acquiescence bias, discards your prior).
4. Scenario cards are mandatory three-field: failure example
   specific to the R, discovery latency, fix cost — understating
   blast radius elicits wrong levels.
5. The chosen tolerance statement becomes the `Rationale`
   near-verbatim.

### 4. Interrogation checklist

Before declaring the draft ready, walk this checklist. Every "no"
is a gap to either fill or explicitly close as Out of Scope. The
checklist generalizes the questions a careful reviewer asks.
**Trim by rigor**: run the heavyweight batteries (concurrency
floor/ceiling, recovery, operator surface) only for `strict` and
`core` Rs; for `loose` Rs a "no" is acceptable by construction and
should not generate PRD text.

**Capability boundary**

- [ ] Concurrency floor — what happens at zero load (cold start,
      empty queue)? Idle alert vs forced-spawn vs accept idle?
- [ ] Concurrency ceiling — what's the max simultaneous work? What
      counts toward the ceiling vs what doesn't (e.g. waiting on
      external input)?
- [ ] Failure modes — what does the runtime do when a subsystem
      keeps failing? No silent fallback (R3 of any runtime
      feature); alert + operator decides.
- [ ] Resource pressure — token budget, data quota, compute,
      external dep. Tier behavior or just rely on failure path?
- [ ] Recovery — what happens after a hard kill mid-cycle? What
      survives in filesystem? What state is rebuilt?

**Operator surface**

- [ ] Inspection paths — can the operator see *what just
      happened* in <30 seconds? Three-tier drill-down (brief →
      show → history) at every entity layer (direction, signal,
      handoff, …) ?
- [ ] Override mechanism — what can the operator stop / pause /
      kill / unblock at any time? What takes precedence over
      automation?
- [ ] Alert sparsity — what writes an alert vs what writes only
      audit? Aim for <5 alerts/day at steady state.
- [ ] Composability — does the CLI support `--json` so external
      thin skills / web frontends can render narrative summaries?

**Implementation discipline**

- [ ] LLM prompt format — does every harness-invoked LLM prompt
      include the exact output template, not just describe it?
- [ ] LLM stdout discipline — is stdout the deliverable for every
      LLM call (no "I'll write to a file" indirection)?
- [ ] Stop semantics — does any "stop" write a persistent
      sentinel BEFORE killing in-flight work?
- [ ] Main-loop ordering — does the runtime check overrides /
      pause sentinels / ceilings BEFORE dispatching new work?
- [ ] Routing exhaustiveness — are routing tables required to
      enumerate every (failure-type, target) pair?
- [ ] Cycle-scoped state — is state from prior cycles tagged so
      stale data does not pollute fresh cycle LLM context?
- [ ] Rules-vs-LLM tradeoff (A2) — does the PRD permit arbiter
      LLM calls (probe pattern) when a rule would require deep
      branching or many special cases?

**Cross-system reuse**

- [ ] Library reuse — for each external dependency, will it be
      registered in `shared/library-registry.md`? PRD should
      reference the registry, not re-litigate vendor choice.
- [ ] Shared infrastructure — is `shared/vendors` reused for any
      multi-vendor LLM calls?
- [ ] Predecessor reuse — does the PRD reuse artifacts /
      services / state from prior `complete/` features rather
      than rebuilding?
- [ ] Platform compatibility — Linux + macOS minimum? Are
      hot-path platform calls deterministic Python? May
      installation artifacts (cron / launchd / systemd) be LLM-
      generated under A2?

**Default discipline**

- [ ] No specific numbers in the PRD (e.g. "30 minutes",
      "20 directions", "5 retries"). Use "configurable, default
      ~30min" style; specific values land at scope/spec stage.
- [ ] No specific output schemas (field names, JSON layouts).
      The PRD requires a depth tier or capability; spec writes
      the fields.
- [ ] No implementation choice unless cross-cutting. PRD
      mandates "use existing broker SDK"; spec picks `ib_insync`.

### 5. Filter rubric

For each bullet a draft introduces, classify:

| Bullet kind | Lands in |
|---|---|
| Core capability | `### R<n>: ...` |
| Default choice (broker = IB, lang = Python) | `## Constraints` |
| Specific number / output field / layout | scope / spec stage, NOT PRD |
| Already implemented in predecessor | strike — don't restate |
| Known limitation, deferred | `## Out of Scope` (with reason) |
| Implementation discipline lesson | `## Constraints → Implementation discipline` subsection |

If a bullet doesn't fit any of these, it's noise. Cut it.

### 6. Lessons carryover

Keep an Implementation Discipline subsection at the end of
`## Constraints` capturing patterns that prior auto-dev-sdk runs
proved necessary. Update this section by appending after every
shipped feature whose run surfaced a new failure mode. Examples
already learned (do NOT re-derive in each PRD; reference and add):

- LLM prompts must include exact output templates, not prose.
- LLM stdout is the deliverable; no side-file output.
- Stop semantics write persistent sentinel before killing.
- Main-loop checks overrides / pauses / ceilings BEFORE dispatch.
- Failure routing enumerates every (failure-type, target) pair.
- Cycle-scoped state is tagged with originating cycle ID.

When this list grows, lift the canonical wording into a shared
`auto-dev-sdk/references/implementation-discipline.md` and
reference it from each PRD's Constraints section.

### 7. Validate semantic intent with a fresh subagent

Before import, test whether the PRD communicates the user's intent without
conversation context:

1. Give a fresh subagent only the PRD and its source references. Do not include
   the intended interpretation, suspected ambiguity, or prior conclusions.
2. Ask it to restate each requirement in plain language and flag multiple
   plausible readings, contradictions, unbounded scope, and accidental
   retention or deletion.
3. Compare its reading with the user's confirmed intent. A material mismatch
   means the PRD is unclear even if the subagent calls it acceptable.
4. Surface the mismatch to the user, make the smallest user-approved wording
   change, and repeat with a fresh independent pass until the readings align.

This is a semantic test, not an approval authority. The subagent must not add,
remove, or relax requirements, and its verdict never replaces user approval.

### 8. Validate format

`autodev prd <feature> --from-file <path>` validates the source before writing
it. After a successful import and before `autodev run`, run
`autodev prd-lint <feature>` and confirm 0 errors. The lint catches missing
sections, malformed R blocks, and duplicate IDs.

### 9. Promote

After import:

```
mv docs/features/<feature>/{planned,active}
```

(promotion is currently manual; see auto-dev-sdk README.) Then
`autodev run <feature>`.

---

## Design-stage POC clause

Optional, unlike the seven canonical sections. Add it only when the design
stage cannot resolve a high-risk architectural question from code,
authoritative documentation, or existing measurements — a genuinely new
mechanism, an unverified platform capability, a security-boundary claim.
Do not add it speculatively "in case design needs one"; an unused clause
invites a POC no requirement asked for, and the design-review gate below
treats every clause as binding once it's in the PRD.

### The clause

Paste (or closely paraphrase — the content matters, not the exact words)
this shape as its own `### A<n>:` architectural principle, or fold it into
an existing "evidence before commitment" principle if one already exists:

> The design phase may run a short proof of concept for a high-risk question
> that cannot be resolved from code, authoritative documentation, or
> existing measurements. A POC must test a named hypothesis against **the
> repository's designated non-production account** (never production),
> verify the resolved account identity at the start of every phase and
> refuse on mismatch, have explicit cost and time bounds, tag every created
> resource uniquely to the POC, record a resource manifest as each resource
> is created, include a negative security test of the POC's own surface,
> and fully tear down — verified, not assumed. The design phase cannot pass
> with residual resources. Every design-stage POC records its hypothesis,
> commands, result, and teardown, and leaves nothing behind.

If the PRD has no `## Architectural principles` section, land the same
content as a bullet under `## Constraints` instead of inventing the section
just for this one clause. Write "the repository's designated non-production
account" literally, or substitute the actual contract-named identity check
this repo already has (e.g. an `environments.yaml`-style `accounts.dev` /
`accounts.paper` entry) — do not invent a project-specific account name in
this shared guide.

### What the clause requires, itemized

- **Named hypothesis** — one falsifiable sentence, not "investigate X".
- **Identity gate** — every phase checks the resolved credential identity
  against the repository's designated non-production account *before*
  creating anything, and refuses (non-zero exit, logged) on mismatch. Not
  optional, even for a one-script POC.
- **Cost and time bounds** — stated up front, then measured against in the
  result. A bound with no measurement afterward is not evidence.
- **Unique tags** — every created resource tagged so a sweep can find it by
  tag alone, independent of the manifest.
- **Resource manifest** — appended at creation time, not reconstructed
  afterward, so a crash mid-POC still leaves a true list of what exists.
- **Negative security test** — the POC's own surface (network exposure, IAM
  scope, public addresses) is itself probed and must fail closed. A POC
  that proves a security mechanism without checking its own footprint
  proves nothing.
- **Verified teardown** — a sweep that checks the tag/manifest against the
  authoritative API for every resource class, not a script that merely
  issued delete calls and assumed they succeeded.
- **Record contents** — hypothesis + bounds + probe design; numbered phase
  scripts each starting with the identity gate; a full command log; the
  manifest; a results file with a one-line verdict and the measured
  numbers; a teardown log; a sweep proof. `autodev/prompts/stage-design.md`'s
  "Design-stage POC" rule gives the design agent the exact layout and the
  writable location that satisfies the harness's containment contract.

### Evidence policy — a recorded attempt is not rerun for ceremony

A timestamped POC attempt that recorded its environment/identity, an
artifact hash or pinned version, its inputs, its result, and its failure
mode (if any) **is evidence**. Do not require a second run just to
reproduce a record already in this shape — that is ceremony, not
verification. A design-stage rerun (panel finding, PRD amendment) reads the
existing record and re-runs the POC only if the underlying question
actually changed: a different hypothesis, a changed subject version, or a
finding that the recorded result doesn't actually establish what it claims.

### Interaction with the rigor table (`## Assurance`)

The clause is orthogonal to per-R rigor (`strict`/`core`/`loose`): rigor
governs how hard the design-review panel scrutinizes an *R's* coverage, not
whether a POC runs — the POC clause is an architectural principle, not an
`R`, and is unconditional once the PRD states it. A POC-mandating principle
is usually tied to a `strict` R (the reason a POC earns its cost is usually
that the R sits on the core/24×7 path), but assign rigor by the elicitation
protocol in step 3 as normal — do not infer `strict` just because a POC
exists, and never skip a required POC because its nearest R is `loose`.
Because the clause has no `R<n>` identifier, it does not appear as a row in
the design-review panel's mechanical PRD-coverage table (that table walks
`### R<N>:` blocks only); it is graded instead by a dedicated gate question
in `autodev/panel/prompts/review-design-review.md` ("Design-stage POC"),
which is where a violation becomes a P0 finding.

---

## Worked example

`docs/features/signal-runtime/prd.md` was authored using this
workflow:

- Bootstrap: read the prior feature's `complete/prd.md` and
  `complete/implemented-spec.md`, a `reference/core-flow-design.md`,
  and an uploaded product brief PDF.
- Skeleton: 7 sections drafted empty, then Out of Scope first
  (carved out tmux dashboard, dead-letter HTTP service, multi-user,
  live execution).
- Interrogation surfaced: token throttle was over-specified
  (multi-vendor reality drops it); concurrency ceiling missing
  (added R8 with population semantics excluding waiting / paused);
  CLI inspection lacked signal+handoff verbs (added to R12);
  language not pinned (added to Constraints with macOS+Linux);
  data source missing semantics not defined (added R10 with alert
  + operator interactive supply); arbiter LLM permitted (A2 +
  references idle-probe pattern).
- Filter: dropped 4 specific number constraints (30min cron, 4
  cooldown tiers, 15-min smoothing window, exact `--json` field
  names) into scope-stage decisions.
- Lessons: appended 6 Implementation Discipline items from the
  prior feature's build.
- Validate: `autodev prd-lint` passed, 14 requirements parsed.

The conversation that produced this PRD is the canonical example
of how the interrogation checklist surfaces gaps one at a time
rather than via single-shot drafting.
