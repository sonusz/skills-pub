# auto-dev-lite

Document-driven development inside a single agent session. You supply (or co-write) a short core requirements document; the orchestrating agent derives a more detailed working spec from it, has fresh subagents dry-run the pair, implement scoped tasks, and review each stage for conformance and proportionality. It is "lite" because there is no external harness: no CLI, no state files, no `docs/features/` folder contract, no separate feature-documentation stage, and no vendor CLIs. Two documents and in-harness subagents are the whole mechanism.

## When to use it

- You have a design or requirements document and want code developed against it, with every change traceable back to what the document says.
- You want the requirements checked against an ordinary reader with no conversation context before any code is written.
- You want reviews that ask "does this match the documents, and is anything here unnecessary or redundant" rather than general code review.
- The feature is small or medium sized, which is most features. This is the
  default choice; `auto-dev-sdk` is for large features and large codebases
  where you want pipeline state on disk, resumable across sessions, with
  multi-vendor review gates enforced by a harness rather than by prompt.

## When not to use it

- Single-line fixes, typos, or any change with no design surface.
- Bug hunts or style review. The reviews here check document conformance and proportionality only; use a dedicated code-review skill for that.
- Work you want the agent to improvise. Anything the documents do not cover stops the pipeline and comes back to you as a question.

## Requirements

- An agent harness that can spawn fresh subagents. The skill's allowed tools are `Task`, `Read`, `Grep`, `Glob`, and `Bash`. Its subagent settings are written in Claude Code terms (`model: "sonnet"`, fresh `general-purpose` agents, never `fork`) and say to use the platform's equivalent everyday-tier model and medium effort elsewhere.
- A target repository. Dev subagents use the repo's existing test and build commands where they exist.
- A core requirements document, or the time to write one with the agent in Stage 0.

This skill is prompt-only. It consists of one `SKILL.md` and three prompt templates. It runs no scripts, has no dependency on the `shared/` modules, and needs no external vendor CLIs.

## Install

Clone the repo and symlink the skill directory into your agent's skills directory. `REPO` is the path to the clone.

```bash
REPO="$PWD/skills-pub"

# Claude Code
ln -sfn "$REPO/skills/auto-dev-lite" ~/.claude/skills/auto-dev-lite

# Codex CLI
ln -sfn "$REPO/skills/auto-dev-lite" ~/.codex/skills/auto-dev-lite
```

The directory contains no symlinks into `shared/`, so a plain copy also works if you prefer not to link.

## How it works

Three invariants hold for the whole run: the orchestrator never edits code (every change goes through a dev subagent), the two documents have two owners (the core document is yours, the detail document is the orchestrator's), and the documents bound all work (nothing they do not cover is improvised).

1. **Stage 0, core document.** If you give a path, the orchestrator reads it and confirms it describes goal, scope, and expected behavior. If not, it interviews you and drafts one, iterating until you approve. Default location is `docs/<feature>-design.md` in the target repo. From here on the core document is user-owned: the orchestrator changes it only after you decide.
2. **Stage 1, detail document.** The orchestrator expands the core document into a slightly more detailed operational spec, by default `<core-doc-basename>.detail.md` next to it. Everything in it must be derivable from the core document. Open decisions are either made conservatively and recorded, or brought to you. This is the only document the orchestrator edits on its own, and it tells you about each edit without waiting for a reply.
3. **Stage 2, comprehension gate.** A fresh subagent with no conversation context reads both documents and the repo, and replies with the plan it would execute, its assumptions, any divergence between the two documents, and any mechanism it sees as removable or redundant. It executes nothing. If the plan shows drift, invention, divergence, or excess, the orchestrator fixes the detail document only and repeats with a brand-new subagent. If the fix would need a core-document change, or the same disagreement persists after three rounds, the question goes to you.
4. **Stage 3, development.** The orchestrator splits the detail document into stages and tasks. Independent tasks run as parallel subagents; dependent work is sequenced. Each dev subagent gets both documents and an explicit scope, and must stop and report rather than improvise when the documents are silent or disagree. If it finds a prescribed mechanism unnecessary, it reports that with a simpler alternative instead of silently omitting it.
5. **Stage 4, review.** After each stage, fresh review subagents see both documents, the stage scope, and the diff, and answer five questions: conformance, completeness, overreach, fidelity to the core document, and proportionality. Each review ends with `PASS`, `FIX`, or `ESCALATE`. Fix findings go back through dev subagents and are re-reviewed; overreach, doc gaps, and fidelity findings go to you. A closing review over the full change set runs the same five checks.
6. **What returns to you.** A report of what was built, mapped section by section to the core document, plus every detail-document edit made along the way.

The one blocking point is the stop-and-discuss rule: the pipeline waits for your decision whenever it finds a problem the documents never discussed, work that needs substantial uncovered effort (a new dependency, schema change, or cross-cutting refactor), significant out-of-scope work already produced, a divergence that cannot be fixed without changing the core document, or the three-round limit in Stage 2. You are shown what was found, why the documents do not answer it, and the options: amend the core document, rule it out of scope, or rethink the approach.

## Usage

Trigger it with one of the phrases from the skill description:

- "auto-dev-lite"
- "doc-driven development"
- "develop against this design doc"
- "implement per this document"
- a request for a lighter alternative to the full auto-dev pipeline

Point it at a requirements document if you have one, for example "auto-dev-lite, develop against docs/export-design.md".

What you will be asked:

- If you did not provide a document, or it is thin or stale: the goal and why, what is in and out of scope, expected behavior and interfaces, constraints (compatibility, performance, style), and what "done" looks like. You then approve the draft.
- During Stage 1 and 2: nothing that blocks. You get short notices of detail-document edits and can reply if you disagree.
- Any time mid-run: optional design feedback in chat ("use X instead of Y", "that's over-engineered"). It is merged as one more reviewer's findings into the next Stage 2 or Stage 4 review round, after checking it against the core document; you are told which round it joined.
- At any stop-and-discuss point: a decision between amending the core document, ruling the item out of scope, or rethinking the approach. The pipeline waits.
- At the end: nothing. You receive the section-by-section report.

## Files

| File | Purpose |
| ---- | ------- |
| `SKILL.md` | The orchestrator's instructions: invariants, subagent settings, Stages 0 to 4, and the stop-and-discuss rule |
| `prompts/doc-dry-run.md` | Stage 2 template: a fresh subagent's dry-run plan, assumptions, core/detail divergence, and proportionality check; executes nothing |
| `prompts/dev-task.md` | Stage 3 template: scoped, document-bound implementation with stop-on-gap and stop-on-conflict rules |
| `prompts/stage-review.md` | Stage 4 template: context-free five-question review of a stage diff ending in `PASS`, `FIX`, or `ESCALATE` |
| `README.md` | This file |

Each template documents its placeholders (such as `<core_doc_content>`, `<detail_doc_content>`, `<repo_root>`) in a comment at the top; the orchestrator substitutes every placeholder before dispatch.
