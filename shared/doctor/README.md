# Doctor module

`shared/doctor/doctor-lib.sh` is a sourced bash library (macOS bash 3.2 safe)
that gives every skill doctor the same output format and result counters. It
is not a skill and has no `SKILL.md`.

## Usage

```bash
#!/usr/bin/env bash
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/../shared/doctor/doctor-lib.sh"

echo "=== my-skill doctor ==="
echo ""

doctor_section "Dependencies"
doctor_require_cmd jq "brew install jq  OR  apt install jq"
doctor_require_cmd perl

doctor_section "Configuration"
doctor_require_file "$HOME/.claude/settings.json" "Run install.sh first"
if [ -n "${MY_TOKEN:-}" ]; then
  doctor_pass "MY_TOKEN is set"
else
  doctor_warn "MY_TOKEN not set -- remote checks skipped"
  doctor_note "export MY_TOKEN=... then re-run"
fi

if ! doctor_summary; then
  echo "Fix the failures above, then re-run this doctor."
  exit 1
fi
```

Output:

```
=== my-skill doctor ===

Dependencies:
  [OK]   jq (jq-1.7.1)
  [OK]   perl (This is perl 5, version 34, ...)

Configuration:
  [OK]   /Users/me/.claude/settings.json
  [WARN] MY_TOKEN not set -- remote checks skipped
         export MY_TOKEN=... then re-run

=== Results: 3 passed, 1 warning(s), 0 failure(s) ===
```

## Functions

| Function | Prints | Counts |
|---|---|---|
| `doctor_section "Title"` | blank line (after the first section) + `Title:` | - |
| `doctor_pass msg` | `  [OK]   msg` | pass |
| `doctor_warn msg` | `  [WARN] msg` | warning |
| `doctor_fail msg` | `  [FAIL] msg` | failure |
| `doctor_note msg` | `         msg` (hint under the previous line) | - |
| `doctor_require_cmd NAME ["hint"]` | pass with the first line of `NAME --version` when available, else fail + hint | pass / failure |
| `doctor_require_file PATH ["hint"]` | pass if `PATH` exists, else fail + hint | pass / failure |
| `doctor_summary` | blank line + `=== Results: N passed, N warning(s), N failure(s) ===` | returns 1 if failures > 0, else 0 |

Counters are the shell variables `DOCTOR_PASS`, `DOCTOR_WARN`, `DOCTOR_FAIL`.

## Composition

A doctor that wraps another doctor (for example `skills/pr-review/scripts/doctor.sh`
runs `shared/github-ops/doctor.sh` first) runs the inner doctor as a
subprocess. The inner doctor prints its own `=== Results ===` line; the outer
doctor keeps the inner exit code, summarizes only its own extra checks, and
fails if either failed. Counters are per process and are not merged.

## Consumers

Skills link the module as `skills/<skill>/shared/doctor -> ../../../shared/doctor`
and source `"$SCRIPT_DIR/../shared/doctor/doctor-lib.sh"` from `scripts/doctor.sh`.
Modules under `shared/` source it as `"$SCRIPT_DIR/../doctor/doctor-lib.sh"`.

Adopted by: `shared/github-ops/doctor.sh`, `shared/secrets/doctor.sh`,
`skills/pr-review`, `skills/pr-watch-auto`.
`skills/panel-review` and `shared/vendors` keep their
table-style doctors.
