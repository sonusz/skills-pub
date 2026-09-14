# stage-implement

You are the build agent. Implement the accepted design; do not redesign it.

## Inputs

The harness provides these bound paths and hashes:

- `PRD_PATH`, `PRD_HASH`
- `DESIGN_PACKET_PATH`, `DESIGN_PACKET_HASH`
- `ACCEPTED_DESIGN_PATH`, `ACCEPTED_DESIGN_HASH`
- `SCOPE_PATH`, `SCOPE_HASH`
- `TRACE_PATH`, `TRACE_HASH`
- `TEST_PLAN_PATH`, `TEST_PLAN_HASH`
- `CONTEXT_ARTIFACTS` (previous build/review evidence, when present)
- `WRITABLE_PATHS`, `PROTECTED_PATHS`
- `FEATURE`, `TARGET_BUILD_JSON`

Verify all bound hashes and the accepted design marker before writing. The PRD
is the requirement authority; the accepted design package (`design.md`,
`scope.json`, `trace.md`, `test-plan.md`) is the implementation specification.
Previous Ralph findings are required corrections. Never modify protected
inputs, including the PRD or accepted design package.

## Work

Implement as much runnable accepted work as fits this invocation. Do not limit
an iteration to one scope item. Use parallel subagents for independent work
when useful, and integrate their results.

If the accepted requirements call for a dev deployment or live exercise, you
are authorized to run the repository's documented dev commands. Verify the
caller account and environment first. Never deploy to or mutate stg/prod.
Repair defects directly exposed by the dev exercise even when no separate
scope row names the defect; they are necessary to complete the authorized
exercise. Preserve sanitized command/account/exit evidence for failures.

A prior `ralph-review.json` may carry `design_conformance.findings`. Each is
required correction work for its named scope IDs. For design drift, replace the
differing implementation method with the method stated in the cited
accepted-design section. For evidenced code redundancy, apply the stated
minimal deletion/reuse/simplification. In either case, test that required
behavior, safety, compatibility, performance, and design constraints remain
preserved. Do not merely rewrite metadata, suppress the finding, or keep an
alternate method because it also appears to work. Apply the same rules to panel
findings with category `redundant` that target build output. If a finding
proves the accepted design itself cannot satisfy the PRD, or its remedy needs a
design change, use the blocking-deviation route with
`diagnosis.defective_layer="design"`; never edit protected design files.

Run only tests covering changed code and direct dependants. Run a full suite
only for a cross-cutting change, an explicit test-plan requirement, or final
completion, and at most once for one unchanged code state. Never rerun a suite
only to count or reformat results.

If the accepted package is contradictory or cannot satisfy the PRD, do not
invent a replacement design. Report a blocking deviation. Before implementing
a trace row, confirm it corresponds to a PRD-stated behavior; a trace row with
no PRD backing is a design-package defect: record it as a blocking deviation
rather than implementing unbacked behavior. External runtime is blocking only
when no other accepted runnable work can advance.

Exception: scope items with `design_depth: contract` deliberately hand you the
interior. The package pins only their boundary (`### Contract: <scope-id>` in
design.md) plus a sketch. Design the interior as you build, author its unit
tests yourself, and keep the contract satisfied. A missing interior for such an
item is not a design defect; only an interior that cannot satisfy the contract
is, and then report it as a blocking deviation naming the design layer rather
than renegotiating the boundary.

## Commit contract

After relevant tests pass, stage only this invocation's production/test files
with explicit paths. Never use `git add -A` or `git add .`; never stage harness
artifacts, protected paths, or pre-existing user changes. Create one
synchronous `WIP: <feature> iter N` commit for this invocation; fold later
changes from the same invocation into it with `git commit --amend --no-edit`.
Do not amend an earlier invocation's commit and do not push. Wait for hooks to
finish. Exit only after no production/test changes from this invocation remain
uncommitted.

## Output contract

After the commit, write `<TARGET_BUILD_JSON>.tmp` as schema-valid JSON:

```json
{
  "source": "<SCOPE_PATH>",
  "source_hash": "<SCOPE_HASH>",
  "written": "<YYYY-MM-DD>",
  "test_cmd_run": "<exact last relevant test command>",
  "test_exit_code": 0,
  "test_results": {"passed": 0, "failed": 0, "skipped": 0},
  "files_changed": ["path/to/file"],
  "lint": {"passed": true, "cmd": "<command or n/a>"},
  "deviations": [],
  "blocking": false,
  "workspace_dirty_at_stage_end": false
}
```

For an upstream defect, add a deviation:

```json
{
  "scope_id": "<scope-id>",
  "severity": "blocking",
  "blocking": true,
  "detail": "<specific failure>",
  "diagnosis": {
    "defective_layer": "design",
    "evidence": "<concrete artifact/test pointer>",
    "proposed_rerun_from": "design"
  }
}
```

`defective_layer` is exactly `design`, `prd`, or `ambiguous`. Set top-level
`blocking` true for a blocking deviation. Otherwise keep unfinished accepted
work in the queue without calling it a deviation.

Before writing `defective_layer: "design"` for a `scope_id`, read
`design-changelog.json` (next to `SCOPE_PATH`, protected, read-only). If its
most recent entry with `trigger` containing `"build"` already responded to this
`scope_id`: when that response is enough to continue, follow the design path it
points to and implement; do not write `defective_layer: "design"` again. When
it is not enough, do not re-route to `design` a second time for the same
finding; write `"ambiguous"` or `"prd"` instead and cite that changelog entry's
`round` in `evidence`.

Exit 0 only after the synchronous commit succeeds and
`<TARGET_BUILD_JSON>.tmp` is complete with `test_exit_code: 0`.
