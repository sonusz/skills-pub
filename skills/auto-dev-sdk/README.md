# auto-dev-sdk

Pipeline harness for AI-driven feature development. Routes LLM calls through
the packaged shared vendor adapter in a filesystem state machine that enforces:

```text
design packet -> design-review -> build/Ralph loop -> implementation-index
-> implemented-spec -> PRD checklist -> close-approval
```

The design stage produces the whole design packet in one agent run,
then a four-vendor panel reviews the design against the PRD. Build
runs inside a Ralph loop until implementation coverage is complete or a
route/human halt intervenes. Final close review compares code-first
implementation facts against the PRD; coverage is produced by the panel,
not precomputed for it.

Ralph and close-approval also flag evidenced removable code redundancy. Ralph
reuses its correction findings to keep only affected scopes incomplete; the
panel routes code-only cleanup to build and design-mandated redundancy to
`arch-design`.

## Why this harness

- **Several vendors review the same artifact.** The `design-review`,
  `trace-review` and `close-approval` gates dispatch every reviewer listed
  under `panel.reviewers` in parallel; the allowed labels are `claude`,
  `codex`/`openai`, `agy`, `cursor` and `grok`, and the loader rejects two
  reviewers that resolve to the same underlying provider. Each reviewer
  states its own verdict and findings; the synthesizer keeps every raw
  finding, groups the ones that describe one underlying issue into
  clusters, and the harness counts clusters, not repeated wording, so
  agreement and divergence between vendors are both visible in the verdict.
- **Vendor switching by remaining quota.** Every LLM role in `vendors.yml`
  (each coding stage, each panel reviewer, the synthesizer, the idle probe)
  may set `min_quota_pct` and an ordered `fallbacks` list. Before a call
  launches, the harness reads that vendor's remaining quota (Claude,
  Codex/OpenAI, Cursor, Agy and Grok have fetchers) and runs the first
  candidate whose reading is at or above its floor; a reading it cannot
  fetch counts as insufficient. A reviewer fallback must be a different
  provider from its primary. When no candidate qualifies the run pauses,
  records the earliest reset time and a repo fingerprint in
  `.quota-pause.json`, and `autodev quota-resume` continues only when the
  time has passed, quota has recovered, and nothing else changed.
- **A lost reviewer does not sink the panel.** A reviewer that times out or
  returns nothing is retried once; the retry re-resolves its candidate with
  a fresh quota reading, so it can land on a fallback vendor. After the
  retry the harness force-refreshes that vendor's quota and omits the
  reviewer only if exhaustion is positively confirmed; unknown quota and
  ordinary failures still block. `min_responding_reviewers` (default 2)
  must still be met, otherwise the same quota-pause path applies. Setting
  `fail_fast_confirmed_quota: true` cancels the whole panel on the first
  confirmed exhaustion instead of waiting for retries.
- **A filesystem state machine that resumes where it stopped.** All state
  lives under `docs/features/<feature>/active/`. The cascade decides the
  next stage by comparing each artifact's recorded `source_hash` with its
  upstream's current hash, so `autodev run` after a crash or `pause`
  re-selects the first stale artifact. The `.pause` sentinel is honored at
  the top of the run loop, when a panel finishes (before its revision is
  dispatched), and at the top of every build round.
- **Persistent provider-native agent sessions with turn rotation.** The
  design, build, Ralph-review and arch-review agents each keep one native
  session per repo and feature, and every panel reviewer slot keeps its own;
  later turns receive a compact continuation prompt with current paths and
  hashes. Sessions rotate automatically at successful-turn boundaries
  (design 15 turns, build 3, arch-review 3, Ralph review and panel reviewers
  5) and can be reset by hand with `autodev reset-session`.
- **Design packages archived as local Git refs.** Each accepted or
  superseded design package is committed as a package-only tree at
  `refs/autodev/design/<feature>/package-NNN`, containing only the design
  artifacts and changelog; it never touches the index, the checked-out
  branch or a remote. Revision reviewers get the previous and current refs
  with bounded `git diff` commands, and `autodev restore-design` brings the
  latest hash-verified package back.
- **Out-of-scope writes are a containment failure, not a warning.** Each
  stage has a writable set and a protected set. The harness snapshots the
  worktree before and after the subprocess, including committed tree changes
  and content fingerprints of protected files; any write outside the
  writable set or inside the protected set is recorded in
  `<stage>-failure.json` as `detected_out_of_scope_write` and halts the run.
  `vendors.yml` flags can only narrow tool access; write-scope flags are
  harness-owned.
