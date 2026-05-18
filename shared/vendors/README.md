# Vendors Module

This directory is a reusable module for other skills, not a standalone skill.
Do not add `SKILL.md` unless you intentionally want `vendors` to become a
directly triggerable skill.

The stable module entrypoints are:

- `scripts/call.sh` for one vendor call or one prompt fanned out to multiple
  vendors in parallel.
- `scripts/doctor.sh` for readiness checks and retained diagnostics on failure.
- `scripts/smoke-test.sh` for a fake-CLI regression test that makes one
  four-vendor fan-out call per caller (fake `codex`, `claude`, `gemini`, and
  `cursor-agent` binaries are generated under a temp dir).
- `scripts/hello-test.sh` for a real vendor call test that asks each selected
  LLM `Who are you?` and verifies non-error output.
- `scripts/nested-test.sh` for a real nested integration test where each outer
  vendor is asked to run a three-vendor inner `call.sh`.
- `vendors.conf` for default model mapping.
- `TROUBLESHOOTING.md` for reusable vendor and caller debugging notes.

## Call Interface

```bash
VENDORS=${VENDORS:-/tmp/skills/vendors}
"$VENDORS/scripts/call.sh" \
  --vendor openai \
  --effort max \
  --yolo \
  --prompt-file /tmp/task.txt \
  --output-dir /tmp/vendor-run
```

Vendor labels are case-insensitive. Use lowercase in configs and examples.

Supported vendors:

- `openai` maps to the local `codex` CLI.
- `claude` maps to the local `claude` CLI.
- `gemini` maps to the local `gemini` CLI.
- `cursor` maps to the local `cursor-agent` CLI.

Common arguments:

- `--vendor openai|claude|gemini|cursor` (required, repeatable)
- `--effort min|low|medium|high|xhigh|max` (optional, best effort)
- `--prompt TEXT`, `--prompt-file FILE`, positional prompt text, or stdin
- `--system TEXT` / `--system-file FILE`
- `--instruction TEXT` / `--instruction-file FILE`
- `--context-file FILE` to inline referenced artifacts with path/hash/size
  boundaries; repeatable
- `--model MODEL` to override `vendors.conf`
- `--yolo` to select each vendor's no-approval/no-confirmation mode
- `--cwd DIR` to run from a specific working directory
- `--output-dir DIR` to write `<id>/out`, `<id>/status`, `<id>/log`,
  and `<id>/usage.json`
  for one or more calls
- `--id ID` to choose output directory ids; repeat once per `--vendor`
- `--min-success N` to set how many selected vendors must succeed
- `--timeout SECONDS` to stop a hanging vendor call
- `--native-arg ARG` for a selected vendor's raw CLI-specific escape hatch
- `--env NAME=VALUE` for per-call environment overrides
- `--schema-file FILE` to constrain the response to a JSON Schema. Output lands
  at `<output-dir>/<id>/out` as `{"structured_output": <conforming-object>}`
  for both supported vendors. Supported on `claude` and `openai` (codex);
  `gemini` and `cursor` are rejected because their CLIs have no native schema
  enforcement.

`model`, `effort`, and `yolo` are the only vendor behavior abstractions. Prompt
transport, output files, timeouts, context inlining, cwd, and environment
variables are wrapper runtime contract, not model capability abstractions. Other
vendor CLI behavior must be passed explicitly with `--native-arg`.

## Yolo Mapping

`--yolo` maps to the closest no-approval mode for each vendor:

| Vendor | Native arguments |
|--------|------------------|
| OpenAI/Codex | `--dangerously-bypass-approvals-and-sandbox` |
| Claude | `--permission-mode bypassPermissions` |
| Gemini | `--yolo` |
| Cursor | `--yolo` (alias of `--force`) |

## Effort Mapping

Effort is a shared ordered scale: `min < low < medium < high < xhigh < max`.
Each vendor receives the exact value when it supports it. Otherwise the module
selects the nearest stronger supported effort; if no stronger value exists, it
selects the nearest weaker supported effort. Gemini and Cursor currently have
no native effort knob, so any effort hint maps to their default behavior; pick
a model variant in `vendors.conf` (e.g. `cursor.model=...-thinking`) when you
need stronger reasoning from those vendors.

