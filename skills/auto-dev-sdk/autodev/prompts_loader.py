"""Load + render stage prompts with orchestrator-supplied variables."""
from __future__ import annotations

from pathlib import Path

from autodev.state.hashing import hash_file

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


_STAGE_PROMPT_FILE = {
    "design": "stage-design.md",
    "build":  "stage-implement.md",
    "spec":   "stage-spec.md",
}


def _prompt_file_for_stage(stage: str) -> Path:
    if stage == "ralph-review":
        return PROMPTS_DIR / "stage-ralph-review.md"
    return PROMPTS_DIR / _STAGE_PROMPT_FILE[stage]


def render_stage_prompt(
    *,
    stage: str,
    feature: str,
    feature_active: Path,
    repo_root: Path,
    primary_target: Path,
    extra_targets: list[Path],
    context_artifacts: list[str] | None = None,
    invocation_bindings: dict[str, str | Path] | None = None,
    preseeded: bool = False,
    continuation: bool = False,
) -> str:
    """Build the prompt string passed to the shared vendors adapter.

    Appends a "## Orchestrator context" section to the stage prompt file
    body, listing the concrete paths + hashes the subagent needs.
    """
    prompt_file = _prompt_file_for_stage(stage)
    if continuation:
        body = (
            f"# Continue the existing `{stage}` agent session\n\n"
            "Keep the role, invariants, output contract, and implementation "
            "discipline established by the initial turn in this session. "
            "The filesystem and hashes below are authoritative for this "
            "turn: re-read changed feedback and targets, do not rely on stale "
            "in-memory file contents, and continue by editing the current "
            "working artifacts rather than restarting the assignment. If a "
            "listed PROMPT_HASH changed, re-read PROMPT_FILE before acting. "
            "If a previous turn failed or was interrupted, reconcile your "
            "memory with the current files before acting.\n"
        )
    else:
        body = prompt_file.read_text(encoding="utf-8")

    ctx_lines: list[str] = [
        "",
        "---",
        "",
        "## Orchestrator context (filled at invocation)",
        "",
        f"- FEATURE: `{feature}`",
        f"- FEATURE_ACTIVE: `{feature_active}`",
        f"- SCRATCH_DIR: `{feature_active / 'scratch'}` — write ANY ad-hoc "
        f"analysis, coverage-tracking, or throwaway working files HERE. This "
        f"is the harness scratch area (under the feature's active/ tree, "
        f"excluded from the implementation change-set). NEVER drop scratch at "
        f"the repo root or anywhere in the product source tree.",
        f"- REPO_ROOT: `{repo_root}`",
        f"- PROMPT_FILE: `{prompt_file}`",
        f"- PROMPT_HASH: `{hash_file(prompt_file)}`",
    ]

    # Per-stage upstream artifacts + hashes. Spec is deliberately
    # code-first: it receives only the harness-authored implementation
    # index, not PRD/scope/design/build semantics.
    upstream_by_stage = {
        "design": [
            (feature_active / "prd.md",     "PRD_PATH",   "PRD_HASH"),
        ],
        "build":  [
            (feature_active / "prd.md",        "PRD_PATH",       "PRD_HASH"),
            (feature_active / "design-packet.json", "DESIGN_PACKET_PATH", "DESIGN_PACKET_HASH"),
            (feature_active / "accepted-design.json", "ACCEPTED_DESIGN_PATH", "ACCEPTED_DESIGN_HASH"),
            (feature_active / "design.md",     "DESIGN_PATH",    "DESIGN_HASH"),
            (feature_active / "scope.json",    "SCOPE_PATH",     "SCOPE_HASH"),
            (feature_active / "trace.md",      "TRACE_PATH",     "TRACE_HASH"),
            (feature_active / "test-plan.md",  "TEST_PLAN_PATH", "TEST_PLAN_HASH"),
        ],
        "spec":   [
            (
                feature_active / "implementation-index.json",
                "IMPLEMENTATION_INDEX_PATH",
                "IMPLEMENTATION_INDEX_HASH",
            ),
        ],
        # ralph-review is deliberately context-isolated: code + trace only.
        # No PRD/scope/build.json — it's a pure "does code match trace?"
        # classification gear, not an intent-vs-impl judge.
        "ralph-review": [
            (feature_active / "trace.md",      "TRACE_PATH",      "TRACE_HASH"),
        ],
    }
    for path, name_key, hash_key in upstream_by_stage.get(stage, []):
        if path.exists():
            ctx_lines.append(f"- {name_key}: `{path}`")
            ctx_lines.append(f"- {hash_key}: `{hash_file(path)}`")
        else:
            ctx_lines.append(f"- {name_key}: `{path}` (MISSING — stage will abort)")

    # Budget account: design and build size their work against the feature's
    # remaining budget, not only their own context window. ralph-review stays
    # context-isolated (pure code-vs-trace classifier) and spec is code-first,
    # so neither receives it. Rendering must survive any metering failure.
    if stage in ("design", "build"):
        try:
            from autodev.budget import format_budget_lines
            ctx_lines.extend(format_budget_lines(feature_active))
        except Exception:
            pass

    # The design stage authors scope.json's `diff_base`. Surface the declared
    # base ref (architecture.md "## Base ref") so it has one source of truth
    # rather than being the agent's guess.
    if stage == "design":
        from autodev.artifacts.workflow_state import discover_base_ref
        base_ref = discover_base_ref(feature_active)
        if base_ref:
            ctx_lines.append(f"- DIFF_BASE: `{base_ref}`")
        # Mechanism 2 (rigor-tier): harness-computed rework trust region
        # for this rerun. Injected only on reruns (a rerun always carries
        # context artifacts); an initial authoring run must not see a
        # stale mode from a prior cycle.
        if context_artifacts:
            from autodev.diagnosis import read_rework_mode
            rework_mode = read_rework_mode(feature_active)
            if rework_mode:
                ctx_lines.append(f"- REWORK_MODE: `{rework_mode}`")

    # Target artifacts
    target_map = {
        "design": [
            ("TARGET_DESIGN", primary_target),
            ("TARGET_SCOPE", extra_targets[0] if len(extra_targets) > 0 else feature_active / "scope.json"),
            ("TARGET_TRACE", extra_targets[1] if len(extra_targets) > 1 else feature_active / "trace.md"),
            ("TARGET_TEST_PLAN", extra_targets[2] if len(extra_targets) > 2 else feature_active / "test-plan.md"),
            ("TARGET_CHANGELOG", extra_targets[3] if len(extra_targets) > 3 else feature_active / "design-changelog.json"),
        ],
        "build":  [("TARGET_BUILD_JSON", primary_target)],
        "spec":   [("TARGET_SPEC",    primary_target),
                   ("TARGET_README",  extra_targets[0] if extra_targets else feature_active / "README.md")],
        "review": [("TARGET_REVIEW",  primary_target)],
        "ralph-review": [("TARGET_RALPH_REVIEW", primary_target)],
    }
    for key, p in target_map.get(stage, []):
        ctx_lines.append(f"- {key}: `{p}`")

    # Stage-specific, harness-authored pointers that do not belong to the
    # canonical upstream/target maps.  Values stay as references in the
    # prompt; file contents are never inlined.  Ralph uses this for the
    # immutable previous-review link and the per-build Git-diff context.
    for key, value in (invocation_bindings or {}).items():
        ctx_lines.append(f"- {key}: `{value}`")

    # v3-core: CONTEXT_ARTIFACTS — stage-relevant feedback artifacts
    # (prior verdict for this stage, own previous output, route feedback,
    # etc.). Empty on initial runs.
    effective_context = context_artifacts or []
    if effective_context:
        ctx_lines.append(f"- CONTEXT_ARTIFACTS: {effective_context}")
        ctx_lines.append("")
        own_artifact_note = (
            "Your own prior artifacts are ALREADY loaded as your "
            "pre-filled `.tmp` working copies (see the write instruction "
            "below) — do not re-read their CONTEXT_ARTIFACTS copies "
            "separately; Read and Edit the `.tmp` files."
            if preseeded else
            "A previous version of your own artifact (same filename) may "
            "be present — revise it in place rather than regenerate from "
            "scratch; preserve stable IDs and incorporate the fixes the "
            "panel asked for."
        )
        ctx_lines.append(
            "Read every path in CONTEXT_ARTIFACTS. Use them to inform this "
            "stage's output: a panel verdict file may contain "
            "`invariant_violation` or `risk` findings pointing at this "
            "stage's artifact — address those (MUST fix). `opinion` "
            "findings may be acknowledged but do not require change. "
            + own_artifact_note
            + " An `*-output-rejection.json` file means the harness "
            "rejected your previous deliverable for a concrete deficiency "
            "(missing artifact, malformed provenance header, or incomplete "
            "classification coverage). Read its `instruction` and "
            "`missing_scope_ids`/`detail` fields and AMEND the prior "
            "artifact to fix exactly that — keep every entry that was "
            "already correct; do NOT start over."
        )
    else:
        ctx_lines.append("- CONTEXT_ARTIFACTS: [] (initial run)")

    # Iteration history manifest — temporal view of artifact / gate
    # events drawn from log.jsonl. Lets the consumer see WHEN each
    # CONTEXT_ARTIFACTS file was produced and whether anything has
    # happened since (most importantly: whether prd.md was amended
    # after the panel verdict was written).
    from autodev.iteration_history import (
        build_iteration_history,
        render_iteration_history,
    )

    history = build_iteration_history(feature_active)
    if history:
        ctx_lines.append("")
        ctx_lines.append("## Iteration history (oldest → newest)")
        ctx_lines.append("")
        ctx_lines.append(render_iteration_history(history))
        ctx_lines.append("")
        ctx_lines.append(
            "Read this manifest to place every file in CONTEXT_ARTIFACTS "
            "and the upstream paths above on a single timeline. Key "
            "inferences it enables: (a) if a panel verdict's row is "
            "older than `prd.md AMENDED`, that verdict was produced "
            "under a previous PRD — its findings may already be "
            "addressed by the latest amendment; treat them as historical "
            "signals to confirm rather than fresh blockers. (b) if your "
            "own previous output (e.g. `design.md` for the design stage) "
            "appears in the manifest, the most recent corresponding row "
            "shows the version on disk you should revise from. (c) "
            "`Current hash` is a sha256 prefix of the file as it stands "
            "right now; mismatches between the row's logical claim and "
            "the current hash mean the file has been rewritten since "
            "the event."
        )

    ctx_lines.append("")
    if preseeded:
        ctx_lines.append(
            "Each TARGET_* `.tmp` is PRE-FILLED with your last landed "
            "version of that artifact. Read each `.tmp` and EDIT it in "
            "place — change only what this round's feedback requires; "
            "leave an unchanged artifact's `.tmp` exactly as-is (it lands "
            "byte-identical, which is correct and cheap). Do NOT "
            "regenerate any artifact from scratch and do NOT re-emit "
            "unchanged content. The orchestrator renames each `.tmp` to "
            "its final name on exit 0; a failed run discards the `.tmp` "
            "and leaves the landed package untouched. Exit non-zero on "
            "abort."
        )
    else:
        ctx_lines.append(
            "Write each TARGET_* to `<path>.tmp`; the orchestrator renames "
            "to final on exit 0. Exit non-zero on abort."
        )
    ctx_lines.append("")

    return body + "\n".join(ctx_lines)
