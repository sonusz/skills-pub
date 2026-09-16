---
name: panel-review
description: >
  Runs one prompt through multiple configured model vendors in parallel and
  synthesizes a comparative review that highlights where they agree and, more
  importantly, where they diverge. Use when reviewing a spec, config, policy,
  or architecture decision whose correctness can't be verified by execution and
  whose cost of error is high. Triggers on "panel review", "cross-vendor
  review", "multi-model check", "second opinion from the other models",
  "sanity-check this spec/config", or explicit `/panel-review`. Do not trigger
  for problems answerable by running tests or to source authority for a
  decision.
---

# Panel Review

## Usage Boundaries

Value comes from **divergence**, not consensus. If you won't act on disagreement, don't run panel review — it's a gate, not a generator.

### Use for

| Scenario | Why |
|----------|-----|
| Spec ambiguity ("Do 3 models read this spec the same way?") | Divergence reveals under-defined specs before implementation bugs |
| High cost of error ("Is this config safe before deploying?") | Different training biases surface different failure modes |
| No ground truth ("Is this architecture sound?") | No test can answer — epistemic diversity is the best proxy |

### Do not use for

| Anti-pattern | Rule |
|-------------|------|
| Authority sourcing ("Panel says X, so build X") | Output never drives ideation/prioritization/selection — refining artifacts is fine, choosing what to create is not. (Includes the circular variant: review guides creation, then cited as validation.) |
| Verifiable problems | If execution can answer it, run that instead (enforced by preflight #3). |

### Reading results

- **Divergence = the signal.** Investigate why models disagree.
- **Consensus ≠ validation.** Vendors share training data, cutoffs, and upstream inputs — they can agree on the same mistake. Unanimous concerns are actionable; unanimous approval is not proof.
- **Outlier = investigate.** When 2 agree and 1 disagrees (or only 2 vendors successful and they disagree), read the outlier's output carefully and decide whether to surface it — not launch an autonomous deep-dive. The outlier may have caught something or misunderstood the prompt; both are worth knowing.

## Guardrails

- Path-based discovery gives reviewers real repo/tool access through `--cwd` +
  `--yolo`; use it only for read-only review. Do not ask panel vendors to edit
  files, commit, push, install dependencies, apply infrastructure, or run
  destructive commands.
- Materialize every reviewed artifact under a local repo/source root before
  launch. Never paste an artifact body, source excerpt, diff hunk, or config
  fragment into the panel prompt. If the artifact cannot be made locally
  readable, stop instead of falling back to inline review.
- Never send credentials, tokens, `.env` contents, or production secrets to
  panel vendors. Reviewers read manifested files themselves, so a secret inside
  one reaches every vendor even though the prompt holds only paths. Before
  launch run `bash shared/secrets/scan.sh <manifest paths...>` (or
  `bash shared/secrets/scan.sh --diff < diff.patch` when the input is a diff);
  it prints `path:line:pattern` per hit and exits 1. On any hit either exclude
  that file from the manifest (and list it under redactions/exclusions at the
  approval checkpoint) or stop and ask. A clean scan is a denylist result, not
  a guarantee -- still exclude files you know hold key material.
- In git worktrees, `launch.sh` snapshots status and diffs before and after
  panel calls. If the workspace changes, stop and report it. Do not
  auto-revert or commit unless the user explicitly asks.
- Keep local-machine setup such as SSL certificates, proxies, auth refreshes,
  and vendor sandbox workarounds outside this skill source and outside the
  vendor config. Which vendors run on this machine is the one thing that IS
  machine-local: the tracked `sample-vendors.yaml` lists every vendor, and the
  git-ignored `vendors.yaml` is that sample pruned to what works here
  (`python3 shared/vendors/scripts/init-vendors.py --sample sample-vendors.yaml --out vendors.yaml`,
  then delete entries the doctor fails). Scripts fall back to the sample when
  `vendors.yaml` is absent.

## Workflow

### 1. Preflight — all four must pass

1. **Artifact + specific review question defined?** Concrete artifact (code, spec, config, policy) and a specific question. Missing either → STOP. Panel review is a gate, not a generator.
2. **Action on disagreement defined?** One of: refine & retry, reject, investigate & verify, escalate to human, accept with documented risk. Vague → STOP. Without a planned action, divergence just produces noise.
3. **Answerable by execution?** YES → STOP, run the tests. Partially → execute what you can, panel-review the rest. NO → proceed.
4. **At least 2 panel calls and the synthesis call ready?** Run `scripts/doctor.sh` (it reads `vendors.yaml`, or `sample-vendors.yaml` when no local file exists) — **once per session only**. If the doctor already passed earlier in this conversation, skip this step and proceed; vendor CLIs rarely come and go mid-session, so re-checking each invocation is wasted effort. With only one panel vendor you have a single-model run, not a panel — no divergence signal is possible. If fewer than 2 panel calls or the synthesis call are ready, STOP and report the failing configured calls. Doctor failures keep diagnostics and point to `shared/vendors/TROUBLESHOOTING.md`.

### 2. Build the prompt

One prompt for all vendors. Use **path-based discovery only**:

1. Choose the narrowest local repo/source root that contains the review inputs.
2. Materialize external, generated, deleted, or historical inputs under that
   root (or a dedicated temporary audit root).
3. Put only the review question, root/path manifest, sizes, and hashes in the
   prompt.
4. Tell reviewers to inspect the files themselves with their available tools.

Do not paste reviewed content into the prompt, even when it is small. If a
required file cannot be read, the reviewer must report a failed or risky review
rather than infer from the manifest. Keep panel output directories outside the
audit root.

Strip credentials and tokens before sending: run `bash shared/secrets/scan.sh` over the manifest paths (or `--diff < diff.patch`) and exclude or stop on hits, as the Guardrails require. Panel members cannot prompt the user mid-run, so the orchestrator owns security up front.

For env-specific launcher quirks (SSL certs, proxy vars, per-vendor flags), check your runtime's user-level notes or memory file — never bake machine-specific setup into skill source.

Write the finalized prompt to a file (e.g., `/tmp/panel-prompt.XXXX.txt`) — `launch.sh` reads from disk so all panel calls see identical bytes regardless of shell quoting.

### 3. Approval checkpoint — explicit, before launch

The configured panel calls run with repo/tool access from the selected
`--cwd`. Panel outputs and the original manifest-only prompt go to the
configured synthesis call. Before launching, show the user: (1) review
question, (2) repo/source cwd, (3) artifact list (paths + sizes, not full
text), (4) redactions/exclusions applied, and (5) configured calls from
`vendors.yaml` (or the sample fallback). Wait for explicit approval. Re-ask when the prompt materially
changes (different artifact, question, cwd, redactions, or added vendor).
Minor reformatting does not need re-approval.

### 4. Launch panel calls in parallel

The configured calls live in `vendors.yaml` (machine-local; see [sample-vendors.yaml](sample-vendors.yaml) for every vendor): the `panel` calls and one `synthesis` call, each with vendor/model/effort settings. Vendor CLI differences are handled by the packaged module at `shared/vendors`; panel-review should not duplicate vendor-specific CLI flags or quirks.

A configured call that fails mid-run is marked failed and never substituted —
substitution would silently weaken the divergence signal. `launch.sh` aborts
if fewer than 2 panel outputs succeed. Each vendor call has a **5 minute**
timeout by default via `PANEL_CALL_TIMEOUT=300`; override with
`PANEL_CALL_TIMEOUT=<seconds>` only for a focused prompt. The timeout kills
only stalled vendors: if a vendor is still producing output when the deadline
hits, the call is extended in 5-minute windows (`PANEL_CALL_TIMEOUT_EXTEND=300`,
`0` disables) and killed only after a full window with no new output. To let a
cheap model judge a silent call before it is killed, set
`PANEL_IDLE_PROBE_VENDOR=<vendor>` (optionally `PANEL_IDLE_PROBE_MODEL`,
`PANEL_IDLE_PROBE_EFFORT`); `launch.sh` and `synthesize.sh` forward these as
`call.sh --idle-probe-*` flags, and the probe reads the process tree and output
tails and answers extend-or-kill (any probe failure still kills; see
`shared/vendors/README.md`, "Idle Probe").

Path-based discovery drives `shared/vendors/scripts/call.sh` with
`--cwd <repo/source>` and `--yolo` for each panel vendor. The shared wrapper
maps that access per vendor: Codex gets
`--dangerously-bypass-approvals-and-sandbox`, Claude gets
`--permission-mode bypassPermissions`, Agy gets
`--dangerously-skip-permissions`, Grok gets `--yolo`, and Cursor gets
`--trust` plus `--yolo`. `--cwd` is honored by Codex via `--cd`, by Grok via
native `--cwd`, and by Claude/Agy/Cursor through the wrapper's cwd execution.

Keep `$RUN_DIR` outside the reviewed git worktree. `launch.sh`
rejects in-worktree output dirs, then records git status/diff snapshots under
`$RUN_DIR/.repo-state` before and after panel calls. Any tracked, staged, or
status-visible workspace change fails the launch and is not reverted
automatically.

Each call writes `$RUN_DIR/<id>/out`, `$RUN_DIR/<id>/log`, `$RUN_DIR/<id>/status`, and `$RUN_DIR/<id>/call.log`. Use `out` for review content; use `status`, `log`, and `call.log` only when diagnosing failed or slow vendor calls.

```bash
RUN_DIR=$(mktemp -d /tmp/panel-review.XXXXXX)
echo "RUN_DIR=$RUN_DIR"   # echo so the agent captures the path for §5
REVIEW_CWD=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
scripts/launch.sh --cwd "$REVIEW_CWD" <prompt_file> vendors.yaml "$RUN_DIR"
# §5 runs the configured synthesis call next.
# Clean up with `rm -rf "$RUN_DIR"` after synthesis — don't `trap ... EXIT`,
# the trap fires when the shell that ran launch.sh exits and some runtimes
# execute §4 and §5 in separate shells, which would delete the
# outputs before the synthesizer reads them.
```

Pass `sample-vendors.yaml` (every vendor) in place of `vendors.yaml` if you
have not generated the local file yet — the doctor/launch default falls back
to the sample automatically only when the config argument is omitted.

`--cwd` is mandatory (`PANEL_REVIEW_CWD` in the environment is accepted as the
same explicit setting; the flag wins when both are set). `--repo` is accepted
as a no-op for callers written against the old two-mode launcher. `launch.sh`
has no inline mode.

### 5. Synthesize with the configured synthesis call

The synthesis call receives the original manifest-only prompt plus all panel
outputs and writes `$RUN_DIR/synthesis/out` by default:

```bash
scripts/synthesize.sh <prompt_file> vendors.yaml "$RUN_DIR"
cat "$RUN_DIR/synthesis/out"
```

As in §4, pass `sample-vendors.yaml` here if you have not generated
`vendors.yaml` yet — the default falls back to the sample only when the
argument is omitted.

The synthesis prompt requires this format:

```
━━━ Panel Review ━━━
Task: {description}
Vendors: {vendor_a} ✅ | {vendor_b} ✅ | {vendor_c} ✅ | {vendor_d} ✅

## Consensus
{Shared findings, including shared concerns}

## Divergence
{Where they disagree, labeled by vendor — or "None"}

## Recommendations
{Process actions to resolve divergence or address unanimous concerns}
━━━ End ━━━
```

**The synthesis call must classify each vendor's output before extracting consensus/divergence:**

| Classification | Criteria |
|---------------|----------|
| **Successful** ✅ | Non-empty AND substantively engages the review question. Refusal, policy block, truncated, or off-topic output → **failed** ❌ with one-line reason (not divergent — non-responsive output ≠ substantive disagreement). |
| **Codex fallback** | a panel `out` begins with `[FALLBACK:` → degraded (one-line summary). Note in synthesis; does not count toward the 2-success minimum. |

**Minimum 2 successful panel outputs** required. Fewer → do not synthesize; report each failure reason and stop.

**Recommendations are procedural only** — clarify, verify, test, escalate, or document risk. Do not recommend product/architecture choices from panel output alone (authority-sourcing anti-pattern, §Usage Boundaries).
