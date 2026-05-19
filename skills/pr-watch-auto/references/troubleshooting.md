# pr-watch-auto — Troubleshooting

Read this when scripts fail to run or CI monitoring behaves unexpectedly. Not needed during normal operation.

## Environment diagnostic

If scripts aren't working, run the diagnostic first — it checks git remote, credential helper, PAT permissions, repo-scoped API access, and required deps (curl, jq, git, auto-fix skill presence):

```bash
bash scripts/doctor.sh
```

## Common failure modes

**Auth error.** Test the credential helper directly:
```bash
printf "protocol=https\nhost=github.com\n" | git credential fill
```

**Identity endpoint warning.** Fine-grained repo-scoped PATs may return 401/403 for `GET /user` while PR and Actions APIs still work. Treat repo-specific PR/Actions checks as authoritative.

**Stuck at pending forever.** The PAT likely lacks `Actions: Read`. Check repo → Settings → Fine-grained tokens → Repository permissions → Actions: Read.

**No workflow runs found.** The PR must exist and have been pushed — the script queries the Actions Workflow Runs API by head SHA.

**Comment check fails.** The PAT needs `Pull requests: Read`. The GraphQL API also requires the token to have access to the repository.

**Previously selected thread disappears.** `shared/github-ops/comment-check.sh` fetches all review-thread pages before filtering to unresolved threads. If a previously selected thread is absent, treat it as resolved and take no action.

**Cannot resolve threads.** Resolving review threads requires `Pull requests: Write` (for GraphQL `resolveReviewThread` mutation and REST comment reply).

## Retry bounds

When unlimited CI retries are configured, still stop after 10 consecutive identical failures (same error, same failing step) or 2 hours elapsed, whichever comes first.

## Stateful-gate refusals

Both `shared/github-ops/comment-resolve.sh` and `scripts/push-with-snapshot.sh` enforce the stateful-gate invariant: the user's confirmation is bound to a specific state snapshot, and the scripts refuse if that state has drifted by the time the action would land. Refusals are not bugs — they're the gate doing its job. Handle them by re-prompting the user with the diff, never by silently re-fetching and retrying.

| Script | Exit | Meaning | Recovery |
|---|---|---|---|
| `shared/github-ops/comment-resolve.sh` | 4 | Thread had new comments before our reply was posted; nothing was sent. | Re-fetch via `shared/github-ops/comment-check.sh`, show the user the new comments, re-confirm, retry with the new snapshot. |
| `shared/github-ops/comment-resolve.sh` | 5 | Reply posted, but a third comment arrived before we could resolve. Reply is visible; thread stays open. | Re-fetch, show the user the new comment, ask whether to resolve anyway or to re-evaluate. |
| `scripts/push-with-snapshot.sh` | 4 | Remote head moved since the gate was confirmed (someone else pushed). No push attempted. | Show the user the new remote head and any new commits between `prior` and `current`, re-confirm whether to proceed, retry with the new `--prior-head-sha`. |

## Token-handling internals

Read this when auditing the security posture or debugging auth-related issues.

- `_init_github_auth()` retrieves the PAT inside a `bash -c` subshell and writes it to a temp env file created via `mktemp "${TMPDIR:-/tmp}/.pr-watch-env.XXXXXX"` (randomized filename, mode `0600`). The value never appears in tool output or stdout. The cleanup `EXIT` trap is registered immediately after `mktemp`, before any other work, so a kill between mktemp and a caller's later trap registration cannot leak the file.
- The token is written as plain `key=value` data, not as a quoted shell expression. `_source_github_auth()` loads it via `read` and a `${var#prefix}` strip, never via `source`, so a token containing `"`, `$`, `` ` ``, or `\` cannot execute as shell. PATs are alphanumeric today; this is defense in depth.
- `_auth_curl()` passes the `Authorization` header via `curl --config` with process substitution — the token never appears in `ps` or `/proc/PID/cmdline`. Both load and curl wrappers temporarily disable `xtrace` around the sensitive line so `bash -x` cannot print the value.
- Callers may still register `trap _cleanup_github_auth EXIT` themselves; re-registering the same handler is a no-op.

## State files

Temp state files (`/tmp/ci-state-*.json`, `/tmp/comment-state-*.json`) contain branch names, error logs, and comment text — never tokens. They are created with `umask 077` (owner-only) and cleaned up by the script that created them.

`/tmp/ci-needs-human-<pr>.md` is different: it's an **intentionally-persistent escalation receipt** written by the orchestrator agent (not a script) when the user declines a CI-fix gate or `auto-fix` returns `escalated`. The skill never deletes it; the user reads it after the run and removes it manually once the failure has been addressed. The path is per-PR so concurrent runs don't collide. May contain CI build-error excerpts; do not commit it to a repo.
