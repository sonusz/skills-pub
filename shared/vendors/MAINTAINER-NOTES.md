# Vendors module — maintainer notes

`shared/vendors` is the only canonical vendors module in this repository. It
wraps the Codex, Claude, Agy, Cursor, and official Grok Build CLIs behind the
uniform `scripts/call.sh` and `scripts/vendor-launch.sh` interface.

## Repository layout

Five committed skill consumers use relative symlinks to the canonical module:

| Consumer | Committed path | Required link target |
|---|---|---|
| auto-dev-sdk | `skills/auto-dev-sdk/shared/vendors` | `../../../shared/vendors` |
| feature-spec | `skills/feature-spec/shared/vendors` | `../../../shared/vendors` |
| multi-lens-review | `skills/multi-lens-review/shared/vendors` | `../../../shared/vendors` |
| panel-review | `skills/panel-review/shared/vendors` | `../../../shared/vendors` |
| pr-review | `skills/pr-review/shared/vendors` | `../../../shared/vendors` |

Do not replace these links with regular-file copies and do not hand-copy changes
between skill directories. Editing `shared/vendors` updates every in-repository
consumer through the same canonical files.

## Validate links and packaging

Run this audit after changing the module:

```bash
canonical=$(cd shared/vendors && pwd -P)
for link in \
  skills/auto-dev-sdk/shared/vendors \
  skills/feature-spec/shared/vendors \
  skills/multi-lens-review/shared/vendors \
  skills/panel-review/shared/vendors \
  skills/pr-review/shared/vendors
do
  test -L "$link"
  test "$(readlink "$link")" = "../../../shared/vendors"
  test "$(cd "$link" && pwd -P)" = "$canonical"
done

# The repository should have one regular-file vendors tree: the canonical one.
find . -type f -path '*/shared/vendors/*' -print | sort
```

Installers, archives, caches, or deployed skill packages may dereference the
symlink and contain regular files. Those are packaging artifacts outside the
canonical repository layout. Regenerate them through their packaging workflow;
never treat an installed copy, Downloads folder, cache, or generated archive as
a source to sync back into this repository.

## Required validation

The module is intentionally compatible with the macOS system Bash 3.2. Run:

```bash
for script in shared/vendors/scripts/*.sh; do
  /bin/bash -n "$script"
done
/bin/bash shared/vendors/scripts/smoke-test.sh
git diff --check -- shared/vendors
```

The smoke test uses fake CLIs and must not spend real model calls. Use
`doctor.sh`, `hello-test.sh`, and guarded `nested-test.sh` only when the relevant
real integration is intentionally being tested.

## Load-bearing implementation details

- Use `vendors_lower()` instead of Bash 4 `${var,,}` syntax.
- Avoid associative arrays; macOS Bash 3.2 does not support them.
- Keep `set -eo pipefail` in `call.sh`; nounset has caused false failures with
  the module's Bash 3.2 array handling.
- Preserve the BSD `script -q /dev/null <runner>` path and Linux fallback for
  Claude PTY execution.
- Strip PTY control bytes before parsing JSON.
- Preserve line-by-line JSON extraction for CLIs that mix progress text with
  JSON.
- Keep `--schema-file` normalization and the common
  `{"structured_output": <object>}` envelope.
- Grok must have `python3` or `python` before native invocation because final
  text, schema, protocol errors, and usage are derived from streaming JSON.
  Missing Python must leave a valid unavailable `usage.json`.
- Keep raw Grok NDJSON in `stream`; expose only normalized final text or the
  structured-output envelope in `out`.
- Keep provider-neutral session state in `scripts/session-state.py`; callers
  supply opaque keys and must not implement provider-specific resume logic.
- Session identity includes the opaque key, normalized vendor, resolved model,
  real effective cwd (including the invocation cwd when `--cwd` is omitted),
  and the ordered native-argument fingerprint. Do not weaken that boundary or
  two incompatible agents can inherit one another's context. Persist only the
  fingerprint, never raw native arguments.
- Session mappings are user-level runtime state, never repository files. Store
  only a key hash, use mode 0700/0600, atomic replacement, and the per-record
  lease. Concurrent turns for one native conversation must fail closed.
