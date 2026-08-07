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

## Vendor Configuration

The default config lives with the SDK at `vendors.yml`. Harness-owned
LLM calls read this file by default: coding stages, Ralph review,
panel reviewers, panel synthesizer, and the idle-timeout probe.
Target repos do not need their own vendor config unless they
intentionally override the harness defaults.

```yaml
stages:
  design:
    vendor: codex
    model: gpt-5.6-sol
    probe_interval_sec: 1200
    effort: max
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
      model: gpt-5.6-sol
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

- one design session per repo + feature;
- one build session and one Ralph-review session per repo + feature; and
- one panel-reviewer session per repo + feature + gate + configured reviewer
  slot/vendor/model.

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
`implement <description>`. The `auto-dev` skill triggers, intakes
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

`run` has three pacings: end-to-end (`autodev run`), one stage at a time
(`autodev next`), and phase-bounded (`autodev run --until design|build`).
`--until design` is the common "design, then let me look before we
build" checkpoint — it does not stop one stage at a time, it carries the
whole design phase to completion (including the gate) and halts before
the first build stage.

## Pipeline stages + gates

Current runtime stages and harness-authored nodes:

| Step | Artifact | Review / next action |
|------|----------|----------------------|
| `design` | `design.md`, `scope.json`, `trace.md`, `test-plan.md` | Harness seals `design-packet.json`, then panel runs `design-review` |
| `build` | code + `build.json` | Ralph loop dispatches `ralph-review.json` until complete, stalled, or routed |
| `implementation-index` | `implementation-index.json` | Harness-authored code navigation index for spec |
| `spec` | `implemented-spec.md` | Code-first implementation facts; no PRD/design/build semantics in prompt context |
| `prd-checklist` | `prd-checklist.json` | Harness-authored PRD requirement IDs only; no coverage/evidence |
| `close-approval` | `panel-close-approval.json` | Panel reviews `implemented-spec.md` against `prd.md` + checklist and emits coverage judgment |

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
reruns the design stage so the design agent can try to remove the
apparent PRD conflict. A second consecutive PRD-targeted design-review
finding halts for human.

## Release Contents

This release checkout intentionally contains only the wrapper skill,
runtime package, vendor config example, and tests:

- `SKILL.md` — thin dispatcher skill that calls the CLI
- `autodev/` — Python harness, bundled stage prompts, and panel prompts
- `shared/vendors/` — bundled unified vendor shell adapter used by all
  harness-owned LLM calls
- `tests/` — offline regression tests plus explicitly marked live tests
- `vendors.yml.example` — current per-repo vendor config template
