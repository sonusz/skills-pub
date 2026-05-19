---
name: pr-watch-auto
description: >
  Use when a PR has been pushed and you need to autonomously monitor GitHub Actions
  CI and PR review comments together, addressing each as they arrive. The agent
  orchestrates a single interleaved loop — CI failures are auto-fixed (up to N
  attempts, default 3), review comments are delegated to the auto-fix skill per
  thread, applied changes are squash-committed and pushed, and threads are
  resolved or escalated. Only activate on explicit opt-in such as "pr-watch-auto",
  "auto-fix ci", "watch ci auto", or "auto watch comments".
allowed-tools: Bash, Read
---

# PR Watch & Auto-Fix Skill

Autonomous variant of `pr-watch`. Only activate on explicit opt-in ("pr-watch-auto", "auto-fix ci", "watch ci auto", "auto watch comments") — generic `pr-watch` phrasing belongs to the approval-gated skill.

**Use pr-watch scripts, not `gh` CLI** — scripts use the git credential helper; `gh` requires separate auth.

The agent owns orchestration. Scripts are one-shot primitives; CI and comments share one interleaved loop.

Modes: `alert` (default) reports CI failures only; `fix` runs `auto-fix` on CI failures up to N attempts (default 3); `--no-watch-comments` disables the default-on comment phase.

---

## Workflow Overview (agent-driven)

```
Init: preflight; snapshot { ci_sha, ci_status, unresolved_n, active_cr_n, issue_comments_n }.

Loop by precedence:
1. failed CI + `fix` attempts remain -> evaluate via `auto-fix`, present issue + proposed fix + warning, get user confirmation; on confirm: apply + push + re-snapshot; on decline or escalation: write `/tmp/ci-needs-human-<pr>.md`, exit
2. failed CI in `alert`, or attempts exhausted -> report, exit
3. unresolved comments -> evaluate threads, user chooses actions, apply approved fixes, test, push, reply/resolve, re-snapshot
4. active change requests OR new issue comments -> present to user (no auto-fix path; no file scope), take user-directed action, re-snapshot
5. CI passed + no unresolved comments + no active change requests -> print summary, write result file, exit
6. otherwise -> `shared/github-ops/wait-for-state.sh`, re-snapshot, loop
```

Invariants:
- Failing CI preempts comment fixes.
- Pending CI does not block comment fixes; a later push supersedes the in-flight run.
- Re-snapshot after every push; never trust a stale head SHA.

---

## Stateful gates

Confirmation binds to `(action, state_snapshot)`, not a blanket yes. CI/test loops take 10–20 minutes; in that window a reviewer can comment or someone else can push, shifting the state the user consented against. Prior consent is **invalid for the new state** — re-prompt with the diff before any remote write.

Two scripts enforce this server-side:

| Action | Gate-time snapshot | Script |
|---|---|---|
| Push a CI-fix commit | Remote `head_sha` of the PR branch (= local HEAD before auto-fix commits) | `scripts/push-with-snapshot.sh --prior-head-sha <sha>` |
| Post reply / resolve thread | Thread comment `database_id`s from `shared/github-ops/comment-check.sh` taken at gate time | `shared/github-ops/comment-resolve.sh ... --prior-comment-ids "<ids>"` |

Refusal exit codes: `4` = drift before the action, `5` = drift after our reply but before resolve. On refusal: fetch fresh state, show the user the diff (new comments / new commits, not just "moved"), get fresh confirmation, retry with the new snapshot.

Never paper over a refusal by re-running the script with re-fetched state without going back to the user.

---

## Preflight Checks

Before any workflow, and again on every CI retry: clean working tree, branch matches PR head, remote matches PR repo. Any failure -> STOP, ask user.

**When something breaks:** read `references/troubleshooting.md` — covers `doctor.sh`, auth/permission failures, token masking, state files.

---

## Primitives

