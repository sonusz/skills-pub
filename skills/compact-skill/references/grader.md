# Grader Reference (regression check)

Compact-skill's step 7 regression check uses this grading pattern to verify the compacted SKILL.md doesn't break the target skill's behavior. This is a scoped adaptation of skill-creator's more general grader — trimmed to what a per-assertion regression diff actually needs.

## Role

Evaluate each assertion in `evals/evals.json` against the outputs of a single run. Produce pass/fail per assertion with cited evidence. Compact-skill's step 7 runs this grader twice — once against the baseline run (SKILL.md.bak) and once against the compacted run — and diffs the results to detect regressions.

## Inputs

- **assertions**: List of assertion strings for the eval being graded, from `evals/evals.json`
- **transcript_path**: Path to the execution transcript (markdown file produced by the subagent)
- **outputs_dir**: Directory containing the run's output files

## Process

1. **Read the transcript.** Note the prompt, steps taken, and any errors encountered.
2. **Examine the output files.** List files in `outputs_dir`, read/inspect each one relevant to the assertions. If outputs aren't plain text (docx, xlsx, pdf, etc.), use format-appropriate inspection tools — don't trust what the transcript claims the executor produced.
3. **Evaluate each assertion.**
   - **PASS** when transcript or outputs clearly demonstrate the assertion is true AND the evidence reflects genuine task completion, not surface compliance (e.g., a file with the right name but empty contents fails)
   - **FAIL** when evidence is missing, contradicts the assertion, or is superficial
4. **Cite evidence.** Quote the specific text from the transcript, or describe the observed file content that justifies the verdict. Vague evidence cannot be diffed across runs and undermines the regression check.

## Output format

Write JSON to `{outputs_dir}/../grading.json`:

```json
{
  "expectations": [
    {
      "text": "The output is a valid docx file",
      "passed": true,
      "evidence": "outputs/report.docx opens with python-docx; contains 3 paragraphs matching the expected structure"
    },
    {
      "text": "Output CSV includes column 'profit_margin'",
      "passed": false,
      "evidence": "outputs/result.csv columns: revenue, cost. No profit_margin column present."
    }
  ],
  "summary": {
    "passed": 1,
    "failed": 1,
    "total": 2,
    "pass_rate": 0.5
  }
}
```

Field names must be exactly `text`, `passed`, `evidence` — step 7's diff logic depends on these names.

## Guidelines

- **No partial credit.** Each assertion is pass or fail.
- **Burden of proof sits on the assertion.** When uncertain, fail.
- **Be specific.** Quote the exact text or name the file and its relevant content. "Looked correct" is not evidence.
- **Check both transcript and outputs.** Process assertions (the skill called a specific script) live in the transcript; result assertions (the file has the right contents) live in outputs. Most assertions need both.

## What this grader does *not* do

Compared to skill-creator's full `agents/grader.md`, this reference deliberately omits:

- Claim extraction and verification (overkill for a pass/fail regression diff)
- Eval critique and improvement suggestions (not the compaction review's concern)
- `user_notes.md` handling (compaction runs don't generate executor notes)
- Metrics / timing aggregation (compact-skill step 8 reports size metrics separately)

If any of those are needed, consult skill-creator's `agents/grader.md` directly — this file is a narrow-purpose adaptation, not a drop-in replacement.
