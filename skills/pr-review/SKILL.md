---
name: pr-review
description: >
  Structured two-phase review of code changes — either a pre-PR diff between
  two local branches or an existing GitHub PR. Phase 1 checks whether code
  delivers what new/modified docs (plan/PRD/spec/design) require; Phase 2
  hunts bugs in the implementation. Findings are surfaced as a synthesized
  report; in GitHub mode they can be posted as Copilot-style inline review
  threads. Use this skill whenever the user asks for a code review, a
  sanity-check on a diff, a spec-conformance check, or wants to verify a
  PR before merge — even when they don't say "review" explicitly. Triggers
  on phrases like "review this PR", "review my branch", "look at PR #N",
  "does this match the spec", "check this diff", "audit my changes",
  "pre-PR check", or explicit `/pr-review`. Do not use for typo fixes,
  formatting-only diffs, or one-line bug fixes — there is nothing two-phase
  about those.
allowed-tools: Bash, Read, Write, Edit, Skill
---

# PR Review

Two-phase code review: docs-compliance first, bug hunt second, optional
inline-thread posting third. Mirrors the workflow used to review the
`maestro-rate-limit` PR end-to-end.

The agent owns orchestration. Scripts are thin, non-classifying primitives
— they fetch state, talk to the GitHub API, and produce path-based review
bundles.
**Any classification (which files are anchor docs, which are implementation,
which are noise, which are binary, which are tests) is the agent's job.**
Hardcoded scripts always have corner cases — a vendored directory might
contain the bug being investigated, a lockfile diff might BE the point of
the PR, a `*_test.go` file might be the only place the new code path is
exercised. The agent has full context the script lacks; let it judge.

**Use shared/github-ops scripts and the scripts in `scripts/`, not `gh` CLI** —
auth comes from the git credential helper. `gh` has its own auth scope,
which can mask permission failures until they surface mid-post.

---

## Modes

| Scenario | Mode |
|----------|------|
| About to open a PR, want a sanity-check first | **local** — `--branches <base> <head>` |
| Reviewing an existing PR on GitHub | **gh** — `--pr <number>` |
| Reviewing a colleague's branch they pushed without a PR yet | **local** — fetch their branch, then `--branches main <their-branch>` |

**Skip the skill when:** the diff is a typo fix, formatting-only change, or
single-line bug fix — the two-phase ceremony costs more than it returns.

---

## Workflow

```
0. Upfront confirm — state mode, target, planned phases, vendors. One go/no-go.
1. Gather context — scripts/gather-context.sh
2. Phase 1 (docs-compliance) — only if the agent identifies anchor docs
3. Phase 2 (bug hunt) — always
4. (gh only) Compare findings against PR threads — scripts/list-all-threads.sh
5. (gh only, opt-in) Draft + post inline threads — scripts/post-review-thread.sh
6. Summary
```

Approval gates exist at two points and each exists for a different reason:

- **Once, upfront, before context-gather** — confirm the mode (local vs.
  gh), the target (branches or PR number), and the plan: gather context,
  run Phase 1 if anchor docs exist, run Phase 2 always, then report.
  Mention the vendors that panel-review is configured to use. One
  decision, made before any work starts. Everything that follows runs
  back-to-back without further prompting. The goal is to never have the
  user wait through context-gather or a panel only to be asked "should
  I proceed?" — by the time they're waiting, they've already approved.
- **Before posting any inline thread** — posts are user-visible and
  reviewer-visible, can't be silently undone, and the user is the one
  whose name appears on the comment.

---

## Step 0 — Upfront confirm

Before doing any work, state the plan to the user in one short message and
wait for a single go/no-go:

- Mode (`local` with base/head branches, or `gh` with PR number)
- Phases that will run: "Phase 1 if anchor docs are found in the diff,
  Phase 2 always"