| Input | OpenAI/Codex | Claude | Gemini | Cursor |
|-------|--------------|--------|--------|--------|
| `min` | `low` | `low` | default | default |
| `low` | `low` | `low` | default | default |
| `medium` | `medium` | `medium` | default | default |
| `high` | `high` | `high` | default | default |
| `xhigh` | `xhigh` | `xhigh` | default | default |
| `max` | `xhigh` | `max` | default | default |

## Native Args

Use `--native-arg` for vendor-specific CLI flags. Repeat it once for each argv
token so quoting and ordering remain explicit. Native args apply to every
selected `--vendor` in that `call.sh` invocation; when native args differ by
vendor, run separate calls or have the caller fan out explicitly.

OpenAI/Codex examples:

```bash
# Sandboxed automatic execution.
"$VENDORS/scripts/call.sh" \
  --vendor openai \
  --native-arg --full-auto \
  --prompt-file "$prompt_file" \
  --output-dir "$RUN_DIR"

# Read-only sandboxed review.
"$VENDORS/scripts/call.sh" \
  --vendor openai \
  --native-arg --sandbox \
  --native-arg read-only \
  --prompt-file "$prompt_file" \
  --output-dir "$RUN_DIR"

# Fast service tier.
"$VENDORS/scripts/call.sh" \
  --vendor openai \
  --native-arg -c \
  --native-arg 'service_tier="fast"' \
  --prompt-file "$prompt_file" \
  --output-dir "$RUN_DIR"
```

Claude examples:

```bash
# Restrict available tools for a read-focused review.
"$VENDORS/scripts/call.sh" \
  --vendor claude \
  --native-arg --allowedTools \
  --native-arg Read,Glob,Grep,LS \
  --prompt-file "$prompt_file" \
  --output-dir "$RUN_DIR"

# Schema-constrained output. Prefer the shared `--schema-file` option below
# over passing `--json-schema` as a native arg; the shared option works the
# same way on codex and produces the same envelope for either vendor.
"$VENDORS/scripts/call.sh" \
  --vendor claude \
  --schema-file "$schema_path" \
  --prompt-file "$prompt_file" \
  --output-dir "$RUN_DIR"
```

Gemini examples:

```bash
# Plan/read-only approval mode.
"$VENDORS/scripts/call.sh" \
  --vendor gemini \
  --native-arg --approval-mode \
  --native-arg plan \
  --prompt-file "$prompt_file" \
  --output-dir "$RUN_DIR"

# Include an extra directory for Gemini.
"$VENDORS/scripts/call.sh" \
  --vendor gemini \
  --native-arg --include-directories \
  --native-arg "$extra_dir" \
  --prompt-file "$prompt_file" \
  --output-dir "$RUN_DIR"
```

## Schema-Constrained Output

Pass `--schema-file <path>` to force the model's response to conform to a
JSON Schema. The wrapper translates the shared option into each vendor's
native flag and normalizes the output to one envelope so callers don't
branch on vendor:

| Vendor | Native flag used | Notes |
|--------|------------------|-------|
| `claude` | `--json-schema "$(cat …)"` | Schema inlined as JSON |
| `openai` (codex) | `--output-schema <path>` | File path passed through |
| `gemini` | (rejected) | CLI has no native schema enforcement |
| `cursor` | (rejected) | CLI has no native schema enforcement |

`<output-dir>/<id>/out` always contains `{"structured_output": <obj>}` —
unwrap `.structured_output` to get the schema-conforming object. The
envelope shape is the same for claude and codex.

```bash
VENDORS=${VENDORS:-/tmp/skills/vendors}
RUN_DIR=$(mktemp -d /tmp/vendor-run.XXXXXX)
SCHEMA=$(mktemp /tmp/schema.XXXXXX.json)
cat > "$SCHEMA" <<'JSON'
{"type":"object","properties":{"city":{"type":"string"}},"required":["city"],"additionalProperties":false}
JSON

"$VENDORS/scripts/call.sh" \
  --vendor codex \
  --schema-file "$SCHEMA" \
  --prompt "Return Tokyo as JSON." \
  --output-dir "$RUN_DIR"

jq .structured_output "$RUN_DIR/openai/out"
```

If a caller already passes `--json-schema` (claude) or `--output-schema`
(codex) via `--native-arg`, the wrapper does not duplicate it; the explicit
native arg wins.

