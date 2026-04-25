"""Trade replay tool — apply hypothetical filter parameters to actual trade
history and compare metrics against the live baseline.

This is *not* a backtester. It cannot generate hypothetical trades a
different strategy would have taken. It only filters the trades that
actually happened. Use it to ask "what if I had been stricter with X?"
and get a directional answer; do not use it to validate that a parameter
change will improve future performance (see CAVEATS at end of run).

Usage:
    venv/bin/python -m backend.analysis.trade_replay
    venv/bin/python backend/analysis/trade_replay.py

Programmatic:
    from backend.analysis.trade_replay import Scenario, run_scenario, load_btc_settled
    trades = load_btc_settled()
    metrics = run_scenario(Scenario(name="my-test", min_edge=0.07), trades)
    print(metrics)

The DB session is opened read-only — no rows are modified, no commits issued.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import List, Optional, Set, Tuple

from backend.models.database import SessionLocal, Trade


# ---------------------------------------------------------------------------
# Filter parameters
# ---------------------------------------------------------------------------

@dataclass
class Scenario:
    """Set of filter parameters defining a hypothetical configuration.

    Defaults match current production settings (slice S1):
      - min_edge=0.05       (the threshold raised from 0.02 in slice S1)
      - max_entry_price=0.55 (config.MAX_ENTRY_PRICE)
      - all directions / hours / weekdays allowed

    A Scenario with no overrides is the BASELINE.
    """
    name: str
    min_edge: float = 0.05
    max_edge: Optional[float] = None
    max_entry_price: float = 0.55
    min_entry_price: Optional[float] = None
    allowed_directions: Set[str] = field(
        default_factory=lambda: {"up", "down"}
    )
    allowed_hours_utc: Set[int] = field(
        default_factory=lambda: set(range(24))
    )
    allowed_weekdays: Set[int] = field(
        default_factory=lambda: set(range(7))
    )


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------

@dataclass
class Metrics:
    """Aggregate stats for a filtered slice of trades."""
    name: str
    n: int
    wins: int
    losses: int
    win_rate: Optional[float]
    ci_lo: Optional[float]
    ci_hi: Optional[float]
    half_width: Optional[float]
    total_pnl: float
    avg_pnl: float
    total_notional: float


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def wilson_ci(wins: int, n: int, z: float = 1.96) -> Tuple[Optional[float], Optional[float]]:
    """95% Wilson score interval for proportion. Returns (lo, hi) or
    (None, None) for n=0. Same math used across diagnostics in this repo."""
    if n == 0:
        return (None, None)
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


# ---------------------------------------------------------------------------
# Data loading + filtering
# ---------------------------------------------------------------------------

def load_btc_settled() -> List[Trade]:
    """Load every settled BTC trade with the fields needed for replay.

    Read-only: opens a session, queries, closes. No commits.
    """
    db = SessionLocal()
    try:
        return (
            db.query(Trade)
            .filter(
                Trade.market_type == "btc",
                Trade.settled == True,  # noqa: E712
                Trade.edge_at_entry.isnot(None),
                Trade.model_probability.isnot(None),
            )
            .all()
        )
    finally:
        db.close()


def matches(trade: Trade, scenario: Scenario) -> bool:
    """Return True if `trade` would be kept under `scenario`'s filters."""
    edge = abs(trade.edge_at_entry)
    if edge < scenario.min_edge:
        return False
    if scenario.max_edge is not None and edge > scenario.max_edge:
        return False
    if trade.entry_price > scenario.max_entry_price:
        return False
    if scenario.min_entry_price is not None and trade.entry_price < scenario.min_entry_price:
        return False
    if trade.direction not in scenario.allowed_directions:
        return False
    if trade.timestamp.hour not in scenario.allowed_hours_utc:
        return False
    if trade.timestamp.weekday() not in scenario.allowed_weekdays:
        return False
    return True