- Vendors panel-review is configured to invoke (read from
  `panel-review`'s `vendors.yaml` — e.g. Claude, Agy, Codex)
- (gh mode) That findings will be cross-referenced against existing PR
  threads, and that posting inline threads is opt-in and requires a
  second confirmation later

Keep it tight — one short paragraph plus the mode/target. Do not run
context-gather, do not classify files, do not invoke panels until the
user approves. Once they do, Steps 1 → 3 (and 4, if gh) run back-to-back
without further prompting.

The only later gate is Step 5 — posting inline threads — which is a
different consent model (user-visible writes) and stays gated.

---

## Step 1 — Gather context

```bash
RUN_DIR=$(mktemp -d /tmp/pr-review.XXXXXX)
echo "RUN_DIR=$RUN_DIR"
# Local mode:
bash scripts/gather-context.sh --branches main feature/foo --out "$RUN_DIR"
# Or gh mode:
bash scripts/gather-context.sh --pr 21 --out "$RUN_DIR"
```

Output (under `$RUN_DIR`):

| File | Contents |
|---|---|
| `mode` | `branches` or `pr` |
| `pr_number` | PR # (gh mode only) |
| `head_sha` | head SHA |
| `merge_base_sha` | merge-base used for `diff.patch` |
| `repo_root` | absolute audit root passed to panel-review as `--cwd` |
| `base_ref`, `head_ref` | branch names (origin/ prefix stripped) |
| `diff.patch` | unified diff |
| `diff-stat.txt` | `git diff --stat` |
| `files.txt` | every changed file, verbatim, no filtering (agent classifies) |
| `summary.json` | machine-readable index |

The script does NOT classify anything. Read `files.txt` and the diff
yourself; for each file decide what it is and how to handle it (no
further approval needed — the user already opted in at Step 0):

- **Anchor doc** (a deliverable spec the code should fulfill: under
  `docs/`, a basename suggesting `plan`/`prd`/`spec`/`design`/`proposal`/
  `rfc`/ADR, or prose-style requirements added by the diff) → input
  to Phase 1.
- **Implementation** (the code being delivered) → input to Phase 2.
- **Test** (test for the implementation) → optional Phase 2 input;
  usually skip unless you suspect a test bug.
- **Meta doc** (README, CHANGELOG, CONTRIBUTING — describes state, not
  future delivery) → skip.
- **Binary / fixture / generated artifact** (PNG, model weights, lockfile,
  `go.sum`, vendored deps, minified JS) → skip; not suitable for this text
  review.
  Generated artifacts especially are noise — the source they come from
  is what matters.
- **CI / config / build** (`.github/workflows/*`, Makefile, Dockerfile)
  → optional Phase 2 input if the diff changes behavior; usually a
  smaller targeted review.

Mention skipped categories in the summary so the user sees what was
intentionally not reviewed.

---

## Step 2 — Phase 1 (docs-compliance)

**Trigger:** agent reads `files.txt` and identifies any file that's an
anchor doc — i.e. a deliverable spec the code is supposed to fulfill.
Typical signals: under `docs/`, basename or path containing `plan`,
`prd`, `spec`, `design`, `requirements`, `proposal`, `rfc`, an ADR, or
the diff itself adds prose-style requirements. README/CHANGELOG/CONTRIBUTING
usually aren't anchor docs (they describe state or process, not future
delivery). When in doubt, read the file: if it lists requirements the
code should meet, it's an anchor.

If no anchor docs exist in this diff, **skip Phase 1 entirely** and tell
the user. Do not invent a docs-compliance review when there are no
deliverables to check.

Otherwise:

1. Read enough locally to classify each anchor doc and select the
   implementation paths most likely to deliver it. The doc may name specific
   files; if not, scope to the obvious entrypoints in the diff.
2. Build `$RUN_DIR/phase1-prompt.txt` from the docs-compliance skeleton in
   `references/prompt-templates.md`. Put only the review question, commit
   identifiers, and path manifest in the prompt. Never paste doc text, source
   text, excerpts, or diff hunks into it.
3. Invoke `panel-review` via the `Skill` tool: `skill="panel-review"`,
   with `args` naming the prompt file and the audit root from
   `$RUN_DIR/repo_root` as `--cwd`. Reviewers read the exact target version
   with `git show <head_sha>:<path>` and inspect `$RUN_DIR/diff.patch`
   themselves. Capture the synthesis output.
   No mid-flow approval gate — the user's Step 0 confirmation covered
   this launch.