- **Revision budgets with auditable single-use rerun grants.** Each panel
  gate has a counter `L` in `revision-state.json` capped at `L_MAX = 10`; a
  blocking verdict that reruns a producer bumps it, a build-reported design
  defect that routes back to design consumes the same counter, and the
  blocking verdict after the cap halts for a human. `autodev grant-rerun`
  adds one credit recorded with reason, author and consumption time; it
  does not raise `L`, does not mark the gate passed, and the next blocking
  verdict halts again.
- **Output-validation retry amends instead of restarting.** When a stage
  exits 0 but its deliverable fails validation (missing artifact, missing
  provenance header, a `ralph-review.json` that skips an active scope), the
  same agent is re-dispatched up to `STAGE_OUTPUT_RETRY_MAX = 3` times with
  a `<stage>-output-rejection.json` naming the defect and an instruction to
  amend the prior artifact in place. Hard subprocess failures and
  containment violations still halt immediately.
- **Reviewers see only what they should judge.** The Ralph reviewer gets the
  accepted design, scope, trace and the iteration diff, but not `prd.md`,
  `build.json` or `implemented-spec.md`; the trace-review panel gets
  `trace.md`, `test-plan.md` and `prd.md` but not `design.md`; the spec
  stage describes code without PRD, design or build context, and the
  close-approval panel then compares that code-first description and the
  code against the PRD.

## The two loops

### Design loop

```text
prd.md
  |
  v
arch-design --> arch-review (single agent; pass | needs_revision)
  ^               | needs_revision: rerun arch-design (5 rounds, then halt)
  |               | pass
  |               v
  |             design --> design.md  scope.json  trace.md  test-plan.md
  |               |
  |               v
  |             design-packet.json (harness seals the four hashes)
  |               |
  |               v
  |             design-review || trace-review   (parallel panels, every
  |               |             configured vendor, one synthesizer each,
  |               |             verdicts merged into one decision)
  |               |
  |               |-- pass: coverage round and budget round both passed
  |               |      -> accepted-design.json -> build loop
  |               |
  |               |-- retry_design: L[design-review] += 1  (L_MAX = 10)
  +---------------+      finding targets prd.md twice in a row
  |               |      -> halt_for_human (PRD update)
  |               |-- L already at L_MAX -> halt_for_human
  |                      operator: autodev grant-rerun <feature> \
  +---------------------   design-review --reason "..."
                         one single-use credit -> one more arch-design
                         rerun; the next blocking verdict halts again
```

The PRD is the only human-owned input. `arch-design` derives an initial
architecture, and the single-agent `arch-review` returns `pass` or
`needs_revision`; five consecutive `needs_revision` rounds halt. The
`design` stage expands the passed architecture into `design.md`,
`scope.json`, `trace.md` and `test-plan.md`, and the harness seals their
hashes into `design-packet.json`. Two panels then run against `prd.md`:
`design-review` judges coverage, architecture fit and scope sizing of
`design.md` and `scope.json`; `trace-review` judges behavioral completeness
and test fidelity of `trace.md` and `test-plan.md`. Their findings merge
into one decision. Rounds follow a six-round cadence, three in a coverage
role (is anything required missing) then three in a budget role (is
anything present unrequired), skipping a role that has already passed on
the current packet, and the gate completes only when both have passed on
the same packet.
A blocking decision (`retry_design`) reruns `arch-design`, which cascades
through `design` again, and bumps `L[design-review]`; a finding that
targets `prd.md` gets one such rerun and halts on the second consecutive
one. At `L_MAX` the run halts for a human, who can either replace the PRD
(`autodev update <feature> --from-file PATH`, which starts a new cycle and
resets the counters) or issue one `grant-rerun`. A `.pause` set while the panel runs takes effect when the
panel finishes, before the revision is dispatched.

### Build loop (the Ralph loop)