def compute_metrics(name: str, trades: List[Trade]) -> Metrics:
    n = len(trades)
    wins = sum(1 for t in trades if t.result == "win")
    losses = sum(1 for t in trades if t.result == "loss")
    settled_outcomes = wins + losses
    total_pnl = sum((t.pnl or 0.0) for t in trades)
    notional = sum(t.size for t in trades)
    avg_pnl = (total_pnl / n) if n else 0.0
    if settled_outcomes:
        wr = wins / settled_outcomes
        lo, hi = wilson_ci(wins, settled_outcomes)
        half_width = (hi - lo) / 2.0 if (lo is not None and hi is not None) else None
    else:
        wr = lo = hi = half_width = None
    return Metrics(
        name=name, n=n, wins=wins, losses=losses,
        win_rate=wr, ci_lo=lo, ci_hi=hi, half_width=half_width,
        total_pnl=total_pnl, avg_pnl=avg_pnl, total_notional=notional,
    )


def run_scenario(scenario: Scenario, trades: Optional[List[Trade]] = None) -> Metrics:
    """Apply scenario filters and compute metrics. Loads trades from the DB
    if `trades` not supplied — pass a pre-loaded list to avoid hitting the
    DB once per scenario in batch comparisons."""
    if trades is None:
        trades = load_btc_settled()
    kept = [t for t in trades if matches(t, scenario)]
    return compute_metrics(scenario.name, kept)


# ---------------------------------------------------------------------------
# Default scenarios — edit this list to add new "what if" questions
# ---------------------------------------------------------------------------

# UTC hours flagged as worst by slice-D-era loss-clustering analysis.
BAD_UTC_HOURS = {8, 16, 20, 22}
# Mondays underperformed in the same analysis (45.4% over n=163).
BAD_WEEKDAYS = {0}  # 0 = Monday in datetime.weekday()


SCENARIOS: List[Scenario] = [
    Scenario(name="BASELINE (current production)"),
    Scenario(name="TIGHTER EDGE 0.06", min_edge=0.06),
    Scenario(name="TIGHTER EDGE 0.07", min_edge=0.07),
    Scenario(name="CAP EDGE @ 0.08", min_edge=0.05, max_edge=0.08),
    Scenario(name="CAP EDGE @ 0.07", min_edge=0.05, max_edge=0.07),
    Scenario(name="UP DIRECTION ONLY", allowed_directions={"up"}),
    Scenario(
        name="SKIP BAD HOURS (UTC 8/16/20/22)",
        allowed_hours_utc=set(range(24)) - BAD_UTC_HOURS,
    ),
    Scenario(
        name="SKIP MONDAYS",
        allowed_weekdays=set(range(7)) - BAD_WEEKDAYS,
    ),
    Scenario(
        name="COMBINED (5-8% edge, no bad hrs/Mondays)",
        min_edge=0.05, max_edge=0.08,
        allowed_hours_utc=set(range(24)) - BAD_UTC_HOURS,
        allowed_weekdays=set(range(7)) - BAD_WEEKDAYS,
    ),
]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def fmt_wr(m: Metrics) -> str:
    return f"{m.win_rate * 100:>5.1f}%" if m.win_rate is not None else "  —  "


def fmt_hw(m: Metrics) -> str:
    return f"±{m.half_width * 100:>4.1f}pp" if m.half_width is not None else "  —   "


def fmt_money(v: float, signed: bool = True) -> str:
    if signed:
        return f"{'+' if v >= 0 else '−'}${abs(v):>7.2f}"
    return f"${v:>7.2f}"


def fmt_money_compact(v: float, signed: bool = True) -> str:
    if signed:
        return f"{'+' if v >= 0 else '−'}${abs(v):.2f}"
    return f"${v:.2f}"


