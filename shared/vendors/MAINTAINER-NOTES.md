# Vendors module — maintainer notes

This folder is the canonical home of the `shared/vendors` module
shared across skills (auto-dev-sdk, alpha-miner-sdk, panel-review,
others). The module wraps Claude / Codex / Agy CLIs behind a
uniform `call.sh` + `vendor-launch.sh` interface so callers do not
have to know vendor-specific argument shapes.

## Where this code actually runs

The live `autodev` CLI resolves `shared/vendors` via
`SDK_ROOT / "shared" / "vendors"` in `autodev/vendors/shared_call.py`.
In this repo, skill-local `shared` folders are symlinks to the repo-level
`shared/` directory, so `shared/vendors` is the canonical source of truth
for panel-review, feature-spec, and auto-dev-sdk.

## Known divergence (snapshot 2026-04-30)

The two copies have drifted because fixes were applied in different
repos and never fully cross-ported. Keep this list in sync with
reality whenever a patch is applied.

### Fixes in `Downloads/shared/vendors/` only (NOT in auto-dev-sdk)

These are needed for **macOS compatibility** and **input
robustness**. They should be ported into `auto-dev-sdk/shared/`.

| Patch | Location | Symptom this fixes |
|---|---|---|
| `vendors_lower()` portable helper + replace `${var,,}` | `vendor-launch.sh`, `call.sh` | macOS ships bash 3.2; `${var,,}` is bash 4+ → unbound variable / syntax error on macOS |
| `script -q /dev/null <file>` BSD form with Linux fallback | `vendor-launch.sh` | macOS `script` uses different argument syntax than util-linux `script`; running on Linux without fallback breaks |
| Strip extra control chars (`\004` EOT, `\010` BS) | `vendor-launch.sh` | macOS `script` injects EOT/BS bytes into transcript; otherwise vendor JSON parsing chokes |
| Line-by-line JSON extraction fallback | `vendor-launch.sh` | When vendor stdout is a mix of log lines + JSON (newer Claude / Codex versions print progress lines first), bare `json.loads(raw_out)` fails; Downloads scans lines in reverse for the last `{...}` line |
| `set -eo pipefail` (drop `-u`) | `call.sh` | bash 3.2 + complex array indexing false-flags as unbound — relaxing nounset is a known macOS-compat workaround |
| Manual array counting instead of `declare -A` | `call.sh` | Bash 3.2 has no associative arrays |

### Fixes in `auto-dev-sdk/shared/vendors/` only (NOT in Downloads)

These were applied as part of harness debugging and need to be
back-ported to this canonical folder.

| Patch | Location | Symptom this fixes |
|---|---|---|
| `vendors.conf` model defaults, including an empty `agy.model` to use agy's configured default | `vendors.conf` | Avoids pinning a display-name model that may not exist on every Agy account |

### Recently ported to Downloads (now in both)

| Patch | Ported on | Notes |
|---|---|---|
| `structured_output` envelope preservation when `--json-schema` is passed (`vendor-launch.sh`) | 2026-04-30 | Coexists with the line-by-line JSON extraction fallback already in Downloads — both run on the same `data` dict |
| Unified `--schema-file` shared option in `call.sh` + codex envelope wrap in `vendor-launch.sh` | 2026-04-30 | Replaces vendor-specific `--json-schema`/`--output-schema` plumbing at call sites. Output is `{"structured_output": <obj>}` for both `claude` and `openai`; `agy` is rejected (no native schema enforcement). Auto-dev-sdk synth no longer pinned to claude. |

### Other observed file diffs

`README.md`, `TROUBLESHOOTING.md`, `doctor.sh`, `hello-test.sh`,
`nested-test.sh`, `smoke-test.sh` also differ — those are mostly
the `vendors_lower()` / bash-3.2 form propagated through helper
scripts. Same root cause (macOS-compat).

## Sync direction

The right end-state is **full unification both ways** — every
divergence above is a real fix that the other side should adopt.
Quick recipe (run from `auto-dev-sdk/`):

```sh
# 1. Capture diffs as patches first (don't blind-overwrite).
diff -ruN \
  skills/auto-dev-sdk/shared/vendors \
  shared/vendors > /tmp/vendors-diff.patch

# 2. Hand-apply each hunk, choosing the macOS-compat form for shell
#    constructs and the structured_output / current-model form for
#    behavior + config. Both sides must end up with both fixes.

# 3. Run the test scripts to confirm:
bash shared/vendors/scripts/doctor.sh
bash shared/vendors/scripts/smoke-test.sh

# 4. Copy unified result to the other location. NEVER blind-rsync —
#    always confirm both sides have all known fixes first.
```

Do NOT cron a one-way `rsync` between these two folders. That has
caused regressions before (whichever side gets overwritten loses
its fixes).

## Verifying a patch is in place

Quick spot-checks for the most load-bearing fixes:

```sh
# structured_output preservation present?
grep -q 'structured_output' shared/vendors/scripts/vendor-launch.sh && echo OK

# unified --schema-file option present?
grep -q 'VENDORS_SCHEMA_FILE' shared/vendors/scripts/call.sh && echo OK

# macOS bash-3.2 helper present?
grep -q 'vendors_lower' shared/vendors/scripts/vendor-launch.sh && echo OK

# vendors.conf models current?
grep -E 'openai.model=gpt-5\.5|agy.model=' shared/vendors/vendors.conf
```

## Common bug signatures and the fix that covers each

When a vendor call misbehaves, match the symptom against this
table before chasing it from scratch:

| Symptom | Likely missing fix |
|---|---|
| Panel synthesizer reads 0-byte input → "synth failed: empty" | `structured_output` envelope preservation |
| `unbound variable` / `${1,,}: bad substitution` in vendor-launch | `vendors_lower()` macOS-compat helpers |
| Vendor call JSON parse error on log-noisy stdout | line-by-line JSON extraction fallback |
| Stray binary chars (`^D`, `^H`) in vendor output | extra control-char strip in vendor-launch |
| `script: invalid option` on macOS | BSD `script` argument form with Linux fallback |
| Vendor returning gpt-5.4 deprecation warning | stale `vendors.conf` |
| Agy rejects `--model` | configured value is not an exact display name from `agy models`; leave `agy.model=` empty to use its configured default |
| Agy print mode pauses for permissions | use `--yolo` only for an explicitly approved workflow; it maps to `--dangerously-skip-permissions` |

## Why this folder exists at all

`shared/vendors` is reused across skills that should not depend on
each other. Keeping a canonical copy outside any single skill
repo makes the dependency graph explicit (each skill consumes
`shared/vendors` rather than the skill-tree being a circular mesh).
But once a skill (auto-dev-sdk) is installed and starts loading the
module from inside its own tree, the canonical copy can drift if
nobody bothers to back-port. This file exists so that drift is
visible and fixable rather than rediscovered every six weeks.

Update this file whenever a vendor patch is applied. Stale notes
are worse than no notes — if you fix a bug, list it in the relevant
"in X only" table above (or, after porting, remove from both).