## Calling From Another Skill

Other skills should call this module by absolute path so their own working
directory does not matter. The caller owns result retention: pass
`--output-dir` when a workflow needs a known run directory, or omit it when a
standalone call can use a generated temp output directory printed by `call.sh`.
Either way, the result directory is intentionally kept for the caller to read
and clean up.

```bash
VENDORS=${VENDORS:-/tmp/skills/vendors}
RUN_DIR=$(mktemp -d /tmp/vendor-run.XXXXXX)
"$VENDORS/scripts/call.sh" \
  --vendor claude \
  --effort medium \
  --prompt-file "$prompt_file" \
  --output-dir "$RUN_DIR"
```

Use `--dry-run` to inspect the resolved vendor, model, and native command
without spending a model call.

`call.sh` also uses an internal temp working directory for normalized prompts
and transcripts. That internal directory is cleaned on normal exit and common
interrupt signals. A hard kill or host crash can still leave temp files behind;
no shell cleanup hook can prevent that.

## Prompt Transport

Callers may pass prompts inline (`--prompt` or positional text), through stdin,
or by `--prompt-file`. The module normalizes all forms into a temporary prompt
file first so multi-vendor calls use identical bytes and avoid shell quoting
surprises.

That temporary file is an internal transport detail. Each vendor launcher then
uses the form the CLI actually tolerates: Codex reads from stdin, Claude runs in
print mode and reads stdin, and Gemini receives `--prompt` while stdin is closed
to prevent headless OAuth hangs.

## Parallel Calls

Repeat `--vendor` when another skill needs the same prompt sent to several
vendors at once:

```bash
VENDORS=${VENDORS:-/tmp/skills/vendors}
RUN_DIR=$(mktemp -d /tmp/vendor-run.XXXXXX)
"$VENDORS/scripts/call.sh" \
  --vendor openai \
  --vendor claude \
  --vendor gemini \
  --effort min \
  --prompt-file "$prompt_file" \
  --output-dir "$RUN_DIR" \
  --min-success 2
```

Outputs are always written as:

- `<output-dir>/openai/out`
- `<output-dir>/openai/status`
- `<output-dir>/openai/log`
- `<output-dir>/openai/usage.json`
- `<output-dir>/claude/out`
- `<output-dir>/gemini/out`
- `<output-dir>/cursor/out`

For a single vendor, the same contract applies. For example, `--vendor claude`
writes `<output-dir>/claude/out`, `<output-dir>/claude/status`,
`<output-dir>/claude/log`, and `<output-dir>/claude/usage.json`.

`usage.json` has normalized top-level fields for callers:

```json
{
  "provider": "claude",
  "model": "claude-sonnet-4-6",
  "available": true,
  "source": "claude_json",
  "total_tokens": 1234,
  "input_tokens": 1000,
  "output_tokens": 234,
  "raw": {}
}
```

Callers should treat `total_tokens` as the portable field and use `raw` only
for diagnostics. When usage cannot be extracted, `available` is `false` and
`total_tokens` is `null`.

The module does not require every supported vendor to be healthy. A caller
selects the vendors it needs for that workflow and sets `--min-success` for
that selected set.

Use repeated `--id` values when calls need call-specific names, such as running
OpenAI once as `openai` and later as `synthesis` in the same directory:

```bash
"$VENDORS/scripts/call.sh" \
  --vendor openai \
  --id synthesis \
  --prompt-file "$prompt_file" \
  --output-dir "$RUN_DIR"
```

`call.sh` does not substitute a failed vendor. Upper-layer skills should decide
whether `--min-success` is all selected vendors, two of three, or one of one.

The same vendor can be selected more than once. If no ids are supplied,
duplicates are suffixed automatically, such as `openai/` and `openai-2/`.
Supplying explicit ids is clearer for multi-stage callers:

```bash
"$VENDORS/scripts/call.sh" \
  --vendor openai --id critique-a \
  --vendor openai --id critique-b \
  --prompt-file "$prompt_file" \
  --output-dir "$RUN_DIR" \
  --min-success 2
```

## Doctor

Run the doctor when setting up the module, changing `vendors.conf`, refreshing
auth, or debugging a vendor failure:

