---
name: compact-skill
description: >
  Compress a SKILL.md without losing detail. Extract scripts, tighten
  prose, merge redundancy, then panel-review to verify no information loss.
requires:
  - name: panel-review
    used_in: verification step (workflow step 6)
    resolve: skill directory
---

# Compact Skill

Reduce context window cost of a skill. Agents read SKILL.md on activation but execute `scripts/` on demand — extracted code costs zero context tokens until called.

- **Extract scripts** → defers tokens to execution time
- **Move branch-specific prose to `references/`** → defers tokens until a branch needs the detail
- **Tighten prose** → removes tokens entirely
- **Deduplicate shared rules** → collapses repeated content into one block with cross-references

## When to Use

Apply after the skill has stabilized AND the body cost is high enough to pay back the work. SKILL.md body tokens are re-sent on every turn a skill is active, so savings compound with turn count.

**Good candidates:**
- Long-running skills (multi-turn loops, polling, subagents) — body tokens re-billed every turn
- Frequently triggered skills — cost compounds across sessions
- Body >10,000 chars (~2,500 tokens), or containing embedded executable blocks, or gated branch sections (AWS/GCP, optional modes, troubleshooting)

**Skip:**
- Still iterating on the skill — compaction fights the development loop
- Body already <6,000 chars (~1,500 tokens) with no code blocks or conditional branches
- One-shot skills where the body loads once per session and the skill ends

## Methods

### 1. Extract scripts

Move executable code from SKILL.md into `scripts/`. SKILL.md retains only: what it does (one line), how to call it (usage), preconditions, why it matters (only if non-obvious).

```
Before (in SKILL.md):
  ```bash
  RUN_DIR=$(mktemp -d /tmp/foo.XXXXXX)
  eval $(awk -F= ...)
  vendor_a_cli --model ... > "$RUN_DIR/vendor-a.out" &
  vendor_b_cli --model ... > "$RUN_DIR/vendor-b.out" &
  wait
  ```

After (in SKILL.md):
  scripts/launch.sh <prompt_file> <models_conf> <output_dir>
```

Rules:
- Script must be self-contained (args in, files out, POSIX + documented dependencies)
- Quirks and workarounds live in the script as comments, not in SKILL.md

### 2. Move branch-specific prose to `references/`

Move prose sections that are only read on certain workflow branches — cloud variants, language variants, optional modes, troubleshooting guides — into `references/<topic>.md`. Leave a one-line pointer in SKILL.md. Unlike script extraction (executable code), this handles prose that would otherwise bloat every activation of the skill even when that branch isn't taken.

```
Before (in SKILL.md):
  ## AWS deployment
  [50 lines of AWS-specific steps]

  ## GCP deployment
  [50 lines of GCP-specific steps]

After (in SKILL.md):
  ## Deployment
  Pick the target cloud, then read the matching reference:
  - AWS → references/aws.md
  - GCP → references/gcp.md
```

Rules:
- Only extract conditional content (headings like "If X", "For Y", "Optional", "Troubleshooting") or sibling variants (AWS/GCP, Python/Go). Content needed every invocation belongs in SKILL.md.
- Reference file must be self-contained — the reader opens it without re-reading SKILL.md
- Add a table of contents to references >300 lines
- Pointer must state *when* to read, not just *where*, so the agent doesn't defensively load everything
- Don't extract if the pointer + read would cost more tokens than the inlined prose it replaced (small sections aren't worth the round-trip)

### 3. Tighten prose

Rewrite verbose sections into compact form without deleting information.

| Technique | Example |
|-----------|---------|
| Merge overlapping rules | Two rules saying the same thing → one rule with both contexts |
| Structured blocks over prose | Paragraph with 4 constraints → 4-line table/YAML block |
| Remove hedging | "It is important to note that X" → "X" |

Rules:
- Every fact in the original must appear in the compressed version
- If two rules look similar but have different edge cases, keep both
- Never remove illustrative examples — compress by shortening them (executable code blocks are extraction candidates, not examples)

### 4. Deduplicate shared rules

When the same rule appears in multiple sections, extract it into a single authoritative section and replace each occurrence with a reference.