- Observe native session ids before `vendors_collect_usage` rewrites `out`.
  This ordering is required for Claude and Cursor. Session-enabled Agy uses
  `--output-format json`; normalize its `response` only after observing
  `conversation_id`. Extract only trusted provider-specific top-level protocol
  fields/event types; nested model and tool payloads are attacker-controlled.
  Validate id shape and reject any mismatch with a wrapper-requested id.
- A keyed provider exit 0 is successful only when native id observation and
  state finalization also succeed. Fail with status exit code 70 and release
  the lease when a dynamic provider omits its id. Session state requires Python
  3.10+ because `session-state.py` uses that language level.
- Reserve each output id with its parent-level hidden lock before touching the
  call directory. Lock every fan-out id before launching any provider. Normal
  cleanup removes owned locks; a `SIGKILL`-orphaned lock stays fail closed and
  requires a verified manual cleanup or a new output directory. Reject `.` and
  `..`, directory symlinks, and any call directory that does not resolve to a
  direct child of the canonical output root.
- A session lease is live while either its timestamp is unexpired or its actual
  per-call supervisor pid is alive. Do not use Bash `$$` from an async function:
  on Bash 3.2 it still names the top coordinator. Keyed calls must own a
  dedicated process group (direct calls re-exec through `os.setsid()` when
  needed). On handled signals, repeatedly drain and verify that group, then
  finalize only the exact plan/token capabilities published by that invocation.
  Never scan leases by numeric PID: PID reuse can release unrelated work.
- Keep Codex native args in the `codex exec` option scope, before the `resume`
  subcommand. Several valid exec flags (including `--oss` and `--sandbox`) are
  rejected in the resume-subcommand scope.
- Preserve stateless compatibility: when `--session-key` is absent, Claude
  retains `--no-session-persistence` and all other launchers keep their prior
  command shape.
- A missing native session invalidates only that mapping. Do not silently retry
  the same logical turn in a fresh context; report the failure and let the next
  workflow attempt establish a new session with its full prompt.

## Common bug signatures

| Symptom | Likely cause or required fix |
|---|---|
| `${1,,}: bad substitution` or an unbound array failure | Bash 4 syntax or nounset was introduced; restore the Bash 3.2-compatible form |
| `script: invalid option` | The BSD/Linux PTY fallback was removed or reordered |
| Stray `^D` or `^H` bytes | PTY control-character cleanup is missing |
| JSON parsing fails after progress lines | The line-by-line JSON extraction fallback is missing |
| Synthesizer reads empty schema output | The normalized `structured_output` envelope was lost |
| Agy rejects `--model` | The configured value is not an exact available display name; leave `agy.model=` empty to use its configured default |
| Cursor blocks on workspace trust | The headless `--trust` default is missing |
| Grok `out` contains NDJSON control frames | The Grok transcript normalizer was bypassed |
| Grok exits zero but produces no final text | Require a non-empty `text` event unless native structured output was requested |
| Grok schema succeeds without an envelope | Treat `structuredOutputError` or missing `structuredOutput` as failure |
| Grok has no Python interpreter | Fail before invoking `grok` and write unavailable Grok usage metadata |
| Keyed call repeatedly reports `session_mode=new` | Native id observation failed; inspect raw protocol before normalization and the keyed `session.json` |
| Exit 75 with `session lease unavailable` | The same logical session has a live turn; wait for it or diagnose its owner instead of running concurrent prompts |
| Exit 70 with a session protocol/state reason | A keyed provider omitted its native id, observation failed, or durable finalization failed; do not treat the turn as resumable |
| `output id is already in use` | Another coordinator owns that `<output-dir>/<id>`, or a killed coordinator left a fail-closed hidden lock; verify the recorded pid before cleanup |
| Resume fails with a native not-found error | The mapping is invalidated intentionally; the next call starts new and must receive the full prompt |

## Maintaining this file

Update these notes when the canonical module layout, consumer symlink list, or a
load-bearing compatibility rule changes. Historical external copies are useful
only as debugging evidence; they are never canonical repository peers.
