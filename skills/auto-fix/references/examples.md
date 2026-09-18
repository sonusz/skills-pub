# Auto-Fix Examples

Read this only when you need sample phrasing for results. `SKILL.md` remains the source of truth for the contract and decision rules.

## Example recommendations

- `"Human review required — change reverses fail-open behavior documented in docs/features/query/prefixcheck.md"`
- `"No design doc exists for this area. Create one (by hand or with your feature-docs generator) before auto-fixing."`
- `"Bot-authored suggestion touches exception handling; no supporting evidence found."`
- `"Docs and code diverge on <behavior> (doc says X at <doc-path>; code does Y at <file>:<line>). Regenerate the docs from current code, confirm the regenerated description matches real intent, then re-run auto-fix."`

## Motivation example

A common failure mode is a reviewer or bot suggesting a small code change that reverses a deliberate design choice, such as fail-open to fail-closed. The diff is tiny, but the semantic change is large. That is why auto-fix checks intent before size.
