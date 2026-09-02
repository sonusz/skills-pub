# Vendors Module

This directory is a reusable module for other skills, not a standalone skill.
Do not add `SKILL.md` unless you intentionally want `vendors` to become a
directly triggerable skill.

The stable module entrypoints are:

- `scripts/call.sh` for one vendor call or one prompt fanned out to multiple
  vendors in parallel.
- `scripts/doctor.sh` for readiness checks and retained diagnostics on failure.
- `scripts/smoke-test.sh` for a fake-CLI regression test that makes one
  five-vendor fan-out call per caller (fake `codex`, `claude`, `agy`,
  `cursor-agent`, and `grok` binaries are generated under a temp dir).
- `scripts/hello-test.sh` for a real vendor call test that asks each selected
  LLM `Who are you?` and verifies non-error output.
- `scripts/nested-test.sh` for a guarded real nested integration test where
  each selected outer vendor is asked to run the selected inner `call.sh`.
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
- `agy` maps to the local `agy` CLI.
- `cursor` maps to the local `cursor-agent` CLI.
- `grok` and `xai` map to the official local Grok Build `grok` CLI.

Common arguments:

- `--vendor openai|claude|agy|cursor|grok` (required, repeatable; `xai` is a
  Grok alias)
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
  `<id>/stream`, and `<id>/usage.json`
  for one or more calls
- `--id ID` to choose output directory ids; repeat once per `--vendor`
- `--session-key KEY` to persist and resume one opaque logical conversation;
  repeat once per `--vendor` in fan-out calls
- `--session-max-turns N` to rotate a keyed native conversation after N
  successful turns; the next call sends the full prompt and starts at turn 1
- `--resume-prompt TEXT` / `--resume-prompt-file FILE` to provide a smaller
  prompt that is sent only after an existing native session is found
- `--min-success N` to set how many selected vendors must succeed
- `--timeout SECONDS` to stop a hanging vendor call
- `--timeout-extend SECONDS` to extend the deadline in SECONDS-long windows
  while the vendor's stream output keeps growing; the call is killed only
  after a full window with no new output (`0` disables, the default)
- `--native-arg ARG` for a selected vendor's raw CLI-specific escape hatch
- `--env NAME=VALUE` for per-call environment overrides
- `--schema-file FILE` to constrain the response to a JSON Schema. Output lands
  at `<output-dir>/<id>/out` as `{"structured_output": <conforming-object>}`
  for every supported schema vendor. Supported on `claude`, `openai` (codex),
  and `grok`; `agy` and `cursor` are rejected because their CLIs have no native
  schema enforcement.

`model`, `effort`, and `yolo` are the only vendor behavior abstractions. Prompt
transport, output files, timeouts, context inlining, cwd, and environment
variables are wrapper runtime contract, not model capability abstractions. Other
vendor CLI behavior must be passed explicitly with `--native-arg`.

## Persistent Sessions

Session persistence is opt-in. Calls without `--session-key` keep the original
stateless behavior. For a keyed call, the first successful invocation sends the
full prompt and records the provider's native conversation id; later calls with
the same key, normalized vendor, resolved model, real working directory, and
ordered native-argument fingerprint resume that conversation. When `--cwd` is
omitted, the invocation directory is the real working directory for this
identity. Raw native arguments are not stored. If a non-empty
`--resume-prompt` is supplied, only that continuation prompt is sent on resume.
Otherwise the full prompt is sent again.

Callers may bound conversational lifetime with `--session-max-turns N`. Turns
1 through N share one native session; the following call atomically forgets
that mapping and starts a new session. Only successful calls advance the
counter. Failed or interrupted calls retain the prior count, while an explicit
`session-state.py reset` clears it. Keyed status files expose `session_turn`,
`session_max_turns`, and `session_auto_reset` for audit.

```bash
VENDORS=${VENDORS:-/tmp/skills/vendors}

# First turn: establishes the provider-native session.
"$VENDORS/scripts/call.sh" \
  --vendor claude \
  --session-key 'my-workflow:design' \
  --prompt-file /tmp/full-design-contract.md \
  --resume-prompt-file /tmp/current-revision.md \
  --cwd /path/to/repo \
  --output-dir /tmp/design-turn-1

# Later turn: resumes the same session and sends current-revision.md.
"$VENDORS/scripts/call.sh" \
  --vendor claude \
  --session-key 'my-workflow:design' \
  --prompt-file /tmp/full-design-contract.md \
  --resume-prompt-file /tmp/current-revision.md \
  --cwd /path/to/repo \
  --output-dir /tmp/design-turn-2
```