```
Before:
  Phase 3: "Divergence is the signal. Unanimous concerns actionable."
  Phase 4: "Divergence is the signal. Unanimous concerns actionable."

After:
  ## Panel Review Rules (new top-level section)
  - Divergence is the signal. Unanimous concerns actionable.

  Phase 3: "... (see Panel Review Rules)"
  Phase 4: "... Apply Panel Review Rules above."
```

Rules:
- Authoritative section must be unconditional and above all reference points
- Never place behind a conditional — downstream references become ambiguous
- Each reference site must still make sense standalone

## Workflow

### 1. Measure

`wc -c` on SKILL.md. Record as baseline.

Characters are a proxy for the real cost — tokens. English prose and code tokenize at roughly 4 chars/token, so character delta tracks token delta within ~10%. The exact token count would require the Anthropic count-tokens API (free but needs `ANTHROPIC_API_KEY`, which compaction shouldn't force as a dep). `wc -c` is zero-config and close enough for a before/after reduction signal.

### 2. Extract

Back up: `cp SKILL.md SKILL.md.bak`

Scan for executable code blocks ≥ 5 lines. Skip illustrative examples, config snippets, output samples. (Heuristic: block inside a Before/After pair, after "e.g."/"example:", or showing expected output → illustrative.) For each:
- Create `scripts/<name>.sh` (or appropriate extension) with usage comment
- Replace block in SKILL.md with one-line call + brief description
- Update surrounding prose referencing the extracted code
- Syntax check (`bash -n`, `python -m py_compile`, etc.)
- If script name exists, rename the new one

### 3. Move references

Apply Methods §2. Scan for conditional headings ("If X", "For Y", "Optional", "Troubleshooting") and sibling variants (AWS/GCP, Python/Go). For each:
- Create `references/<topic>.md` with the full prose (add TOC if >300 lines)
- Replace the section in SKILL.md with a one-line pointer that states the trigger condition
- Confirm the pointer makes the branch explicit — an agent reading SKILL.md should know when to open the reference without re-deriving it

### 4. Tighten

Apply Methods §3 techniques to prose sections. Do not change YAML frontmatter or top-level headings.

### 5. Deduplicate

Apply Methods §4: extract repeated rules into authoritative sections, replace occurrences with cross-references.

### 6. Verify

Run panel-review with both versions (original backup + compressed). Each model answers:

> Does the compressed version allow you to reconstruct all operational
> steps from the original without guessing?
> Return: **PASS** (fully reconstructable) or **FAIL** (any step requires inference or is missing)

Pass: all models return PASS.
Fail: restore flagged detail, re-compress differently, re-verify. Max 3 cycles — if still failing, revert: `cp SKILL.md.bak SKILL.md` and delete newly created files under `scripts/` and `references/`.

### 7. Regression check (optional)

Run only if the skill has `evals/evals.json` with populated `assertions`. Panel-review checks the *text* survived; this checks the *behavior* survived. Skip cleanly when evals are absent or assertions are empty — panel-review is then the sole verification.

Preconditions:
- `evals/evals.json` exists in the skill directory with non-empty `assertions` (prompts alone aren't enough — nothing objective to compare against)
- `SKILL.md.bak` still present from step 2

Procedure:
- For each eval, spawn two subagents with the same prompt: one pointed at `SKILL.md.bak` (baseline), one at the compacted SKILL.md
- Grade each run against its assertions using the pattern in `references/grader.md`
- Compare per-assertion pass rates between baseline and compacted runs

Pass: every assertion that passed under baseline also passes under the compacted version.
Fail: any assertion regressed. Restore the relevant detail from `SKILL.md.bak`, re-run step 6 and this step. Max 3 cycles — if still failing, revert per step 6's fallback.

### 8. Measure again

`wc -c` on the compacted SKILL.md. Report as:
`{original_chars} → {compressed_chars} chars — ≈{token_pct}% fewer tokens per turn the skill is active (baseline ≈{baseline_tokens} tok → compacted ≈{compacted_tokens} tok, chars/4).`

Per-turn is the unit that matters: body tokens are re-sent on every turn the skill is active, so savings multiply by turn count. A long-running skill with N turns saves roughly `(baseline_tok − compacted_tok) × N` input tokens over its lifetime.

Clean up: `rm SKILL.md.bak`.
