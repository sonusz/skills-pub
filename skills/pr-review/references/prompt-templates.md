# Prompt Templates

Two reusable prompt skeletons for the panel-review phases. Each template
is a *skeleton* — the agent fills in the artifacts and tailors the questions
to the specific diff. Don't copy verbatim; treat as a structural guide.

---

## Phase 1 — Docs-compliance

**Goal:** Does the delivered code fulfill the explicit requirements in the
new/modified doc(s)? For each requirement, classify yes / partial / no /
unclear, with a code citation.

**Inline:** the doc(s) verbatim, plus the implementation files the doc
references (or, if the doc doesn't name files, the implementation files in
the diff most likely to deliver the doc's content).

**Skeleton:**

```
You are reviewing whether a code change fulfills a short product/engineering
plan. Read the plan and the implementation; for each requirement in the plan,
state:

1. whether it is satisfied (yes / partial / no / unclear),
2. the specific code location(s) that satisfy or contradict it,
3. any deviation, ambiguity, or missing behavior worth flagging.

End with a short "gaps" list: requirements that are NOT satisfied or are only
partially satisfied. Be concrete. Quote line numbers / function names. Do not
generalize.

============================================================================
ARTIFACT 1 — <doc path>
============================================================================

<doc content verbatim>

============================================================================
ARTIFACT 2 — <implementation file path>
============================================================================

```<lang>
<file content>
```

============================================================================
ARTIFACT 3 — <next implementation file path> (if multiple)
============================================================================

...

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
scope to the files the doc actually names + the entrypoint. Bug-hunt
phase will cover anything missed.

**Trim policy**: in this phase, it is OK to selectively inline the parts of
implementation files most relevant to the doc's claims. The bug-hunt phase
will inline full files separately, so coverage isn't lost.

---

## Phase 2 — Bug hunt

**Goal:** Find bugs in the implementation. Spec compliance is **explicitly
out of scope** — Phase 1 covers that. The two phases together produce
non-overlapping output.

**Inline:** every code file in the diff, **in full**. No trimming. Bugs hide
in env-var parsing, error mapping, and other "boring" helpers. If the prompt
exceeds 50 KB, split into focused rounds (one file per round) rather than
truncating mid-file.

**Skeleton:**

```
You are a senior <language> reviewer doing a focused bug hunt on <subsystem>.
Read the complete source below and find ONLY bugs — not style nits, not
"could be cleaner" suggestions, not future-proofing. Bugs.

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

============================================================================
ARTIFACT 1 — <file path> (FULL)
============================================================================

```<lang>
<file content verbatim>
```

============================================================================
ARTIFACT 2 — <file path> (FULL)
============================================================================

...

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
4. **Don't include the test files in the bug-hunt prompt** unless you
   suspect a test bug. Tests double the prompt size and rarely contain
   production bugs.
5. **For both phases, write the finalized prompt to a file** (e.g.
   `$RUN_DIR/phase1-prompt.txt`). The panel-review launcher reads from disk
   so identical bytes go to every vendor.
