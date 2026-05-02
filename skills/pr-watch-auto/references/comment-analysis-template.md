# Comment Analysis Template

Read this only when formatting the per-thread analysis blocks after `auto-fix` evaluation mode.

## Status mapping

- `✅` — no attention needed from the user; use when `auto-fix` agrees with the comment and the planned action/response is straightforward
- `❓` — human input needed; use for `doc-drift`, deferred follow-up, stale threads, custom-response choices, or any case where direction is not settled. Unresolved outdated threads are still actionable; do not mark them stale solely for being outdated.
- `❌` — `auto-fix` does not agree with the comment, the proposed fix, or the planned response to the comment

## Recommended per-thread template

```text
✅ Thread PRRT_123
file: api/handler.go
outdated: no
comment: "Use externalID instead of external_id."
response already provided: no
current response: none
resolved: no
auto-fix: WOULD_APPLY
recommendation: Apply fix
why: Matches documented package naming convention.
evidence: docs/features/identity/conventions.md says identity fields use camelCase.
default reply: Fixed in <sha>. Renamed `external_id` to `externalID` to match the documented convention.
user options: apply fix | response with reject reason | customize response | resolve

❓ Thread PRRT_456
file: service/retry.py
outdated: yes
comment: "Stop retrying on timeout."
response already provided: yes
current response: "We saw this, but have not changed retry behavior yet."
resolved: no
auto-fix: ESCALATED (doc-drift)
recommendation: Human decision required
why: Docs and current code disagree on retry behavior.
evidence: docs say one thing; current code does another.
default reply: Deferred — docs and code diverge here. Human review is needed before changing retry behavior.
default resolution: leave unresolved
user options: response with reject reason | customize response | resolve

❌ Thread PRRT_789
file: parser/decoder.go
outdated: no
comment: "Refactor this into ParserContext across five files."
response already provided: no
current response: none
resolved: no
auto-fix: ESCALATED (above-minor)
recommendation: Reject for auto-fix
why: Exceeds file/line limits for automatic fixing.
evidence: touches 5 files and is a refactor, not a small fix.
default reply: Not applied — this is above the minor-fix threshold and needs a broader design review.
user options: response with reject reason | customize response | resolve
```
