# Result Schema

Read this only when you need the exact shape of `/tmp/comment-fix-result-<pr-number>.json`.

## Stdout summary

```text
| # | File | Comment | Action | Commit |
|---|------|---------|--------|--------|
| 1 | service.py | Field name mismatch | Applied fix + replied + resolved | abc1234 |
| 2 | analyzer.py | Early return | Responded with reject reason | — |
| 3 | scheduler.go | Decoupling | Untouched | — |
```

## Result file

```json
{"analysis": [{"thread_id": "PRRT_...", "status": "would_apply", "category": "applied", "summary": "Rename variable", "evidence": "docs/features/... says camelCase"}],
 "actions": [{"thread_id": "PRRT_...", "user_choice": "apply fix|response with reject reason|customize response|resolve"}],
 "fixed": [{"thread_id": "PRRT_...", "reply": "Fixed in abc1234 — renamed variable"}],
 "rejected": [{"thread_id": "PRRT_...", "reply": "Not applied — would reverse documented retry behavior."}],
 "customized": [{"thread_id": "PRRT_...", "reply": "Custom reply text approved by user", "resolved": false}],
 "untouched": [{"thread_id": "PRRT_...", "reason": "user gave no action, new replies required re-evaluation, or thread was already resolved"}]}
```
