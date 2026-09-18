# pr-watch-auto

An agent-driven skill for the phase after a pull request is pushed. It runs one
interleaved loop that watches the PR's GitHub Actions CI and its review comments
together: CI failures are triaged by the `auto-fix` skill and retried up to N
attempts (default 3), each unresolved review thread is evaluated by `auto-fix`,
approved fixes are squash-committed and pushed, and threads are replied to and
resolved or escalated back to you. Every remote write (push, reply, resolve) is
bound to a state snapshot and refused by the scripts if the PR moved in between.

## When to use it

- A PR exists on GitHub, the branch is pushed, and you want the agent to follow
  CI and reviewer feedback to completion instead of checking back by hand.
- The repository keeps design intent under `docs/features/`, which `auto-fix`
  reads before deciding whether a fix is safe to apply.
- You are willing to confirm each CI-fix attempt and each thread action; the
  loop is autonomous about watching and triage, not about consent.

It activates only on explicit opt-in. The trigger phrases in `SKILL.md` are
`pr-watch-auto`, `auto-fix ci`, `watch ci auto`, and `auto watch comments`.

## When not to use it

- The PR has not been pushed yet, or the remote is not GitHub. The scripts
  resolve owner and repo from the git remote and query the Actions and Pull
  Requests APIs by head SHA.
- You want a one-off code review rather than a watch loop. Use
  `../pr-review/` for that.
- You want a fix applied without a docs check. `auto-fix` escalates when it
  finds no design-intent evidence; this skill does not edit code around an
  escalation.
- Feedback that is not anchored to a file (a top-level CHANGES_REQUESTED
  review, a "rename the branch" comment) cannot be auto-fixed. The skill
  surfaces it and waits for your instruction.

## Requirements

- Linux or macOS, bash, `git`, `curl`, `jq`. `scripts/doctor.sh` checks for
  the last three.
- A git credential helper that returns a GitHub token for `https://github.com`
  (`git credential fill`). The scripts do not use the `gh` CLI. The doctor's
  hint for macOS is `git config --global credential.helper osxkeychain`.
- A fine-grained PAT with repository permissions `Pull requests: Read`,
  `Actions: Read`, `Contents: Read`, and `Issues: Read` (issue comments).
  Replying to and resolving threads needs `Pull requests: Write`. Pushing uses
  plain `git push` through the same credential helper.
- The `auto-fix` skill installed as a sibling. `scripts/doctor.sh` warns
  when `skills/auto-fix/SKILL.md` is missing, and `SKILL.md` tells the agent
  to stop and ask you to install it.
- Run `bash scripts/doctor.sh` from inside the target repository; it also
  probes the identity, pull requests, and Actions endpoints with your token.

## Install

Skills are installed as symlinks so the relative links inside the skill keep
resolving. `REPO` is the path of your clone of this repository.

Claude Code:

```bash
REPO=/path/to/skills-pub
mkdir -p ~/.claude/skills
ln -s "$REPO/skills/pr-watch-auto" ~/.claude/skills/pr-watch-auto
ln -s "$REPO/skills/auto-fix"      ~/.claude/skills/auto-fix
```

Codex CLI:

```bash
REPO=/path/to/skills-pub
mkdir -p ~/.codex/skills
ln -s "$REPO/skills/pr-watch-auto" ~/.codex/skills/pr-watch-auto
ln -s "$REPO/skills/auto-fix"      ~/.codex/skills/auto-fix
```

Inside the skill, `shared/github-ops` and `shared/doctor` are relative links to
`../../../shared/...`, and `skills/auto-fix` is a relative link to
`../../auto-fix`. If you copy the directory instead of linking it, you must
materialize those three links yourself (and `auto-fix`'s own
`shared/secrets` link), otherwise the scripts and the doctor cannot find them.

## Usage

Say one of the opt-in phrases to the agent (`pr-watch-auto`, `auto-fix ci`,
`watch ci auto`, `auto watch comments`) and name the PR number or branch. With
no target the scripts default to the current branch.

Modes described in `SKILL.md`:

- `alert` (default): CI failures are reported, not fixed.
- `fix`: CI failures go through `auto-fix`, up to N attempts, default 3.
- `--no-watch-comments`: disables the comment phase, which is otherwise on.

