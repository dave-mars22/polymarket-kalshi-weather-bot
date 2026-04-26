"""BRTI vs Coinbase BTC-USD diagnostic for KXBTCD daily settlements.

Slice T3 (2026-04-25). Motivated by the TODO flagged in RESEARCH_NOTES
Section 3i: the bot prices Kalshi BTC barriers using Coinbase BTC-USD
spot, but Kalshi resolves these contracts using CF Benchmarks BRTI
(a 60-second average of CF's Bitcoin Real-Time Index). If the two
sources diverge meaningfully at resolution time, the bot is computing
prices against one number while Kalshi resolves against a different
number — a hidden source of calibration error unrelated to model quality.

This script: query the bot's tradingbot.db for settled KXBTCD-* trades,
fetch each contract's expiration_value from Kalshi's public market endpoint,
fetch the Coinbase BTC-USD 1-minute candle close at the same UTC minute,
report per-contract gap and aggregate stats.

Re-runnable as more contracts settle — fetches fresh API data each run,
does NOT write to the production database.

Limitations:
- Kalshi BRTI is a 60-second average of CF Benchmarks' Bitcoin Real-Time
  Index. Coinbase reference here is a 1-minute candle close (Coinbase
  Exchange API doesn't expose finer granularity than 60s). Apples-to-
  oranges in window definition; magnitude should still be informative.
- Coinbase BTC-USD is one of CF Benchmarks' constituent exchanges, so
  the two series should track closely by construction. A persistent
  large gap would point at execution-time issues (which Coinbase tick?
  settlement minute clock drift?), not at fundamentally different price
  discovery.

Usage:
    venv/bin/python backtest/brti_vs_coinbase.py
    venv/bin/python backtest/brti_vs_coinbase.py --csv-out /tmp/x.csv --md-out /tmp/x.md
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
COINBASE_BASE = "https://api.exchange.coinbase.com"

# KXBTCD daily contracts close at 17:00 in the New York wall-clock zone.
# zoneinfo + tzdata handle the EDT/EST transition automatically across
# the year so the script stays correct for future settlements.
NY_TZ = ZoneInfo("America/New_York")

# Polite delays between API calls to stay well clear of rate limits even
# as the settled-contracts list grows. Both Kalshi and Coinbase are
# generous with their public read limits but free hits are free hits.
SLEEP_BETWEEN_CALLS_S = 0.2

# Months to numeric for ticker date parsing.
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def query_settled_kxbtcd(db_path: str) -> list[dict]:
    """Read all settled KXBTCD-* trades from the bot's database. Read-only;
    we never write back. SQLite handles concurrent reads fine while the
    live bot has the same file open."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, market_ticker, direction, entry_price, size, pnl,
               settlement_value, datetime(settlement_time) AS settlement_time
        FROM trades
        WHERE settled = 1 AND market_ticker LIKE 'KXBTCD-%'
        ORDER BY settlement_time
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def parse_threshold_from_ticker(ticker: str) -> float | None:
    """KXBTCD-26APR2517-T78749.99 -> 78749.99. Returns None on unexpected
    ticker shape rather than raising; the script logs and continues."""
    parts = ticker.split("-")
    if len(parts) < 3:
        return None
    last = parts[-1]
    if not last.startswith("T"):
        return None
    try:
        return float(last[1:])
    except ValueError:
        return None


def settlement_close_utc_for(ticker: str) -> datetime | None:
    """Parse the close time encoded in the ticker. KXBTCD-26APR2517-...
    means April 25 2026 at 17:00 in the New York wall-clock zone (i.e.,
    17:00 EDT in summer or 17:00 EST in winter). Returns a UTC datetime."""
    parts = ticker.split("-")
    if len(parts) < 2:
        return None
    date_part = parts[1]
    if len(date_part) != 9:
        return None
    try:
        yy = int(date_part[0:2])
        mon = date_part[2:5].upper()
        dd = int(date_part[5:7])
        hh_local = int(date_part[7:9])
    except ValueError:
        return None
    mm = _MONTHS.get(mon)
    if mm is None:
        return None
    yyyy = 2000 + yy
    local_dt = datetime(yyyy, mm, dd, hh_local, 0, 0, tzinfo=NY_TZ)
    return local_dt.astimezone(timezone.utc)