```bash
VENDORS=${VENDORS:-/tmp/skills/vendors}
"$VENDORS/scripts/doctor.sh"
```

By default it probes openai, claude, gemini, and cursor through
`scripts/call.sh` with a small `READY` prompt. Use `--vendor` to check a subset:

```bash
VENDORS=${VENDORS:-/tmp/skills/vendors}
"$VENDORS/scripts/doctor.sh" --vendor openai
```

The probe uses the same launcher path as normal calls, not a separate PATH-only
check. This intentionally catches expired auth, bad model names, proxy/network
breakage, and headless CLI hangs before a real workflow depends on the vendor.

Failed doctor probes keep their diagnostic directory automatically. Use
`--keep-output` or `--output-dir DIR` to inspect successful probes too. See
`TROUBLESHOOTING.md` for known failure signatures and the current debug playbook.

## Vendor Notes

These details mirror the hard-won behavior captured in `panel-review`:

- Codex/OpenAI writes the final assistant message with `--output-last-message`.
  If that file is empty, the launcher falls back to the CLI transcript and marks
  it as degraded output.
- Gemini runs with stdin redirected from `/dev/null` in non-interactive mode.
  Without this, the CLI may open an interactive OAuth prompt and hang in
  headless runs.
- Cursor runs `cursor-agent -p --trust --output-format stream-json` with stdin
  redirected from `/dev/null` for the same headless-safety reason. `--trust` is
  the headless equivalent of clicking "trust this workspace" in the IDE; the
  launcher always passes it because every fresh cwd otherwise blocks on a
  workspace-trust prompt that has no headless answer. The launcher parses the
  terminal `result` event for both the assistant text and the
  `usage:{inputTokens,outputTokens,cacheReadTokens,cacheWriteTokens}` payload.
  Authenticate once with `cursor-agent login` (or export `CURSOR_API_KEY`); the
  CLI reuses that credential across every call, so this module does not require
  per-call env vars. List available model ids with `cursor-agent --list-models`
  and pin the chosen one in `vendors.conf` as `cursor.model=<id>`.
- Doctor probes make a tiny model call. Run them after setup, auth refreshes,
  model/config changes, or vendor failures; do not pay the probe cost before
  every routine call.

Local-machine setup such as SSL certs, proxy variables, custom PATH, CLI auth
state, and sandbox workarounds should stay in user-level shell/vendor/Codex
configuration outside this module. Do not bake machine-specific setup into
`README.md`, `vendors.conf`, or scripts.

## Smoke Test

Run this after editing the module:

```bash
VENDORS=${VENDORS:-/tmp/skills/vendors}
"$VENDORS/scripts/smoke-test.sh"
```

It uses fake `codex`, `claude`, `gemini`, and `cursor-agent` binaries and
performs one call per caller. Each call selects all four vendors and verifies
the full output/status/log/usage.json contract across the run.

## Real Response Test

Run this when you intentionally want to spend real vendor calls:

```bash
VENDORS=${VENDORS:-/tmp/skills/vendors}
"$VENDORS/scripts/hello-test.sh"
```

It defaults to openai, claude, gemini, and cursor, asks each selected LLM
`Who are you?`, and verifies `exit_code=0` plus non-empty output. It does not
judge answer content. Use repeated `--vendor` flags to check a subset, or pass
`--prompt` to ask a different short probe.

## Real Nested Test

Run this only when you intentionally want to spend real nested vendor calls and
grant the outer LLMs command execution permission:

```bash
VENDORS=${VENDORS:-/tmp/skills/vendors}
"$VENDORS/scripts/nested-test.sh" --run-real-nested
```

By default it asks openai, claude, gemini, and cursor as outer agents to each
execute one inner `call.sh` that fans out to openai, claude, gemini, and cursor.
That is 4 outer model calls plus 16 inner model calls. It verifies each inner
call has `exit_code=0` and non-empty output.

Nested calls do not pass model permissions from the outer LLM to the inner
vendors. The outer LLM only needs enough tool/shell permission to execute
`call.sh`. The inner calls then use the local machine's `codex`, `claude`,
`gemini`, and `cursor-agent` CLI auth, PATH, environment, and the inner
`call.sh` arguments. Keep each nested layer on its own `--output-dir` subtree
and set timeouts at every layer.
