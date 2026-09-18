# pr-review

A two-phase review of a code change. Phase 1 (docs conformance) checks whether the code delivers what the plan, PRD, spec or design documents changed in the same diff require. Phase 2 (bug hunt) looks for bugs in the implementation, with spec conformance explicitly out of scope so the two phases do not overlap. Both phases are delegated to the sibling `panel-review` skill, which runs one prompt through several model vendors and returns a synthesized report. The input is either a diff between two local branches (local mode) or an open GitHub pull request (GitHub mode); in GitHub mode the findings can also be posted as Copilot-style inline review threads.

## When to use it

- You want a code review, a sanity check on a diff, a spec-conformance check, or want to verify a PR before merge. The skill triggers on these requests even when the word "review" is not used.
- You are about to open a PR and want a check first: local mode, `--branches <base> <head>`.
- You are reviewing an existing PR on GitHub: GitHub mode, `--pr <number>`.
- A colleague pushed a branch without a PR: fetch it, then local mode against `main`.

## When not to use it

- Typo fixes, formatting-only diffs, or one-line bug fixes. The two-phase process costs more than it returns on those.
- Applying fixes. The skill is review-only; fixes belong to the author or to `../auto-fix/`.
- CI monitoring. That is `../pr-watch-auto/`.
- Replying to or resolving existing PR threads. The skill only posts new threads.

## Requirements

- bash, git, Linux or macOS. The shared doctor library is written for macOS bash 3.2; install hints cover `brew` and `apt`.
- `jq` (both modes) and `curl` (GitHub mode). `scripts/doctor.sh` checks for them.
- `perl` for the secret scan. Without it, `gather-context.sh` still runs but marks the diff as unscanned.
- The `panel-review` skill installed next to `pr-review`. `scripts/doctor.sh` looks for `../panel-review/SKILL.md` relative to the skill directory. `pr-review`'s own scripts never call a vendor CLI; `panel-review` does, using the vendors configured in its `vendors.yaml` (or `sample-vendors.yaml` when no local file exists). At least two working vendors are needed for a panel. See `../panel-review/` and `../../shared/vendors/README.md`.
- GitHub mode only: the reviewed repository has a `github.com` remote, and `git credential fill` for `github.com` returns a token, that is, a fine-grained personal access token stored in your git credential helper. The scripts do not use the `gh` CLI, and `SKILL.md` tells the agent not to either, because `gh` has its own auth scope that can mask permission failures until they surface mid-post. The shared GitHub doctor probes the Pull requests and Actions APIs and lists `Pull requests: Read`, `Actions: Read` and `Contents: Read` as the permissions to grant; posting inline threads additionally needs `Pull requests: Write`.
- A clean working tree. The doctor warns rather than fails, but uncommitted edits can leak into verification reads.

## Install

Clone the repository and symlink the skill directory. Install `panel-review` the same way, since `pr-review` depends on it as a sibling.

Claude Code:

```bash
REPO=/path/to/skills-pub
mkdir -p ~/.claude/skills
ln -s "$REPO/skills/pr-review"    ~/.claude/skills/pr-review
ln -s "$REPO/skills/panel-review" ~/.claude/skills/panel-review
```

Codex CLI:

```bash
REPO=/path/to/skills-pub
mkdir -p ~/.codex/skills
ln -s "$REPO/skills/pr-review"    ~/.codex/skills/pr-review
ln -s "$REPO/skills/panel-review" ~/.codex/skills/panel-review
```

Link, do not copy. `skills/pr-review/shared/{doctor,github-ops,secrets,vendors}` are relative symlinks to `../../../shared/...`, which resolve inside the clone when the skill directory itself is a symlink. If you copy the directory instead, those links dangle; you would have to replace each one with a copy of its target from `REPO/shared/`.

Verify from inside a repository you want to review:

```bash
cd /path/to/your/repo
bash ~/.claude/skills/pr-review/scripts/doctor.sh
```

## Usage

