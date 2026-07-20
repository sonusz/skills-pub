# Vendors Troubleshooting

This module is shared infrastructure for other skills. Keep vendor-specific
debugging knowledge here so each caller can use the same interface instead of
rediscovering CLI quirks.

## First Checks

Start with the cheapest check that answers the question you actually have:

- `scripts/smoke-test.sh` uses fake CLIs. It validates the shell contract,
  output files, fan-out behavior, and fake prompt transport. It does not prove
  real vendor auth, model names, or network access.
- `scripts/doctor.sh` makes tiny real calls through `scripts/call.sh`. It is
  the normal readiness check for installed CLIs, auth, model config, network
  access, and headless launch behavior.
- `scripts/hello-test.sh` makes real calls and accepts any non-empty,
  non-error output. It is useful when the question is whether each vendor can
  answer at all.
- `scripts/nested-test.sh --run-real-nested` asks outer LLMs to execute inner
  vendor calls. This spends multiple real calls and grants command-execution
  permissions; use it only when debugging nested-agent workflows.

`doctor.sh` and the real tests write the same unified output contract as normal
runtime calls:

- `<output-dir>/<id>/out`
- `<output-dir>/<id>/status`
- `<output-dir>/<id>/log`
- `<output-dir>/<id>/stream`
- `<output-dir>/<id>/usage.json`

For `doctor.sh`, failed probes are kept automatically. Use `--keep-output` or
`--output-dir DIR` when you want to inspect successful probes too.

## Output Contract

All callers should inspect `status` first. The `exit_code` field is the
machine-readable success signal; `out` is the model response; `stream` is the
live transcript/progress file for idle detection; `log` is the launcher or CLI
stderr/stdout capture; `usage.json` is normalized token usage when the vendor
CLI exposes it. A `log` file can be empty on success.

Use `usage.json.available` and `usage.json.total_tokens` for programmatic token
accounting. Vendor-specific usage payloads are retained under `raw` for
diagnostics; callers should not depend on `raw` for normal workflow logic.

`call.sh` does not substitute a failed vendor. Use `--min-success` to decide how
many selected vendors must succeed, then let the upper-layer skill decide how to
handle partial results.

Use `--timeout SECONDS` for every workflow that can be run unattended. If one
vendor hangs, the wrapper can mark that vendor failed while the remaining
outputs stay available.

## Prompt Transport

Callers may provide prompts inline (`--prompt` or positional text), through
stdin, or with `--prompt-file`. The module normalizes every form into a
temporary prompt file so all selected vendors receive identical bytes.
Use repeated `--context-file FILE` when the prompt refers to local artifacts
that every vendor must inspect. The wrapper inlines those files with
path/hash/size boundaries, avoiding vendor-specific file-access differences.

The per-vendor transport is deliberately different:

- Codex/OpenAI reads the normalized prompt from stdin and writes the final
  message with `--output-last-message`.
- Claude runs in print mode and reads the prompt from stdin.
- Agy receives `--print <prompt>` and runs with stdin redirected from
  `/dev/null`.
- Cursor receives the prompt as a positional argument after `--`, runs with
  `cursor-agent -p --trust --output-format stream-json`, and has stdin
  redirected from `/dev/null`. `--trust` is required because every fresh cwd
  otherwise blocks on a workspace-trust prompt with no headless answer. The
  launcher parses the terminal `result` event for the assistant text and the
  `usage` payload.

Do not pass prompts through raw native args unless you are intentionally
debugging a vendor CLI. That bypasses the stable module contract.

## Known Failure Signatures

### Nested Permission Model

Nested calls do not inherit model permissions from the outer vendor. The outer
vendor only needs enough command-execution permission to run `call.sh`; the
inner vendor calls use the local machine's installed CLIs, auth state, PATH,
environment, and the arguments passed to the inner `call.sh`.

