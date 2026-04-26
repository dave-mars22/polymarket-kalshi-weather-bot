# T6 — Daily Morning Health Report

A read-only script that snapshots the bot's overnight state into a markdown
report at `~/Desktop/bot-reports/morning-YYYY-MM-DD.md`. Scheduled via
launchd to run every day at 8:00 AM local time.

## Why

After two days of manual end-of-day and morning health checks, two silent
failures turned up that no monitoring would have caught on its own:

- **B1**: settlement credentials silently misconfigured — settlements
  failed without raising errors. Found by manually querying for stale
  pending trades.
- **B3**: `uvicorn --reload` thrashing caused a 10-hour scheduler dead
  zone. Process stayed alive and `/api/health` kept returning 200, but
  the scheduler was firing only ~12% of expected ticks. Found by
  manually grepping the overnight log for `was missed by`.

T6 automates the morning version of that audit so it always runs,
regardless of whether the user remembers.

## What the report contains

Each section returns one of: `✅ NORMAL`, `ℹ️ NOTABLE`, `⚠️ FLAG`, or
`🚨 PROBLEM`. The header summarises into `ALL GREEN`, `MINOR FLAGS`, or
`ATTENTION NEEDED`.

1. **Process health** — pid alive, uptime, `/api/health`. If the bot is
   dead, the report stops here with `🚨` so nothing further obscures the
   alert.
2. **BotState delta** — bankroll, total P&L, total/winning trades, with
   deltas relative to the previous report's embedded snapshot.
3. **Overnight trade activity** — opens / settlements since the previous
   report's snapshot timestamp (or the last 24 h on first run).
4. **Currently open positions** — every unsettled trade with age. Flags
   `KXBTCMAXMON-26APR30` (the April 30 evaluation gate).
5. **Stuck pending check** — B1 invariant: zero trades pending >7 d, and
   no daily contracts pending >24 h.
6. **Scheduler health** — successful firing counts and missed-by warnings
   from the latest `overnight-*.log`.
7. **Reload disruption (B3 sentinel)** — counts `WatchFiles`,
   `Reloading`, `Application startup complete`, `Started reloader process`.
   Anything non-zero (or >2 startups) means B3 has regressed.
8. **Cache health** — `cache miss` and `stale` lines.
9. **Errors / tracebacks** — `ERROR` / `Traceback` / `Failed to`. Known
   benign lines (`KXBTCY` 429s) are subtracted.
10. **API surface** — `/api/health`, `/api/dashboard`,
    `/api/microstructure?underlying=BTC`, `/api/mc/portfolio`,
    `/api/stats`. All should return 200.
11. **Performance** — three `/api/dashboard` latency measurements.
12. **Disk** — DB size, log volume, free space.

## Read-only by design

- All DB queries open the production SQLite DB with `mode=ro`.
- All HTTP calls are GETs to `localhost` with a 5 s timeout.
- The script never restarts the bot, never writes to the DB, never
  calls any cache-mutating endpoint, never auto-fixes anything.
- The script is **independent of the bot's APScheduler** — running it
  in-process would mean the report can't tell you the bot is dead.

## Run it manually

```bash
# Default output path: ~/Desktop/bot-reports/morning-YYYY-MM-DD.md
venv/bin/python backtest/morning_report.py

# Or specify a path:
venv/bin/python backtest/morning_report.py /tmp/test-report.md
```

The first run writes a baseline; subsequent runs compute deltas against
the embedded `BOT_STATE_SNAPSHOT` HTML comment in the previous report.

## Install the daily launchd job

```bash
cp scripts/launchd/com.dariomars.tradingbot.morning-report.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.dariomars.tradingbot.morning-report.plist
launchctl list | grep tradingbot.morning-report   # should show the label
```

Subsequent reports run at 8:00 AM local time every day. launchd's own
stdout/stderr go to `~/Desktop/bot-reports/launchd.log`.

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.dariomars.tradingbot.morning-report.plist
rm ~/Library/LaunchAgents/com.dariomars.tradingbot.morning-report.plist
```

The script itself stays in the repo and remains runnable manually.

## Where things live

| Path | What |
|---|---|
| `backtest/morning_report.py` | The script. |
| `scripts/launchd/com.dariomars.tradingbot.morning-report.plist` | Versioned plist. |
| `~/Library/LaunchAgents/com.dariomars.tradingbot.morning-report.plist` | Installed copy (per-machine, not committed). |
| `~/Desktop/bot-reports/morning-YYYY-MM-DD.md` | Daily report output. |
| `~/Desktop/bot-reports/launchd.log` | launchd's stdout/stderr from each run. |
