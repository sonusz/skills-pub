# Prompt Templates

Two reusable prompt skeletons for the panel-review phases. Each template
is a *skeleton* — the agent fills in the artifacts and tailors the questions
to the specific diff. Don't copy verbatim; treat as a structural guide.

---

## Phase 1 — Docs-compliance

**Goal:** Does the delivered code fulfill the explicit requirements in the
new/modified doc(s), and does it stay within them? For each requirement,
classify yes / partial / no / unclear, with a code citation. Separately,
flag anything the diff adds beyond those requirements (overreach) or any
new mechanism disproportionate to what the requirements need.

**Path inputs:** the audit repo root, merge-base/head SHAs, `diff.patch`, the
anchor-doc paths, and the implementation paths most likely to deliver the
docs. Reviewers read the exact files themselves; the prompt never contains
the reviewed bodies.

**Skeleton:**

```
You are reviewing whether a code change fulfills a short product/engineering
plan. The reviewed content is available from the audit root and revision
manifest below. Read the exact plan and implementation files yourself before
judging; do not infer from the path list or diff stat. For each requirement in
the plan, state:

1. whether it is satisfied (yes / partial / no / unclear),
2. the specific code location(s) that satisfy or contradict it,
3. any deviation, ambiguity, or missing behavior worth flagging.

End with a short "gaps" list (requirements NOT satisfied or only partially
satisfied) and an "excess" list (see items 4-5 below). Be concrete. Quote
line numbers / function names. Do not generalize.

AUDIT_ROOT: <absolute repo root>
MERGE_BASE_SHA: <merge-base SHA>
TARGET_HEAD_SHA: <head SHA>
CHANGE_DIFF: <absolute path to diff.patch>
CHANGED_FILES: <absolute path to files.txt>

ANCHOR_DOC_PATHS:
- <repo-relative doc path>

IMPLEMENTATION_PATHS:
- <repo-relative implementation path>

Reading contract:
- Run file and git reads from AUDIT_ROOT.
- Read a target file exactly as reviewed with:
  git show TARGET_HEAD_SHA:<repo-relative-path>
- Use CHANGE_DIFF or:
  git diff MERGE_BASE_SHA..TARGET_HEAD_SHA -- <path>
- Read every listed anchor doc and selected implementation file before
  answering.
- If any required path or revision is unreadable, report the review as
  incomplete. Do not guess.

============================================================================
What you need to produce
============================================================================

1. A line-by-line check of plan requirements vs the code. Walk through each
   section in order.
2. For each requirement, cite the function name(s) responsible. Quote the
   relevant constant or branch.
3. Highlight any deviation, ambiguity, or missing item.
4. Overreach: does the diff add behavior, a dependency, or structure the
   anchor doc(s) never required? Flag it even if the code looks good —
   undocumented work is a finding, not a bonus. Cite the location and the
   requirement (or absence of one) that makes it overreach. Tests, logging,
   and helpers that serve a documented requirement are not overreach.
5. Proportionality: for each new mechanism, apply the two-step test —
   (a) would removing it break a specific anchor-doc requirement or
   constraint? If not, it is removable — report it. (b) If it would, is
   there a cheaper mechanism — already in the repo, or simply simpler —
   that satisfies the same requirement? Report only if so. Every finding
   needs the location, the proposed removal or alternative, and why every
   affected requirement and constraint still holds without the mechanism
   as built. A bare "could be
   simpler," a style preference, flexibility reserved for unstated future
   work, or line-count reduction alone is not evidence. Safety or guardrail
   code that protects against a real failure mode is not excess — but a
   rule, check, or hard stop that traces to no requirement and no real
   failure mode, and makes the system less robust (rejects valid inputs or
   states, hard-fails where degrading is safe, demands exact matches where
   the requirement tolerates variation — a robustness-principle
   violation), is excess: name what it breaks. Strictly rejecting
   ambiguous or security-relevant input is not excess.
6. End with a concrete "gaps" list (requirements NOT satisfied or only
   partially satisfied) and a concrete "excess" list (overreach and
   proportionality findings from steps 4-5; write "none" if there are
   none), plus a yes/partial/no verdict per plan section.

Be concise but specific. Reviewers will use your output to decide whether to
merge.
```

**Sizing**: keep total prompt ≤ 50 KB. If the implementation is larger,
scope the manifest to the files the doc actually names plus the entrypoint.
The prompt stays small because it contains paths, not file bodies. Bug-hunt
phase covers the rest.

---

## Phase 2 — Bug hunt

**Goal:** Find bugs in the implementation. Spec compliance is **explicitly
out of scope** — Phase 1 covers that. The two phases together produce
non-overlapping output.

**Path inputs:** every selected code file in the diff. Reviewers must read
each selected file **in full** from the target revision. Bugs hide in env-var
parsing, error mapping, and other "boring" helpers. If the set is too broad,
split it into focused path-based rounds rather than truncating files or
embedding their text.

**Skeleton:**