```text
accepted-design.json
  v
+-> build ---- build.json blocking=true, diagnosis.defective_layer:
|     |          design -> route_to_layer (L[design-review] += 1)
|     |                    -> rerun design -> design-review again
|     |          prd | ambiguous -> halt for human (autodev update)
|     v
|   ralph-review  (single agent; sees accepted design, scope, trace and
|     |            the iteration diff; never prd.md or build.json)
|     |   trace rows: Fully | Partial | Missing | Deviated | Deferred
|     |   design_conformance: Aligned | Deviated + correction findings
|     |-- every active scope Fully and no open finding -> exit loop
+-----+-- otherwise the affected scopes stay incomplete -> next round
          (.pause checked at the top of every round; ralph-state.json
           persists, so autodev resume + run re-enters the loop)
  |
  v
implementation-index.json (harness) -> spec -> implemented-spec.md
  |                                            (code-first; no prd.md)
  v
prd-checklist.json (harness; PRD requirement IDs only)
  v
close-approval panel: code + implemented-spec.md vs prd.md
  |-- pass -> pipeline-done
  |-- blocking, routed by finding target (L[close-approval] += 1):
        build.json                                 -> rerun build
        design.md scope.json trace.md test-plan.md -> rerun arch-design
        prd.md or an architecture doc              -> halt for human
```

`build` implements against the accepted design, one scope item per
iteration, and commits as it goes. If it hits a defect it cannot fix in
code it writes a blocking deviation to `build.json` with a
`defective_layer`: `design` routes back to the design stage and consumes
the design-review budget; `prd` or `ambiguous` halts for a human. Otherwise
`ralph-review` classifies every trace row against the code on disk and
independently audits the diff for drift from the accepted design and for
evidenced redundancy; each correction finding caps its scopes below
complete, so only the affected scopes return to `build`. A mechanism the
accepted design mandates is not removable here; that goes through the
design route above. The loop exits when every active scope is `Fully` with
no open finding. The harness then writes `implementation-index.json`, the
`spec` stage writes the code-first `implemented-spec.md`, the harness writes
`prd-checklist.json`, and the `close-approval` panel compares the code and
spec against the PRD. Its blocking findings route by target: `build.json`
for code-only fixes and cleanup, the design artifacts for design-mandated
problems (rerun `arch-design`), and `prd.md` halts on the first occurrence.
`autodev pause` is honored between build rounds and after each panel;
`autodev resume` clears the sentinel and the next `autodev run` continues
from the persisted state.

## Vendor Configuration

The default config lives with the SDK at `vendors.yml`. Harness-owned
LLM calls read this file by default: coding stages, Ralph review,
panel reviewers, panel synthesizer, and the idle-timeout probe.
Target repos do not need their own vendor config unless they
intentionally override the harness defaults.

`vendors.yml` is machine-local and git-ignored. The tracked template is
`sample-vendors.yml`, which lists every vendor in every role; generate the
local file from it and then edit by hand:

```bash
python3 shared/vendors/scripts/init-vendors.py \
  --sample sample-vendors.yml --out vendors.yml
```

The generator removes panel reviewers whose CLI is not installed and clamps
`min_responding_reviewers`; it only warns about `stages`, `synthesizer`, and
`probe`, because a coding stage needs a vendor. Vendor ids and known model
ids are catalogued in `shared/vendors/sample-vendors.yaml`.

```yaml
stages:
  design:
    vendor: codex
    model: gpt-6-astra
    probe_interval_sec: 1200
    effort: high
  build:
    vendor: codex
    model: gpt-5.6-terra
    probe_interval_sec: 3600
    effort: high
  spec:
    vendor: codex
    model: gpt-5.6-terra
    probe_interval_sec: 900
    effort: high
  review:
    vendor: codex
    model: gpt-5.6-terra
    probe_interval_sec: 900
    effort: high

  panel:
  # Default transport quorum. An omitted reviewer must first fail a retry and
  # then have quota exhaustion positively confirmed.
  min_responding_reviewers: 2
  reviewers:
    - vendor: claude
      model: opus
      effort: high
    - vendor: grok
      model: grok-4.5
      effort: high
    - vendor: agy
      model: gemini-3.1-pro-high
      effort: high
    - vendor: codex
      model: gpt-6-astra
      effort: high
  synthesizer:
    vendor: codex
    model: gpt-5.6-luna
    effort: high
  reviewer_probe_interval_sec: 600
  synthesizer_probe_interval_sec: 300

probe:
  vendor: claude
  model: claude-sonnet-5
  timeout_sec: 60
  effort: low
```

Notes:
- Vendor config resolution order is explicit `--vendors-yml`, then
  `AUTODEV_VENDORS_YML`, then SDK-root `vendors.yml`, then legacy
  target-repo `vendors.yml` for compatibility.
- Vendor labels are case-insensitive and normalized to lowercase. `codex`
  and `openai` both route through the Codex CLI. An Agy panel reviewer may
  omit `model` to use the model selected in agy's own configuration.