Trigger phrases from the skill description: "review this PR", "review my branch", "look at PR #N", "does this match the spec", "check this diff", "audit my changes", "pre-PR check", or `/pr-review`.

You supply the target: two branch names for local mode (both must be fetched locally), or a PR number for GitHub mode. Local mode never touches the network. GitHub mode detects owner and repo from the git remote and fetches the PR's base and head refs without checking them out.

The agent first states its plan (mode, target, which phases will run, which vendors `panel-review` will call) and waits for one go/no-go. After that, context gathering, Phase 1 and Phase 2 run back to back without further prompts. The only later gate is posting inline threads, which is opt-in and requires a second confirmation of the specific drafts.

The output is a report. Phase 1 quotes the panel synthesis's `## Consensus` and `## Divergence` sections verbatim and adds a per-requirement verdict (yes / partial / no / unclear). Phase 2 lists bugs by severity (CRITICAL, HIGH, MEDIUM, LOW) with a verification status; the agent spot-checks the top findings against `git show <head_sha>:<path>` before presenting them. In GitHub mode each finding is also classified as new, overlapping an open thread, or overlapping a thinly resolved thread. The summary lists skipped file categories and recommended next steps. Inline threads, when requested, follow `references/comment-style-guide.md` (behavior statement, mechanism, consequence, suggested fix; no reference to the panel or any automated origin) and the comment URL is printed for each one posted.

The scripts can be run directly. Run them from inside the reviewed repository; they locate it with `git rev-parse --show-toplevel`.

```bash
SKILL=~/.claude/skills/pr-review
RUN_DIR=$(mktemp -d /tmp/pr-review.XXXXXX)

bash "$SKILL/scripts/doctor.sh"

# Local mode
bash "$SKILL/scripts/gather-context.sh" --branches main feature/foo --out "$RUN_DIR"
# GitHub mode
bash "$SKILL/scripts/gather-context.sh" --pr 21 --out "$RUN_DIR"

# GitHub mode: every review thread on the PR, resolved and unresolved
bash "$SKILL/shared/github-ops/comment-check.sh" 21 --include-resolved > "$RUN_DIR/all-threads.json"

# GitHub mode: post one inline thread on a single line or a line range
bash "$SKILL/scripts/post-review-thread.sh" --pr 21 --path src/queue.go \
  --body-file "$RUN_DIR/thread-1.md" --line 42
bash "$SKILL/scripts/post-review-thread.sh" --pr 21 --path src/queue.go \
  --body-file "$RUN_DIR/thread-1.md" --start-line 40 --end-line 48 [--commit <sha>]
```

`gather-context.sh` writes into `--out`: `mode`, `pr_number` (GitHub mode), `head_sha`, `merge_base_sha`, `repo_root`, `base_ref`, `head_ref`, `diff.patch`, `diff-stat.txt`, `files.txt` (every added or modified file, unfiltered), `secrets.txt` and `summary.json`. The diff is taken from the merge base so it reflects what the change adds, not unrelated commits on the base branch.

`post-review-thread.sh` re-fetches the PR head SHA immediately before posting unless `--commit` is given, prints a JSON line `{id, html_url, path, line, start_line, commit_id}` on success, and exits 1 on usage error, 2 on auth or network failure, 3 when GitHub rejects the comment (for example a line outside the diff).

## How it works

The scripts are deliberately thin. They fetch state, talk to the GitHub REST API, and produce a bundle of paths and metadata. All classification is the agent's job: it reads `files.txt` and the diff and decides which files are anchor docs, implementation, tests, meta docs (README, CHANGELOG), binaries or generated artifacts, or CI and build config. Skipped categories are named in the summary.

"Docs" here means anchor docs: a deliverable specification the code is supposed to fulfil. Typical signals are a path under `docs/`, a basename or path containing `plan`, `prd`, `spec`, `design`, `requirements`, `proposal`, `rfc`, an ADR, or prose-style requirements added by the diff. README, CHANGELOG and CONTRIBUTING describe state or process and usually do not count. When in doubt the agent reads the file.

