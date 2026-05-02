# Review of auto-dev-sdk v3 DESIGN.md

**Verdict**: `needs_revision`

Here is the evaluation based on cross-checking the v3 design proposals against the v2 codebase.

## 1. Diagnosis (§2)
The diagnosis is **accurate**.
The design document claims the user responded to divergence by adding biased gate prompts with blocklists and forced output formats. A review of the current v2 codebase confirms this: `autodev/panel/prompts/review-prd-review.md` (and all other `review-*.md` prompts) explicitly contain `"Do NOT flag:"` sections, enforce a `"Best guess"` / `"Follow-up"` / `"Alternative reading"` output format, and suppress findings via instructional bias. The diagnosis correctly identifies that this band-aid exists in v2.

## 2. Principles (§3)
**P6 (Synthesizer-side blame filter)** introduces a critical **`risk`**.
P6 mandates that the synthesizer drop any finding that targets *only* the anchor artifact (e.g., dropping a PRD-only finding at the G2 plan gate), citing the "presumption of closure" (P4). However, if G1 genuinely misses a fatal flaw or contradiction in the PRD, and the G2 reviewer catches it because they have the PRD in their visibility packet, the synthesizer will silently drop the finding. This ensures the error persists into the build stage rather than being escalated.

## 3. Delete List (§6)
- **`autodev/revision_loop.py` + `revision_state.py`**: **`invariant_violation`**.
  The v2 revision loop tracks `L` (local) and `G` (global) counters to bound retries (`L_MAX=3`, `G_MAX=12`). DESIGN.md proposes replacing this with a simple decision logic in §5 (e.g., `elif all invariant_violations target scope.json: gate = needs_revision → rerun scope subagent`). By deleting the state counters, the v3 orchestrator has no termination condition. If a subagent repeatedly fails to satisfy the panel, the pipeline will infinite-loop.
- **`autodev/vendors/allowlist.py`**: **`invariant_violation`**.
  Deleting the vendor abstraction layer removes the `build_base_flags()` function (`autodev/vendors/allowlist.py:32`), which injects crucial sandbox-scoping flags like `--add-dir`. DESIGN.md proposes using "temp dir with symlinks" for visibility, but the `build` stage *must* write to the repository root. Without the `--add-dir` flag injection, the pipeline either loses write isolation or breaks entirely for the build stage.
- **`autodev/artifacts/overrides.py`**: **`invariant_violation`**.
  This module provides `skip-gate` and `acknowledge-dirty`. The orchestrator (`autodev/orchestrator.py:90`) strictly raises a `DirtyWorkspace` exception unless `has_active_dirty_ack()` is true. Deleting this module without a replacement means the pipeline will permanently hard-block on any uncommitted changes, and users will have no mechanism to bypass stuck gates.
- **Build-blocking routing (g-23, g-24)**: **`opinion` / `risk`**.
  Deleting the `route_to_layer` logic (`autodev/orchestrator.py:771`) means the pipeline gives up autonomous upstream error recovery. If `build` hits an un-implementable trace row, it will halt and require manual human intervention instead of automatically re-running `plan`. While DESIGN.md explicitly states "User handles upstream fixes manually," this is a significant regression in agent autonomy.
- **`review-*.md` prompts**: **`pass`**. Safe to delete and replace, as they contain the biased instructions identified in the diagnosis.

## 4. Implementation Order (§7)
**Step 7** introduces a major **`risk`**.
It proposes simultaneously removing the revision-loop framework, vendor abstraction, build-blocking routing, and markdown→JSON bridge. This bundles four massive, highly-coupled subsystem deletions (~1,000+ LOC) into a single step, violating the requirement for independently testable, non-invasive incremental changes. If the pipeline breaks, isolating the cause among the four distinct missing subsystems will be difficult.

## 5. Net Effect
Relative to v2, the proposed v3 harness **gains** explicit attribution (P3), mechanical pre-checks to save LLM costs, and removes prompt-level biases that suppress true divergence.

However, v3 **gives up**:
1. **Infinite loop protection**: Without `L` and `G` counters, the pipeline is vulnerable to stuck agents spinning forever on a single gate.
2. **Autonomous upstream recovery**: Build stage anomalies (g-24) will now require manual user intervention instead of automatically routing back to `plan` or `scope`.
3. **User overrides**: The removal of `overrides.py` eliminates the ability to skip gates or acknowledge dirty workspaces, making the CLI much more rigid and frustrating for human operators.
4. **Sandbox security**: Deleting the vendor configuration without replacing the `--add-dir` flag injection leaves write-scoping undefined.
