# Harness-owned watch heartbeat and alerts

Use this for every background `autodev run` or `autodev next`. The harness owns
the heartbeat cadence and terminal outcome markers; outer agents subscribe to
one fixed protocol instead of inventing polling per feature.

## How

```bash
autodev run <feature> --watch
```

Run in the background so stdout is monitorable. `--watch` sets
`AUTODEV_WATCH=1`, starts a 60-second heartbeat, emits selected JSONL
transitions, and guarantees one terminal marker for every ordinary return path.
The JSONL log on disk is unaffected. `AUTODEV_WATCH_HEARTBEAT_SEC` may override
the interval for tests or unusual operator environments; wrappers should use
the advertised value rather than set their own cadence.

## Lifecycle markers

```text
[autodev:watch] started protocol=1 feature=<f> verb=run heartbeat_sec=60
[autodev:watch] heartbeat protocol=1 feature=<f> verb=run elapsed_sec=<n>
[autodev:watch] terminal protocol=1 feature=<f> verb=run outcome=<outcome> exit_code=<n> elapsed_sec=<n>
```

Terminal outcomes are `complete`, `gate_pending`, `error`, or `lock_conflict`.
The marker is emitted for success and failure, so process EOF is never the only
signal available on an ordinary exit. A hard kill cannot emit from the dead
process; that is why the Monitor must enforce heartbeat absence.

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

## Required Monitor contract

1. Start the run in background with `--watch`.
2. Attach one Monitor to stdout. Without it, background PTY output is buffered
   and cannot wake the outer agent.
3. Read `heartbeat_sec` from `started`. Reset the silence deadline on every
   watch marker. Do not forward routine heartbeats to the user.
4. If two heartbeat intervals pass without any marker, wake immediately, run
   `autodev status`, and surface the missing-heartbeat alert.
5. On `terminal outcome=complete`, report completion and stop. On any other
   terminal outcome, wake immediately, run `autodev status`, and surface the
   gate/error/lock state.

Do not add `sleep` loops, cron entries, or a second status poller. This contract
is identical for every feature.

## When NOT to use `--watch`

- Foreground `autodev run` invocations the user is watching live (the alerts add no signal).
- Tests / CI unless they are explicitly testing this protocol.

## Alert format

```
[autodev:<stage>] <event> [<key>=<value> …]
```

Transition alerts surface detail keys in priority order: `verdict`, `gate`,
`kind`, `vendor`, `model`, `reason`. Long reasons are truncated to 80 chars.

## Implementation pointers

- Whitelist + formatter: `autodev/state/log.py` (`_ALERT_EVENT_KEYS`, `_ALERT_EVENT_NAMES`, `_format_alert`).
- Heartbeat + terminal protocol: `autodev/watch.py`.
- CLI integration: `autodev/cli.py` (`_dispatch_with_watch`).
- Tests: `tests/v2/test_log_watch.py`, `tests/v2/test_watch_heartbeat.py`.
