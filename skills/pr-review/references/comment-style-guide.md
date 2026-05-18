# Inline Comment Style Guide

Format for posting review threads on a PR. Mirrors the style used by
GitHub Copilot's PR reviewer so threads land in the same visual register
as existing review tooling — readers don't have to switch mental modes
between bot and human comments.

---

## Structure (in order)

1. **Behavior statement** — one sentence: what the code does that's wrong.
   Quote the function name or the specific construct (e.g. ``CreateFormFile``,
   ``WeightedQueue.notify``). Don't editorialize ("this is bad"); state it.
2. **Mechanism / sub-claims** — if the bug has multiple facets, enumerate
   them as a numbered list. Each entry one sentence.
3. **Concrete consequence** — what does the operator / client see, and when.
   Include the failure mode: status code, log line, behavior change.
   Distinguish "currently observable" from "silent contract drift".
4. **Suggested fix** — a fenced code block:
   - ` ```suggestion` block when the fix is small enough to one-click apply
     in GitHub's UI. The block must contain the exact replacement lines for
     the comment's target range (and the comment must be a multi-line
     comment with `start_line` / `line` matching).
   - ` ```<lang>` block (regular code reference) when the fix needs new
     fields, refactors, or multi-file changes. Show the structure; don't
     pretend it's apply-able.

---

## Length

3–6 short paragraphs is the sweet spot. The longest justified comment is
~12 lines of prose plus a code block. Anything longer should be a draft
in the PR description, an issue, or a doc — not an inline thread.

If the rationale needs more space than that, you're trying to teach,
not review. Link to a doc instead.

---

## Tone

- **Technical, not interpretive.** "X drops the original part headers"
  not "this could be problematic if".
- **Bounded claims.** "On hostPath / PVC mounts (which `DATA_ROOT_DIR`
  typically points to), these directories accumulate" — specify the
  conditions, don't blanket-claim.
- **Mention severity only via consequence**, not adjectives. "client gets
  408" lands harder than "this is critical".
- **Don't reference the panel** or any AI/automated origin. The thread
  should read as if a careful human wrote it.
- **No emojis.**

---

## Targeting (line vs. multi-line)

- **Single-line** (`line` only) — when the comment refers to one specific
  line and the suggestion (if any) is exactly one line.
- **Multi-line** (`start_line` + `line`) — when the suggestion replaces
  multiple lines, or when the comment refers to a contiguous block whose
  meaning depends on the whole.

Test before posting: if you'd want a "Apply suggestion" button in the UI
for a multi-line suggestion, you need a multi-line comment. Single-line
comments only allow single-line suggestions to be applied.

---

## Cross-reference before posting

Findings come from the panel — they're produced by reading the diff,
not by scanning threads. Use the existing threads (resolved + unresolved
via `shared/github-ops/comment-check.sh <pr> --include-resolved`) to
decide whether each finding is worth a new thread, given what's already
on the PR.

For each finding, look at threads on the same file in the same function
or contiguous code region (don't require exact line match — refactoring
shifts lines, the relevant comparison is topic). Then:

1. **No related thread** → post.
2. **Open thread covering the same concern** → skip. The reviewer has
   already filed this; duplicating wastes their time. Note the overlap
   in the summary so the user knows.
3. **Resolved thread covering the same concern** → look at *how* it was
   resolved. Read the last comment, or check whether the resolver left a
   reply at all.
   - **Substantive resolution** (a reply explaining the fix, a commit
     reference, a code change applied) → assume addressed; skip.
   - **Thin resolution** (silently resolved, single-emoji reply,
     "thanks", no rationale) → post the new finding anyway. **Do not
     reopen the resolved thread** — the prior reviewer made a call,
     reopening fights it. The new thread stands on its own. Surface
     the overlap and the thinness of the prior resolution to the user
     as a local warning so they're not surprised when the PR author
     responds with "we already discussed this".

Duplicate or near-duplicate threads are the single fastest way to lose
trust as a reviewer. Honest accounting of overlap — including the
thin-resolution case — is the difference between useful and noisy.

---

## Examples

### Example 1 — multipart header loss (small fix, multi-line, suggestion block)