Opting into `fix` is session-level consent only. Before each CI-fix attempt is
applied and pushed, the agent shows the issue, the proposed fix, and the remote
head SHA it is bound to, and asks you to confirm. For each review thread you
choose `apply fix`, `response with reject reason`, `customize response`, or
`resolve`; threads you do not mention are left untouched.

The loop ends when CI has passed, no inline threads are unresolved, and no
change request is active. It also exits when CI fails in `alert` mode, when
`fix` attempts are exhausted, or when you decline a fix. With unlimited
retries configured, `references/troubleshooting.md` still stops after 10
identical failures or 2 hours.

Escalated back to you:

- `/tmp/ci-needs-human-<pr>.md`, written when you decline a CI fix or
  `auto-fix` escalates. It holds the triage or conflict report and is never
  deleted by the skill.
- Top-level change requests and new PR comments, printed with author and body
  and marked "no code anchor". The skill never tries to clear a
  CHANGES_REQUESTED review; only the reviewer can.
- Snapshot refusals: a push or thread action that finds the PR changed shows
  you the new commits or comments and asks again.

At the end a summary table is printed and `/tmp/comment-fix-result-<pr-number>.json`
is written in the shape given by `references/result-schema.md`.

## How it works

The agent composes one-shot scripts; only `wait-for-state.sh` blocks.

1. Preflight: clean working tree, branch matches the PR head, remote matches
   the PR repo. Repeated on every CI retry.
2. Snapshot: `ci-check.sh` gives `passed | failed | pending | error` plus the
   failing jobs and error log; `comment-check.sh` lists unresolved inline
   threads with each comment's `database_id`; `pr-review-check.sh` lists
   active change requests, review bodies, and issue comments.
3. CI failure in `fix` mode: affected files are taken only from paths named in
   the error log. `auto-fix` runs in `evaluate` mode, you confirm, then it runs
   in `apply` mode with the evaluate hash so the applied change must match what
   you saw. Failing CI preempts comment work.
4. Push: `scripts/push-with-snapshot.sh --prior-head-sha <sha>` compares the
   remote head against the SHA captured at confirmation time and pushes only
   if they match; exit 4 means someone else pushed and you are re-prompted.
5. Review threads: `auto-fix` evaluates each thread from its comment text,
   author type (bot or human), and file path. Approved fixes are applied per
   thread, squashed into one commit, tested locally, then pushed.
6. Reply and resolve: `shared/github-ops/comment-resolve.sh` verifies the
   thread still matches `--prior-comment-ids`, posts the reply, verifies
   again, then resolves. Exit 4 means the thread changed before the reply,
   exit 5 after it. A thread is never resolved without a reply.
7. Idle: `wait-for-state.sh` blocks until CI status or the unresolved count
   changes, or `--max-wait` (default 1200 s) elapses. New top-level reviews
   are only noticed on the next snapshot.
8. Re-snapshot after every push or comment action, then loop.

Never run the scripts with `bash -x`; token handling is described in
`references/troubleshooting.md`.

## Files

| Path | Purpose |
|---|---|
| `SKILL.md` | The agent's instructions: loop, gates, guardrails |
| `scripts/doctor.sh` | Runs `shared/github-ops/doctor.sh`, then checks the `auto-fix` sibling |
| `scripts/push-with-snapshot.sh` | Push only if the remote head still equals `--prior-head-sha` |
| `references/comment-analysis-template.md` | Per-thread triage block format |
| `references/comment-replies.md` | Reply shapes and the reply-before-resolve rule |
| `references/result-schema.md` | Summary table and result JSON shape |
| `references/troubleshooting.md` | Doctor, PAT permissions, exit codes, state files |
| `shared/github-ops/` | Link to the GitHub primitives: `ci-check.sh`, `comment-check.sh`, `pr-review-check.sh`, `wait-for-state.sh`, `comment-resolve.sh`, `github-remote.sh`, `doctor.sh` |
| `shared/doctor/` | Link to `doctor-lib.sh`, the shared doctor output format |
| `skills/auto-fix/` | Link to the sibling skill that owns every fix-or-escalate decision (`../auto-fix/`) |