- Quota gates support Claude, Codex/OpenAI, Cursor, Agy, and Grok. Agy reads
  `RetrieveUserQuotaSummary` from its prompt-free local server; Grok reads ACP
  billing. Both probes avoid issuing a model request.
- All harness-owned LLM calls go through the packaged
  `shared/vendors/scripts/call.sh` interface and use its unified
  `<id>/out`, `<id>/status`, `<id>/log`, `<id>/stream` output contract.
- `probe_interval_sec` is the stream-output idle threshold for coding
  stages and panel calls. The harness monitors shared/vendors'
  `<id>/stream` live transcript. When output is quiet for that long, the
  read-only probe decides whether to extend or kill. A much larger hard
  wall-clock backstop is derived internally.
- `effort` is the shared vendor scale `min|low|medium|high|xhigh|max`;
  `shared/vendors` maps it to the nearest supported native effort for the
  selected vendor.
- `review` is still a required vendor slot because the Ralph loop uses
  it to dispatch `ralph-review`; it is not the old post-spec
  `review.json` coverage stage.
- Panel reviewers (`design-review` and `close-approval`) and the
  synthesizer are configured under top-level `panel`. The synthesizer
  uses `--schema-file` through `shared/vendors` to enforce its output
  schema, which works on `claude`, `grok`, and `codex`/`openai`. `agy` is not
  supported as a synthesizer because its CLI has no native schema
  enforcement.
- Panel transport defaults to two responding reviewers. A failed reviewer is
  retried once immediately. The harness then force-refreshes that vendor's
  quota and permits omission only when exhaustion is positively confirmed;
  unknown quota and ordinary transport failures still block. If confirmed
  quota leaves fewer than `min_responding_reviewers`, the normal quota-pause
  path is used instead of synthesizing an undersized panel.
- `probe` configures the read-only idle-timeout LLM probe. It is not a
  product reviewer; it only decides whether a quiet subprocess looks
  wedged or should get more time, and it also routes through
  `shared/vendors` when invoked.

## Persistent agent sessions

The agents that revise the same work across pipeline iterations keep distinct
provider-native sessions:

- one design session per repo + feature — `arch-design` shares this session,
  since it is the same agent's preliminary architecture pass before the
  design (packet-expansion) turns;
- one build session, one Ralph-review session, and one arch-review session
  per repo + feature; and
- one panel-reviewer session per repo + feature + gate + configured reviewer
  slot/vendor/model.

An `arch-design` turn whose `arch-review` verdict is `pass` is credited back
to the design session — that turn is not counted toward the session's
15-turn rotation budget. An `arch-design` turn sent back for revision counts
normally, as do all design (packet-expansion) turns.

Reviewer sessions are never shared with one another or across
`design-review`, `trace-review`, and `close-approval`. On later turns the
harness sends a compact continuation prompt containing current paths and
hashes; the full role contract remains in the native session. Filesystem state
and current hashes are always authoritative, so a resumed agent must re-read
changed feedback rather than trust stale conversational memory.

The `spec` producer, panel synthesizer, and idle probe remain stateless because
they do not participate in iterative producer/reviewer revision. Session
mappings are maintained by canonical `shared/vendors` in the user's state
directory, outside the target repo, so they do not dirty feature worktrees.

## Design revision navigation

Every archived design package has a local, package-only Git ref at
`refs/autodev/design/<feature>/package-NNN`. The commits contain only the
archived design artifacts and changelog; they do not stage the user's index,
capture unrelated worktree changes, move the checked-out branch, or push
anything. Revision reviewers receive the previous/current refs and bounded
`git diff` commands so they can inspect changes before selectively re-reading
the authoritative current files.

The current feature's harness-owned `docs/features/<feature>/active/**` files
are excluded from the preflight dirty-worktree decision. Changes elsewhere in
the repository still require `acknowledge-dirty`, and writes outside the
allowed stage scope—including another feature's active directory—remain
containment failures.

## Verify

```bash
autodev --help
autodev status <any-feature-name>    # "not found" if feature absent
scripts/llm-test.sh --vendor claude  # real shared-vendors adapter smoke
python3 -m pytest -q                 # source checkout verification
```

## First feature

**Through the skill**: in Claude Code, say
`implement <description>`. The `auto-dev-sdk` skill triggers, intakes
the PRD via interview, dispatches the pipeline. You get asked to
clarify when a reviewer needs intent.

**Direct CLI** (for dogfooding / debugging):