```markdown
`forceSamplingOnMultipart` rebuilds each non-metadata part with `CreateFormFile` / `CreateFormField`, which synthesize headers from scratch. Three things break:

1. File parts lose their declared `Content-Type` (e.g. `application/pdf` → `application/octet-stream` hardcoded by `CreateFormFile`).
2. The `metadata` part loses `Content-Type: application/json` (`CreateFormField` writes no Content-Type at all).
3. RFC 7578-compliant file uploads without `filename=` get reclassified as regular form fields — FastAPI's `file: UploadFile = File(...)` binding then rejects them with 422.

#3 is an immediate hard break for any client that omits `filename=`. #1/#2 are silent today (Python reads MIME from `metadata.meta.type`) but become real failures the moment Python adds any header inspection. Affects every `/convert/text` request.

Preserve the original part headers in one step:
```suggestion
		nextPart, err := writer.CreatePart(part.Header)
		if err != nil {
			return nil, "", err
		}
		if _, err := nextPart.Write(partBytes); err != nil {
			return nil, "", err
		}
```
```

Targeting: multi-line, `start_line=962, line=979` — the suggestion replaces
the entire if/else block.

### Example 2 — concurrency bug (small fix, multi-line, suggestion block)

```markdown
`WeightedQueue.notify` is a buffered channel with capacity 1, and `Enqueue` non-blocking-sends into it. When ≥2 producers enqueue between consumer wake-ups, only the first send lands in the buffer; subsequent sends hit the `default` branch and are dropped.

The "between consumer wake-ups" race window is small but practically always hit by concurrent requests: it's the time between `Enqueue`'s notify-send and the moment a parked worker is actually scheduled to receive from the channel. The mutex serializes producers in nanoseconds, but worker wake-up takes microseconds — so two HTTP handlers calling `Enqueue` back-to-back will both observe the buffer full.

Concrete consequence: when a burst arrives into an idle queue, only one worker wakes for that burst. It self-drains via `TryDequeue` (so items aren't lost), but the remaining workers stay parked until a future `Enqueue` re-arms the signal.

For the multimodal queue this combines badly with the 30s `FastJobTTL`. The queue is sized for 6 credits with videos weighted at 3 — designed for two concurrent videos. If two video requests arrive in the same scheduling window, only one worker takes a video; the second waits in the queue. By the time the first 60s job finishes and the worker picks up the second, `processJob`'s TTL check sees `time.Since(CreatedAt) > 30s` and returns **408 "inspection expired in fast queue"** — without ever attempting the conversion.

Re-fire `notify` after a successful dequeue when items remain, so each wake-up passes the token to the next worker:
```suggestion
	job := q.items[0]
	q.items = q.items[1:]
	q.used -= job.Weight
	if len(q.items) > 0 {
		select {
		case q.notify <- struct{}{}:
		default:
		}
	}
	return job
```
```

### Example 3 — structural fix (multi-file change, single-line, code reference)

```markdown
`server.Shutdown(ctx)` honors `ShutdownTimeout`, but `workerWaitGroup.Wait()` is unbounded. Workers blocked in `httpClient.Do` are bounded only by `BackendTimeout` (default 10 minutes), so `Shutdown` can stall well past `GO_MAESTRO_SHUTDOWN_TIMEOUT` (default 15s).

Under K8s rolling restart this drives the pod into SIGKILL at `terminationGracePeriodSeconds`. SIGKILL bypasses Maestro Python's `atexit.register(self.fini)` cleanup, so `maestro-worker-{pid}-…` directories accumulate on persistent volumes — eventually the disk-health check fails and triggers another restart, feeding a vicious cycle.

Bound the wait by the shutdown context, and cancel a worker-scoped context so in-flight `httpClient.Do` calls actually abort:
```go
func (g *Gateway) Shutdown(ctx context.Context) error {
    serverErr := g.server.Shutdown(ctx)
    g.cancelWorkers()
    done := make(chan struct{})
    go func() { g.workerWaitGroup.Wait(); close(done) }()
    select {
    case <-done:
    case <-ctx.Done():
    }
    return serverErr
}
```
with the worker-cancel context threaded into `forwardToMaestro`'s HTTP request.
```

Targeting: single-line on the `workerWaitGroup.Wait()` line. Fix is a code
*reference*, not a `suggestion` — it depends on a new struct field that
the PR doesn't have yet.

---

## What NOT to file as inline threads

- **Style nits, formatting, ordering** — lint job's responsibility.
- **"Could be cleaner"** — review threads are a precious channel. Don't
  spend it on subjective preferences.
- **Future-proofing for hypothetical requirements** — file an issue if
  the concern is real.
- **Multiple unrelated bugs in one thread** — open a separate thread per
  bug, even if they're in the same function.
- **LOW-severity findings on a busy PR** — surface in the report, but
  don't post unless explicitly requested.