Give each nested layer its own `--output-dir` subtree and set `--timeout` at
every layer. Two layers is the normal practical limit for workflows; deeper
trees multiply cost, latency, and failure probability quickly.

### Claude Says No Input Was Provided

Symptom:

```text
Input must be provided either through stdin or as a prompt argument when using --print
```

Cause: Claude's native `--tools` option is variadic. Passing `--tools ""`
followed by a positional prompt can cause the prompt to be consumed as part of
the tools list.

Fix: the shared launcher keeps Claude prompt delivery on stdin. If a caller
needs Claude tool controls, pass native args explicitly, for example
`--native-arg --allowedTools --native-arg Read,Glob,Grep,LS`. Avoid Claude's
variadic `--tools` unless you have tested the exact argv shape.

### Agy Fails In Headless Runs

Cause: Agy may need a one-time interactive sign-in, or a requested model name
may not match one of the names returned by `agy models`.

Fix: run `agy` once in a normal shell to sign in, check available model names
with `agy models`, then run `doctor.sh --vendor agy`. The launcher deliberately
omits `--model` when `agy.model` is empty so agy's configured default is used.

### Cursor Reports `Not authenticated` Or Hangs On First Probe

Cause: `cursor-agent` requires either an active local login or
`CURSOR_API_KEY`. A fresh machine with neither will fail the doctor probe with
an authentication error in `<output-dir>/cursor/out`; an expired interactive
session can occasionally hang while cursor-agent attempts to refresh.

Fix: run `cursor-agent login` once in a normal shell (browser-based OAuth) and
re-run the doctor. For headless / CI environments, export
`CURSOR_API_KEY=cursor_…` before invoking the wrapper. The shared launcher
deliberately does not push credentials through `--api-key` per call so each
machine's auth state stays out of source.

### Cursor Model ID Was Renamed Or Removed

Cause: Cursor periodically rotates model ids (a `composer-3-fast` may replace
`composer-2-fast`, a `claude-4.7-…` may replace `claude-4.6-…`, etc.). When the
pinned `cursor.model=` no longer resolves on the account, `cursor-agent` returns
a model-not-found error in `<output-dir>/cursor/out`.

Fix: run `cursor-agent --list-models` to see what the account currently
exposes, update `cursor.model=` in `vendors.conf` to a current id, and re-run
the doctor. `cursor.model=auto` lets the server pick if you would rather not
pin; you trade a stable id in logs for resilience to renames.

### Codex Succeeds But `out` Is Empty

Cause: Codex may emit a transcript without writing the
`--output-last-message` file.

Fix: the launcher falls back to the transcript and prefixes the output with
`[FALLBACK: ...]`. Upper-layer skills may treat that as degraded output instead
of full success when response quality matters.

### Nested OpenAI Does Not Run The Inner Command

Cause: ordinary autonomous mode was not enough for the outer Codex agent to
execute the nested command in this environment.

Fix: nested real tests pass `--yolo` to the outer call. The shared launcher maps
that to Codex approval bypass, Claude `bypassPermissions`, Agy
`--dangerously-skip-permissions`, or Cursor `--yolo`. This is only appropriate
for explicit nested command-execution tests, not routine vendor calls.

### Smoke Passes But Doctor Fails

The shell contract is probably intact. Check CLI installation, `vendors.conf`
model names, auth state, proxy/SSL environment, and vendor service/network
availability. Keep machine-specific fixes in user-level shell/vendor/Codex
configuration, not in this module.

## Panel Review Notes

`panel-review` is a caller of this module. Its `vendors.yaml` defines three
panel calls and one synthesis call. `panel-review/scripts/doctor.sh` probes all
four configured calls through `vendors/scripts/call.sh` and requires at least
two ready panel calls plus a ready synthesis call.

Panel-review should not duplicate vendor CLI flags or workarounds. Add new
vendor transport fixes here, then let panel-review keep its config focused on
which calls it wants to make.
