"""Slice T5: micro-cache state probe to diagnose variance in
_build_multi_microstructure measurements.

Runs N successive calls to _build_multi_microstructure in this script's
own Python process, printing per-trial latency plus the script's
in-process cache state (P4 _micro_cache, deeper _kline_cache). The
typical pattern is trial 1 slow (cold-process / cold-kline) and
trials 2..N fast (warm kline cache). If that's what you see, T4-style
measurements that show "variance" in this function are a measurement
artifact, not a production issue.

Why this happens — important to understand before drawing conclusions
from any per-process measurement of dashboard helpers:

  - Each Python process has its own module-level state. The bot
    (`run.py` in one process) and any measurement script (separate
    process) do NOT share `_micro_cache` or `_kline_cache`.
  - The bot's caches are kept warm by scan_and_trade_job (60s interval).
    A measurement script's caches start empty and warm up on first call.
  - `_build_multi_microstructure` falls back to inline
    `compute_crypto_microstructure(u)` on cache miss, which calls
    `fetch_klines(u)` — that uses the kline cache (30s TTL). With both
    caches cold (script's first call), 4 sequential HTTP fetches to
    Coinbase ≈ 600-1000 ms. With the kline cache warm, indicator math
    is sub-millisecond.

The bot's actual /api/dashboard latency for this component is ~1 ms in
steady state. Verifiable independently: grep for "[dashboard] micro
cache miss" in the bot's log — zero hits since restart means the bot's
P4 cache is always populated when /api/dashboard runs, which means
the inline fallback never fires in production.

Usage:
    venv/bin/python backtest/probe_micro_cache.py            # 10 trials
    venv/bin/python backtest/probe_micro_cache.py --trials 20

Re-runnable: useful if a future T4-style measurement shows similar
variance and you need to disambiguate "real cache bug" from
"per-process cold-start artifact".
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

# This script lives in backtest/ but imports from backend/. Put the
# repo root on sys.path so the imports below resolve regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.api.main import _build_multi_microstructure
from backend.core.dashboard_cache import _micro_cache
from backend.data.crypto import _kline_cache


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=10,
                        help="number of consecutive trials (default 10)")
    args = parser.parse_args()

    # Show initial in-process cache state — both should be empty for a
    # fresh script process. If they aren't, the import order may have
    # populated them (unlikely but worth seeing).
    print(f"=== This script's process state at startup ===")
    print(f"  _micro_cache (P4 cache):  {dict(_micro_cache)}  ({len(_micro_cache)} entries)")
    print(f"  _kline_cache underlyings: {sorted(_kline_cache.keys())}")
    print()

    # Time N successive calls. The hypothesis predicts trial 1 slow,
    # trials 2..N fast (within the script's now-warm 30s kline cache).
    print(f"=== Trial-by-trial latency for _build_multi_microstructure() ===")
    print(f"{'trial':>5}  {'ms':>9}  {'response micros':<28}  {'kline cache keys':<20}")
    print(f"{'-'*5:>5}  {'-'*9:>9}  {'-'*28:<28}  {'-'*20:<20}")
    times_ms = []
    for i in range(1, args.trials + 1):
        t0 = time.perf_counter()
        result = await _build_multi_microstructure()
        t1 = time.perf_counter()
        elapsed = (t1 - t0) * 1000.0
        times_ms.append(elapsed)
        micro_keys = sorted(result.microstructures.keys())
        kline_keys = sorted(_kline_cache.keys())
        print(f"{i:>5}  {elapsed:>9.2f}  {str(micro_keys):<28}  {str(kline_keys):<20}")

    print()
    print(f"=== Summary ===")
    n = len(times_ms)
    sorted_times = sorted(times_ms)
    median = (
        sorted_times[n // 2] if n % 2
        else (sorted_times[n // 2 - 1] + sorted_times[n // 2]) / 2
    )
    trimmed = (
        sum(sorted_times[1:-1]) / (n - 2) if n >= 3 else float("nan")
    )
    print(f"  trial 1:           {times_ms[0]:>9.2f} ms (cold-process / cold-kline-cache)")
    print(f"  trials 2..{n}:        {min(times_ms[1:]):>9.2f}-{max(times_ms[1:]):.2f} ms")
    print(f"  mean (all):        {sum(times_ms)/n:>9.2f} ms")
    print(f"  median (all):      {median:>9.2f} ms")
    print(f"  trimmed mean:      {trimmed:>9.2f} ms  (excludes min and max)")
    print(f"  min:               {min(times_ms):>9.2f} ms")
    print(f"  max:               {max(times_ms):>9.2f} ms")

    print()
    print(f"=== Diagnosis ===")
    if times_ms[0] > 100 and max(times_ms[1:]) < 50:
        print("  CONFIRMED: variance is a per-process cold-start artifact.")
        print(f"  Trial 1 ({times_ms[0]:.0f} ms) paid the cold-kline HTTP fetch cost.")
        print(f"  Trials 2..{n} ({max(times_ms[1:]):.1f} ms max) ran fast against this script's")
        print(f"  warmed kline cache. The BOT's process has its kline cache warmed by the")
        print(f"  scanner every 60s and never sees this cold-start cost in steady state.")
        print(f"  T4's measurement methodology — calling the function 5 times in a fresh")
        print(f"  process — guarantees one cold-start trial in every measurement run.")
        print(f"  Median or trimmed mean is the more representative number, not arithmetic mean.")
    elif max(times_ms) < 50:
        print(f"  Variance NOT reproduced. All {n} trials fast. Either the prior cold-start")
        print(f"  was a one-off (unlikely, see hypothesis) or some other state was different.")
    else:
        print(f"  Inconclusive — variance present but not the simple cold-start pattern.")
        print(f"  Trial 1 = {times_ms[0]:.1f} ms; max of trials 2..{n} = {max(times_ms[1:]):.1f} ms.")
        print(f"  Worth deeper investigation if max(2..{n}) is also large.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
