# github-ops — GitHub without `gh`

Every script here talks to the GitHub REST API with the personal access
token that **git already holds**. Nothing is logged in separately, and no
token is typed, pasted or exported.

## How a new issue is created with only git

`git` itself cannot create an issue: issues are not objects in the
repository. What git does have is the credential it pushes with. The
credential helper answers

```sh
printf 'protocol=https\nhost=github.com\n' | git credential fill
```

with a `password=<PAT>` line, and that PAT — if it carries the Issues
permission on the repository — is enough for the REST API:

```sh
POST https://api.github.com/repos/<owner>/<repo>/issues
{"title": "...", "body": "...", "labels": ["bug"]}
```

`github-remote.sh` is the shared implementation of exactly that, with the two
rules that matter:

1. **the token never appears on a command line** — curl reads the
   `Authorization` header from `--config <(printf ...)`, so `ps` and
   `/proc/*/cmdline` never carry it (`_auth_curl`);
2. **the token is never printed** — it lives for one process in a 0600
   temp file (`_init_github_auth`), and `_cleanup_github_auth` removes it on
   exit.

Owner and repository come from the checkout's `origin` remote, so run the
scripts from inside the repository you mean.

## The verbs

| I want to | Run |
|---|---|
| create ONE issue from a file (`title` on line 1, body after) | `issue.sh create <file> [label,label]` → prints `<number> <url>` |
| create MANY issues from one markdown file (`## ` per issue), idempotent by title | `create-issues.sh <file> [--label L] [--dry-run]` |
| read an issue (title, labels, state, body) | `issue.sh get <n>` |
| read its comments | `issue.sh comments <n>` |
| post a comment from a file | `issue.sh comment <n> <file>` |
| replace the body | `issue.sh body <n> <file>` |
| rewrite one bold-marker line in the body, e.g. `**修复 commit**` | `issue.sh setline <n> "**修复 commit**" "\`abc1234\`"` |
| close (completed / not_planned) | `issue.sh close <n> [not_planned]` |
| list issues | `issue.sh list [open\|closed\|all]` |
| another repository than the checkout's | prefix any verb with `--repo owner/repo` |

Write the body in a file and pass the file — never inline JSON on the command
line: quoting breaks on backticks and Chinese punctuation, and a file is
what you can review before it goes out.

## Minimal example

```sh
cat > /tmp/issue.md <<'EOF2'
staging 的镜像仓库有 68 个镜像,超过审计上限 50
**类型** ops · **载体** `terraform` · **修复 commit** 无(未开始)

Every image publish pushes a new tag and nothing prunes old ones …
EOF2
cd ~/git/your-repo
~/git/skills/shared/github-ops/issue.sh create /tmp/issue.md ops
# 34 https://github.com/<owner>/<repo>/issues/34
```

## When it refuses

| Message | Meaning |
|---|---|
| `no GitHub token in the git credential helper` | `git credential fill` returned nothing for github.com: push once over HTTPS so the helper stores the PAT, or store it with `git credential approve`. |
| HTTP 404 on POST | the PAT lacks the Issues (write) permission for this repository, or the remote is not the repository you think — GitHub answers 404, not 403, for both. |
| HTTP 422 | validation: an unknown label (create it first — `create-issues.sh` does), or an empty title. |
| HTTP 403 with `rate limit` | wait; the limit is per token. |

`doctor.sh` checks the token, the remote and the permissions in one go.
