# Push alerts via `--watch`

Use this when launching `autodev run` (or `autodev next`) in the background and you want push-style alerts on key state transitions instead of polling status.

## How

```bash
autodev run <feature> --watch
```

Run via `Bash(run_in_background: true)` so the bash output stream is monitorable. The flag sets `AUTODEV_WATCH=1`; `autodev.state.log.JsonlLog.emit` adds a stdout line for each whitelisted transition. The JSONL log on disk is unaffected.

## Whitelisted alerts

Only these transitions emit a stdout line. Every other event stays silent.

| Stage / event | When it fires |
|---|---|
| `[autodev:gate] panel-done verdict=…` | Any panel verdict (design-review, close-approval) |
| `[autodev:gate] revision-loop-triggered …` | Panel failed; about to re-dispatch a producer |
| `[autodev:gate] revision-loop-halt …` | L_MAX hit; needs human |
| `[autodev:<stage>] subprocess-failed kind=…` | Any vendor subprocess failure |
| `[autodev:orchestrator] pipeline-done` | Run reached terminal state |
| `[autodev:orchestrator] paused` | Pause sentinel detected |

Routine progress (`subprocess-dispatch`, `subprocess-start`, `artifact-written`, `iteration-recorded`, intermediate `stage-complete`) is intentionally suppressed — those would drown the outer agent in notifications.

## Combining with Monitor and the deadman wakeup

1. Start the run in background with `--watch`.
2. (Optional) attach a `Monitor` task to the bash output so each alert line wakes the outer agent instantly. Without Monitor, alerts are visible only when the bash command finally exits.
3. Keep the periodic `ScheduleWakeup` running as a 10-min sliding deadman (R5 rule 4). Each alert resets the deadman; if no alert arrives for 10 min, the wakeup fires and the agent does a `autodev status` to confirm progress or surface a stall.

## When NOT to use `--watch`

- Foreground `autodev run` invocations the user is watching live (the alerts add no signal).
- Tests / CI (the JSONL log is the source of truth there).

## Alert format

```
[autodev:<stage>] <event> [<key>=<value> …]
```

Surfaced detail keys, in priority order: `verdict`, `gate`, `kind`, `vendor`, `model`, `reason`. Long string values (`reason`) are truncated to 80 chars.

## Implementation pointers

- Whitelist + formatter: `autodev/state/log.py` (`_ALERT_EVENT_KEYS`, `_ALERT_EVENT_NAMES`, `_format_alert`).
- CLI flag: `autodev/cli.py` (`run`, `next` parsers; `cmd_run`/`cmd_next` set `AUTODEV_WATCH=1`).
- Tests: `tests/v2/test_log_watch.py`.