4. Surface results: quote the synthesis report's `## Consensus` and
   `## Divergence` sections verbatim — paraphrasing loses model-level
   confidence cues. Add a per-requirement gap verdict in your own words.
5. Proceed straight to Phase 2 — do not ask. (User already opted into the
   whole review at Step 0.)

---

## Step 3 — Phase 2 (bug hunt)

Always runs.

1. Curate the implementation paths from `files.txt`. Skip files that are
   obviously not implementation (anchor docs reviewed in Phase 1, vendored
   fixtures, README-style docs). Bugs hide in env-var parsers, error mappers,
   and other "boring" helpers, so do not silently omit such paths.
2. If the changed implementation surface is too broad for one focused review,
   partition the path list by subsystem and run multiple path-based rounds.
   Never truncate a file and never move its body into the prompt.
3. Build `$RUN_DIR/phase2-prompt.txt` from the bug-hunt skeleton in
   `references/prompt-templates.md`. The prompt contains paths and revision
   metadata only. Spec-compliance is **explicitly out of scope** — Phase 1
   covered that.
4. Invoke `panel-review` with `--cwd "$(cat "$RUN_DIR/repo_root")"`.
   Reviewers read every selected file at `$RUN_DIR/head_sha` themselves.
   Capture synthesis. No mid-flow approval gate — the user's Step 0
   confirmation covered this launch.
5. **Verify findings before presenting.** Models hallucinate line
   numbers and occasionally invent bugs that don't exist. Spot-check the
   top 2–3 by severity against `git show <head_sha>:<path>` and confirm the
   bug is real. Do not verify against a potentially dirty working-tree copy.
   Adjust the severity downward (or label "could not verify") if the panel
   misunderstood the code. False findings waste reviewer time and erode trust
   faster than missed findings.
6. Surface results: by severity, with verification status. Two-vendor
   agreement is suggestive; agreement-on-the-same-mistake is also a
   thing (vendors share training data) — verification overrides.

---

## Step 4 — Cross-reference against PR threads (gh mode only)

Phase 2 produces a list of findings. **The findings come first.** The
cross-reference step's job is not to filter what the panel looks at —
the panel looks at the diff, period — but to decide which findings are
worth a new thread vs. already-covered.

```bash
bash shared/github-ops/comment-check.sh <pr-number> --include-resolved \
  > "$RUN_DIR/all-threads.json"
```

`comment-check.sh --include-resolved` returns every review thread on the PR
(resolved **and** unresolved), with comment bodies, resolution state, the
resolver's login, and a `last_comment` denormalization for quick
resolution-quality judgment. Without the flag the same script returns
unresolved-only (the contract pr-watch / pr-watch-auto consume); the flag
opts into the richer schema. The agent uses it as a lookup, not a scope.

For each finding from Phase 2 (and Phase 1 gaps if the user wants to
post those too):

1. Find threads on the same file, in the same function or contiguous
   code region (don't require exact line match — refactoring shifts
   lines, and the relevant comparison is the topic, not the line
   number). Use the comment body to judge topic overlap.
2. Decide:
   - **No related thread** → eligible for a new thread.
   - **Open (unresolved) thread covering the same concern** → skip.
     The reviewer has already filed this; duplicating wastes their time.
     Note the overlap in the summary so the user knows.
   - **Resolved thread covering the same concern** → look at *how* it
     was resolved. Read the last comment / resolver login.
     - **Substantive resolution** (a reply explaining the fix, a
       commit reference, code change applied) → assume the issue was
       addressed; skip.
     - **Thin resolution** (silently resolved, single-emoji reply,
       "thanks", no rationale) → still post the new finding, *and*
       emit a local warning that "this code overlaps with resolved
       thread <URL> but the resolution looked thin — your finding
       may still be valid." **Do not re-open the resolved thread**;
       reopening fights the prior reviewer's decision and is not
       this skill's call. The new thread stands on its own.