def fetch_kalshi_expiration_value(client: httpx.Client, ticker: str) -> float | None:
    """Returns Kalshi's reported expiration_value (the BRTI 60-second
    average at close time) as a float, or None on any error."""
    url = f"{KALSHI_BASE}/markets/{ticker}"
    try:
        resp = client.get(url, timeout=15.0)
        resp.raise_for_status()
        market = resp.json().get("market", {})
        v = market.get("expiration_value")
        return float(v) if v is not None else None
    except Exception as e:
        print(f"  WARN: Kalshi fetch failed for {ticker}: {e}", file=sys.stderr)
        return None


def fetch_coinbase_close_at(client: httpx.Client, settlement_dt_utc: datetime) -> float | None:
    """Fetch the Coinbase BTC-USD 1-minute candle whose start aligns with
    the settlement minute, return its close price.

    Coinbase candles API response: [[timestamp, low, high, open, close, volume], ...]
    newest-first. timestamp is the start of the candle in seconds since epoch.
    The 1-min candle whose start is `settlement_dt_utc` covers the settlement
    minute exactly; we ask for a 4-minute window centered on it to ensure
    the API returns at least the candle we want even if Coinbase's clock
    snaps to a slightly different minute boundary."""
    start = settlement_dt_utc - timedelta(minutes=2)
    end = settlement_dt_utc + timedelta(minutes=2)
    params = {
        "granularity": 60,
        "start": start.replace(tzinfo=timezone.utc).isoformat(),
        "end": end.replace(tzinfo=timezone.utc).isoformat(),
    }
    url = f"{COINBASE_BASE}/products/BTC-USD/candles"
    try:
        resp = client.get(url, params=params, timeout=15.0)
        resp.raise_for_status()
        candles = resp.json()
        if not candles:
            return None
        target_ts = settlement_dt_utc.replace(tzinfo=timezone.utc).timestamp()
        # Pick the candle whose start time is closest to settlement_dt_utc.
        best = min(candles, key=lambda c: abs(c[0] - target_ts))
        return float(best[4])  # close price
    except Exception as e:
        print(f"  WARN: Coinbase fetch failed for {settlement_dt_utc}: {e}", file=sys.stderr)
        return None


