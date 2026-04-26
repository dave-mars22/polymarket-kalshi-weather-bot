"""Morning health report — read-only daily snapshot of the trading bot.

Slice T6 (2026-04-26). After two days of manual end-of-day and morning
health checks (which surfaced B1's silent settlement bug and B3's silent
10-hour scheduler dead-zone), this script automates the morning version
so the same audit runs every day at 8am EDT via launchd, regardless of
whether the user remembers to do it.

Read-only by design:
- All DB queries use sqlite:?mode=ro
- All HTTP calls are GETs to localhost with a 5s timeout
- Never modifies the bot, the DB, the scheduler, or any production state
- Never restarts anything, never "fixes" anything found
- Independent of the bot's APScheduler so it can correctly report a
  dead bot (which an in-process job could never do)

Output: a markdown file at ~/Desktop/bot-reports/morning-YYYY-MM-DD.md
(directory created on first run). The previous day's report is parsed
for an embedded BotState snapshot so the new report can show real deltas.

Usage:
    venv/bin/python backtest/morning_report.py
    venv/bin/python backtest/morning_report.py /tmp/test.md
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DB_PATH = _REPO_ROOT / "tradingbot.db"
_DB_URI = f"file:{_DB_PATH}?mode=ro"
_PID_FILE = _REPO_ROOT / ".bot.pid"

_HEALTH_URL = "http://localhost:8000/api/health"
_DASHBOARD_URL = "http://localhost:8000/api/dashboard"
_MICRO_URL = "http://localhost:8000/api/microstructure?underlying=BTC"
_PORTFOLIO_URL = "http://localhost:8000/api/mc/portfolio"
_STATS_URL = "http://localhost:8000/api/stats"
_HTTP_TIMEOUT = 5.0

OK = "✅ NORMAL"
INFO = "ℹ️ NOTABLE"
FLAG = "⚠️ FLAG"
PROB = "🚨 PROBLEM"

_SNAPSHOT_RE = re.compile(
    r"<!-- BOT_STATE_SNAPSHOT (?P<json>\{[^}]+\}) -->"
)


def _open_ro() -> sqlite3.Connection:
    return sqlite3.connect(_DB_URI, uri=True)


def _read_pid() -> Optional[int]:
    if not _PID_FILE.exists():
        return None
    try:
        return int(_PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def _process_etime(pid: int) -> Optional[str]:
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "etime="],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def _http_get(url: str) -> tuple[Optional[int], Optional[float], Optional[dict]]:
    """Returns (status_code, elapsed_seconds, json_body_or_none)."""
    t0 = time.perf_counter()
    try:
        r = httpx.get(url, timeout=_HTTP_TIMEOUT)
        elapsed = time.perf_counter() - t0
        try:
            body = r.json()
        except Exception:
            body = None
        return r.status_code, elapsed, body
    except (httpx.HTTPError, OSError):
        return None, None, None


def _find_latest_log() -> Optional[Path]:
    candidates = list(_REPO_ROOT.glob("overnight-*.log"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _grep_count(path: Path, pattern: str) -> int:
    try:
        out = subprocess.run(
            ["grep", "-cE", pattern, str(path)],
            capture_output=True, text=True, timeout=10,
        )
        return int(out.stdout.strip() or 0)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return 0


def _find_previous_report(out_dir: Path, current_path: Path) -> Optional[Path]:
    if not out_dir.exists():
        return None
    candidates = [
        p for p in out_dir.glob("morning-*.md")
        if p.resolve() != current_path.resolve()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _parse_snapshot(report_path: Path) -> Optional[dict]:
    try:
        text = report_path.read_text(errors="replace")
    except OSError:
        return None
    m = _SNAPSHOT_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group("json"))
    except json.JSONDecodeError:
        return None


def section_process_health() -> tuple[str, list[str], bool]:
    """Returns (marker, lines, is_dead). is_dead=True means stop the report early."""
    lines: list[str] = []
    pid = _read_pid()
    if pid is None:
        return PROB, ["- `.bot.pid` missing or unreadable"], True
    alive = _process_alive(pid)
    if not alive:
        return PROB, [f"- pid **{pid}** is NOT running"], True
    etime = _process_etime(pid) or "unknown"
    status, elapsed, body = _http_get(_HEALTH_URL)
    lines.append(f"- pid **{pid}** alive, uptime `{etime}`")
    if status == 200 and body and body.get("status") == "healthy":
        lines.append(f"- `/api/health` → `{json.dumps(body)}` ({elapsed*1000:.0f}ms)")
        return OK, lines, False
    lines.append(f"- `/api/health` returned {status!r}; bot may be hung")
    return PROB, lines, True


def section_botstate_and_trades(prev_snapshot: Optional[dict]) -> tuple[str, list[str], dict]:
    """Returns (marker, lines, current_snapshot_dict)."""
    lines: list[str] = []
    snapshot: dict = {}
    try:
        with _open_ro() as con:
            cur = con.cursor()
            cur.execute(
                "SELECT bankroll, total_trades, winning_trades, total_pnl, last_run "
                "FROM bot_state ORDER BY id LIMIT 1"
            )
            row = cur.fetchone()
    except sqlite3.Error as e:
        return FLAG, [f"- DB error: {e}"], snapshot

    if row is None:
        return FLAG, ["- bot_state table empty"], snapshot

    bankroll, total_trades, winning_trades, total_pnl, last_run = row
    snapshot = {
        "bankroll": bankroll,
        "total_trades": total_trades,
        "winning_trades": winning_trades,
        "total_pnl": total_pnl,
        "captured_utc": datetime.utcnow().isoformat(timespec="seconds"),
    }

    if prev_snapshot:
        d_bank = bankroll - prev_snapshot.get("bankroll", bankroll)
        d_pnl = total_pnl - prev_snapshot.get("total_pnl", total_pnl)
        d_tr = total_trades - prev_snapshot.get("total_trades", total_trades)
        d_w = winning_trades - prev_snapshot.get("winning_trades", winning_trades)
        prev_when = prev_snapshot.get("captured_utc", "?")
        lines.append(f"Baseline: previous report snapshot at `{prev_when}` UTC.\n")
        lines.append("| Metric | Previous | Current | Δ |")
        lines.append("|---|---:|---:|---:|")
        lines.append(
            f"| bankroll | ${prev_snapshot.get('bankroll', 0):.2f} | "
            f"${bankroll:.2f} | **{d_bank:+.2f}** |"
        )
        lines.append(
            f"| total_pnl | ${prev_snapshot.get('total_pnl', 0):.2f} | "
            f"${total_pnl:.2f} | **{d_pnl:+.2f}** |"
        )
        lines.append(
            f"| total_trades | {prev_snapshot.get('total_trades', 0)} | "
            f"{total_trades} | **{d_tr:+d}** |"
        )
        lines.append(
            f"| winning_trades | {prev_snapshot.get('winning_trades', 0)} | "
            f"{winning_trades} | **{d_w:+d}** |"
        )
    else:
        lines.append("Baseline: no previous report found — first run, no deltas.\n")
        lines.append(f"- bankroll: ${bankroll:.2f}")
        lines.append(f"- total_pnl: ${total_pnl:.2f}")
        lines.append(f"- total_trades: {total_trades}")
        lines.append(f"- winning_trades: {winning_trades}")

    if prev_snapshot and (bankroll - prev_snapshot.get("bankroll", bankroll)) <= -10:
        return FLAG, lines, snapshot
    return OK, lines, snapshot


def section_overnight_activity(since_utc: datetime) -> tuple[str, list[str]]:
    lines: list[str] = []
    cutoff = since_utc.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _open_ro() as con:
            cur = con.cursor()
            cur.execute(
                "SELECT COUNT(*), COALESCE(SUM(pnl), 0) FROM trades "
                "WHERE timestamp > ?", (cutoff,),
            )
            opened_count, _ = cur.fetchone()
            cur.execute(
                "SELECT market_type, result, COUNT(*), ROUND(COALESCE(SUM(pnl),0),2) "
                "FROM trades WHERE settlement_time > ? "
                "GROUP BY market_type, result ORDER BY market_type, result",
                (cutoff,),
            )
            settled_groups = cur.fetchall()
            cur.execute(
                "SELECT COUNT(*), ROUND(COALESCE(SUM(pnl),0),2) FROM trades "
                "WHERE settlement_time > ?", (cutoff,),
            )
            settled_total, settled_pnl = cur.fetchone()
    except sqlite3.Error as e:
        return FLAG, [f"- DB error: {e}"]

    lines.append(f"Window: trades since `{cutoff}` UTC.\n")
    lines.append(f"- **Opened**: {opened_count}")
    lines.append(f"- **Settled**: {settled_total} (net P&L: **${settled_pnl:+.2f}**)")
    if settled_groups:
        lines.append("")
        lines.append("| market_type | result | count | pnl |")
        lines.append("|---|---|---:|---:|")
        for mt, res, cnt, pnl in settled_groups:
            lines.append(f"| {mt} | {res} | {cnt} | ${pnl:+.2f} |")
    return OK, lines


def section_open_positions() -> tuple[str, list[str]]:
    lines: list[str] = []
    try:
        with _open_ro() as con:
            cur = con.cursor()
            cur.execute(
                "SELECT id, market_ticker, market_type, direction, size, entry_price, timestamp "
                "FROM trades WHERE settled = 0 OR settled IS NULL ORDER BY timestamp"
            )
            rows = cur.fetchall()
    except sqlite3.Error as e:
        return FLAG, [f"- DB error: {e}"]

    if not rows:
        return OK, ["- No open positions."]

    now = datetime.utcnow()
    lines.append(f"- **{len(rows)} open trades**\n")
    lines.append("| id | ticker | type | dir | size | entry | age |")
    lines.append("|---:|---|---|---|---:|---:|---|")
    has_apr30 = False
    for tid, ticker, mtype, direction, size, entry, ts in rows:
        try:
            opened = datetime.fromisoformat(ts.split(".")[0])
            age_seconds = max(0, int((now - opened).total_seconds()))
            age = f"{age_seconds // 3600}h {(age_seconds % 3600) // 60}m"
        except (ValueError, AttributeError):
            age = "?"
        if "26APR30" in (ticker or ""):
            has_apr30 = True
        lines.append(
            f"| {tid} | `{ticker}` | {mtype} | {direction} | "
            f"{size:.2f} | {entry:.3f} | {age} |"
        )
    if has_apr30:
        lines.append("")
        lines.append("- KXBTCMAXMON-26APR30 contracts present (April 30 evaluation gate).")
    return OK, lines


def section_stuck_pending() -> tuple[str, list[str]]:
    lines: list[str] = []
    try:
        with _open_ro() as con:
            cur = con.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM trades WHERE (settled=0 OR settled IS NULL) "
                "AND timestamp < datetime('now', '-7 days')"
            )
            (over_7d,) = cur.fetchone()
            cur.execute(
                "SELECT id, market_ticker, timestamp FROM trades "
                "WHERE (settled=0 OR settled IS NULL) "
                "AND timestamp < datetime('now', '-1 day') ORDER BY timestamp"
            )
            over_24h = cur.fetchall()
    except sqlite3.Error as e:
        return FLAG, [f"- DB error: {e}"]

    lines.append(f"- Pending >7d: **{over_7d}** (B1 invariant: must be 0)")
    if over_24h:
        # KXBTCMAXMON-26APR30 are expected to be open longer than 24h —
        # they settle April 30. KXBTCD daily contracts settle within ~24h
        # so a daily contract still pending past 24h is suspicious.
        suspicious = [r for r in over_24h if "26APR30" not in (r[1] or "")]
        lines.append(
            f"- Pending >24h: **{len(over_24h)}** "
            f"({len(over_24h) - len(suspicious)} expected long-duration, "
            f"{len(suspicious)} unexpected)"
        )
        if suspicious:
            lines.append("")
            lines.append("| id | ticker | opened |")
            lines.append("|---:|---|---|")
            for tid, ticker, ts in suspicious:
                lines.append(f"| {tid} | `{ticker}` | {ts} |")
            return FLAG, lines
    else:
        lines.append("- Pending >24h: **0**")

    if over_7d > 0:
        return PROB, lines
    return OK, lines


def section_scheduler(log_path: Optional[Path]) -> tuple[str, list[str]]:
    if log_path is None:
        return FLAG, ["- No `overnight-*.log` found in repo root."]
    lines: list[str] = []
    lines.append(f"Latest log: `{log_path.name}` "
                 f"(size {log_path.stat().st_size // 1024} KB)\n")
    counts = {
        "scan_and_trade_job": _grep_count(log_path, "scan_and_trade_job.*executed successfully"),
        "settlement_job": _grep_count(log_path, "settlement_job.*executed successfully"),
        "mc_scan_and_trade_job": _grep_count(log_path, "mc_scan_and_trade_job.*executed successfully"),
        "heartbeat_job": _grep_count(log_path, "heartbeat_job.*executed successfully"),
    }
    missed = _grep_count(log_path, "was missed by")
    lines.append("| job | successful firings |")
    lines.append("|---|---:|")
    for k, v in counts.items():
        lines.append(f"| {k} | {v} |")
    lines.append("")
    lines.append(f"- `was missed by` warnings: **{missed}**")
    if missed > 0:
        return FLAG, lines + [
            "- Non-zero missed-by warnings — possible scheduler pressure or sleep event."
        ]
    return OK, lines


def section_reload_sentinel(log_path: Optional[Path]) -> tuple[str, list[str]]:
    """B3 regression sentinel — these should all be 0/1 post-B3."""
    if log_path is None:
        return FLAG, ["- No log to check."]
    watch = _grep_count(log_path, "WatchFiles")
    reloading = _grep_count(log_path, "Reloading")
    startups = _grep_count(log_path, "Application startup complete")
    reloader = _grep_count(log_path, "Started reloader process")
    lines = [
        f"- `WatchFiles` lines: **{watch}** (expected 0)",
        f"- `Reloading` lines: **{reloading}** (expected 0)",
        f"- `Application startup complete`: **{startups}** (expected 1, possibly 2 after a manual restart)",
        f"- `Started reloader process`: **{reloader}** (expected 0)",
    ]
    if watch > 0 or reloading > 0 or reloader > 0 or startups > 2:
        return PROB, lines + ["- B3 fix has regressed — uvicorn is reloading."]
    return OK, lines


def section_cache(log_path: Optional[Path]) -> tuple[str, list[str]]:
    if log_path is None:
        return FLAG, ["- No log to check."]
    miss = _grep_count(log_path, "cache miss")
    stale = _grep_count(log_path, "stale")
    lines = [
        f"- `cache miss` lines: **{miss}**",
        f"- `stale` warnings: **{stale}**",
    ]
    if stale > 0:
        return FLAG, lines
    return OK, lines


def section_errors(log_path: Optional[Path]) -> tuple[str, list[str]]:
    if log_path is None:
        return FLAG, ["- No log to check."]
    errors = _grep_count(log_path, "ERROR")
    tracebacks = _grep_count(log_path, "Traceback")
    failed = _grep_count(log_path, "Failed to")
    # KXBTCY 429 rate-limits are known-benign; subtract from "Failed to".
    benign_429 = _grep_count(log_path, "Failed to fetch Kalshi series KXBTCY")
    unknown_failed = max(0, failed - benign_429)
    lines = [
        f"- `ERROR` lines: **{errors}**",
        f"- `Traceback` lines: **{tracebacks}**",
        f"- `Failed to` lines: **{failed}** ({benign_429} known-benign KXBTCY 429s, "
        f"**{unknown_failed} unrecognized**)",
    ]
    if errors > 0 or tracebacks > 0 or unknown_failed > 0:
        return FLAG, lines
    return OK, lines


def section_api_surface() -> tuple[str, list[str]]:
    endpoints = [
        ("/api/health", _HEALTH_URL),
        ("/api/dashboard", _DASHBOARD_URL),
        ("/api/microstructure?underlying=BTC", _MICRO_URL),
        ("/api/mc/portfolio", _PORTFOLIO_URL),
        ("/api/stats", _STATS_URL),
    ]
    lines = ["| endpoint | status | latency |", "|---|---:|---:|"]
    bad = 0
    for label, url in endpoints:
        status, elapsed, _ = _http_get(url)
        if status == 200:
            lines.append(f"| `{label}` | 200 | {elapsed*1000:.0f}ms |")
        else:
            bad += 1
            lines.append(f"| `{label}` | **{status}** | — |")
    if bad > 0:
        return FLAG, lines
    return OK, lines


def section_performance() -> tuple[str, list[str]]:
    timings: list[float] = []
    for _ in range(3):
        _, elapsed, _ = _http_get(_DASHBOARD_URL)
        if elapsed is not None:
            timings.append(elapsed * 1000)
    if not timings:
        return FLAG, ["- Could not measure dashboard latency."]
    line = "- /api/dashboard: " + " / ".join(f"{t:.0f}ms" for t in timings)
    if max(timings) > 500:
        return FLAG, [line, "- Latency >500ms exceeds P5 baseline."]
    return OK, [line]


def section_disk() -> tuple[str, list[str]]:
    lines: list[str] = []
    try:
        db_size_mb = _DB_PATH.stat().st_size / 1024 / 1024
        lines.append(f"- `tradingbot.db`: **{db_size_mb:.1f} MB**")
    except OSError:
        lines.append("- `tradingbot.db`: missing")

    log_files = list(_REPO_ROOT.glob("overnight-*.log"))
    total_log_mb = sum(p.stat().st_size for p in log_files) / 1024 / 1024
    lines.append(f"- {len(log_files)} overnight logs, total **{total_log_mb:.1f} MB**")

    try:
        usage = shutil.disk_usage(_REPO_ROOT)
        free_gb = usage.free / (1024 ** 3)
        lines.append(f"- Disk free: **{free_gb:.1f} GB**")
        if free_gb < 5:
            return FLAG, lines + ["- Less than 5 GB free."]
    except OSError:
        pass

    return OK, lines


def _render(
    title: str,
    sections: list[tuple[str, str, list[str]]],
    snapshot: dict,
) -> str:
    out: list[str] = []
    snapshot_safe = {k: v for k, v in snapshot.items()
                     if isinstance(v, (int, float, str))}
    out.append(f"<!-- BOT_STATE_SNAPSHOT {json.dumps(snapshot_safe)} -->")
    out.append(f"# {title}")
    out.append("")

    markers = [m for _, m, _ in sections]
    if any(m == PROB for m in markers):
        overall = "🚨 ATTENTION NEEDED"
    elif any(m == FLAG for m in markers):
        overall = "⚠️ MINOR FLAGS"
    else:
        overall = "✅ ALL GREEN"
    out.append(f"**Overall status:** {overall}")
    out.append("")

    flag_sections: list[str] = []
    for name, marker, _ in sections:
        if marker in (FLAG, PROB):
            flag_sections.append(f"- {name} ({marker})")
    if flag_sections:
        out.append("**Sections to look at:**")
        out.extend(flag_sections)
        out.append("")

    for i, (name, marker, lines) in enumerate(sections, start=1):
        out.append(f"## {i}. {name} — {marker}")
        out.extend(lines)
        out.append("")

    return "\n".join(out) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "output", nargs="?", default=None,
        help="Output markdown path. Default: ~/Desktop/bot-reports/morning-YYYY-MM-DD.md",
    )
    args = ap.parse_args(argv)

    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    if args.output:
        out_path = Path(args.output).expanduser()
    else:
        out_path = Path.home() / "Desktop" / "bot-reports" / f"morning-{date_str}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    prev_path = _find_previous_report(out_path.parent, out_path)
    prev_snapshot = _parse_snapshot(prev_path) if prev_path else None
    if prev_snapshot and "captured_utc" in prev_snapshot:
        try:
            since_utc = datetime.fromisoformat(prev_snapshot["captured_utc"])
        except (TypeError, ValueError):
            since_utc = datetime.utcnow() - timedelta(hours=24)
    else:
        since_utc = datetime.utcnow() - timedelta(hours=24)

    title = f"Morning Report — {now.strftime('%a %b %d %Y %H:%M %Z')}".rstrip()
    if not now.strftime("%Z"):
        title = f"Morning Report — {now.strftime('%a %b %d %Y %H:%M')}"

    sections: list[tuple[str, str, list[str]]] = []

    proc_marker, proc_lines, dead = section_process_health()
    sections.append(("Process health", proc_marker, proc_lines))
    if dead:
        snapshot = {"captured_utc": datetime.utcnow().isoformat(timespec="seconds")}
        body = _render(
            title,
            sections + [("Report aborted", PROB,
                         ["- Bot is not responding. Skipping all downstream checks."])],
            snapshot,
        )
        out_path.write_text(body)
        print(f"Bot is dead — minimal report written to {out_path}")
        return 1

    log_path = _find_latest_log()

    bs_marker, bs_lines, snapshot = section_botstate_and_trades(prev_snapshot)
    sections.append(("BotState delta", bs_marker, bs_lines))

    ot_marker, ot_lines = section_overnight_activity(since_utc)
    sections.append(("Overnight trade activity", ot_marker, ot_lines))

    op_marker, op_lines = section_open_positions()
    sections.append(("Currently open positions", op_marker, op_lines))

    sp_marker, sp_lines = section_stuck_pending()
    sections.append(("Stuck pending check", sp_marker, sp_lines))

    sch_marker, sch_lines = section_scheduler(log_path)
    sections.append(("Scheduler health", sch_marker, sch_lines))

    rs_marker, rs_lines = section_reload_sentinel(log_path)
    sections.append(("Reload disruption (B3 sentinel)", rs_marker, rs_lines))

    c_marker, c_lines = section_cache(log_path)
    sections.append(("Cache health", c_marker, c_lines))

    e_marker, e_lines = section_errors(log_path)
    sections.append(("Errors / tracebacks", e_marker, e_lines))

    api_marker, api_lines = section_api_surface()
    sections.append(("API surface", api_marker, api_lines))

    perf_marker, perf_lines = section_performance()
    sections.append(("Performance", perf_marker, perf_lines))

    disk_marker, disk_lines = section_disk()
    sections.append(("Disk", disk_marker, disk_lines))

    body = _render(title, sections, snapshot)
    out_path.write_text(body)
    print(f"Report written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