The whole point of this step is honest accounting: don't post
duplicates, don't silently swallow findings just because someone
clicked "Resolve" on adjacent code.

---

## Step 5 — Drafting and posting (gh mode only, opt-in)

The default after Step 4 is **report and stop**. Only post on explicit
user instruction ("draft threads for the HIGH ones", "post these", etc.).

1. For each finding the user wants to file: read
   `references/comment-style-guide.md` and produce a thread body. Write
   each to `$RUN_DIR/thread-N.md`.
2. Show all drafts in one message. Each header: `Thread N → <path> line <L>`.
   If a finding has a thin-resolution warning from Step 4, surface that
   alongside the draft so the user knows what they're stepping into.
3. **Approval gate.** Wait for confirmation. The user may edit drafts;
   re-show after each edit.
4. Post each via `scripts/post-review-thread.sh --pr <N> --path <path>
   --body-file $RUN_DIR/thread-N.md [--line N | --start-line A --end-line B]`.
   The script re-fetches the PR head SHA from the API right before
   posting, so the agent doesn't need to track it.
5. Print the comment URL for each posted thread.

---

## Step 6 — Termination

Print a summary table:

- Phase 1 gaps (if any), with verdict and PR-thread URL if posted.
- Phase 2 bugs by severity, with verification status, cross-reference
  classification (new / overlaps-open / overlaps-thin-resolved), and
  PR-thread URL if posted.
- Recommended next steps (what to fix, what to file as an issue, what
  to accept-with-doc).

Clean up `$RUN_DIR` only after the user confirms they don't need the
intermediate prompts. Some users want them for their own records.

---

## Example interactions

### Example 1 — gh mode

```
USER: review PR 21
AGENT: Plan: gh mode, PR #21. I'll gather the diff, run Phase 1
       (docs-compliance) if I find anchor docs in the diff, then Phase 2
       (bug hunt) always. Panel-review will call Claude/Agy/Codex.
       After Phase 2 I'll cross-reference against existing PR threads
       and report findings. Posting inline threads is opt-in and I'll
       ask separately. OK?
USER: go
AGENT: [runs gather-context.sh --pr 21]
       → 1 anchor doc found: docs/rate_limit_plan.md
       → 35 implementation files, 200KB diff
       [Phase 1 runs, surfaces gaps]
       Phase 1 gaps: NUM_FLEX_WORKERS clobbered at startup (unanimous).
       [Phase 2 runs, surfaces bugs]
       3 HIGH findings verified.
       [runs list-all-threads.sh, classifies]
       → Bug #1 (multipart): 2 resolved threads on same function for
         a different concern (resource leak). New finding is net-new.
       → Bug #2 (notify wake-up): no overlapping threads.
       → Bug #3 (shutdown): no overlapping threads.
       Want me to draft and post these as inline threads?
```

### Example 2 — local mode

```
USER: pre-PR check on my feature branch
AGENT: Plan: local mode, base=main, head=feature/foo. I'll gather the
       diff, run Phase 1 if I find anchor docs, then Phase 2 always.
       Panel-review will call Claude/Agy/Codex. Local mode so no
       PR-thread cross-reference and no posting. OK?
USER: go
AGENT: [runs gather-context.sh --branches main feature/foo]
       → 0 anchor docs in this diff. Skipping Phase 1.
       → 8 implementation files, 14KB diff.
       [Phase 2 runs]
       2 MEDIUM findings, both verified. Here's the report. Want to
       address before pushing?
```

### Example 3 — thin-resolved warning

```
AGENT: Bug #1 overlaps with resolved thread
       https://github.com/x/y/pull/21#discussion_r3185483094 — resolved
       by user "alice" with no reply, just clicked Resolve. The new
       finding may still be valid; the thin resolution doesn't establish
       that the underlying concern was addressed. Drafting anyway.
       Want me to post?
```

---

## Preflight Checks

Before any workflow:

1. Clean working tree — `git status --porcelain` empty. (Less critical
   for local mode if you're operating purely on git refs, but uncommitted
   edits in the working tree can leak into `Read` calls during
   verification — easier to just stash.)