The provider mappings are native: Codex uses `exec resume`, Claude and Grok
use explicit session UUIDs plus `--resume`, Agy resumes by conversation id, and
Cursor uses `--resume`. Session-enabled Agy calls use its JSON output envelope
to discover `conversation_id`; `out` is normalized back to the plain response.
Native arguments that override session selection/persistence (and Agy/Cursor
output formats needed to observe their dynamic ids) are rejected on keyed
calls; `--session-key` owns that transport.

A successful keyed provider process is not sufficient by itself: the wrapper
must also obtain a native session id. If a dynamic provider omits that id, or
session observation/finalization fails, the call reports exit code 70 in its
status and does not create a resumable mapping. Session support requires Python
3.10 or newer. Session ids are accepted only from each provider's trusted
top-level protocol event (never nested model/tool payloads), must have a safe
token shape, and must match a wrapper-requested id when one was supplied.

Runtime mappings live outside repositories under
`${VENDORS_SESSION_STATE_DIR}`, `${XDG_STATE_HOME}/shared-vendors/sessions`, or
`~/.local/state/shared-vendors/sessions` (in that order). The opaque key itself
is never stored; only its SHA-256 identity is retained in mode-0600 files. A
per-session lease rejects concurrent turns with exit code 75 rather than
corrupting conversation order. An unexpired lease remains reserved after its
owner crashes; a live owner remains reserved even if a long call outlives the
lease timestamp. A conclusive native "session not found" error invalidates the
mapping; the failed call remains failed, and the next caller starts a fresh
native session. A handled `INT`, `TERM`, or `HUP` releases leases only after
the keyed call's dedicated kernel process group is empty. Direct keyed calls
automatically re-exec into such a group when needed. Cleanup repeatedly scans
the group so reparented or post-signal children cannot escape, then releases
only the exact random lease tokens published in that invocation's session
plans. An unverified shutdown keeps both session and output locks fail closed.

The bundled `session-state.py reset --state-dir DIR --key KEY` operator command
forgets every provider/model/cwd mapping for one logical key. It is serialized
against new plans and refuses to reset while any matching lease is live. The
next keyed call therefore starts a fresh provider-native conversation.

Each `<output-dir>/<id>` also has an atomic coordinator lock acquired before
any call artifact is created or truncated. Reusing that output id concurrently
fails immediately. IDs `.` and `..` and symlinked call directories are rejected;
an existing directory must resolve to a direct child of the canonical output
root. Normal exits and verified handled signals remove the lock. A coordinator
killed with `SIGKILL` intentionally leaves a fail-closed stale lock; verify its
recorded pid is gone before removing that hidden lock directory, or choose a
new `--output-dir`.

Keyed status files additionally expose `session_mode=new|resume`, `session_id`,
`session_key_hash`, `session`, and `session_invalidated`. Treat native session
ids as local runtime metadata, not portable workflow artifacts.

## Yolo Mapping

`--yolo` maps to the closest no-approval mode for each vendor:

| Vendor | Native arguments |
|--------|------------------|
| OpenAI/Codex | `--dangerously-bypass-approvals-and-sandbox` |
| Claude | `--permission-mode bypassPermissions` |
| Agy | `--dangerously-skip-permissions` |
| Cursor | `--yolo` (alias of `--force`) |
| Grok Build | `--yolo` (alias of `--always-approve`) |

## Effort Mapping

Effort is a shared ordered scale: `min < low < medium < high < xhigh < max`.
Each vendor receives the exact value when it supports it. Otherwise the module
selects the nearest stronger supported effort; if no stronger value exists, it
selects the nearest weaker supported effort. Agy and Cursor currently have
no native effort knob, so any effort hint maps to their default behavior. Grok
supports `low`, `medium`, and `high` through `--reasoning-effort`; the generic
nearest-effort mapping sends `min` to `low` and `xhigh`/`max` to `high`. Pick
a model variant in `vendors.conf` (e.g. `cursor.model=...-thinking`) when you
need stronger reasoning from those vendors.

