# Secrets module

`shared/secrets` is a reusable module for other skills, not a standalone skill.
It holds one denylist of high-confidence secret shapes (`patterns.pl`) and two
commands that apply it:

| Command | Contract |
|---|---|
| `redact.sh` | stdin -> stdout, every recognised secret replaced by `<redacted>` (prefix kept where it helps: `gh<redacted>`, `AKIA<redacted>`, `sk-ant-<redacted>`). Line by line. Exit 1 with a message if `perl` is missing. |
| `scan.sh [FILE...]` | Prints one `path:line:pattern-name` per hit, never the matched text; a path that is itself secret-shaped (a file named after a token) is printed redacted. No args or `-` reads stdin (path `-`). Binary files are skipped. Exit 0 clean, 1 on any hit, 2 on usage error / missing perl / unreadable input. |
| `scan.sh --diff [FILE...]` | Same, for unified diffs: only added lines are checked, `path` comes from the `+++ b/` header and `line` is the new-file line number. |
| `doctor.sh` | perl present, `patterns.pl` loads, and a self-test with synthetic tokens (redacted, reported with exit 1, clean line exits 0), the PEM block cases (same-line, header-only mention, real block, unterminated block) and a secret-shaped file name. |

```bash
echo "$build_log" | shared/secrets/redact.sh
shared/secrets/scan.sh src/config.py deploy/values.yaml   # 1 if anything matched
git diff main...HEAD | shared/secrets/scan.sh --diff       # added lines only
```

Only `perl` is required (macOS ships it).

## Patterns

Defined in `patterns.pl` as `[ name, qr/regex/, replacement, (options) ]`; the
file evaluates to that list so both scripts share one source of truth. Order
matters for redaction: specific shapes first, the generic assignment rule last.

| name | matches |
|---|---|
| `aws-access-key` | `AKIA` + 16 uppercase alphanumerics |
| `aws-session-key` | `ASIA` + 16 uppercase alphanumerics |
| `github-pat` | `ghp_` / `gho_` / `ghu_` / `ghs_` / `ghr_` + 16 or more alphanumerics |
| `jwt` | three base64url segments starting with `eyJ` |
| `slack-token` | `xoxb-` / `xoxp-` / `xoxa-` / `xoxr-` / `xoxs-` tokens |
| `authorization-header` | `Authorization: <scheme> <value>` (case-insensitive) |
| `bearer-basic-token` | inline `Bearer <token>` / `Basic <token>` (16 or more token chars) |
| `uri-credentials` | `scheme://user:password@host` (only the password is replaced) |
| `private-key-block` | `-----BEGIN (RSA \| EC \| OPENSSH \| DSA \| ENCRYPTED \| PGP )?PRIVATE KEY( BLOCK)?-----`; redaction keeps both markers. A BEGIN...END pair on one line (GCP JSON, a `.env` one-liner with `\n`-escaped PEM) is redacted between the markers. A BEGIN marker alone at the end of its line (trailing quotes or a literal `\n` allowed, i.e. a real PEM header) opens a block: the body collapses to one `<redacted>` line until the END line, which is printed from the END marker onward, or for at most 128 body lines, after which normal mode resumes. A BEGIN marker followed by other text and no END (a log line quoting the header) is redacted to the end of that line only. scan reports the BEGIN line |
| `openai-anthropic-key` | `sk-`, `sk-proj-`, `sk-ant-` + 20 or more key chars |
| `google-api-key` | `AIza` + 35 key chars |
| `xai-key` | `xai-` + 20 or more alphanumerics |
| `gitlab-pat` | `glpat-` + 20 or more key chars |
| `secret-assignment` | `api_key` / `api-key` / `apikey` / `secret` / `secret_key` / `access_key` / `token` / `password` / `passwd` (any case, any prefix such as `client_secret` or `"access_token"`) followed by `=` or `:` and a value; the name, separator and quote are kept, the value becomes `<redacted>`. The value must be a quoted string of 8+ characters or a bare 8+ character token of `[A-Za-z0-9_/+=-]`, and must not start with `$` or `<`, so `token=${TOKEN}`, `token = os.environ["TOKEN"]`, `t = config.token` and `<your-token>` placeholders are not hits |

## Denylist, not guarantee

The list is deliberately narrow so that a hit is almost always real. The price
is recall: a secret in a novel format, a random string assigned to a variable
with an unlisted name, a base64 blob, a password that is an ordinary word, or a
token split across lines all pass through untouched. A clean `scan.sh` means
"nothing recognisable", not "no secrets". Callers still own judgement about
what they paste into prompts, tool results and logs, and should exclude whole
files they know to be sensitive (`.env`, key material, credential stores)
rather than rely on line-level matching.

Redaction runs line by line with exactly the semantics of the original
`skills/auto-fix/scripts/redact-secrets.sh`; `\s` never crosses a line.

## How skills use it

Skills link the module as `skills/<skill>/shared/secrets -> ../../../shared/secrets`.

- **auto-fix** filters evidence: any `evidence` quoted from `error_log` or
  fetched files is piped through `redact.sh` before it lands in the structured
  output. `skills/auto-fix/scripts/redact-secrets.sh` is now a thin wrapper
  around this module's `redact.sh`.
- **panel-review**, **pr-review**, **multi-lens-review** scan before launch.
  Panel prompts are path manifests and reviewers read the files themselves
  with repo access, so a secret inside a manifested file still reaches every
  vendor. Run `scan.sh <manifest paths...>` (or `scan.sh --diff < diff.patch`)
  before launching; on any hit either drop that file from the manifest (and say
  so in the report) or stop and ask. pr-review's `gather-context.sh` records
  the diff scan in `secrets.txt` automatically.
- **create-skill-secured** names `redact.sh` / `scan.sh` as the standard tool
  for its Output Sanitization checklist item.

Adding a pattern: append to `patterns.pl`, run `perl -c patterns.pl`, then
`bash doctor.sh`. Keep new entries high-confidence; a noisy pattern makes the
scan step in review flows untrustworthy.
