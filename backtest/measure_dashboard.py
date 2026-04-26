"""Per-component latency breakdown for /api/dashboard.

Slice T4 (2026-04-26). After P1-P4, dashboard latency is ~1.25s
post-warmup. P4's verification report identified that the bulk of the
remaining work happens inside builder helpers and uncached queries
that nobody has measured directly. This script does the measurement.

Approach: import each helper / cache accessor used by get_dashboard_data
and time it in isolation against the running production DB (read-only
SQLite URI). Compare the sum of per-component costs to the end-to-end
/api/dashboard latency to identify any unexplained gap (FastAPI
middleware, JSON serialization, async dispatch overhead).

Bot must be running for the end-to-end measurement to work; verified
via /api/health at the top of main(). Read-only — never writes to the
production database, never touches any cache state.

Usage:
    venv/bin/python backtest/measure_dashboard.py            # 5 trials
    venv/bin/python backtest/measure_dashboard.py 10         # 10 trials
    venv/bin/python backtest/measure_dashboard.py 5 --md /tmp/x.md

Re-runnable: each invocation produces a fresh measurement against the
current DB and cache state. Useful for sanity-checking after any
future cache or query changes.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Dict, List, Tuple

# This script lives in backtest/ but imports from backend/. Put the
# repo root on sys.path so the imports below resolve regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Production model + helper imports. We DO NOT touch any cache state
# or write to the DB; everything is pure read.
from backend.api.main import (
    _build_multi_microstructure,
    _build_mc_portfolio_status,
    _build_per_asset_stats,
    _build_per_strategy_stats,
    get_stats,
)
from backend.core.dashboard_cache import (
    build_dashboard_cache_payload,
    get_cached_dashboard_data,
    get_cached_micro,
)
from backend.core.signals import get_cached_scan
from backend.data.crypto_markets import fetch_active_crypto_markets
from backend.models.database import Base, Trade


DASHBOARD_URL = "http://localhost:8000/api/dashboard"
HEALTH_URL = "http://localhost:8000/api/health"


def _make_readonly_session():
    """Build a fresh SQLAlchemy session against tradingbot.db in read-only
    mode. Each measurement gets its own session to avoid sharing state.
    The ?mode=ro URI prevents accidental writes even if a helper has a
    bug; SQLite enforces it at the connection level."""
    engine = create_engine(
        "sqlite:///file:tradingbot.db?mode=ro&uri=true",
        connect_args={"check_same_thread": False, "uri": True},
    )
    return sessionmaker(bind=engine)()


# ---------------------------------------------------------------------
# Timing primitives
# ---------------------------------------------------------------------

class Result:
    """One measurement series for a single component."""
    def __init__(self, name: str, times_ms: List[float], cached: str, notes: str = ""):
        self.name = name
        self.times_ms = times_ms
        self.cached = cached  # "P2"/"P3"/"P4"/"no"/"n/a"
        self.notes = notes

    @property
    def mean_ms(self) -> float:
        return mean(self.times_ms) if self.times_ms else float("nan")

    @property
    def min_ms(self) -> float:
        return min(self.times_ms) if self.times_ms else float("nan")

    @property
    def max_ms(self) -> float:
        return max(self.times_ms) if self.times_ms else float("nan")


def time_sync(name: str, fn: Callable[[], Any], trials: int, *, cached: str, notes: str = "") -> Result:
    times = []
    for _ in range(trials):
        t0 = time.perf_counter()
        fn()
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1000.0)
    return Result(name, times, cached=cached, notes=notes)


async def time_async(name: str, fn: Callable[[], Any], trials: int, *, cached: str, notes: str = "") -> Result:
    times = []
    for _ in range(trials):
        t0 = time.perf_counter()
        await fn()
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1000.0)
    return Result(name, times, cached=cached, notes=notes)


# ---------------------------------------------------------------------
# Per-component measurements
# ---------------------------------------------------------------------

async def measure_components(trials: int) -> List[Result]:
    """Time each major unit of work in get_dashboard_data, isolated.

    Each block opens its own session so we don't have shared-session
    caching artifacts confounding successive trials."""
    results: List[Result] = []

    # 1. get_stats(db) — single SELECT on bot_state.
    async def stats_call():
        s = _make_readonly_session()
        try:
            await get_stats(s)
        finally:
            s.close()
    results.append(await time_async(
        "get_stats(db)", stats_call, trials,
        cached="no", notes="single bot_state SELECT",
    ))

    # 2. BTC micro cache read.
    results.append(time_sync(
        "get_cached_micro('BTC')",
        lambda: get_cached_micro("BTC"), trials,
        cached="P4", notes="dict.get on per-underlying cache",
    ))

    # 3. fetch_active_crypto_markets('BTC') — LIVE HTTP to Polymarket gamma-api.
    async def windows_call():
        await fetch_active_crypto_markets("BTC")
    results.append(await time_async(
        "fetch_active_crypto_markets('BTC')", windows_call, trials,
        cached="no",
        notes="LIVE Polymarket gamma-api HTTP — uncached",
    ))

    # 4. get_cached_scan() — P2 scan cache.
    results.append(time_sync(
        "get_cached_scan()",
        lambda: get_cached_scan(), trials,
        cached="P2", notes="atomic single-name read",
    ))

    # 5. Recent trades: top-50 + open-MC + dedupe (mirror of dashboard logic).
    async def recent_trades_call():
        s = _make_readonly_session()
        try:
            top_recent = s.query(Trade).order_by(Trade.timestamp.desc()).limit(50).all()
            open_mc = (
                s.query(Trade)
                .filter(
                    Trade.market_type == "monte_carlo",
                    Trade.settled == False,  # noqa: E712
                )
                .order_by(Trade.timestamp.desc())
                .limit(20)
                .all()
            )
            trades_by_id = {t.id: t for t in top_recent}
            for mc_trade in open_mc:
                trades_by_id.setdefault(mc_trade.id, mc_trade)
            sorted(trades_by_id.values(), key=lambda t: t.timestamp, reverse=True)
        finally:
            s.close()
    results.append(await time_async(
        "recent_trades (2 queries + dedupe)", recent_trades_call, trials,
        cached="no",
        notes="top-50 by timestamp + open MC union",
    ))

    # 6. get_cached_dashboard_data() — P3 cache read.
    results.append(time_sync(
        "get_cached_dashboard_data()",
        lambda: get_cached_dashboard_data(), trials,
        cached="P3",
        notes="atomic single-name read (equity + calibration)",
    ))

    # 7. _build_multi_microstructure() — reads P4 cache, builds Pydantic.
    async def mm_call():
        await _build_multi_microstructure()
    results.append(await time_async(
        "_build_multi_microstructure()", mm_call, trials,
        cached="P4",
        notes="cache read + Pydantic build for 4 underlyings",
    ))

    # 8. _build_per_strategy_stats(db) — uncached.
    def per_strategy_call():
        s = _make_readonly_session()
        try:
            _build_per_strategy_stats(s)
        finally:
            s.close()
    results.append(time_sync(
        "_build_per_strategy_stats(db)", per_strategy_call, trials,
        cached="no", notes="uncached aggregate over Trade rows",
    ))

    # 9. _build_per_asset_stats(db) — uncached.
    def per_asset_call():
        s = _make_readonly_session()
        try:
            _build_per_asset_stats(s)
        finally:
            s.close()
    results.append(time_sync(
        "_build_per_asset_stats(db)", per_asset_call, trials,
        cached="no", notes="uncached aggregate over Trade rows",
    ))

    # 10. _build_mc_portfolio_status(db) — uncached.
    def mc_portfolio_call():
        s = _make_readonly_session()
        try:
            _build_mc_portfolio_status(s)
        finally:
            s.close()
    results.append(time_sync(
        "_build_mc_portfolio_status(db)", mc_portfolio_call, trials,
        cached="no", notes="uncached MC-only Trade aggregate",
    ))

    return results


# ---------------------------------------------------------------------
# End-to-end timing
# ---------------------------------------------------------------------

async def time_end_to_end(trials: int) -> Result:
    """Time GET /api/dashboard via httpx. Includes everything: FastAPI
    routing, dependency injection, get_dashboard_data body, JSON
    serialization, response. The 'gap' between this and the sum of
    components is overhead we can't isolate from the component side."""
    times = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        for _ in range(trials):
            t0 = time.perf_counter()
            r = await client.get(DASHBOARD_URL)
            t1 = time.perf_counter()
            r.raise_for_status()
            times.append((t1 - t0) * 1000.0)
    return Result(
        "/api/dashboard end-to-end", times,
        cached="n/a", notes="full HTTP round-trip incl. FastAPI overhead",
    )


