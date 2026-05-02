# Decision packet schema

The canonical JSON shape that `evaluate` mode hashes (via `scripts/compute-evaluate-hash.sh`). Same packet → same 16-char hash, on macOS or Linux. The Confirmation gate's passthrough path re-derives the hash in `apply` mode and refuses if it has drifted from what the user reviewed.

Read this file only when **building** the packet (during evaluate, or during apply's confirmation re-check). Don't load it on every activation.

```json
{
  "inputs": {
    "mode": "evaluate",
    "comment_text": "...",
    "error_log": "...",
    "author_type": "human | bot",
    "affected_files": ["sorted/path/list"],
    "branch": "...",
    "head_sha": "..."
  },
  "decision": {
    "status": "would_apply | escalated",
    "category": "applied | pre-existing | behavioral | doc-drift | above-minor",
    "summary": "one-line description of the change",
    "evidence": "design-intent quotes or 'none found'",
    "would_apply_description": "concrete description of what apply would do"
  }
}
```

## Field rules

- `inputs.mode` is always `"evaluate"` for hashing — apply re-runs the evaluate logic to recompute the hash, so the canonical hashed mode is fixed.
- Include `comment_text` OR `error_log`, never both — auto-fix takes one input shape per call.
- `author_type` only when present (review-comment input shape); omit for CI-failure input.
- `affected_files` must be sorted (`sort` lexicographically) before hashing — order changes break determinism.
- `branch` and `head_sha` only when present in the input (review-comment context typically supplies both; CI-failure context typically omits them).
- `decision` fields all required for `would_apply`. For `escalated`, the hash is not produced — the gate passthrough only applies to would-apply outcomes.

## What is NOT in the packet

Anything that varies between identical evaluations:

- Timestamps (would invalidate hashes immediately)
- Random nonces, request IDs
- The hash itself (would be circular)

## Computing the hash

The agent assembles the packet, sorts `affected_files`, and pipes the JSON to `scripts/compute-evaluate-hash.sh`. The script does the canonicalization (`jq -cS`) and the SHA-256, returning a 16-char hex prefix.
