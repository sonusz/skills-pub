# Troubleshooting

Common failure modes when running the pr-review skill, in roughly the
order you'd hit them.

---

## `doctor.sh` reports `panel-review skill not found`

The skill delegates both review phases to `panel-review`. If it's missing,
install it from the same skills-dev repo. The path is hard-coded relative
to this skill: `../panel-review/SKILL.md`.

---

## `gather-context.sh` says base or head ref not found locally

You're in branches mode and one of the refs isn't fetched. Either:
```bash
git fetch origin <branch>
```
or, if the branch is local-only, push it first or pass `git rev-parse`-able
SHAs directly.

For PR mode, the script auto-fetches `origin/<base>` and the PR head. If it
still can't find the head SHA, the PR is from a private fork and your token
doesn't have access — switch to a token with `pull_requests: read` on the
fork or ask the author to mirror the branch.

---

## Panel-review preflight: "fewer than 2 successful panel calls"

One of:

- **Vendor CLI not installed / not on PATH.** Run
  `shared/vendors/scripts/doctor.sh`.
- **Vendor took longer than the call timeout** (default 5 min). Larger
  prompts → longer responses. Either trim the prompt (Phase 1: scope to
  files the doc names; Phase 2: split into one-file-per-round) or raise
  `PANEL_CALL_TIMEOUT` via env.
- **Auth drifted mid-session.** For Gemini, the most common cause is stdin
  not being redirected to `/dev/null`, causing the CLI to hang on an
  interactive auth prompt. Check the launcher script.

Don't lower the bar to 1 vendor — that defeats the purpose of panel review.

---

## Panel found bugs that don't actually exist

Models hallucinate, especially with line numbers. Always verify before
presenting findings to the user:

1. Read the cited file at the cited line.
2. If the bug doesn't match the code, downgrade or drop the finding.
3. If two vendors agree, confidence is higher — but agreement is not proof,
   they share training data.

The bug-hunt template asks vendors to lead with the worst bug; verify that
one first. If it's hallucinated, every other finding deserves the same
scrutiny.

---

## `post-review-thread.sh` returns "Pull request review thread for X is not part of the diff"

GitHub rejects comments on lines outside the PR diff. Causes:

- **Stale `--commit` SHA.** The PR has new commits since you started.
  Re-fetch the head SHA and retry. The skill is supposed to do this
  automatically right before posting; if you bypass that, you'll hit it.
- **Line refers to context (unchanged) lines** that aren't in the diff
  hunk. Pick a line that's actually added or modified.
- **Multi-line comment crosses hunk boundaries.** Tighten the range to
  fit inside one hunk.

---

## `post-review-thread.sh` returns "Validation failed"

GitHub didn't like the payload. Common causes:

- **`--start-line > --end-line`.** The script checks this; you shouldn't
  hit it.
- **`line` not numeric** — quoting issue in the caller.
- **Body too large.** GitHub limits comment body length (~65 KB). Split
  into multiple threads or move the long explanation to a doc/issue.

---

## I posted a duplicate thread

Recovery options:

1. **Delete the new comment** via the GitHub UI or
   `DELETE /repos/{owner}/{repo}/pulls/comments/{comment_id}`.
2. **Resolve the new thread** with a brief reply like "Already covered in
   the existing thread #NNN — closing".

Then add the affected file/region to the cross-reference checklist for the
next run, so the same blind spot doesn't repeat.

---

## Working tree was dirty when I started

The skill warns but doesn't stop. If you reach Phase 2 and realize the dirty
state contaminated your context (e.g. you have local edits that look like
bugs the panel flagged), do this:

1. Save the panel output (don't lose the run).
2. `git stash` your local changes.
3. Re-run `gather-context.sh` to confirm the diff matches the actual PR or
   branch range.
4. Re-verify the flagged bugs against the clean source.

In gh mode the input is a remote PR, so local dirty state can't contaminate
the diff itself — but it can contaminate "Read"-tool reads if the agent
falls back to reading the working tree instead of the PR head. Verify
critical findings against `git show <pr-head>:<path>` if in doubt.

---

## Auth helper "no such file or directory" on the temp env file

The auth helper writes a per-session token file that gets cleaned up on
shell exit. If you run two Bash blocks in sequence, each is a fresh shell
and the second one can't see the first's file.

**Fix:** put the entire flow (init + use + cleanup) into one script invocation:

```bash
cat > /tmp/op.sh <<'EOF'
SCRIPT_DIR=...
source "$SCRIPT_DIR/shared/github-ops/github-remote.sh"
_init_github_auth >/dev/null
_source_github_auth
trap _cleanup_github_auth EXIT
# ... do the API calls here ...
EOF
bash /tmp/op.sh
```

`post-review-thread.sh` and `gather-context.sh` already do this internally.
Hit this if you're calling helper functions directly.

---

## Never use `bash -x` on these scripts

Token leaks via variable expansion. Same rule as pr-watch-auto.

If you need debugging, add `echo` statements or use `set -x` *after*
`_init_github_auth` returns and *before* the auth-bearing variables are
referenced — and remove the trace before any output is captured.