The agent composes one-shot commands; only `shared/github-ops/wait-for-state.sh` blocks.

| Intent | Command |
|---|---|
| Current CI status for PR/branch | `shared/github-ops/ci-check.sh [<pr-or-branch>]` |
| Unresolved inline review threads | `shared/github-ops/comment-check.sh <pr-or-branch>` |
| Active top-level change requests, review summaries, issue comments | `shared/github-ops/pr-review-check.sh <pr-or-branch>` |
| Block until CI or comment state changes | `shared/github-ops/wait-for-state.sh <pr-or-branch> --ci-status pending --unresolved N [--max-wait 1200]` |
| Diagnose environment | `scripts/doctor.sh` |

`shared/github-ops/comment-check.sh` and
`shared/github-ops/pr-review-check.sh` are complementary: the first covers
inline review THREADS (line-anchored comments); the second covers everything
else — `CHANGES_REQUESTED` reviews whose body isn't tied to a line, bot
review summaries, and PR-level issue comments. A reviewer asking "rename the
branch" only shows up in `shared/github-ops/pr-review-check.sh`. Always run
both.

`shared/github-ops/wait-for-state.sh` does NOT watch top-level reviews directly. New
top-level reviews are picked up only on the next re-snapshot triggered by
some other change (CI status flip, inline-comment count change, or
timeout). This is acceptable because PR-level reviews are infrequent — but
do not rely on the wait loop to wake on a new `CHANGES_REQUESTED`.

Scripts auto-detect owner/repo from the git remote; default target is the current branch. Bounded retry details live in `references/troubleshooting.md`.

---

## Fix decisions delegate to `auto-fix`

CI failures invoke `auto-fix` in `mode: "apply"`. Comment threads use `mode: "evaluate"` for triage, then `mode: "apply"` only for threads the user approved with `apply fix`.

`auto-fix` owns all fix-or-escalate gates: design intent, minimum guard, semantic reversal, and bot-author asymmetry. If it is missing, stop and ask the user to install it.

---

## Agent Loop Details

### Snapshot (every iteration)

`ci_sha = git rev-parse HEAD` or PR head SHA; `ci_json = shared/github-ops/ci-check.sh <target>`; `ci_status = jq -r .status` (`passed | failed | pending | error`); `unresolved_n = shared/github-ops/comment-check.sh <target> | jq -r .total_unresolved`; `pr_review_json = shared/github-ops/pr-review-check.sh <target>`; `active_cr_n = jq -r .total_active_change_requests`; `issue_comments_n = jq -r .total_issue_comments`.

Re-snapshot any time the head SHA changes (after push) — the old snapshot is stale.

### CI-failure action

When `ci_status == "failed"` in `fix` mode and attempts remain:

