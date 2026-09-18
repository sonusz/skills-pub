# compact-skill

Compresses a `SKILL.md` so it costs fewer context tokens on every turn the skill is active, without dropping information. It extracts executable blocks into `scripts/`, moves branch-only prose into `references/`, tightens the remaining text and collapses repeated rules, then asks a multi-vendor panel (the sibling `panel-review` skill) whether the original can still be reconstructed from the compressed version. If the target skill ships evals with assertions, a regression grader compares behavior before and after.

## When to use it

- The skill has stabilized and its body is expensive enough to pay back the work. Body tokens are re-sent on every turn a skill is active, so savings compound with turn count.
- Long-running skills (multi-turn loops, polling, subagents), where the body is re-billed every turn.
- Frequently triggered skills, where the cost compounds across sessions.
- A body over 10,000 characters (about 2,500 tokens), or one containing embedded executable blocks, or gated branch sections such as AWS/GCP variants, optional modes, or troubleshooting.

## When not to use it

- You are still iterating on the skill. Compaction fights the development loop.
- The body is already under 6,000 characters (about 1,500 tokens) with no code blocks or conditional branches.
- One-shot skills where the body loads once per session and the skill ends.

## Requirements

- The `panel-review` skill installed next to `compact-skill`. The frontmatter declares it as a required skill, resolved as a skill directory, and `skills/panel-review` inside this skill is a relative symlink to `../../panel-review`. Verification (workflow step 6) runs through it, so `panel-review` needs its own prerequisites: a `vendors.yaml` (or the tracked `sample-vendors.yaml`) with at least two working panel vendors plus a synthesis call, checked by its `scripts/doctor.sh`. See `../panel-review/`.
- Standard shell tools: `wc -c` for the size measurement and `cp` for the backup. Extracted scripts are syntax-checked with the matching tool (`bash -n`, `python -m py_compile`, and so on), so those interpreters must be present for the languages you extract.
- No API key. Sizes are measured in characters as a proxy for tokens precisely so the Anthropic count-tokens API does not become a dependency.
- Step 7 (regression check) needs an agent runtime that can spawn subagents, and a target skill with `evals/evals.json` containing non-empty `assertions`. Without that file the step is skipped and the panel is the sole verification.
- `references/grader.md` is described in its own text as a scoped adaptation of the `agents/grader.md` in Anthropic's `skill-creator` plugin (available on the official Claude Code plugin marketplace). It borrows the role, inputs, per-assertion PASS/FAIL process, evidence rules and `grading.json` output shape, and drops claim extraction, eval critique, `user_notes.md` handling and metrics aggregation. The plugin does not have to be installed; the reference only points to it if you need the omitted features.

## Install

Clone the repository and symlink the skill directory. Install `panel-review` the same way, as a sibling.

Claude Code:

```bash
REPO=/path/to/skills-pub
mkdir -p ~/.claude/skills
ln -s "$REPO/skills/compact-skill" ~/.claude/skills/compact-skill
ln -s "$REPO/skills/panel-review"  ~/.claude/skills/panel-review
```

Codex CLI:

```bash
REPO=/path/to/skills-pub
mkdir -p ~/.codex/skills
ln -s "$REPO/skills/compact-skill" ~/.codex/skills/compact-skill
ln -s "$REPO/skills/panel-review"  ~/.codex/skills/panel-review
```

Link, do not copy. `skills/compact-skill/skills/panel-review` is a relative symlink to `../../panel-review`, which resolves inside the clone as long as the skill directory itself is a symlink. If you copy the directory instead, that link dangles and you have to replace it with a copy of `REPO/skills/panel-review` (which in turn has its own `shared/` symlinks to materialize).

## Usage

Ask the agent to compact or compress a skill and name the `SKILL.md` to work on. The skill description that triggers it reads: "Compress a SKILL.md without losing detail." There are no scripts or flags of its own; the agent follows the workflow in `SKILL.md` directly.

