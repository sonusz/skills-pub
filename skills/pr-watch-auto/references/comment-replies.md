# Comment Replies

Read this reference only when handling review threads.

## Reply-before-resolve invariant

Never resolve a thread without a new or existing reply. A silent resolve looks answered in the UI but gives the reviewer no record of what was understood, changed, or intentionally left untouched.

Every resolve reply must record:
- Why the change was made, or why it was not, citing the design-intent evidence from `auto-fix`
- How it was made: `file:line` when applicable, otherwise a one-line summary
- What was not changed when the comment implied a broader scope than what was applied; naming out-of-scope items prevents the reviewer from assuming the rest was missed

When `pr-watch-auto` prints a separate analysis block from `auto-fix` evaluation mode, keep the reply concise and action-oriented. The printed analysis is the detailed rationale; the thread reply is the durable summary.

The user chooses the action for each thread after reading the printed analysis:
- `apply fix` — fix, then reply, then resolve if the issue is immediately resolved
- `response with reject reason` — no code change; reply with the rejection rationale, usually based on `auto-fix`'s evaluation
- `customize response` — use user-provided reply text; resolve only if the user explicitly wants that and the issue is immediately resolved
- `resolve` — close an already-settled thread with an explicit reply when the user asks for that

If the user says nothing about a thread, leave it untouched.

Any action that changes remote state requires a final confirmation immediately before execution. Remote-changing actions include commit/push, posting or editing a reply, and resolving a thread.

Use `shared/github-ops/comment-resolve.sh <pr> <thread-id> <reply-message> <comment-database-id> [--no-resolve]` when posting a new reply. The helper requires all four positional arguments; if the reply POST fails it exits before resolving, leaving the thread open.

If a suitable response is already present, do not post a duplicate. Use `shared/github-ops/comment-resolve.sh <pr> <thread-id> "<existing response summary>" - --already-replied` after explicit confirmation; this records the existing response and resolves only. Do not bypass the helper with direct GraphQL calls.

## Outdated threads

`is_outdated: true` means the diff hunk moved or a newer push changed the code. It does not mean the thread is resolved. `shared/github-ops/comment-check.sh` fetches all review-thread pages and returns only unresolved threads. If a thread is absent from that unresolved snapshot, treat it as resolved and take no action. For threads still present and unresolved, compare against the prior snapshot; if an approved action addressed it, still reply/resolve it even when outdated.

Do not call an outdated thread stale only because the head SHA changed after an approved push. Stale means new reviewer replies appeared after the snapshot or the thread state conflicts with the planned action; re-evaluate before acting.

## Identifier details

`comment-database-id` is the REST numeric id of the original comment: field `database_id` on each comment object returned by `shared/github-ops/comment-check.sh`. It is distinct from the GraphQL `thread_id` (`PRRT_...`). Capture both from the same `shared/github-ops/comment-check.sh` fetch.

## Reply shape

Default shape: two sentences, action first, brief second.

- Fixed: `"Fixed in <sha>. <one-line what changed>."`
- Rejected: `"Not applied — <one-line reason citing doc or commit>."`
- Deferred: `"Deferred — <where the follow-up is logged: commit sha, ticket, or note>."` Use this when the issue is real but a human decision has postponed it; the deferral is typically logged in a follow-up fix commit message or an issue tracker.

Longer replies are allowed when needed, but short is the default.

Default resolution rule:
- Resolve automatically after reply when an applied fix was successfully committed and pushed
- Otherwise resolve after reply only when the user explicitly wants the thread closed and the issue is immediately settled
- Leave unresolved for `doc-drift`, deferred human follow-up, stale threads that need re-evaluation, or any user instruction to leave the thread open