| Input | OpenAI/Codex | Claude | Agy | Cursor | Grok |
|-------|--------------|--------|-----|--------|------|
| `min` | `low` | `low` | default | default | `low` |
| `low` | `low` | `low` | default | default | `low` |
| `medium` | `medium` | `medium` | default | default | `medium` |
| `high` | `high` | `high` | default | default | `high` |
| `xhigh` | `xhigh` | `xhigh` | default | default | `high` |
| `max` | `xhigh` | `max` | default | default | `high` |

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

Agy examples:

```bash
# Plan/read-only approval mode.
"$VENDORS/scripts/call.sh" \
  --vendor agy \
  --native-arg --mode \
  --native-arg plan \
  --prompt-file "$prompt_file" \
  --output-dir "$RUN_DIR"

# Include an extra directory for Agy.
"$VENDORS/scripts/call.sh" \
  --vendor agy \
  --native-arg --add-dir \
  --native-arg "$extra_dir" \
  --prompt-file "$prompt_file" \
  --output-dir "$RUN_DIR"
```

Grok Build example:

```bash
"$VENDORS/scripts/call.sh" \
  --vendor xai \
  --model grok-4.5 \
  --effort medium \
  --schema-file "$schema_path" \
  --prompt-file "$prompt_file" \
  --output-dir "$RUN_DIR"
```

The launcher uses headless `--prompt-file` transport and
`--output-format streaming-json` by default. It retains the NDJSON protocol as
`stream`, concatenates only `text` events into `out`, and extracts the terminal
`end.usage` object into `usage.json`.

## Schema-Constrained Output

Pass `--schema-file <path>` to force the model's response to conform to a
JSON Schema. The wrapper translates the shared option into each vendor's
native flag and normalizes the output to one envelope so callers don't
branch on vendor:

| Vendor | Native flag used | Notes |
|--------|------------------|-------|
| `claude` | `--json-schema "$(cat …)"` | Schema inlined as JSON |
| `openai` (codex) | `--output-schema <path>` | File path passed through |
| `grok` | `--json-schema "$(cat …)"` | Native Grok schema enforcement |
| `agy` | (rejected) | CLI has no native schema enforcement |
| `cursor` | (rejected) | CLI has no native schema enforcement |

`<output-dir>/<id>/out` always contains `{"structured_output": <obj>}` —
unwrap `.structured_output` to get the schema-conforming object. The
envelope shape is the same for Claude, Codex, and Grok.

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

If a caller already passes `--json-schema` (Claude/Grok) or `--output-schema`
(Codex) via `--native-arg`, the wrapper does not duplicate it; the explicit
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
print mode and reads stdin, Agy receives `--print <prompt>` while stdin is
closed, Cursor receives a positional prompt, and Grok receives the temporary
file through native `--prompt-file`.

## Parallel Calls

Repeat `--vendor` when another skill needs the same prompt sent to several
vendors at once:

```bash
VENDORS=${VENDORS:-/tmp/skills/vendors}
RUN_DIR=$(mktemp -d /tmp/vendor-run.XXXXXX)
"$VENDORS/scripts/call.sh" \
  --vendor openai \
  --vendor claude \
  --vendor agy \
  --effort min \
  --prompt-file "$prompt_file" \
  --output-dir "$RUN_DIR" \
  --min-success 2
```

Outputs are always written as:

- `<output-dir>/openai/out`
- `<output-dir>/openai/status`
- `<output-dir>/openai/log`
- `<output-dir>/openai/stream`
- `<output-dir>/openai/usage.json`
- `<output-dir>/claude/out`
- `<output-dir>/agy/out`
- `<output-dir>/cursor/out`
- `<output-dir>/grok/out`

For a single vendor, the same contract applies. For example, `--vendor claude`
writes `<output-dir>/claude/out`, `<output-dir>/claude/status`,
`<output-dir>/claude/log`, `<output-dir>/claude/stream`, and
`<output-dir>/claude/usage.json`.

