# panel-review

Sends one review prompt to several LLM vendor CLIs in parallel, then runs a
separate synthesis call that reports where the vendors agree and where they
diverge. The reviewers read the files under a directory you name; the prompt
itself carries only the question and a path manifest.

## When to use it

From the skill description: reviewing a spec, config, policy, or architecture
decision whose correctness cannot be verified by execution and whose cost of
error is high. The value is the divergence signal: the run tells you whether
several models read the same artifact the same way. Trigger phrases are
"panel review", "cross-vendor review", "multi-model check", "second opinion
from the other models", "sanity-check this spec/config", or `/panel-review`.

## When not to use it

- Problems that tests or execution can answer. Run those instead.
- Sourcing authority for a decision ("the panel said X, so build X"). The
  skill refines an existing artifact; it does not choose what to build.
- Anything where you would not act on disagreement. The workflow stops
  unless you have named the action you will take if the vendors disagree.
- Prompts that ask reviewers to edit, commit, install, or run destructive
  commands. Reviewers run with tool access for read-only inspection.

## Requirements

- Linux or macOS. The shared scripts stay compatible with the macOS system
  Bash 3.2, so no newer bash is required.
- At least two of these vendor CLIs on `PATH` and logged in, plus one for the
  synthesis call (it can be one of the same two):

  | vendor id | CLI binary | login |
  |---|---|---|
  | `openai` | `codex` | `codex login` |
  | `claude` | `claude` | `claude login` |
  | `agy` | `agy` | `agy login` |
  | `grok` | `grok` | `grok login` |
  | `cursor` | `cursor-agent` | `cursor-agent login` or `CURSOR_API_KEY` |

  The synthesis vendor should be `claude`, `openai`, or `grok` (the shared
  catalog notes synthesis needs schema-constrained output).
- `python3` for `init-vendors.py` and for `grok` calls.
- `perl` for the secrets scan the workflow runs before launch
  (`shared/secrets/scan.sh`).

## Install

Symlink the skill directory from your clone; the `shared/vendors` and
`shared/secrets` entries inside it are relative links to `../../../shared/`,
so they resolve only while the skill lives inside the checkout.

```bash
REPO=~/git/skills-pub                 # your clone
ln -sfn "$REPO/skills/panel-review" ~/.claude/skills/panel-review   # Claude Code
ln -sfn "$REPO/skills/panel-review" ~/.codex/skills/panel-review    # Codex
```

If you copy the directory instead of linking it, replace `shared/vendors` and
`shared/secrets` in the copy with real copies of the modules from `shared/`.
A copy does not receive updates when you pull.

## Configure vendors

Two files in the skill directory control which vendors run:

- `sample-vendors.yaml` is tracked by git and lists every vendor the skill
  can call, with a model and effort per entry.
- `vendors.yaml` is git-ignored and machine-local: the sample pruned to what
  works on this machine. The scripts read it first and fall back to the
  sample (every vendor) when it is absent.

Generate the local file, then probe it:

```bash
python3 shared/vendors/scripts/init-vendors.py \
  --sample skills/panel-review/sample-vendors.yaml \
  --out    skills/panel-review/vendors.yaml
bash skills/panel-review/scripts/doctor.sh      # then delete entries that fail
```

`init-vendors.py` keeps an entry when its CLI binary is on `PATH`;
`--vendor openai --vendor claude` keeps an explicit list instead, `--force`
overwrites an existing output, and `--out -` prints to stdout. If the synthesis
vendor was pruned, the script points synthesis at the first kept panel entry.
It exits 1 when fewer than two panel entries survive, because the scripts
refuse a one-vendor panel.

`scripts/doctor.sh [--output-dir DIR] [--keep-output] [vendors_yaml]` sends
"reply with the single word READY" to every configured call in parallel with a
60 second timeout (`PANEL_DOCTOR_TIMEOUT` overrides it) and exits 0 only when
at least two panel calls and the synthesis call answer. On failure it keeps its
diagnostics directory and points to `shared/vendors/TROUBLESHOOTING.md`.

Inside the yaml, `panel` is a list of entries, each with `id`, `vendor`,
`effort`, and `model`. Every panel entry receives the same prompt in parallel.
`synthesis` is a single entry with the same keys; it receives the prompt plus
all panel outputs and writes the comparative report. Effort uses the shared
scale `min, low, medium, high, xhigh, max`; Cursor ignores it and takes effort
from the model SKU instead. Vendor ids, binaries, and known model ids are
catalogued in `shared/vendors/sample-vendors.yaml`, which is itself a valid
config: `scripts/doctor.sh shared/vendors/sample-vendors.yaml` probes all of
them.

## Usage