def render_comparison(results: List[Metrics]) -> str:
    """Build the comparison table string. results[0] is the baseline."""
    if not results:
        return "(no scenarios)"
    base = results[0]
    lines: List[str] = []

    # Header
    cols = (
        f"{'scenario':<42s}  {'n':>4s}  {'drop':>4s}  {'W/L':>9s}  "
        f"{'win%':>6s}  {'±CI':>7s}  {'PnL':>10s}  {'avg':>9s}  "
        f"{'ΔPnL':>10s}  {'Δwr':>6s}"
    )
    lines.append(cols)
    lines.append("-" * len(cols))

    for m in results:
        is_base = (m is base)
        n_drop = base.n - m.n
        d_pnl = m.total_pnl - base.total_pnl
        d_wr = (m.win_rate - base.win_rate) * 100 if (m.win_rate is not None and base.win_rate is not None) else None

        wl = f"{m.wins}/{m.losses}"
        d_pnl_str = "(base)" if is_base else fmt_money_compact(d_pnl)
        d_wr_str = "(base)" if is_base else (f"{d_wr:+.1f}pp" if d_wr is not None else "  —  ")
        drop_str = "—" if is_base else f"{n_drop}"

        lines.append(
            f"{m.name:<42s}  {m.n:>4d}  {drop_str:>4s}  {wl:>9s}  "
            f"{fmt_wr(m):>6s}  {fmt_hw(m):>7s}  "
            f"{fmt_money(m.total_pnl):>10s}  ${m.avg_pnl:>+7.4f}  "
            f"{d_pnl_str:>10s}  {d_wr_str:>6s}"
        )
    return "\n".join(lines)


def interpretation_hint(base: Metrics, m: Metrics) -> str:
    """One-line gist of what changed vs baseline. Caller skips baseline."""
    n_drop = base.n - m.n
    pct_drop = (n_drop / base.n * 100) if base.n else 0.0
    d_pnl = m.total_pnl - base.total_pnl
    d_wr = (m.win_rate - base.win_rate) * 100 if (m.win_rate and base.win_rate) else 0.0
    bits = [f"keeps {m.n} trades ({pct_drop:.0f}% dropped)"]
    if d_wr:
        bits.append(f"win rate {d_wr:+.1f}pp")
    bits.append(f"PnL {fmt_money_compact(d_pnl)} vs baseline")
    if m.n < 50:
        bits.append("⚠ n<50, low confidence")
    elif m.half_width and m.half_width > 0.10:
        bits.append("⚠ wide CI")
    return f"  {m.name}:  {', '.join(bits)}"


CAVEATS = """
⚠️ CAVEATS — read before drawing conclusions:
  1. This tool only filters trades that ACTUALLY HAPPENED under historical
     conditions. It cannot tell you what trades a different strategy WOULD
     HAVE GENERATED.
  2. Per-bucket sample sizes shrink as filters tighten — a "great" scenario
     with n<50 is statistically meaningless.
  3. Pre-rebuild trades (before ~Apr 24 03:37 UTC) had different sizing
     ($10+ avg) than post-rebuild ($1 avg). PnL comparisons are confounded
     by sizing changes.
  4. The 8-10% edge dip and hour/weekday patterns were DISCOVERED in this
     same data. Filtering on patterns you found in-sample is overfitting.
     To validate, a filtered scenario must beat baseline on FUTURE data.
  5. Use this tool to GENERATE HYPOTHESES to test on future data, not to
     make parameter changes today.
"""


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main(scenarios: Optional[List[Scenario]] = None) -> None:
    scenarios = scenarios or SCENARIOS
    trades = load_btc_settled()
    print(f"Loaded {len(trades)} settled BTC trades from DB (read-only).")
    print(f"Date range: "
          f"{min(t.timestamp for t in trades) if trades else '—'} → "
          f"{max(t.timestamp for t in trades) if trades else '—'}")
    print()

    results = [run_scenario(s, trades) for s in scenarios]
    print(render_comparison(results))
    print()
    print("Interpretation hints:")
    base = results[0]
    for m in results[1:]:
        print(interpretation_hint(base, m))
    print(CAVEATS)


if __name__ == "__main__":
    main()