`stream` is the live transcript/progress file callers should monitor for idle
detection while the process is running. `out` remains the final normalized model
response and may be written only after the vendor exits.

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
`total_tokens` is `null`. Stateless Agy text-mode calls use that unavailable
form; session-enabled Agy calls use its JSON envelope and expose normalized
usage.

The module does not require every supported vendor to be healthy. A caller
selects the vendors it needs for that workflow and sets `--min-success` for
that selected set.

## Grok Capability Matrix

The wrapper/runtime guarantees below are separate from Grok model quality.
`smoke-test.sh` proves the adapter deterministically; bounded real proofs use
the named entrypoints and retain their output directory for diagnosis.

| Capability | Wrapper/runtime contract | Grok-native mechanism | Evidence |
|---|---|---|---|
| Aliases and model | `grok`/`xai` normalize to id `grok`; config or `--model` selects model | `grok --model` | smoke aliases/default/override; real `call.sh` |
| Prompt inputs | inline/file/stdin/system/instruction/context keep wrapper order | `--prompt-file` | smoke transport; real `hello-test.sh`/`call.sh` |
| Runtime controls | cwd, per-call env, native argv, timeout | `--cwd`; process environment; raw argv | smoke runtime/timeout; bounded real `call.sh` |
| Effort | nearest mapping over low/medium/high | `--reasoning-effort` | smoke all six inputs; real min/medium/max |
| No approval | shared `--yolo` | Grok `--yolo` | smoke dry run; guarded real calls |
| Output artifacts | isolated `out`, `status`, `log`, `stream`, `usage.json` | streaming NDJSON `text`/`end` | smoke success/failure/fan-out; real calls |
| Usage | normalized token totals plus retained raw values | terminal `end.usage` | smoke controlled totals; real `usage.json` |
| Structured output | shared `structured_output` envelope and failure status | native `--json-schema`; `end.structuredOutput` | smoke valid/invalid; bounded real schema |
| Readiness/headless | bounded noninteractive call through production launcher | official installed/authenticated CLI | fake and real `doctor.sh`; `hello-test.sh` |
| Nested execution | explicit guard and per-level timeouts | outer Grok `--yolo`; inner production launcher | bounded `nested-test.sh --run-real-nested` |

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

By default it probes openai, claude, agy, cursor, and grok through
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
- Agy receives its prompt through `--print` and runs with stdin redirected from
  `/dev/null`, which keeps print mode non-interactive.
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
- Grok runs the official `grok` executable headlessly with
  `--output-format streaming-json`, `--prompt-file`, and the configured
  `grok.model` (default `grok-4.5`). Its final `text` chunks, terminal usage,
  and native `structuredOutput` are normalized without exposing protocol
  control frames through `out`. Non-dry-run Grok calls require `python3` or
  `python` for this normalization and fail before invoking Grok when neither is
  available; `--dry-run` remains parser-free. Authenticate the official CLI
  outside this module; the wrapper never reads or changes `~/.grok`.
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

It uses fake `codex`, `claude`, `agy`, `cursor-agent`, and `grok` binaries and
performs one call per caller. Each call selects all five vendors and verifies
the full output/status/log/stream/usage contract. Grok-specific checks cover
aliases, models, effort, yolo, prompts, cwd/env/native args, schema, failures,
timeouts, fan-out, and doctor.

## Real Response Test

Run this when you intentionally want to spend real vendor calls:

```bash
VENDORS=${VENDORS:-/tmp/skills/vendors}
"$VENDORS/scripts/hello-test.sh"
```

It defaults to openai, claude, agy, cursor, and grok, asks each selected LLM
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

By default it asks openai, claude, agy, cursor, and grok as outer agents to each
execute one inner `call.sh` that fans out to all five vendors. That is expensive;
prefer explicit `--outer-vendor` and `--inner-vendor` selections for bounded
proofs. It verifies each inner call has `exit_code=0` and non-empty output.

Nested calls do not pass model permissions from the outer LLM to the inner
vendors. The outer LLM only needs enough tool/shell permission to execute
`call.sh`. The inner calls then use the local machine's `codex`, `claude`,
`agy`, `cursor-agent`, and `grok` CLI auth, PATH, environment, and the inner
`call.sh` arguments. Keep each nested layer on its own `--output-dir` subtree
and set timeouts at every layer.
