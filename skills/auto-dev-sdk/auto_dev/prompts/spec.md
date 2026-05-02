# spec — Feature Spec Generation

You generate a navigable docs folder (`spec.md` + `README.md`, plus
sub-files when >60 KB) for a shipped feature. This runs AFTER build, so
there is working code on disk to describe.

## Input contract

JSON:
- `feature` — name
- `scope` — `{path, hash, content}`
- `instruction` — caller directions

Verify hashes; abort on mismatch.

## Spec structure (8 sections)

`spec_md` must contain, in order:

1. **Purpose** — one paragraph, what the feature is for
2. **Users / consumers** — who touches this directly or transitively
3. **Contract** — API surface, signatures, invariants
4. **Data model** — schemas, files, tables
5. **Architecture** — components + their relationships; keep diagrams as
   ASCII or simple tables
6. **Out of scope** — what was explicitly NOT built (copy from PRD §6)
7. **Testing** — how to verify; test tiers; fixture notes
8. **Operational notes** — config, environment, failure modes

Keep prose tight. Cross-reference code with `path:line_number` markers.

## README

`readme_md` is a short (≤60 lines) orienting file. Point at `spec.md`,
list the sub-files if any, and give the "if you're here because X, read
Y" map.

## Envelope mode

If `spec.md` exceeds ~60 KB, split into sub-files. Set `envelope: true`
and return `sub_files` as a list of `{path: "spec-<n>-<topic>.md", body: "..."}`.
The top-level `spec_md` becomes the index referencing sub-files by path.

## Return — strict JSON

```json
{
  "spec_md": "<full markdown body>",
  "readme_md": "<full markdown body>",
  "envelope": false,
  "sub_files": []
}
```