```bash
# Seed a feature with an existing PRD file
autodev prd myfeature --from-file /path/to/prd.md

# Move planned/ → active/ so the harness picks it up
mv docs/features/myfeature/{planned,active}

# Run until a gate needs you
autodev run myfeature

# Or advance only through the design phase, then stop before build
# (runs design + design-review + any in-design revision reruns, then
# returns cleanly with a stopped-at-boundary event)
autodev run myfeature --until design

# Inspect state
autodev status myfeature
tail docs/features/myfeature/active/log.jsonl
```

```bash
# Change requirements: write the full new PRD to a scratch file, then
# replace prd.md in place (old version + diff land in
# docs/features/myfeature/active/prd-history/)
autodev update myfeature --from-file /path/to/new-prd.md
```

When a blocking gate has exhausted its local revision budget, an operator can
authorize one more producer correction without amending the PRD or bypassing
review:

```bash
autodev grant-rerun myfeature design-review \
  --reason "one narrow correction after reviewing the blocking verdict"
autodev run myfeature
```

The grant is auditable and single-use. It leaves the counter capped, does not
mark the gate passed, and another blocking verdict halts again.

`run` has three pacings: end-to-end (`autodev run`), one stage at a time
(`autodev next`), and phase-bounded (`autodev run --until design|build`).

Persistent design/build/Ralph conversations can be restarted deliberately:

```bash
autodev pause myfeature
autodev reset-session myfeature design
```

Reset refuses an active session lease; the agent's next turn starts a fresh
provider-native conversation.

The harness also rotates conversations automatically at successful-turn
boundaries: design keeps at most 15 turns, build at most 3, arch-review at
most 3, and Ralph review plus each panel reviewer at most 5. Turn 16 for
design, turn 4 for build, turn 4 for arch-review, and turn 6 for Ralph review
and panel reviewers start fresh with the full current artifact packet.
Failures and handled interruptions do not advance the count.

If an interrupted or mistaken invalidation removed the active design package,
restore the latest hash-verified archive while the feature remains paused:

```bash
autodev restore-design myfeature
```
`--until design` is the common "design, then let me look before we
build" checkpoint — it does not stop one stage at a time, it carries the
whole design phase to completion (including the gate) and halts before
the first build stage.

## Pipeline stages + gates

Current runtime stages and harness-authored nodes:

| Step | Artifact | Review / next action |
|------|----------|----------------------|
| `arch-design` | `arch-design.md` | Single-agent `arch-review.json` review; a `pass` verdict advances the cascade into `design` |
| `design` | `design.md`, `scope.json`, `trace.md`, `test-plan.md` | Harness seals `design-packet.json`, then panel runs `design-review` |
| `build` | code + `build.json` | Ralph loop dispatches `ralph-review.json` until complete, stalled, or routed |
| `implementation-index` | `implementation-index.json` | Harness-authored code navigation index for spec |
| `spec` | `implemented-spec.md` | Code-first implementation facts; no PRD/design/build semantics in prompt context |
| `prd-checklist` | `prd-checklist.json` | Harness-authored PRD requirement IDs only; no coverage/evidence |
| `close-approval` | `panel-close-approval.json` | Panel reviews `implemented-spec.md` against `prd.md` + checklist and emits coverage judgment |
| `requirement` (optional, user-supplied) | `requirement.md` | Read-only reference for `arch-design`, `arch-review`, `design`, `build` (`spec` and `ralph-review` excluded — the latter judges code only against the accepted design); imported via `autodev prd --requirement`; excluded from the staleness cascade |
| `human feedback` (any review point) | `human-feedback-<point>.json` | Written by `autodev feedback`; merged once into the named review point's own package, then marked consumed — see "Human feedback at review points" below |

`stage-review.md` is retained as a deprecated legacy prompt, but the
main cascade no longer schedules a post-spec `review.json` stage.

### Output-validation retry (amend, don't restart)

When a stage agent exits 0 but produces a deficient deliverable — a
missing artifact, a markdown file with no provenance header, or a
`ralph-review.json` that fails to classify every active scope item or
isn't valid JSON — the harness does **not** halt the run on the first
slip. It re-dispatches the *same* agent up to a small fixed cap
(`STAGE_OUTPUT_RETRY_MAX`, currently 3), handing it a
`<stage>-output-rejection.json` that names the exact deficiency (e.g.
`missing_scope_ids`) plus the prior artifact, with an explicit
instruction to **amend in place** — keep the entries that were already
correct and fix only what failed, rather than regenerate from scratch.
Only after the cap is exhausted does the schema error propagate. This is
distinct from the panel revision loop (which consumes the per-gate `L`
budget for *semantic* reruns) and from hard subprocess failures or
out-of-scope writes, which still halt immediately. Watch the
`output-rejected-retrying` / `output-rejected-exhausted` log events.