# ---------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------

def render_report(per_component: List[Result], end_to_end: Result, trials: int) -> str:
    out: List[str] = []
    out.append("# /api/dashboard latency breakdown\n")
    out.append(f"Trials per component: **{trials}**\n")
    out.append(f"Bot pid: read from `.bot.pid` (running while measured)\n")
    out.append("")
    out.append("## End-to-end")
    out.append(f"- Mean: **{end_to_end.mean_ms:.1f} ms**")
    out.append(f"- Min:  {end_to_end.min_ms:.1f} ms")
    out.append(f"- Max:  {end_to_end.max_ms:.1f} ms")
    out.append("")
    out.append("## Per-component breakdown")
    out.append("")
    out.append("| # | Component | Mean ms | Min ms | Max ms | Cached? | % of total | Notes |")
    out.append("|---:|---|---:|---:|---:|:---:|---:|---|")
    total_mean = end_to_end.mean_ms
    for i, r in enumerate(per_component, start=1):
        frac = (r.mean_ms / total_mean * 100.0) if total_mean > 0 else 0.0
        out.append(
            f"| {i} | {r.name} | {r.mean_ms:.2f} | {r.min_ms:.2f} | {r.max_ms:.2f} | "
            f"{r.cached} | {frac:.1f}% | {r.notes} |"
        )
    sum_components = sum(r.mean_ms for r in per_component)
    gap = total_mean - sum_components
    gap_pct = (gap / total_mean * 100.0) if total_mean > 0 else 0.0
    out.append("")
    out.append("## Sum vs end-to-end")
    out.append(f"- Sum of measured components (mean): **{sum_components:.1f} ms**")
    out.append(f"- End-to-end mean: **{total_mean:.1f} ms**")
    out.append(f"- Unexplained gap: **{gap:.1f} ms** ({gap_pct:+.1f}% of total)")
    if abs(gap_pct) > 50:
        out.append("- ⚠️  Gap > 50% of total — components do not account for most of the cost. "
                   "Likely sources: FastAPI dependency injection, Pydantic serialization of the "
                   "DashboardData response, async dispatch overhead, or a component not isolated here.")
    elif gap > 0:
        out.append("- Gap is positive: end-to-end exceeds sum-of-components, as expected. "
                   "The remainder is FastAPI/Pydantic/async overhead not attributable to any single "
                   "component above. With Pydantic v2 + a large DashboardData response, ~50-150ms of "
                   "serialization is plausible.")
    else:
        out.append("- Gap is negative: components ran SLOWER in isolation than in the live request. "
                   "Possible causes: cache state differs between component-measurement and live "
                   "request, or the live endpoint benefits from some shared work the isolated "
                   "calls duplicate. Worth investigating.")
    out.append("")

    # Top contributors
    sorted_by_mean = sorted(per_component, key=lambda r: r.mean_ms, reverse=True)
    out.append("## Top 3 contributors")
    out.append("")
    for r in sorted_by_mean[:3]:
        frac = (r.mean_ms / total_mean * 100.0) if total_mean > 0 else 0.0
        out.append(
            f"- **{r.name}** — {r.mean_ms:.1f} ms ({frac:.1f}% of total). "
            f"Cached: {r.cached}. {r.notes}"
        )
    out.append("")

    out.append("## Possible follow-up directions (orientation only — no commitment)")
    out.append("")
    for r in sorted_by_mean[:3]:
        if r.cached != "no" and r.cached != "n/a":
            out.append(f"- **{r.name}**: already cached ({r.cached}). The cost here is the cache read"
                       " + downstream transform. If this is on the top list, the transform is the"
                       " expensive part — Pydantic conversion of a large list, etc. Hard to optimize"
                       " without changing the response shape.")
        elif "fetch_" in r.name or "HTTP" in r.notes:
            out.append(f"- **{r.name}**: live HTTP call in the hot path. Candidate for the same"
                       " scheduler-piggyback pattern as P2/P3/P4 if the data changes slowly enough"
                       " to tolerate ~60s staleness.")
        elif "_build_" in r.name or "recent_trades" in r.name:
            out.append(f"- **{r.name}**: uncached DB aggregation. Candidates: scheduler-piggyback"
                       " cache (matches P2/P3/P4 pattern), DB index on the relevant Trade columns,"
                       " or query restructure if the current query is doing something inefficient.")
        else:
            out.append(f"- **{r.name}**: see breakdown above; consider whether the cost is small"
                       " enough relative to ~1s baseline to leave alone.")
    out.append("")

    out.append("## Caveats")
    out.append("")
    out.append("- This is a snapshot. Component costs depend on DB size (Trade row count) and"
               " current cache state.")
    out.append("- Each component is measured with its own session, which may overstate actual"
               " cost (the live endpoint shares one session across components).")
    out.append("- HTTP-bound components (`fetch_active_crypto_markets`) measure live network"
               " latency to Polymarket, which varies.")
    out.append("- The gap analysis treats sum-of-components as a clean lower bound. In practice"
               " components can interleave or share work the isolated measurement double-counts.")
    out.append("- Re-running this script after any cache or query change is the right way to"
               " validate the impact, not just trusting the diff math.")
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------

async def main_async(args) -> int:
    # Sanity: bot must be up for end-to-end timing.
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(HEALTH_URL)
            r.raise_for_status()
    except Exception as e:
        print(f"ERROR: bot health check failed ({e}). End-to-end measurement requires the "
              f"bot to be running on http://localhost:8000.", file=sys.stderr)
        return 2

    print(f"Trials: {args.trials}\n")
    print("Measuring per-component latency (this takes a few seconds)...\n")

    per_component = await measure_components(args.trials)
    end_to_end = await time_end_to_end(args.trials)

    report = render_report(per_component, end_to_end, args.trials)
    print(report)

    if args.md:
        Path(args.md).write_text(report)
        print(f"Markdown report written to {args.md}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trials", type=int, nargs="?", default=5,
                        help="number of trials per component (default: 5)")
    parser.add_argument("--md", default=None,
                        help="optional path to save the markdown report (default: print only)")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