1. Derive `affected_files` from `ci_json.error_log` — only files named there, no inference.
2. Invoke `auto-fix` with `{ mode: "evaluate", error_log, branch, affected_files, attempt_number }`. **Capture `evaluate_hash` from the response.**
3. **Capture the gate snapshot**: `prior_head_sha = git ls-remote origin refs/heads/<branch>` (or the snapshot's `ci_sha`).
4. Present the triage with this framing, then ask for explicit confirmation:
   - **Issue**: one-paragraph summary from the error log and `auto-fix`'s evaluation.
   - **Proposed fix**: `auto-fix`'s description of the change.
   - **Snapshot**: branch + short `prior_head_sha`.
   - **Warning**: *"If you confirm, this fix will be applied, committed, and pushed without further confirmation — but only if the remote head is still `<prior_head_sha>` when the push runs. If someone else pushed in the meantime, the push will refuse and you'll be re-prompted."*
5. Decline → write `/tmp/ci-needs-human-<pr>.md` (triage + reason) and exit. The file is a persistent per-PR escalation receipt; the skill never deletes it.
6. Confirm → invoke `auto-fix` with `{ mode: "apply", error_log, branch, affected_files, attempt_number, confirmed_evaluate_hash: <step-2 hash> }`. If auto-fix escalates with `evaluate hash mismatch`, the proposed change has drifted — re-run from step 2 with fresh evaluate output and a fresh confirmation.
   - `applied`: push via `scripts/push-with-snapshot.sh --prior-head-sha "$prior_head_sha"`. Push exit 4 (remote drifted) → fetch fresh state, show the user the new head + new commits, get fresh confirmation, retry. Push success → re-snapshot, loop.
   - `escalated`: write `/tmp/ci-needs-human-<pr>.md` with `conflict`/`evidence`/`recommendation` and exit.

Each attempt repeats steps 1–6; gates are per-attempt, never carried. On the final attempt, skip `auto-fix` and write the needs-human file directly.

### Comment-batch action

When `unresolved_n > 0`:

1. Fetch threads via `shared/github-ops/comment-check.sh`. **Capture per-thread gate snapshots**: `prior_comment_ids[thread_id] = ",".join(c.database_id for c in thread.comments)` straight from this output. Do not rebuild later.
2. For each thread, derive `author_type` (bot vs human) and `affected_files` from the comment's `path`.
3. Invoke `auto-fix` per thread with `{ mode: "evaluate", comment_text, author_type, affected_files, pr_number, head_sha }`. **Capture `evaluate_hash` per thread.**
4. Print one triage block per thread using `references/comment-analysis-template.md`; analysis comes from `auto-fix`'s evaluation, not a local heuristic.
5. Ask per-thread: `apply fix` / `response with reject reason` / `customize response` / `resolve`. Unmentioned threads untouched. Consent is bound to `prior_comment_ids[thread_id]`; drift → script refuses (Stateful gates) → re-prompt.
6. For each `apply fix` thread, invoke `auto-fix` with `{ mode: "apply", comment_text, author_type, affected_files, pr_number, head_sha, confirmed_evaluate_hash: <step-3 hash for this thread> }`. The hash satisfies auto-fix's per-thread Confirmation gate, so an "approve all" still produces one consent per fix. Hash mismatch → re-evaluate that thread, re-show, re-confirm, retry. `auto-fix` may create a local commit on `applied`.
7. If any apply succeeded, squash the resulting commits into one (matching the last commit's format) and run tests locally.
8. Before any remote-changing action — commit/push (use `scripts/push-with-snapshot.sh --prior-head-sha <prior_head_sha>`, where `<prior_head_sha>` is the remote head observed at gate time), posting/editing a reply, or resolving a thread — get a final confirmation immediately before execution. Decline → stop before the remote change, keep local changes local.
9. For each reply/resolve: `shared/github-ops/comment-resolve.sh <pr> <thread_id> <reply> <comment_db_id> --prior-comment-ids "<prior_comment_ids[thread_id]>"`. Exit 4 (drift before reply) → re-triage. Exit 5 (drift after reply, before resolve) → reply is visible, thread stays open, re-evaluate. Either case: fetch fresh state, show diff, re-confirm, retry.
10. Draft reply/resolve content from `references/comment-replies.md`; the script does post + verify + resolve atomically.
11. Re-snapshot (including `prior_comment_ids` for unresolved threads) after any push or comment action; loop.

### PR-level review action

When `active_cr_n > 0` or new `issue_comments` have appeared since the prior snapshot:

1. These signals are **not** routed through `auto-fix`. A top-level CHANGES_REQUESTED review or a free-form PR comment isn't anchored to a file path; `auto-fix` has no scope to evaluate, and silently inferring one is exactly the kind of pattern-matching the bot-author asymmetry rule warns against.
2. For each active change request, print: author, `submitted_at`, full body, and "no code anchor — cannot auto-fix".
3. For new issue comments, print author, `created_at`, and body. Mark each as informational.
4. Ask the user what to do per signal. Typical actions and how to execute them:
   - **Reply with a comment** → post via `https://api.github.com/repos/<owner>/<repo>/issues/<pr>/comments` (issue comment, visible on the PR conversation). The skill does not own a script for this yet; compose with `_auth_curl` from `shared/github-ops/github-remote.sh` and ask the user to confirm the exact reply text before posting.
   - **Address by changing code** → user describes the fix; if it's a recognizable feature change, route through `auto-fix` with an explicit `affected_files` list provided by the user. Do not infer files from the review body.
   - **Address by branch/repo state** (e.g., rename branch, retarget base) → the skill does not perform these autonomously. Print the command(s) and let the user run them.
   - **Acknowledge and leave for human** → no action; the change request stays active and the skill will surface it again on the next snapshot.
5. Any action that produces a remote write (reply, push) requires explicit user confirmation immediately before execution, same as Guardrails demand for comment threads. Top-level reviews have no GraphQL `resolve` API — the only way to clear a CHANGES_REQUESTED is for the reviewer to approve or dismiss, so the skill never tries to.
6. Re-snapshot after any action and loop.

### Idle wait

When CI is pending and `unresolved_n` / `active_cr_n` / `issue_comments_n` are unchanged, call `shared/github-ops/wait-for-state.sh` with the snapshot. It blocks until CI status or inline-thread count changes (top-level review changes are not watched — see Primitives), or times out. Then you re-snapshot and loop.

### Termination

If CI passed, `unresolved_n == 0`, and `active_cr_n == 0`, and no action is pending, print the summary table, write the result file, then exit. Outstanding active change requests block termination — the agent surfaces them and waits for the user, rather than declaring "all clear" while a reviewer still requires changes. Output schemas live in `references/result-schema.md`.

---

## Guardrails

The skill must never:

- Resolve a thread without a new or existing reply. See `references/comment-replies.md`; do not silently close reviewer feedback.
- Commit or push comment-fix changes without explicit user confirmation
- Post a reply or resolve a thread without explicit user confirmation
- Apply, commit, or push a CI-fix attempt without the per-attempt confirmation described in the CI-failure action — opting into `fix` mode is session-level consent, not per-attempt consent
- Push, reply, or resolve without passing the gate snapshot (`--prior-head-sha` for pushes, `--prior-comment-ids` for replies/resolves) — the user's confirmation is bound to that snapshot, and the scripts will refuse if state has drifted
- Re-run a script with re-fetched state after a snapshot refusal without first showing the user the diff and getting fresh consent — that bypasses the very gate the refusal exists to enforce
- Push if local tests fail or you are uncertain
- Refactor unrelated code outside of what `auto-fix` changed
- Edit the code yourself to work around an `auto-fix` escalation — escalations exist for a reason
- Skip the re-snapshot after a push — the head SHA has moved and all prior state is stale

---

## Files

| Script | Purpose |
|--------|---------|
| `scripts/doctor.sh` | Diagnose environment (wraps `shared/github-ops/doctor.sh` + auto-fix sibling check) |
| `shared/github-ops/ci-check.sh` | One-shot CI status |
| `shared/github-ops/comment-check.sh` | Fetch unresolved inline review threads |
| `shared/github-ops/pr-review-check.sh` | Fetch active top-level change requests, review bodies, and issue comments |
| `shared/github-ops/wait-for-state.sh` | Block until CI/inline-comment state changes or timeout (does NOT watch top-level reviews) |
| `shared/github-ops/comment-resolve.sh` | Reply to + resolve a thread; refuses on thread-state drift (snapshot-gated) |
| `scripts/push-with-snapshot.sh` | Push only if remote head still matches the gate snapshot; refuses on remote drift |
| `shared/github-ops/github-remote.sh` | Detect owner/repo from git remote |

**Security:** Never run scripts with `bash -x` — token leaks via variable expansion. Details in `references/troubleshooting.md`.