Each panel runs its configured claude + grok + agy + codex/openai reviewers
independently. The synthesizer preserves every raw finding and adds semantic
issue clusters; the harness validates membership and counts clusters, rather
than repeated reviewer wording, as tickets. Every finding also has an
independent `P0` / `P1` / `P2` release priority. PRDs default to
`Release threshold: P1` (historical behavior) and may select `P0` so P1/P2
findings remain auditable without dispatching redesign. Blocking clusters
either dispatch a producer rerun (bumping the gate's L counter) or halt for
human decision. Cross-round recurrence first reuses synthesizer cluster IDs
and falls back to structural identity that deliberately ignores summary prose.
For `design-review`, a blocking finding that targets `prd.md` first
reruns `arch-design` so the arch-design agent can try to remove the
apparent PRD conflict. A second consecutive PRD-targeted design-review
finding halts for human.

### Human feedback at review points

Mid-pipeline human feedback is injected as an independent reviewer's finding
at one of five review points: `arch-review`, `design-review`, `trace-review`,
`close-approval`, `ralph-review`. The wrapper skill shapes a natural-language
comment into that point's own finding structure and writes it with
`autodev feedback <f> <point> --from-file PATH | --text JSON`. The finding is
**merged into the package that point's own review agent (or panel) already
produced** — it is never fed to that review point's own agent or panel
(their inputs and prompts are unchanged), and no agent is rerun to
accommodate it; the merged package then flows downstream like any other
finding (e.g. an arch-design revision reads the merged `arch-review.json`;
the next build reads the merged `ralph-review.json`).

Merging happens at whichever of three points in time comes first for a given
pending feedback:

- **at verb time**, if a fresh response/synthesis package for that point
  already exists on disk;
- **just before routing** (panel points only), at the top of the harness's
  blocking-verdict check, which catches a package that turned fresh while
  the feedback was pending;
- **at the harness hook**, immediately after the review agent's (or panel's)
  output is validated and about to be consumed, for a package that has not
  been produced yet.

A given piece of feedback is consumed by exactly one of these three — once
merged, its status moves from `pending` to `consumed` and it is not
re-merged into a later run of the same point. `design-review` and
`trace-review` alternate coverage and budget rounds and rewrite their
judgment files each round. At verb time and at the pre-routing check,
feedback pending for either point stays `pending` until both rounds are
complete and their two judgments are paired; the harness hook, however,
merges into whichever round's judgment is produced next, without waiting
for both rounds. A non-blocking human finding merged into the coverage
round is overwritten when the budget round rewrites the file (a blocking
finding is routed immediately, before it can be overwritten).

The point's overall verdict is **recomputed from the merged finding set
alone** — the human finding's own reported `verdict` is recorded for audit
but carries no weight and no veto. At the three panel points
(`design-review`, `trace-review`, `close-approval`) it moves the outcome
only through its `severity`/`priority`, following the same routing and
blocking rules as every other reviewer's finding; at `arch-review` and
`ralph-review`, any human finding forces the verdict to `needs_revision` /
`Deviated` respectively, the same as when the review agent itself writes a
finding. If merging would fail validation against the point's own package
(for example, a `ralph-review` finding naming a scope that stopped being
active while the feedback was pending), the harness does not inject it or
halt the run — it marks the feedback `rejected` with a reason, visible in
`autodev status`, and the pipeline continues; re-injecting is the
operator's job.

`requirement.md` is the conflict baseline the wrapper skill checks feedback
and the PRD against before choosing where to inject it — the PRD remains
the requirement anchor and hash root. It is read-only to the stage agents
listed in the table above; import path and staleness-cascade exclusion are
covered there too.

## Release Contents

This release checkout intentionally contains only the wrapper skill,
runtime package, vendor config example, and tests:

- `SKILL.md` — thin dispatcher skill that calls the CLI
- `autodev/` — Python harness, bundled stage prompts, and panel prompts
- `shared/vendors/` — bundled unified vendor shell adapter used by all
  harness-owned LLM calls
- `tests/` — offline regression tests plus explicitly marked live tests
- `sample-vendors.yml` — tracked template listing every vendor in every
  role; `vendors.yml` (git-ignored) is generated from it per machine