2. Both branches reachable locally (local mode) OR PR is open and
   accessible (gh mode).
3. `panel-review` skill present. `scripts/doctor.sh` verifies this and
   the `shared/github-ops` scripts.
4. For gh mode: GitHub credential helper returns a token. `doctor.sh`
   checks.

Any failure → STOP and ask the user to fix.

---

## Primitives

| Intent | Command |
|---|---|
| Diagnose environment | `scripts/doctor.sh` |
| Gather diff + file list | `scripts/gather-context.sh --branches <base> <head> --out <dir>` OR `scripts/gather-context.sh --pr <n> --out <dir>` |
| List all PR threads (resolved + unresolved) | `shared/github-ops/comment-check.sh <pr> --include-resolved` |
| Run Phase 1 / Phase 2 panels | `panel-review` with the phase prompt and `--cwd "$(cat "$RUN_DIR/repo_root")"` |
| Post one inline review thread | `scripts/post-review-thread.sh --pr N --path PATH --body-file FILE [--line N \| --start-line N --end-line M]` |

Scripts auto-detect owner/repo from the git remote in gh mode. Local mode
never touches the network.

---

## Guardrails

The skill should not:

- **Start any work — context-gather or panel — without the Step 0
  upfront confirmation.** Panels burn time and tokens, and the user
  must opt in before the skill does anything. Once they've approved at
  Step 0, do not re-gate between phases; the user explicitly does not
  want to wait through context-gather or Phase 1 only to be asked again.
- **Post any inline thread without explicit confirmation of that specific
  draft.** Posts are user-visible, can't be silently undone, and the
  user's name is on them.
- **Re-open a resolved thread**, even when the resolution was thin.
  The prior reviewer made a call; reopening it is not this skill's
  decision. Post a new thread with a local warning instead.
- **Skip the cross-reference step in gh mode.** Duplicating an existing
  thread is the worst outcome — wastes reviewer time and undermines
  trust faster than any false negative.
- **Inflate Phase 2 with style nits, "could be cleaner" suggestions, or
  other non-bugs.** The bug-hunt prompt template explicitly excludes
  those; don't forward them up to the user. Style belongs to lint,
  not to a review thread.
- **Inline reviewed content into a panel prompt.** Both phases are path-based:
  pass the repo root, revision IDs, diff path, and selected file paths; let
  each reviewer read the exact files itself.
- **Edit code in this skill.** Review-only is the contract. Fixes belong
  to `auto-fix` or to the human author.
- **Use `gh` CLI.** Its auth scope is independent of this session's git
  credentials, which can mask permission failures until they surface
  mid-post.

---

## Files

| Script | Purpose |
|--------|---------|
| `scripts/doctor.sh` | Diagnose environment (wraps shared github-ops doctor + checks panel-review skill) |
| `scripts/gather-context.sh` | Build a unified review context bundle from local branches or a PR |
| `scripts/post-review-thread.sh` | Post one inline review thread; supports single-line and multi-line targets; re-fetches head SHA by default |
| `shared/github-ops/*.sh` | Shared GitHub primitives (auth, ci-check, comment-check, etc.) |
| `shared/vendors/*` | Shared vendor CLIs (used indirectly via panel-review) |

| Reference | Purpose |
|-----------|---------|
| `references/prompt-templates.md` | Phase 1 (docs-compliance) and Phase 2 (bug-hunt) prompt skeletons |
| `references/comment-style-guide.md` | Copilot-style inline-comment format with examples |
| `references/troubleshooting.md` | Common failure modes (auth drift, head-SHA mismatch, oversized prompt) |

---

## Out of scope

- **Fixing** anything the review surfaces. Use `auto-fix` to apply suggestions.
- **CI monitoring.** That's `pr-watch` / `pr-watch-auto`.
- **Resolving or replying to existing threads.** Different consent model.
  This skill posts new threads only.
- **Cherry-picking commits.** Different concern; out of scope here.

If the user asks for any of these mid-review, hand off — don't expand scope.
