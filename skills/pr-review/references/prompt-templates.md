# Prompt Templates

Two reusable prompt skeletons for the panel-review phases. Each template
is a *skeleton* — the agent fills in the artifacts and tailors the questions
to the specific diff. Don't copy verbatim; treat as a structural guide.

---

## Phase 1 — Docs-compliance

**Goal:** Does the delivered code fulfill the explicit requirements in the
new/modified doc(s)? For each requirement, classify yes / partial / no /
unclear, with a code citation.

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

End with a short "gaps" list: requirements that are NOT satisfied or are only
partially satisfied. Be concrete. Quote line numbers / function names. Do not
generalize.

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
4. End with a concrete "gaps" list and a yes/partial/no verdict per plan
   section.

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
3. **Strip credentials and tokens** before sending. Check env-loaded values,
   default-config strings, anything `*_KEY` or `*_TOKEN`.
4. **Never include reviewed bodies in the prompt.** No source, doc text,
   excerpt, or diff hunk. Include only instructions and the path/revision
   manifest.
5. **Don't include the test files in the bug-hunt manifest** unless you
   suspect a test bug. Tests double the prompt size and rarely contain
   production bugs.
6. **For both phases, write the finalized prompt to a file** (e.g.
   `$RUN_DIR/phase1-prompt.txt`). The panel-review launcher reads from disk
   so identical bytes go to every vendor.