From Claude Code or Codex, ask for a "panel review" (or any trigger phrase
above) of a concrete artifact with a specific question, and say what you will
do if the vendors disagree. The agent runs the SKILL.md workflow: preflight,
prompt construction, an explicit approval checkpoint listing the question,
cwd, artifact paths, exclusions, and configured calls, then launch and
synthesis.

The scripts can also be run directly. The prompt must be a file, and the
output directory must be outside the reviewed git worktree.

```bash
RUN_DIR=$(mktemp -d /tmp/panel-review.XXXXXX)
REVIEW_CWD=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
scripts/launch.sh --cwd "$REVIEW_CWD" prompt.txt vendors.yaml "$RUN_DIR"
scripts/synthesize.sh prompt.txt vendors.yaml "$RUN_DIR"
cat "$RUN_DIR/synthesis/out"
rm -rf "$RUN_DIR"
```

`--cwd` is mandatory (`PANEL_REVIEW_CWD` in the environment is the accepted
alternative; the flag wins). `--repo` is accepted as a no-op for old callers;
there is no inline mode, so `--inline` is rejected. Pass
`sample-vendors.yaml` in place of `vendors.yaml` to try every vendor.

Environment knobs: `PANEL_CALL_TIMEOUT` (default 300 seconds per panel call),
`PANEL_CALL_TIMEOUT_EXTEND` (default 300; a call still producing output at the
deadline gets another window, `0` disables), `SYNTHESIS_CALL_TIMEOUT` and
`SYNTHESIS_CALL_TIMEOUT_EXTEND` (same defaults for the synthesis call), and
`PANEL_IDLE_PROBE_VENDOR` with optional `PANEL_IDLE_PROBE_MODEL` and
`PANEL_IDLE_PROBE_EFFORT`, which let a cheap model decide whether a silent
call is extended or killed.

## How it works

1. `launch.sh` reads the panel ids from the config and exits if fewer than two
   are configured. It resolves `--cwd`, rejects an output directory inside
   that git worktree, and snapshots `git status`, unstaged and staged diffs,
   and HEAD under `$RUN_DIR/.repo-state/before.*`.
2. Each panel entry is launched in the background through
   `shared/vendors/scripts/call.sh` with the entry's vendor, effort, and
   model, plus `--cwd`, `--yolo`, the timeout flags, and `--prompt-file`.
   The shared module maps `--yolo` to each CLI's permission-bypass flag.
3. Every call writes `$RUN_DIR/<id>/out` (the review), `log`, `call.log`,
   and a `status` file with `id`, `kind=panel`, `vendor`, `cwd`, `exit_code`,
   and file paths. A failed call is marked failed and never substituted.
4. After all calls return, the repo state is snapshotted again. Any change
   fails the launch and nothing is reverted. The launch also fails unless at
   least two calls exited 0 with non-empty `out`.
5. `synthesize.sh` assembles a synthesis prompt in a temp directory: fixed
   instructions (classify each output as successful or failed before
   extracting consensus and divergence; recommendations must be procedural),
   the exact output template, the original prompt, and each panel output
   with its status file. It sends that to the `synthesis` entry via the same
   `call.sh` and writes `$RUN_DIR/<synthesis id>/out` (`synthesis/out` with
   the shipped config) plus `log`, `call.log`, and `status`.
6. The report has a `Vendors:` line with per-vendor status, then
   `## Consensus`, `## Divergence`, and `## Recommendations`.

`scripts/panel-config.sh` is the shared library (config parsing and the
`vendors.yaml` fallback). Another skill can preset `PANEL_SKILL_DIR` before
sourcing it to reuse the launcher with its own config and `shared/vendors`.

## Files

| Path | Purpose |
|---|---|
| `SKILL.md` | Agent instructions: usage boundaries, guardrails, four-step preflight, prompt rules, approval checkpoint, launch and synthesis steps. |
| `sample-vendors.yaml` | Tracked catalog of every vendor entry the skill can run. |
| `vendors.yaml` | Generated, git-ignored, machine-local pruning of the sample. Read first when present. |
| `scripts/panel-config.sh` | Sourced library: yaml parsing, default config resolution, `call.sh` argument building. |
| `scripts/launch.sh` | Runs the panel calls in parallel with repo-state guard and success threshold. |
| `scripts/synthesize.sh` | Builds the synthesis prompt from panel outputs and runs the synthesis call. |
| `scripts/doctor.sh` | Readiness probe for every configured call. |
| `shared/vendors` | Relative symlink to the shared vendor CLI module (`call.sh`, catalog, troubleshooting). |
| `shared/secrets` | Relative symlink to the shared secrets scanner used before launch. |
| `tests/smoke.sh` | Offline smoke test: runs doctor, launch, and synthesize against fake `codex`, `claude`, `agy`, `grok`, and `cursor-agent` binaries. |