def analyze_one(client: httpx.Client, trade: dict) -> dict:
    """Returns a row dict suitable for both CSV and markdown rendering.
    Fields with `None` indicate the upstream API call failed; downstream
    aggregation skips these rows."""
    ticker = trade["market_ticker"]
    threshold = parse_threshold_from_ticker(ticker)
    close_dt = settlement_close_utc_for(ticker)

    print(
        f"Analyzing {ticker}  threshold=${threshold:,.2f}  close="
        f"{close_dt.isoformat() if close_dt else '?'}"
    )

    kalshi_brti = fetch_kalshi_expiration_value(client, ticker)
    time.sleep(SLEEP_BETWEEN_CALLS_S)

    coinbase = fetch_coinbase_close_at(client, close_dt) if close_dt else None
    time.sleep(SLEEP_BETWEEN_CALLS_S)

    row: dict = {
        "ticker": ticker,
        "threshold": threshold,
        "kalshi_brti": kalshi_brti,
        "coinbase_close": coinbase,
        "abs_gap_usd": None,
        "rel_gap_bps": None,
        "threshold_straddling": None,
        "settlement_value": trade["settlement_value"],
        "trade_pnl": trade["pnl"],
        "settlement_close_utc": close_dt.isoformat() if close_dt else None,
    }

    if kalshi_brti is None or coinbase is None:
        return row

    row["abs_gap_usd"] = abs(coinbase - kalshi_brti)
    row["rel_gap_bps"] = (coinbase - kalshi_brti) / kalshi_brti * 10000

    # Threshold-straddling: do the two prices land on different sides of
    # the contract's strike? If yes, the gap was large enough that the
    # choice of price source could have flipped the resolution.
    if threshold is not None:
        row["threshold_straddling"] = (
            (kalshi_brti >= threshold) != (coinbase >= threshold)
        )

    return row


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def render_markdown(rows: list[dict]) -> str:
    out: list[str] = []
    out.append("# BRTI vs Coinbase BTC-USD diagnostic\n")
    out.append(
        f"Generated {datetime.now(timezone.utc).replace(microsecond=0).isoformat()}\n"
    )
    out.append(f"Settled KXBTCD trades analyzed: **{len(rows)}**\n")
    out.append("")
    out.append("## Per-contract")
    out.append("")
    out.append(
        "| ticker | threshold | kalshi_brti | coinbase | abs gap (USD) | rel gap (bps) | straddles strike? |"
    )
    out.append(
        "|---|---:|---:|---:|---:|---:|:---:|"
    )
    for r in rows:
        thr = f"${r['threshold']:,.2f}" if r["threshold"] is not None else "—"
        kb = f"${r['kalshi_brti']:,.2f}" if r["kalshi_brti"] is not None else "—"
        cb = f"${r['coinbase_close']:,.2f}" if r["coinbase_close"] is not None else "—"
        ag = f"${r['abs_gap_usd']:,.2f}" if r["abs_gap_usd"] is not None else "—"
        rg = f"{r['rel_gap_bps']:+.2f}" if r["rel_gap_bps"] is not None else "—"
        if r["threshold_straddling"] is True:
            st = "**YES**"
        elif r["threshold_straddling"] is False:
            st = "no"
        else:
            st = "—"
        out.append(
            f"| `{r['ticker']}` | {thr} | {kb} | {cb} | {ag} | {rg} | {st} |"
        )
    out.append("")

    valid = [r for r in rows if r["abs_gap_usd"] is not None]
    if valid:
        gaps = [r["abs_gap_usd"] for r in valid]
        bps = [r["rel_gap_bps"] for r in valid]
        bps_abs = [abs(b) for b in bps]
        n_straddle = sum(1 for r in valid if r["threshold_straddling"])
        out.append(f"## Aggregate (n = {len(valid)})")
        out.append("")
        out.append(f"- Mean abs gap: **${sum(gaps) / len(gaps):,.2f}**")
        out.append(f"- Max abs gap:  **${max(gaps):,.2f}**")
        out.append(f"- Mean abs(rel gap): **{sum(bps_abs) / len(bps_abs):.2f} bps**")
        out.append(f"- Max abs(rel gap): **{max(bps_abs):.2f} bps**")
        out.append(
            f"- Mean signed rel gap (Coinbase − BRTI): **{sum(bps) / len(bps):+.2f} bps** "
            "(positive = Coinbase systematically higher than BRTI)"
        )
        out.append(f"- Threshold-straddling gaps: **{n_straddle} of {len(valid)}**")
        out.append("")
        out.append("## Interpretation")
        out.append("")
        out.append(
            f"With n = {len(valid)} the data is too thin for firm conclusions. "
            "The question being asked is whether the typical Coinbase-vs-BRTI "
            "gap is small enough to be ignored (<10 bps, negligible for pricer "
            "calibration) or large enough to matter (>50 bps, materially affects "
            "whether the bot's GBM probabilities map cleanly to the distribution "
            "Kalshi resolves against). A handful of additional KXBTCD daily "
            "settlements over the coming days will firm up the estimate."
        )
        out.append("")
        out.append("## Caveats")
        out.append("")
        out.append(
            "- Kalshi BRTI is a 60-second average of CF Benchmarks' Bitcoin "
            "Real-Time Index. The Coinbase reference used here is a 1-minute "
            "candle close (Coinbase Exchange API does not expose finer "
            "granularity than 60s). Apples-to-oranges in window definition; "
            "the magnitude should still be informative."
        )
        out.append(
            "- Coinbase BTC-USD is one of CF Benchmarks' constituent exchanges, "
            "so the two series should track closely by construction. A "
            "persistent large gap would point at execution-time issues (which "
            "Coinbase tick? settlement-minute clock drift?), not at "
            "fundamentally different price discovery."
        )
    else:
        out.append("## Aggregate")
        out.append("")
        out.append(
            "No contracts had complete data (Kalshi + Coinbase both fetched "
            "successfully). Check the warnings above and re-run."
        )
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default="tradingbot.db",
        help="path to bot's SQLite DB (read-only). Default: tradingbot.db",
    )
    parser.add_argument(
        "--csv-out", default="/tmp/brti_vs_coinbase.csv",
        help="path for per-contract CSV output. Default: /tmp/brti_vs_coinbase.csv",
    )
    parser.add_argument(
        "--md-out", default=None,
        help="optional path to also save the markdown report",
    )
    args = parser.parse_args()

    trades = query_settled_kxbtcd(args.db)
    print(f"Found {len(trades)} settled KXBTCD trade(s).\n")
    if not trades:
        print("No settled KXBTCD trades to analyze. Re-run after settlements accumulate.")
        return 0

    rows: list[dict] = []
    with httpx.Client() as client:
        for t in trades:
            rows.append(analyze_one(client, t))

    csv_path = Path(args.csv_out)
    write_csv(rows, csv_path)
    print(f"\nCSV written: {csv_path}")

    md = render_markdown(rows)
    print()
    print(md)
    if args.md_out:
        Path(args.md_out).write_text(md)
        print(f"Markdown written: {args.md_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