```
You are a senior <language> reviewer doing a focused bug hunt on <subsystem>.
Read every selected source file from the audit root and target revision, then
find ONLY bugs — not style nits, not "could be cleaner" suggestions, not
future-proofing. Bugs.

A bug is: code that produces wrong output, hangs, leaks resources, races,
panics, drops data, mishandles errors in a way that causes operator-visible
incorrect behavior, has a security flaw, or has a semantic mismatch with its
evident purpose. Spec-compliance issues are out of scope (a separate review
covered those).

# What to look for (non-exhaustive)

- Concurrency: data races on shared maps/slices, goroutine leaks, deadlocks,
  missed wake-ups, double-close, nil channel sends
- Resource handling: missing Close, leaked file descriptors, unbounded
  buffers, missing context cancellations, goroutines that never exit on
  shutdown
- Error handling: errors swallowed and overwritten, fallthrough bugs,
  ignored returns from Write/Close/Flush, double-write of HTTP response
- HTTP correctness: response not flushed before client disconnect path,
  headers added after WriteHeader, panics that propagate to ServeHTTP,
  requests forwarded with stale Body
- Multipart handling: parts not closed, parts read twice, file vs form
  distinguished by FileName() which can return "" for legitimate files
- JSON/serialization: silent type coercion bugs, missing fields treated as
  zero, integer overflow on uint64↔int conversions
- Time/TTL: TTL evaluated against the wrong clock, monotonic vs wall-clock
  confusion, zero-value timeouts, time.Now captured before/after the
  relevant event
- Edge cases: empty input, oversized input, unicode in mime types,
  content-type without boundary, env var parse silently falling back to
  default on user typo
- Logic: off-by-one in queue capacity, weight==0 enqueue success that
  bypasses backpressure, panic on negative weight
- Brittle rigidity: a check, rule, or hard stop with no evident purpose
  that turns a normal or recoverable situation into a failure — valid
  input rejected by an over-strict validator, exact-match or ordering
  assumptions the data doesn't guarantee, fail-closed on a transient or
  optional dependency, a retry/limit/timeout that aborts healthy work.
  Judge against the robustness principle (Postel's law): be liberal in
  what you accept from peers — tolerate unknown fields, extra whitespace,
  harmless reordering, optional-field absence, benign version skew — and
  conservative in what you send — emit well-formed, spec-exact output.
  The limit: liberal acceptance must never silently accept input that is
  ambiguous, security-relevant, or would be misinterpreted downstream;
  there strict rejection with a clear error is correct. Report a violation
  in either direction (rejecting harmless variation; emitting sloppy or
  non-conformant output; silently guessing on ambiguous input) as a bug
  only with the concrete input or state that trips it and the
  operator-visible failure; "I'd have written it more leniently" is not a
  bug

# Output format — REQUIRED

For each bug, produce:

```
BUG #N: <one-line title>
  Location:    <function name + approximate line range>
  Severity:    CRITICAL | HIGH | MEDIUM | LOW
  Reproduction: <how an operator/client would trigger it; concrete inputs if possible>
  Impact:      <what the operator sees: lost requests, hang, corrupted response, leak rate, etc.>
  Fix:         <one-or-two-sentence patch direction>
```

Severity rubric:
- CRITICAL: data loss, infinite hang under normal traffic, security flaw,
  panic on common input
- HIGH: resource leak under load, data race that can corrupt state, dropped
  responses on common code path
- MEDIUM: edge-case panic, error in uncommon path, observability defect
- LOW: minor incorrect status/log, harmless under current callers

Do not pad. If you only find 2 bugs, return 2. If you find none, say so
explicitly. False positives waste reviewer time. Be specific and cite line
numbers. Lead with the worst bug.

AUDIT_ROOT: <absolute repo root>
MERGE_BASE_SHA: <merge-base SHA>
TARGET_HEAD_SHA: <head SHA>
CHANGE_DIFF: <absolute path to diff.patch>
CHANGED_FILES: <absolute path to files.txt>

IMPLEMENTATION_PATHS:
- <repo-relative implementation path>
- <next repo-relative implementation path>

Reading contract:
- Run file and git reads from AUDIT_ROOT.
- Read each listed file in full at TARGET_HEAD_SHA with:
  git show TARGET_HEAD_SHA:<repo-relative-path>
- Inspect its change with CHANGE_DIFF or:
  git diff MERGE_BASE_SHA..TARGET_HEAD_SHA -- <path>
- Do not read a working-tree copy as the reviewed version.
- If a listed file cannot be read, report the review as incomplete. Do not
  silently skip it or guess.

# Notes that may help (optional — include any subtle invariants the model
# might miss without prompting)

- <invariant 1>
- <invariant 2>

What is the worst bug? Lead with that one.
```

**Note hints**: when the agent knows a non-obvious invariant about the code
(e.g., "this map is written once at startup and read concurrently"), include
it in the "Notes that may help" section. Models can miss subtle setups.

---

## Both phases — common rules

1. **Don't ask leading questions.** "Is this code correct?" produces
   sycophancy. "Find bugs" produces useful output.
2. **Demand a specific output format.** Free-form output is harder to
   cross-reference and harder to verify.
3. **Strip credentials and tokens** before sending. Read `$RUN_DIR/secrets.txt`
   (written by `gather-context.sh` via `shared/secrets/scan.sh --diff`): every
   `path:line:pattern` there names a file whose added lines match the secret
   denylist. Exclude that file from the manifest or redact a copy under a
   temporary audit root before building the prompt — reviewers read manifested
   files themselves, so a path is enough to leak the value. Then still check
   env-loaded values, default-config strings, anything `*_KEY` or `*_TOKEN`:
   the denylist is high-confidence, not complete.
4. **Never include reviewed bodies in the prompt.** No source, doc text,
   excerpt, or diff hunk. Include only instructions and the path/revision
   manifest.
5. **Don't include the test files in the bug-hunt manifest** unless you
   suspect a test bug. Tests double the prompt size and rarely contain
   production bugs.
6. **For both phases, write the finalized prompt to a file** (e.g.
   `$RUN_DIR/phase1-prompt.txt`). The panel-review launcher reads from disk
   so identical bytes go to every vendor.