You supply the path to the skill directory containing the `SKILL.md`. Expect one approval prompt during step 6, because `panel-review` shows the review question, cwd, artifact list and configured vendor calls and waits for explicit approval before launching.

What comes back:

- A rewritten `SKILL.md`, with YAML frontmatter and top-level headings unchanged.
- New files under the target skill's `scripts/` and `references/` for any code or branch-only prose that was extracted.
- The panel verdicts (PASS or FAIL per model) and, when evals exist, the per-assertion regression comparison.
- A final size report in the form `{original_chars} → {compressed_chars} chars — ≈{token_pct}% fewer tokens per turn the skill is active (baseline ≈{baseline_tokens} tok → compacted ≈{compacted_tokens} tok, chars/4)`. The `SKILL.md.bak` backup is removed at the end.
- If verification still fails after three cycles, the original `SKILL.md` is restored from the backup and the newly created `scripts/` and `references/` files are deleted.

## How it works

`SKILL.md` defines four methods and an eight-step workflow that applies them in order.

Methods: (1) extract scripts, keeping only a one-line description, usage, preconditions and non-obvious rationale in `SKILL.md`; (2) move branch-specific prose to `references/<topic>.md` with a one-line pointer that states when to read it; (3) tighten prose by merging overlapping rules, using structured blocks, and removing hedging, while keeping every fact and every illustrative example; (4) deduplicate shared rules into one unconditional authoritative section placed above all reference points.

Workflow:

1. **Measure.** `wc -c` on `SKILL.md`, recorded as the baseline. Characters track tokens at roughly 4 chars per token.
2. **Extract.** Back up to `SKILL.md.bak`. Find executable code blocks of 5 or more lines, skipping illustrative examples, config snippets and output samples. Move each into `scripts/<name>.<ext>` with a usage comment, replace it with a one-line call, update surrounding prose, syntax-check, and rename on name collision.
3. **Move references.** Find conditional headings ("If X", "For Y", "Optional", "Troubleshooting") and sibling variants. Move each into `references/<topic>.md` (with a table of contents if over 300 lines) and leave a pointer that names the trigger condition.
4. **Tighten.** Apply method 3 to prose sections. Frontmatter and top-level headings are not changed.
5. **Deduplicate.** Apply method 4.
6. **Verify.** Keep the backup and the compressed file as separate files under one local audit root and run `panel-review` with that directory as `--cwd`. The prompt holds only the two paths, their sizes or hashes, and the question "Does the compressed version allow you to reconstruct all operational steps from the original without guessing?" with a PASS/FAIL answer. Neither body is pasted into the prompt; each model reads both files itself. Pass requires PASS from all models. On FAIL, restore the flagged detail, re-compress differently and re-verify, at most 3 cycles, then revert.
7. **Regression check (optional).** Only when the target skill has `evals/evals.json` with non-empty `assertions` and `SKILL.md.bak` is still present. For each eval, two subagents run the same prompt, one against the baseline and one against the compacted file. Each run is graded per assertion with `references/grader.md`, which writes `grading.json` with `text`, `passed` and `evidence` fields. Pass means every assertion that passed under baseline also passes under the compacted version. Failures are fixed by restoring detail and re-running steps 6 and 7, at most 3 cycles, then revert as in step 6.
8. **Measure again.** `wc -c` on the result, report the size line above, and `rm SKILL.md.bak`.

## Files

| Path | Purpose |
|---|---|
| `SKILL.md` | The skill: when to use it, the four compaction methods, and the eight-step workflow including panel verification and the optional regression check |
| `references/grader.md` | Per-assertion PASS/FAIL grading pattern used by step 7; inputs, process, `grading.json` output format, and what it omits relative to `skill-creator`'s grader |
| `skills/panel-review` | Relative symlink to `../../panel-review`, the sibling skill that runs the step 6 verification |