Phase 1 runs only if the diff contains at least one anchor doc; otherwise it is skipped and the user is told. The agent builds `phase1-prompt.txt` from the docs-compliance skeleton in `references/prompt-templates.md` and invokes `panel-review` with the repo root as `--cwd`. Reviewers read the anchor docs and the implementation files themselves at the head SHA and classify each requirement as satisfied, partial, missing or unclear, ending with a gaps list.

Phase 2 always runs. The agent curates the implementation paths (partitioning by subsystem if the surface is too broad, never truncating files), builds `phase2-prompt.txt` from the bug-hunt skeleton, and invokes `panel-review` again. The template asks for bugs only, in a fixed format with location, severity, reproduction, impact and fix direction, and excludes style nits and spec compliance. Findings are verified against `git show <head_sha>:<path>`, never a working-tree copy, and downgraded or dropped when they do not match the code.

In GitHub mode the agent then loads every review thread with `comment-check.sh --include-resolved` and compares each finding by file and code region. A finding with no related thread is eligible for posting; one covered by an open thread is skipped and noted; one covered by a substantively resolved thread is skipped; one covered by a thinly resolved thread (silent resolve, emoji, "thanks") is still posted as a new thread with a local warning. Resolved threads are never reopened.

Both prompts are path manifests. They contain the review question, the repo root, the merge-base and head SHAs, the paths of `diff.patch` and `files.txt`, and the selected file paths. Doc text, source, excerpts and diff hunks are never pasted in. Because reviewers read the manifested files with repository access, a secret in a listed file would reach every vendor. `gather-context.sh` therefore runs `shared/secrets/scan.sh --diff` over the added lines of `diff.patch` and writes `secrets.txt`, one `path:line:pattern` per hit and never the value; `summary.json` records `secret_scan` as `clean`, `hits`, `failed` (for example perl missing) or `missing`. The agent reads `secrets.txt` before building any prompt and, for each listed file, either excludes it from both manifests (saying so in the report) or redacts a copy under a temporary audit root, or stops and asks. The denylist is high-confidence but narrow, so a clean scan does not license manifesting `.env` files, key material or credential stores. GitHub tokens are read from the credential helper into a `mktemp` file with mode 0600 and never printed; `references/troubleshooting.md` warns never to run these scripts with `bash -x` because of variable expansion.

## Files

| Path | Purpose |
|---|---|
| `SKILL.md` | The skill: modes, the seven-step workflow (steps 0 to 6), approval gates, guardrails and out-of-scope list |
| `scripts/doctor.sh` | Runs the shared GitHub doctor, then checks for the `panel-review` sibling, `jq`, `perl`, the secrets module and a clean working tree |
| `scripts/gather-context.sh` | Builds the review bundle from two local branches or a PR: diff, stat, file list, secret scan, `summary.json` |
| `scripts/post-review-thread.sh` | Posts one inline review comment on a PR line or line range via the REST API, re-fetching the head SHA by default |
| `references/prompt-templates.md` | Phase 1 (docs-compliance) and Phase 2 (bug-hunt) prompt skeletons plus rules common to both |
| `references/comment-style-guide.md` | Copilot-style inline comment structure, tone, single- vs multi-line targeting, cross-reference rules, examples |
| `references/troubleshooting.md` | Failure modes: missing sibling skill, unfetched refs, panel preflight, hallucinated findings, out-of-diff comments, auth temp file, `bash -x` |
| `shared/doctor` | Symlink to `../../../shared/doctor`: output helpers used by `doctor.sh` |
| `shared/github-ops` | Symlink to `../../../shared/github-ops`: remote detection, credential-helper auth, `comment-check.sh`, GitHub doctor |
| `shared/secrets` | Symlink to `../../../shared/secrets`: `scan.sh` denylist scanner used by `gather-context.sh` |
| `shared/vendors` | Symlink to `../../../shared/vendors`: vendor CLI adapters, used indirectly through `panel-review` |
