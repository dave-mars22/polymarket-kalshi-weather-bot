"""
Backtest replay harness (Scope A).

Replays settled trades from tradingbot.db through candidate parameter
combinations and reports counterfactual P&L.

Usage:
    python backtest/replay.py
    python backtest/replay.py --top 30
    python backtest/replay.py --csv-only

Limitations:
- Only sweeps post-signal parameters (edge threshold, Kelly fraction,
  sizing floor/cap, calibration). Cannot re-test signal generation logic
  (convergence thresholds, indicator weights) since per-trade indicator
  votes aren't stored.
- Replays historical trades only. Does not discover new opportunities
  in markets the bot didn't trade.
"""

import argparse
import csv
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from itertools import product
from typing import List, Optional


# -------- DATA LOADING --------

@dataclass
class HistoricalTrade:
    """A settled trade pulled from the DB."""
    id: int
    timestamp: str
    market_type: str
    direction: str
    entry_price: float
    original_size: float
    edge_at_entry: float
    market_price_at_entry: float
    model_probability: float
    result: str
    original_pnl: float


def load_settled_trades(db_path: str) -> List[HistoricalTrade]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT id, timestamp, market_type, direction, entry_price, size,
               edge_at_entry, market_price_at_entry, model_probability,
               result, pnl
        FROM trades
        WHERE settled = 1
          AND edge_at_entry IS NOT NULL
          AND market_price_at_entry IS NOT NULL
          AND model_probability IS NOT NULL
          AND size IS NOT NULL AND size > 0
          AND entry_price IS NOT NULL
        ORDER BY id
        """
    ).fetchall()
    con.close()

    out = []
    for r in rows:
        out.append(HistoricalTrade(
            id=r["id"],
            timestamp=str(r["timestamp"]),
            market_type=r["market_type"] or "btc",
            direction=r["direction"],
            entry_price=float(r["entry_price"]),
            original_size=float(r["size"]),
            edge_at_entry=float(r["edge_at_entry"]),
            market_price_at_entry=float(r["market_price_at_entry"]),
            model_probability=float(r["model_probability"]),
            result=r["result"] or "loss",
            original_pnl=float(r["pnl"] or 0.0),
        ))
    return out


# -------- SIZING LOGIC (mirrors backend/core/signals.py calculate_kelly_size) --------

def kelly_size(edge: float, model_prob: float, market_price: float,
               direction: str, bankroll: float,
               kelly_fraction: float, max_trade_size: float) -> float:
    """Replica of calculate_kelly_size. Matches current signals.py logic."""
    if direction in ("up", "yes"):
        win_prob = model_prob
    else:
        win_prob = 1.0 - model_prob

    lose_prob = 1.0 - win_prob

    if market_price <= 0 or market_price >= 1:
        return 0.0
    odds = (1.0 - market_price) / market_price
    if odds <= 0:
        return 0.0

    kelly = (win_prob * odds - lose_prob) / odds
    kelly *= kelly_fraction

    kelly = min(kelly, 0.05)
    kelly = max(kelly, 0.0)

    size = kelly * bankroll
    size = min(size, max_trade_size)
    return size


# -------- P&L RECALC --------

def expected_pnl_from_result(size: float, entry_price: float, result: str) -> float:
    """
    Reconstruct P&L at a candidate size using same logic as the live bot.
    Binary Polymarket-style contract: win pays $1, profit per share = (1 - entry_price).
    Shares = size / entry_price. Winning P&L = size * (1 - entry_price) / entry_price.
    Losing P&L = -size. (Ignores slippage/fees, matches how DB pnl is stored.)
    """
    if entry_price <= 0 or entry_price >= 1:
        return 0.0
    if result == "win":
        return size * (1.0 - entry_price) / entry_price
    else:
        return -size


# -------- CALIBRATION ESTIMATE --------

def estimate_calibration_multiplier(trades_so_far: List[HistoricalTrade],
                                    market_type: str,
                                    min_trades: int,
                                    max_mult: float,
                                    cal_min: float = 0.1) -> float:
    """
    Estimate what calibration would have returned at this point in history.
    """
    relevant = [t for t in trades_so_far if t.market_type == market_type]
    if len(relevant) < min_trades:
        return 1.0

    predicted = sum(abs(t.edge_at_entry) for t in relevant) / len(relevant)
    realized = sum(
        (t.original_pnl / t.original_size) for t in relevant if t.original_size > 0
    ) / len(relevant)

    if abs(predicted) < 0.001:
        return 1.0
    if realized <= 0:
        return cal_min

    raw = realized / predicted
    return max(cal_min, min(max_mult, raw))


# -------- BACKTEST RUN --------

@dataclass
class RunResult:
    edge_threshold: float
    kelly_fraction: float
    min_floor: Optional[float]
    max_trade_size: float
    calibration: str
    trades: int = 0
    wins: int = 0
    total_pnl: float = 0.0
    total_size: float = 0.0

    @property
    def win_rate(self) -> float:
        return (self.wins / self.trades * 100.0) if self.trades else 0.0

    @property
    def avg_size(self) -> float:
        return (self.total_size / self.trades) if self.trades else 0.0


def run_backtest(trades: List[HistoricalTrade],
                 edge_threshold: float,
                 kelly_fraction: float,
                 min_floor: Optional[float],
                 max_trade_size: float,
                 use_calibration: bool,
                 starting_bankroll: float = 200.0,
                 cal_min_trades: int = 100,
                 cal_max_mult: float = 0.7) -> RunResult:
    """Run one parameter combo across all trades."""
    res = RunResult(
        edge_threshold=edge_threshold,
        kelly_fraction=kelly_fraction,
        min_floor=min_floor,
        max_trade_size=max_trade_size,
        calibration="ON" if use_calibration else "OFF",
    )

    trades_so_far: List[HistoricalTrade] = []

    for t in trades:
        if abs(t.edge_at_entry) < edge_threshold:
            trades_so_far.append(t)
            continue

        size = kelly_size(
            edge=abs(t.edge_at_entry),
            model_prob=t.model_probability,
            market_price=t.market_price_at_entry,
            direction=t.direction,
            bankroll=starting_bankroll,
            kelly_fraction=kelly_fraction,
            max_trade_size=max_trade_size,
        )

        if use_calibration:
            mult = estimate_calibration_multiplier(
                trades_so_far, t.market_type,
                min_trades=cal_min_trades, max_mult=cal_max_mult,
            )
            size *= mult

        size = min(size, starting_bankroll * 0.03)

        if min_floor is not None and size < min_floor:
            size = min_floor

        if size <= 0.01:
            trades_so_far.append(t)
            continue

        pnl = expected_pnl_from_result(size, t.entry_price, t.result)

        res.trades += 1
        if t.result == "win":
            res.wins += 1
        res.total_pnl += pnl
        res.total_size += size

        trades_so_far.append(t)

    return res


# -------- DRIVER --------

def parameter_grid():
    """Cartesian grid of sweep dimensions."""
    return list(product(
        [0.02, 0.05, 0.07, 0.10],
        [0.05, 0.10, 0.15, 0.25],
        [None, 1.0, 10.0],
        [5.0, 10.0, 20.0],
        [True, False],
    ))


def main():
    ap = argparse.ArgumentParser(description="Scope A backtest harness")
    ap.add_argument("--db", default="tradingbot.db", help="Path to SQLite DB")
    ap.add_argument("--top", type=int, default=25, help="Show top-N results")
    ap.add_argument("--csv-only", action="store_true", help="Skip terminal ranking")
    ap.add_argument("--outdir", default="backtest", help="Where to save CSV")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: db not found at {args.db}", file=sys.stderr)
        sys.exit(1)

    print("=" * 66)
    print("SCOPE-A BACKTEST HARNESS")
    print("=" * 66)

    trades = load_settled_trades(args.db)
    if not trades:
        print("No settled trades with full fields. Exiting.")
        sys.exit(0)

    print(f"Loaded {len(trades)} settled trades.")

    baseline_pnl = sum(t.original_pnl for t in trades)
    baseline_wins = sum(1 for t in trades if t.result == "win")
    baseline_size = sum(t.original_size for t in trades) / len(trades) if trades else 0
    print()
    print("BASELINE (what actually happened):")
    print(f"  Trades: {len(trades)} | Win: {100*baseline_wins/len(trades):.1f}%"
          f" | P&L: ${baseline_pnl:+.2f} | Avg size: ${baseline_size:.2f}")
    print()

    grid = parameter_grid()
    print(f"Sweeping {len(grid)} parameter combinations...")
    results: List[RunResult] = []
    for params in grid:
        results.append(run_backtest(trades, *params))

    results.sort(key=lambda r: r.total_pnl, reverse=True)

    os.makedirs(args.outdir, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M")
    csv_path = os.path.join(args.outdir, f"results_{ts}.csv")
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "edge_threshold", "kelly_fraction", "min_floor", "max_trade_size",
            "calibration", "trades", "win_rate_pct", "total_pnl", "avg_size",
        ])
        for r in results:
            w.writerow([
                r.edge_threshold, r.kelly_fraction,
                r.min_floor if r.min_floor is not None else "None",
                r.max_trade_size, r.calibration,
                r.trades, f"{r.win_rate:.2f}",
                f"{r.total_pnl:.2f}", f"{r.avg_size:.2f}",
            ])

    if not args.csv_only:
        print()
        print(f"TOP {args.top} COUNTERFACTUALS (sorted by P&L):")
        print("-" * 66)
        print(f"  {'edge':>5} {'kelly':>6} {'floor':>6} {'maxsz':>6} {'calib':>5} "
              f"{'trades':>7} {'win%':>6} {'pnl':>10} {'avgsz':>7}")
        for r in results[: args.top]:
            floor_s = "None" if r.min_floor is None else f"${r.min_floor:.0f}"
            print(f"  {r.edge_threshold:>5.2f} {r.kelly_fraction:>6.2f} {floor_s:>6} "
                  f"${r.max_trade_size:>4.0f} {r.calibration:>5} "
                  f"{r.trades:>7d} {r.win_rate:>5.1f}% ${r.total_pnl:>+8.2f} ${r.avg_size:>5.2f}")
        print()
        print("CURRENT-LIVE-CONFIG RESULT (post-Option-B, calibration ON):")
        for r in results:
            if (r.edge_threshold == 0.05 and r.kelly_fraction == 0.10
                    and r.min_floor == 1.0 and r.max_trade_size == 10.0
                    and r.calibration == "ON"):
                print(f"  Trades: {r.trades} | Win: {r.win_rate:.1f}%"
                      f" | P&L: ${r.total_pnl:+.2f} | Avg size: ${r.avg_size:.2f}")
                break
        print()
        print("WORST 5 COMBINATIONS:")
        for r in results[-5:]:
            floor_s = "None" if r.min_floor is None else f"${r.min_floor:.0f}"
            print(f"  edge={r.edge_threshold} kelly={r.kelly_fraction} floor={floor_s}"
                  f" maxsz=${r.max_trade_size:.0f} calib={r.calibration}"
                  f" -> {r.trades} trades, {r.win_rate:.1f}% win, ${r.total_pnl:+.2f}")

    print()
    print("=" * 66)
    print(f"Saved: {csv_path}")
    print("=" * 66)


if __name__ == "__main__":
    main()