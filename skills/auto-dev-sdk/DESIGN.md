# auto-dev v3 — consolidated design plan

**Status:** v3-core landed 2026-04. This document is the design record
of intent; the implementation has moved on in places. For current
state, read the source under `autodev/` and the stage/panel prompts
under `autodev/prompts/` + `autodev/panel/prompts/`. Notable post-
design changes: (1) `architecture-review` gate was merged into
`prd-review` (one panel checks PRD↔scope coverage AND scope↔arch
fit); (2) `review.md` → `review.json` with per-PRD-requirement
coverage; (3) `ralph-review` output is JSON; (4) the G counter and
severity-escalation machinery were removed — only per-gate L (cap
L_MAX=3) remains.

---

## 1. Scope — what v3 is and isn't

**v3 is not a rewrite of v2.** It is a set of targeted changes to
the existing `auto-dev-sdk` codebase at `/Users/xzhong/Downloads/auto-dev-sdk/`:

- **Keep v2's architectural skeleton**: orchestrator (program, not
  LLM) + subprocess dispatch per stage + artifact cascade +
  filesystem-as-state. Two panel-reviews in this session both
  ended up endorsing this shape.
- **Remove v2's band-aid and over-built parts**: biased gate
  prompts, revision-loop framework (G/L/M/severity machinery),
  vendor abstraction layer, build-blocking routing, markdown→JSON
  synthesizer bridge.
- **Add v3-specific patterns**: pairwise+anchor gate structure,
  attribution-preserving generation, verifiable-before-panel
  pre-check, outer interactive skill for PRD intake.

Net code-size estimate: v2 ~6,200 LOC → v3 target ~4,000–4,500 LOC
(~25-35% reduction, mostly from deleted over-engineering).

## 2. Motivation

### v0.1 (deferred 2026-04-19)

Primary failure: the agent ignored SKILL.md's prose instructions
and went too far before anyone noticed. Also: built a vendor-agnostic
library (~3,400 LOC) when the environment already had
`claude`/`gemini`/`codex` CLIs; the `run_subagent → JSON` adapter
contract was incompatible with multi-turn TDD. Full POST-MORTEM at
`/Users/xzhong/Downloads/auto-dev-sdk/docs/features/auto-dev-sdk/deferred/POST-MORTEM.md`.

### v2 (running, dogfooded, mixed signals)

v2 architecturally got it right: orchestrator + subprocess per
stage + artifact cascade. **During dogfood runs, the PRD-review
gate struggled to converge** — each PRD amendment drew new cross-
layer findings. The user responded with a dialogue-mode prompt
rewrite:

- Scoping lists ("Do NOT flag wording preferences / imagined risks /
  implementation detail") that narrow the reviewer's question
- Best-guess + follow-up output format (explicitly to resolve
  intent ambiguities in one round)
- Finding-category constraints (only `invariant_violation` blocks
  at PRD layer; non-PRD layers may still emit `risk`)

**That rewrite did converge PRD and ship phase-5b.** The
dialogue-mode pattern is not a band-aid — it is a validated fix
documented in `docs/OPEN-ITEMS.md` §2. The remaining concern v3
is trying to address is narrower: **the current dialogue-mode
format pre-shapes the output space (CLARIFICATION + best-guess +
alternative + follow-up)**, which means three cross-vendor
reviewers often produce structurally-similar findings. That
*hypothesis* — that pre-shaped output space collapses useful
divergence — is what v3's pairwise+anchor design exists to
validate empirically. It is not a settled diagnosis of v2
failure; it is a direction the incremental rollout (§7 step 4)
is designed to test. If G1 with no output-shape constraints does
not converge, v3 needs a rollback plan to the current dialogue-
mode pattern.

### What v3 does

1. **Remove the biased prompts**: replace with pairwise+anchor
   gate structure. Visibility partition enforces focus
   mechanically; no "Do NOT flag …" needed.
2. **Trim v2's over-built subsystems**: revision loops, vendor
   abstraction, build-blocking routing, markdown→JSON bridge.
3. **Add the PRD intake layer**: outer interactive skill ensures
   PRDs reach scope with basic schema + structure, so downstream
   attribution works mechanically.
4. **Formalize attribution contracts**: every generator produces
   artifacts with explicit provenance columns, so reviewers can
   verify many findings mechanically (script or retrieve) without
   paying panel cost.

---

## 3. Governing principles

Six principles distilled from the two panel reviews and the
subsequent refinements. Each governs multiple design decisions.

### P1. Context scope, not role

For LLM reviewers, the primary partition axis is **what they see**,
not **what role they play**. Every LLM can do everything; what
varies across instances is their context. Human-workflow concepts
like "Product Owner / Architect / QA" do not map onto LLM
instances.

**Implication**: reviewers at different gates differ by their
visibility packet, not by persona prompts. Same prompt, different
packet = different (useful) output. Same packet, different
persona prompts = partitioned (less useful) output.

### P2. Verifiable before panel

Panel review is expensive and only earns its cost on questions
that have **no authoritative source and no deterministic
verification path**. Everything else should be handled by:

- **Structural scripts** — schema, cross-reference integrity,
  coverage set-difference, ID uniqueness
- **Retrieve-and-verify** — claims against external authorities
  (RFCs, standards, API docs): retrieve and compare, don't
  triangulate vendor training data

This mirrors panel-review's own Preflight #3 ("answerable by
execution → stop, run tests").

### P3. Attribution-preserving generation

Every generator in the pipeline writes explicit attribution for
every item it produces. Reviewers verify attribution mechanically
(the cited source exists, says what the artifact claims) before
panel fires for semantic judgment.

**Standardized attribution vocabulary**:

| Tag | Meaning |
|---|---|
| `prd:<section>` | Directly derived from PRD text |
| `scope:<id>` | Derived from a scope item |
| `trace:<req-id>` | Derived from a trace row |
| `spec:<section>` | Derived from a spec section |
| `inferred` | Derived by reasoning from sources the artifact itself cites |
| `commonsense` | Added from generator's training knowledge, not grounded in any upstream artifact |

The `commonsense` tag is the key disclosure — when plan agent adds a
test case from "training knows empty-input validation usually tests
null too," tagging it `commonsense` makes the choice visible to
reviewers.

### P4. Primary pair + PRD anchor

Each review gate has the same shape:

- **Primary pair** (the handoff being audited): two adjacent
  artifacts. Finding blame goes to one of these.
- **PRD anchor** (locked, presumption of closure): original intent.
  Provides ground truth for semantic verification without inviting
  re-litigation of earlier gates.

Rationale: pairwise handoff gives blame attribution and physical
layer-bleed prevention; PRD anchor lets reviewers verify semantic
fidelity back to original intent; presumption of closure prevents
repeatedly re-raising concerns earlier gates already accepted.

### P5. No reading restrictions in reviewer prompts

Reviewer prompts may state:

- What artifacts are in the visibility packet
- What question the gate is asking
- What finding categories to produce
- That other artifacts are physically not available

Reviewer prompts may NOT state:

- "Do NOT flag X" (suppresses divergence)
- "Focus on Y, skip Z" (causes missed coverage — producer-missed
  items would stay missed)
- "Anchor is quick reference, read selectively" (same problem)

**Any constraint on reading is bias. Any reasonable finding should
be raisable; noise is filtered at synthesis, not at reviewer.**

### P6. Synthesizer-side blame filter

Enforcing "presumption of closure" via reviewer instructions is
leaky (reviewers can self-censor when they shouldn't). Instead,
the synthesizer post-processes findings:

- Finding that targets ONLY the anchor (e.g., PRD wordsmithing) →
  drop with reason `"belonged to earlier gate (G1 / G2 / …)"`
- Finding that cross-references anchor AND primary pair → keep;
  this is a genuine emergent-composition concern

This mechanizes the "anchor is locked" discipline without
censoring the reviewer.

**Schema requirement**: P6 requires the finding schema to carry
enough information for the synthesizer to attribute targets
deterministically. v2's current schema
(`autodev/artifacts/verdict.py::Finding`) stores
`{severity, vendor, summary}` and a free-form `cited_artifact_span`.
v3 adds a required `targets: list[str]` field where each entry is
one of `"primary_pair.<artifact_name>" | "anchor"`. Reviewers emit
this at write time (v3 reviewer prompts instruct them to tag each
finding with the artifact it blames). Without this schema change,
P6's anchor-filter is advisory; synthesizer cannot deterministically
drop anchor-only findings.

### P7. Iteration budget (explicit cap)

v3 deletes v2's G/L/M severity counter machinery (§6). To prevent
infinite rerun loops when a gate keeps producing findings, v3
carries a simpler per-gate cap:

- `L_MAX = 3` reruns per gate per cycle. On the 4th failure, halt
  for user decision instead of looping.
- No global G counter (v2's G was its Achilles heel — every
  amendment bumped it, unrelated to the current gate's state).
- No severity-weighted accounting. `L` is just "number of times
  this gate's producer has been rerun in this cycle."
- Reset on `autodev update --amendment` (same as v2 behavior).

This is the minimum machinery needed to prevent the infinite-loop
failure mode §6's delete list introduces.

---

## 4. Architecture

Three distinct layers, none of them a continuous LLM:

```
┌─────────────────────────────────────────────────────────┐
│                                                         │
│  User ↔ Outer skill (interactive)                       │
│  Slash command, markdown prompt, lives in user's        │
│  Claude Code session. Schema-enforces PRD, interviews   │
│  if needed. Hands off to orchestrator when PRD is       │
│  schema-compliant.                                      │
│                                                         │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  Orchestrator (program, non-LLM)                        │
│  Reads filesystem state, sequences stages, spawns       │
│  fresh subagents per stage via claude -p, validates     │
│  artifacts, enforces gate predicates. Owns the "push    │
│  progress forward" responsibility. Does not read user   │
│  messages mid-run.                                      │
│                                                         │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  Stage subagents (fresh, single-shot, headless)         │
│  - scope subagent: PRD → scope.json                     │
│  - plan subagent: PRD + scope → trace.md + test-plan    │
│  - ralph's internal loop: build → spec → review         │
│    (runs repeatedly until Fully or stall)               │
│                                                         │
│  Each invocation is `claude -p` with bounded prompt +   │
│  bounded visibility. Stateless. Exits after writing     │
│  its artifact.                                          │
│                                                         │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  Review gates (fresh LLM reviewer instances)            │
│  - G1 (PRD ↔ scope)                                     │
│  - G2 (scope ↔ plan + test-plan, PRD anchor)            │
│  - Close (spec ↔ review.md, PRD anchor)                 │
│                                                         │
│  Each gate dispatches fresh claude -p instances with    │
│  its visibility packet. Same prompt across all          │
│  reviewers at the same gate.                            │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

**No continuous driver**. The orchestrator is the only thing that
persists across the pipeline, and it's not an LLM. Every LLM
invocation is fresh, bounded, and exits after producing its output.

This splits cleanly across the three failure modes identified in
the first panel:

| Failure mode | Enforcement |
|---|---|
| Agent skips a required step | Orchestrator's gate predicates refuse to advance |
| Agent stalls ("your call?") | Each invocation is non-conversational; orchestrator doesn't pause |
| Agent drifts off-task | Bounded prompt + bounded artifact contract per invocation |

---

## 5. Pipeline walkthrough

### Stage 0 — PRD intake (outer skill)

**Invocation**: user types `/auto-dev-v3 implement <feature>` in
Claude Code. This loads an interactive markdown prompt that talks
directly with the user.

**Purpose**: produce a schema-compliant `prd.md` that the orchestrator
can reliably hand to downstream stages.

**What it does:**

1. **Schema check**: PRD must have six sections:
   - Problem / Users / Requirements / Constraints / Success criteria /
     Out of scope
2. **Structured requirements**: requirements must be addressable
   markers (`### R1: …`, `### R2: …`) so scope can reference them via
   `prd_ref: "R1"` and G1's mechanical pre-check can verify refs
   resolve.
3. **Basic sanity check**: each requirement has a clear object +
   action; success criteria are observable; out-of-scope is explicit.
4. **Interactive interview** (if user has no draft): go through the
   six fields conversationally.
5. **Hand off**: once PRD is schema-compliant and human-approved,
   write to `docs/features/<name>/active/prd.md` and invoke the
   orchestrator.

**What it does NOT do:**

- ❌ Semantic review of PRD quality — that's G1's job, with scope
  as reference point. Single-file PRD review degenerates into
  wordsmithing (the v2 PRD-review failure mode).
- ❌ Sign off "PRD approved" as a gate. This layer only ensures
  form; substance gets reviewed at G1.
- ❌ Call a panel. Cheap single-LLM + script is enough here.

### Stage 1 — Scope generation

**Invocation**: orchestrator calls `claude -p` with the scope
subagent prompt. Inputs: `prd.md`, `prd_hash`.

**Output contract**: `scope.json` with:

```
{
  "feature": "...",
  "source": "prd.md",
  "source_hash": "sha256:...",
  "mode": "initial" | "update",
  "in_scope": [
    { "id": "bf-1", "description": "...", "prd_ref": "R1", "status": "active" },
    ...
  ]
}
```

Every `in_scope` item has `prd_ref` pointing at a PRD section — this
is scope's attribution column (per P3).

**No review at this stage**. The orchestrator validates `scope.json`
is parseable, hashes match, and moves to G1.

### Stage 2 — G1 gate: PRD ↔ scope

**Visibility packet:**
- `prd.md` (full)
- `scope.json` (full)

Nothing else. Reviewer sees no trace, test-plan, code, or prior
reviewers' findings.

**Verifiable pre-check** (runs first, no panel):

| Category | Check |
|---|---|
| Structural (script) | scope.json schema valid; IDs unique; `prd_ref` strings resolve to actual PRD sections; status enum valid; hash provenance matches |
| Coverage (script) | Every active `### R<n>:` in PRD has at least one `in_scope` item referencing it |
| Retrieve-and-verify (triggers only if PRD cites external authorities) | RFC/standards citations exist; API refs match current docs |

**Panel prompt** (same text to all three reviewers):

```
You are an interface validator.

You see two artifacts:
  PRD — original intent
  scope.json — decomposition of PRD into addressable items

Read both in full.

Report findings in these categories:

MISSING  — a PRD requirement not covered by any active scope item
INVENTED — a scope item whose description extends beyond / contradicts
           what the PRD states
AMBIGUOUS — a scope item or PRD clause that admits two reader
            interpretations, such that two downstream planners
            would derive incompatible plans

Severity per finding:
  invariant_violation — would make downstream work unsound
  risk                — could make downstream work unsound
  opinion             — observational

Output plain markdown. No JSON tags.

You are deliberately given only PRD and scope.json. If you find
yourself wanting to ask "how will this be tested?" or "which file
gets modified?" — those belong to later gates. Trust the chain and
focus on whether scope.json is a faithful decomposition of PRD.

IMPORTANT (Codex): write your full analysis to the output file.
```

Notes: no "Do NOT flag" blocklist. The visibility partition is the
mechanical restriction. Prompt only states the reviewer's question,
finding categories, and severity taxonomy.

**Panel cardinality**: **cross-vendor** (Claude + Gemini + Codex
in parallel). G1 is the intent-frontier gate where PRD's human
ambiguity is translated to formal decomposition — the canonical
"same-question, different-answer divergence" case.

**Synthesizer**: extracts per-vendor verdicts and findings, does NOT
merge across vendors, does NOT derive overall verdict (orchestrator
decides). For G1, no anchor-filter is needed (PRD is already in the
primary pair).

**Orchestrator decision logic**:

```
if pre_check_fails:
    gate = fail; reason = pre-check violation
elif no invariant_violation findings:
    gate = pass
elif all invariant_violations target scope.json:
    gate = needs_revision → rerun scope subagent with findings
elif all invariant_violations target PRD:
    gate = needs_user_decision (PRD revision belongs to user)
else (mixed blame):
    gate = needs_user_decision
```

### Stage 3 — Plan generation

**Invocation**: orchestrator spawns plan subagent with
`(prd.md, scope.json)`. Output: `trace.md` + `test-plan.md`.

**v2's existing `stage-plan.md` is kept** — it's a clean generator
prompt that already reads both PRD and scope to produce richer
test-plans (see "P3 corollary" below).

**Required addition (v3)**: plan agent must tag each row with a
`Source` column per P3's attribution vocabulary. This is a
prompt-level change, not a schema overhaul.

**P3 corollary — why plan reads PRD and why G2 still needs PRD
anchor**: plan agent reads PRD directly (not just scope) so
test-plan captures PRD's invariants / SLAs / edge cases that scope's
one-liners would lose. Cost: plan agent might over-infer — introduce
test cases scope didn't authorize or training-data-derived cases
without PRD grounding. G2's PRD anchor is the corresponding review
mechanism. Alternative (plan reads only scope) would cause
**under-inference** — PRD invariants silently dropped, surfacing
only at close gate where fix cost is high. v2's choice (plan reads
PRD) is preserved.

### Stage 4 — G2 gate: scope ↔ plan (PRD anchor)

**Visibility packet:**
- Primary pair: `scope.json` + `trace.md` + `test-plan.md`
- Anchor: `prd.md`

Nothing else.

**Verifiable pre-check:**

| Category | Check |
|---|---|
| Structural | trace.md rows reference valid scope_ids; test-plan cases reference valid scope_ids; every active scope item has ≥1 trace row + ≥1 test case (set-diff = 0) |
| Attribution | every `Source: scope:<id>` / `Source: prd:<section>` resolves; count of `Source: commonsense` rows reported |
| Retrieve-and-verify | external authority citations (RFC etc.) in test descriptions verified against retrieved source |

**Panel prompt** (same text to all reviewers):

```
You are an interface validator.

You see three artifacts:
  Primary (subject of review):
    scope.json    — decomposition accepted at G1
    trace.md      — requirement-to-test mapping (with Source column)
    test-plan.md  — test cases (with Source column)
  Anchor (locked, accepted at earlier gates):
    prd.md        — original intent

Read all three in full. Raise any finding you see.

Finding categories:
  MISSING   — a scope item or PRD invariant not covered by plan
  INVENTED  — a test case with no grounding in scope or PRD
  AMBIGUOUS — a test case whose description admits two readings
              that would lead to incompatible implementations

Context for framing findings (not reading restrictions):
- PRD and scope.json were reviewed at G1. Default blame for new
  findings is the plan stage (trace.md / test-plan.md).
- If a finding points at PRD or scope, include enough evidence
  for the synthesizer to judge whether it's a genuine
  cross-artifact composition issue (escalate) or a re-raised
  earlier-gate concern (drop).

Severity: invariant_violation / risk / opinion.

Output plain markdown. No JSON.

You are NOT given code, spec, build.json, or review.md. Those
belong to later stages and gates.

IMPORTANT (Codex): write your full analysis to the output file.
```

**Panel cardinality**: **same-vendor multi-instance** (typically 2
Claude instances). G2 is mostly a structural/attribution-driven
handoff; cross-vendor triangulation doesn't earn its cost here
(attribution plus script catch most of what matters; panel covers
the remaining judgment calls on test sufficiency).

**Synthesizer**: extracts per-vendor findings. **Applies anchor-
filter**: findings that target PRD alone (no cross-reference to
primary pair) are dropped with reason `"PRD-only concerns belong to
G1 — presumption of closure"`.

**Orchestrator decision logic**: similar to G1. Plan-target blame
→ rerun plan subagent; PRD-target blame → escalate to user;
mixed/ambiguous → escalate.

### Stage 5 — Ralph loop (implemented in v2)

**Status: already done in v2 (phase-5a primitives + phase-5b
orchestrator wiring).**

Wraps `build → spec → review` in a macro loop. Exit conditions:

1. All active scope items classified `Fully` in `review.md`
2. Stall (Fully set hasn't grown for K=3 consecutive iters)
3. User invokes `ralph-cleared` to accept gaps explicitly

State tracked in `ralph-state.json` (outside main cascade).
Primitives in `autodev/ralph.py`. See
[`panel-reviews/2026-04-21-gate-redesign/`](panel-reviews/2026-04-21-gate-redesign/)
for detailed analysis.

**Potential v3 additions** (not required for v3 correctness):

- `.ralph-cleared` sentinel for accept-with-gaps escape (vs.
  lossy `update --amendment`)
- Mid-iter JSONL progress events for long iterations
  (user-visibility during 5–30 minute runs)

**Required v3 prompt addition (same as plan stage):** spec subagent
must tag each behavior claim with `scope:<id>` + `path:line_number`;
review subagent must produce Evidence column per item.

### Stage 6 — Close gate: spec ↔ review (PRD anchor)

**Visibility packet:**
- Primary pair: `spec.md` + `review.md`
- Anchor: `prd.md`

Nothing else.

**Verifiable pre-check:**

| Category | Check |
|---|---|
| Structural | review.md classification enum valid; count consistency (summary counts match per-item table); spec.md 8 required sections present |
| Attribution | every `Evidence: spec:§N` / `code:path:line` in review.md resolves to actual content |
| Retrieve-and-verify | PRD's external-authority claims (if any) are demonstrably handled in spec |

**Panel prompt** (same text to all reviewers):

```
You are an interface validator at the close gate.

You see three artifacts:
  Primary (subject of review):
    spec.md    — what was shipped
    review.md  — per-scope-item self-classification
  Anchor (locked, accepted at earlier gates):
    prd.md     — original intent

Read all three in full. Raise any finding you see.

Finding categories:
  MISSING     — PRD requirement not evident in delivery
  INVENTED    — spec describes behavior with no PRD basis
  UNDELIVERED — review.md classifies Fully/Partial but spec
                doesn't support that classification
  AMBIGUOUS   — PRD clause has multiple readings, delivery only
                matches the less-likely one

Context for framing findings:
- PRD, scope, plan, code, and ralph's internal review are all
  locked. Default blame for new findings is spec.md or review.md
  (the primary pair).
- If a finding points at PRD or earlier-gate work, note the
  cross-reference — synthesizer decides whether it's an emergent
  composition concern (keep) or a re-litigation (drop).

Severity: invariant_violation / risk / opinion.

Output plain markdown. No JSON.

IMPORTANT (Codex): write your full analysis to the output file.
```

**Panel cardinality**: **cross-vendor**. Close is a final pre-ship
gate with high cost of error; cross-vendor triangulation against
PRD's human intent earns its cost.

**Synthesizer**: same as G2, with anchor-filter dropping PRD-only
findings.

**Orchestrator decision logic**:

```
if pre_check_fails:
    gate = fail
elif no invariant_violation:
    gate = pass → orchestrator moves feature from active/ to complete/
elif all blame spec (INVENTED / UNDELIVERED):
    gate = needs_revision → either rerun stage-spec or rerun
    ralph's review stage, user decides
elif all blame MISSING (PRD requirement not delivered):
    gate = needs_user_decision (amend PRD? accept with gap?
    drop feature?)
else (mixed):
    gate = needs_user_decision
```

Close is escalation-biased: revisions at this point are expensive,
so the orchestrator defaults to asking the user rather than
auto-rerunning stages.

---

## 6. What changes in v2 (concrete diff)

### Already done (stale from prior draft; flagged for doc cleanup)

| Target | Note |
|---|---|
| `auto_dev/` (v0.1 archive) | Deleted 2026-04-21 |
| `panel_wrapper.py` markdown→JSON bridge | Already removed at G15 (panel/__init__.py + panel/runner.py replaced it) |

### Delete (load-bearing pieces explicitly re-homed below)

| Target | LOC | Reason | Replacement |
|---|---|---|---|
| `autodev/revision_loop.py` G21 severity classification | ~100 of 400 | Severity-based escalation is the v2-specific over-engineering | Simpler per-gate L counter (P7); one severity treatment |
| All four `autodev/panel/prompts/review-*.md` | ~370 | Replaced by pairwise+anchor prompts (§5) | New G1/G2/close prompts |

**Total delete: ~470 LOC + 4 prompts.** Much less than the prior
draft; most of what was listed for deletion was actually load-bearing.

### Keep (panel review flagged as load-bearing)

| Target | Reason to keep |
|---|---|
| `autodev/artifacts/revision_state.py` (simplified to L counter only) | Still owns cycle-scoped state: `L[gate]`, `pending_feedback[stage]` for stage-rerun feedback plumbing. Delete only the G/M/auto_pass_next fields (phase-5 severity extras). |
| `autodev/revision_loop.py` route_to_layer + feedback helpers | g-24 upstream routing is a real capability shipped in phase-5b. Keep; simplify prompt-arg shape if needed. |
| `autodev/artifacts/overrides.py` | Skip-gate + dirty-ack are operational escape hatches the close contract depends on (cmd_close enforces skip-gate ceiling; _advance_gate writes synthetic skipped verdicts). |
| Build-blocking routing (g-23/g-24) in orchestrator | Enables build-stage self-correction on diagnosed upstream defects. Deleting regresses phase-5b behavior. |
| `autodev/vendors/allowlist.py` | Safety envelope (codex bypass flags, claude write-scope `--add-dir`). Security boundary, not vendor abstraction. |

### Simplify

| Target | Change |
|---|---|
| `autodev/vendors/config.py` | Trim per-vendor model-inheritance chain (current implementation is deeper than needed). Keep multi-vendor panel config (cross-vendor at G1 and Close per §5). |
| `autodev/revision_loop.py` | Remove severity classification + M counter + auto-pass synthesis. Keep L counter + route_to_layer + pending_feedback plumbing. |
| `autodev/orchestrator.py` gate decision | Drop severity branches. Gate decisions: `pass` / `needs_revision(target, L_bump=1)` / `needs_user_decision`. L_MAX=3 before halt-for-human. |
| Synthesizer (`autodev/panel/prompts/synthesize.md`) | Add anchor-filter step: drop findings whose `targets` field is `["anchor"]` only. Requires schema extension (P6 Schema requirement). |

### Add

| Target | Rough size |
|---|---|
| `auto-dev-v3/outer-skill.md` — interactive PRD intake | ~100 lines of prompt |
| Attribution-column requirements in `stage-plan.md`, `stage-spec.md`, `stage-review.md` | ~10 lines per prompt |
| Retrieve-and-verify pre-check scripts | ~150 LOC Python |
| Mechanical pre-check scripts per gate | ~100 LOC total |
| New gate prompts (G1, G2, close) per §5 above | ~80 lines each = 240 lines |

**Total add: ~600 LOC code + 500 lines of prompts.**

**Net v3 size: v2 ~6,200 LOC − ~5,260 (delete) + ~600 (add) + ~500 prompts ≈ 2,000 LOC + prompts.** This gets close to the POST-MORTEM's "10x smaller than v0.1" aspiration for the first time.

---

## 7. Implementation order

Suggested sequence, each step independently testable except where
noted. ~~Step 1 (delete v0.1)~~ already done 2026-04-21; kept
numbered for cross-reference continuity below.

1. ~~**Delete v0.1 `auto_dev/`**~~ — done.
2. **Add attribution columns to stage prompts** (plan, spec, review).
   v2's ralph still runs; just richer outputs.
3. **Write mechanical pre-check scripts** for each gate. During
   transition: pre-checks run informational-only (logged, not
   blocking); compare findings against panel output to verify
   coverage before making them blocking.
4. **Extend finding schema with `targets` field** (P6 Schema
   requirement). Makes the anchor-filter deterministic. Additive
   change; old panel verdicts without `targets` default to
   `["primary_pair"]` for back-compat.
5. **Swap stage order: scope before PRD-review**. v2's current
   cascade runs `panel_prd_review` on `prd.md` alone (before scope
   exists). v3's new G1 is `PRD ↔ scope` pairwise, so scope must
   exist at G1 dispatch. This is a cascade change in
   `autodev/state/cascade.py::ARTIFACTS` — modify the upstream
   chain so `scope → panel_prd_review` (renamed G1). Implement
   behind a feature flag or in a single atomic step; the pipeline
   is not runnable with cascade mid-rewrite.
6. **Replace G1 gate prompt** (`review-prd-review.md`) with
   pairwise+anchor design. Keep v2's (simplified) revision loop +
   L counter in place. Measure:
   - Does PRD+scope converge without the output-shape
     constraints of the current dialogue-mode prompt?
   - Does cross-vendor divergence return?

   **Rollback plan**: if G1 doesn't converge in 5 feature runs,
   re-add the dialogue-mode output-shape constraints (best-guess +
   lookahead) to the v3 G1 prompt. Document rollback via `git
   revert` on the gate-prompt commit.
7. **Replace remaining gate prompts** (G2, close) once G1 proves
   the pattern.
8. **Write outer skill** for PRD intake. Once proven, unlocks the
   schema-enforcement side of G1's mechanical pre-check.
9. **Simplify revision loop**: delete severity classification + M
   counter + auto-pass synthesis. Keep L counter, route_to_layer,
   pending_feedback.
10. **Simplify vendor config inheritance** (keep allowlist + panel
    multi-vendor).
11. **Optional**: mid-iter ralph visibility + `.ralph-cleared`
    sentinel.

Each step keeps the pipeline runnable; no flag-day cutover except
step 5 (cascade reordering), which is explicitly atomic. v3 is
**incremental cleanup of v2**, not a parallel rebuild.

---

## 8. Panel-review findings (condensed)

Two rounds of cross-vendor panel review (Claude, Gemini, Codex)
over 2026-04-21. Raw outputs preserved under
[`panel-reviews/`](panel-reviews/).

### Round 1 — gate-redesign evaluation

Evaluated a prior "A+B+C" proposal (collapse 4 gates → 1, role
lenses, diff-locked revisions). Unanimous consensus on six
critiques:

1. Dropping PRD panel entirely causes "pay-before-you-fail" — PRD
   defects surface only after scope+plan+test-plan are drafted.
   Human approval alone is NOT third-party oversight.
2. Role-differentiated lenses produce **task-partition diversity**
   (non-overlapping coverage), NOT **interpretive divergence**
   (same-question, different-answer disagreement). The A+B+C
   proposal claimed the latter but delivered only the former.
3. Consistency / coverage / ambiguity lens triple is not
   orthogonal.
4. Diff-lock too strict AND abusable by the driver agent.
5. Lens myopia at seams — strict role boundaries cause hybrid
   issues to fall through.
6. Synthesizer schema mismatch with role-differentiated lenses.

### Round 2 — context-isolation reframe

Follow-up panel after noting that Round 1's proposals all imported
a human-workflow model. For an LLM harness, every agent can do
everything; what's engineered is **context isolation**, not
specialty. Unanimous consensus:

1. Context scope is the primary partition axis, not role.
2. Persona effects are real but secondary (thumb on the scale,
   not a filter).
3. Context isolation ≠ cross-vendor epistemic diversity — different
   problems, different solutions.
4. Same-vendor multi-instance is the default; cross-vendor is an
   escalation instrument for ambiguity frontiers.

**Claude's self-retraction**: its Round-1 "three vendors, same
prompt, same context = context isolation" claim was explicitly
corrected in Round 2. That pattern is cross-vendor diversity on a
single context — useful for ambiguity detection, but not context
isolation (which requires different visibility packets).

### What drove the design

The six governing principles (§3) are direct distillations of the
panel findings plus the user's subsequent refinements:

- P1 ← Round 2 consensus
- P2 ← user observation on "verifiable ≠ panel-worthy" (external
  authority case)
- P3 ← user refinement after seeing that v2 plan agent reads PRD
- P4 ← user's "pairwise+anchor" synthesis of Options G / C / K
- P5 ← user observation that "don't suggest where to look" causes
  missed coverage
- P6 ← "presumption of closure" pattern extended from reviewer to
  synthesizer to avoid bias

---

## 9. Open decisions

### D1 — Update verb UX

v2's `autodev update --amendment` appends dated PRD amendments,
re-scopes with `mode: "update"`, cascades via hash staleness. The
hook-based + pairwise-gate design supports this naturally (hash
mismatch on predecessor invalidates downstream gates), but UX needs
concrete design:

- Which artifacts get regenerated when scope changes?
- What if only PRD changes and scope delta is zero?
- Does G1 re-run on every amendment, or only when scope hash
  changed?

Deferrable until first real use case.

### D2 — Close verb cleanup policies

v2 has four close reasons (complete / retiring / deferred / cancelled)
with different keep/strip policies. The pairwise+anchor gate model
doesn't change this directly, but the `.ralph-cleared` sentinel
(if adopted) needs to interact with close's `deferred`/`complete`
statuses cleanly.

Also deferrable.

### D3 — Mid-iter ralph visibility

Not strictly needed for correctness. Adding per-scope-item JSONL
progress events lets `tail -f log.jsonl` show live status during
long iters. Cost: ~30 LOC + prompt addition. Benefit: user can
distinguish "agent is stuck" from "agent is slow" during
long-running builds.

Optional enhancement.

### D4 — Outer skill granularity

The outer skill enforces PRD schema + does interview. Open question:
should the interview be a single back-and-forth with user, or should
it iterate (asking follow-up questions if initial answers are still
ambiguous)? Latter is richer but longer conversations. Single-round
with clear prompts is probably sufficient for most features.

---

## 10. References

- Current skill (source of inspiration): [`../auto-dev/SKILL.md`](../auto-dev/SKILL.md)
- v0.1 post-mortem: `/Users/xzhong/Downloads/auto-dev-sdk/docs/features/auto-dev-sdk/deferred/POST-MORTEM.md`
- v2 repo (target for incremental changes): `/Users/xzhong/Downloads/auto-dev-sdk/autodev/`
- Panel review raw outputs: [`panel-reviews/`](panel-reviews/)
  - Round 1: [`2026-04-21-gate-redesign/`](panel-reviews/2026-04-21-gate-redesign/)
  - Round 2: [`2026-04-21-context-isolation-reframe/`](panel-reviews/2026-04-21-context-isolation-reframe/)
- Claude Code PreToolUse hooks (potential defense-in-depth layer,
  not core to v3): `~/.claude/settings.json`
