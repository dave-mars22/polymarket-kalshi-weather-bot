"""Slice P3 (2026-04-25): in-process cache for dashboard equity-curve and
calibration-summary queries.

Background
----------
After P2 (commit dc0861f), `/api/dashboard` latency was measured at
~2 seconds. The remaining DB cost broke down as:

  - ~0.3s — equity curve: db.query(Trade).filter(settled == True).all()
            loaded all 750+ settled trades on every poll (Audit Finding #3).
  - ~0.3s — calibration summary: same shape — load all settled signals
            and aggregate (Audit Finding #4 — the dashboard variant).
  - ~0.4s — other DB queries + per-strategy stats + MC portfolio build.
  - ~1.0s — multi-asset microstructure parallel fetches (separate
            audit finding, NOT in P3 scope).

This module addresses Findings #3 and #4 together because they share
architecture: same trigger (post-settlement), same source data (settled
trades / settled signals), naturally paired update.

Concurrency model
-----------------
Same as P2's scan cache (see backend/core/signals.py):

  - Single-writer: scheduler.settlement_job calls refresh_dashboard_cache()
    once per 2-minute cycle.
  - Multi-reader: any /api/dashboard request can call get_cached_dashboard_data().
  - Atomic single-name binding under CPython's GIL: replacing
    `_dashboard_cache = (payload, ts)` is atomic, so a reader sees either
    the previous tuple or the new tuple, never partial state.
  - Storing payload + timestamp together as one tuple is what makes the
    read atomic. If they were separate variables a reader could observe
    a fresh payload with an old timestamp (cosmetic, but worth doing
    right). No threading.Lock or asyncio.Lock needed for this pattern.

Cache miss behaviour
--------------------
If the cache has never been populated (e.g., immediately post-restart
before settlement_job has fired), get_cached_dashboard_data() returns
(None, None). The dashboard endpoint falls back to running the queries
once — the dashboard is the read path, the scheduler is the write path,
and the dashboard MUST NOT update the cache from its fallback (single-
writer invariant). The scheduler will populate the cache on its next
tick, after which subsequent dashboard reads are fast.

Why move CalibrationSummary into this module
--------------------------------------------
Pre-P3, the Pydantic model and its computation lived in main.py, which
made dashboard_cache.py importing it a circular reference. Moving both
to this module establishes the correct dependency direction:
main.py imports CalibrationSummary from dashboard_cache.py rather than
the reverse. Other parts of main.py keep working because the symbol
itself is unchanged — just relocated.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.config import settings
from backend.data.crypto import CryptoMicrostructure
from backend.data.crypto_markets import CryptoUpDownMarket
from backend.models.database import Signal, Trade

logger = logging.getLogger("trading_bot")


# ---------------------------------------------------------------------
# Public response type — moved here from main.py in slice P3 to break
# the dashboard_cache → main.py circular import. main.py re-imports
# this name so existing references stay valid.
# ---------------------------------------------------------------------
class CalibrationSummary(BaseModel):
    """Aggregate calibration view over all settled signals."""
    total_signals: int
    total_with_outcome: int
    accuracy: float
    avg_predicted_edge: float
    avg_actual_edge: float
    brier_score: float


# ---------------------------------------------------------------------
# Cache state. Single tuple for atomic-read; payload is a dict so callers
# that want only equity_curve OR only calibration can index the dict
# instead of unpacking a longer tuple.
# ---------------------------------------------------------------------
_dashboard_cache: Optional[Tuple[Dict[str, Any], datetime]] = None


def get_cached_dashboard_data() -> Tuple[Optional[Dict[str, Any]], Optional[datetime]]:
    """Return the latest cached (payload, timestamp). Returns (None, None)
    if the cache has never been populated.

    Payload shape:
        {
            "equity_curve": List[Dict] — see _build_equity_curve below
            "calibration": Optional[CalibrationSummary]
        }
    """
    snapshot = _dashboard_cache  # atomic single-name read
    if snapshot is None:
        return (None, None)
    return snapshot


def update_cached_dashboard_data(payload: Dict[str, Any]) -> None:
    """Atomically replace the cache with a new payload tagged with the
    current UTC time. Single-writer pattern — only call from
    scheduler.settlement_job (or from refresh_dashboard_cache below).

    Stores the payload by reference; callers should treat the dict as
    immutable after passing it to this function. The standard caller
    (refresh_dashboard_cache) builds a fresh dict each tick so this is
    safe in practice."""
    global _dashboard_cache
    _dashboard_cache = (payload, datetime.now(timezone.utc))


def _build_equity_curve(db: Session) -> list[dict]:
    """Build the cumulative-PnL equity curve from all settled trades.
    Mirrors the inline logic the dashboard ran pre-P3 (main.py:1303 area).
    Each row: {"timestamp": ISO8601 str, "pnl": cumulative_pnl,
    "bankroll": INITIAL_BANKROLL + cumulative_pnl}."""
    settled_trades = (
        db.query(Trade)
        .filter(Trade.settled == True)  # noqa: E712 — SQLAlchemy expr
        .order_by(Trade.timestamp)
        .all()
    )
    curve: list[dict] = []
    cumulative_pnl = 0.0
    for trade in settled_trades:
        if trade.pnl is not None:
            cumulative_pnl += trade.pnl
            curve.append({
                "timestamp": trade.timestamp.isoformat(),
                "pnl": cumulative_pnl,
                "bankroll": settings.INITIAL_BANKROLL + cumulative_pnl,
            })
    return curve


def _compute_calibration_summary(db: Session) -> Optional[CalibrationSummary]:
    """Compute calibration summary from settled signals. Moved from
    main.py in slice P3; logic unchanged (matches the pre-P3 behaviour
    line-for-line so backtests, dashboards, and downstream consumers
    see identical numbers)."""
    total_signals = db.query(Signal).count()
    settled_signals = db.query(Signal).filter(Signal.outcome_correct.isnot(None)).all()

    if not settled_signals:
        if total_signals == 0:
            return None
        return CalibrationSummary(
            total_signals=total_signals,
            total_with_outcome=0,
            accuracy=0.0,
            avg_predicted_edge=0.0,
            avg_actual_edge=0.0,
            brier_score=0.0,
        )

    total_with_outcome = len(settled_signals)
    correct = sum(1 for s in settled_signals if s.outcome_correct)
    accuracy = correct / total_with_outcome if total_with_outcome > 0 else 0.0

    avg_predicted_edge = sum(abs(s.edge) for s in settled_signals) / total_with_outcome
    # Actual edge: for correct predictions, edge was real; for incorrect, negative.
    avg_actual_edge = sum(
        abs(s.edge) if s.outcome_correct else -abs(s.edge)
        for s in settled_signals
    ) / total_with_outcome

    # Brier score: mean squared error of probability forecasts.
    # For each signal: (predicted_prob - actual_outcome)^2.
    brier_sum = 0.0
    for s in settled_signals:
        # Model probability is for UP; actual is 1.0 if UP won, 0.0 if DOWN won.
        actual = s.settlement_value if s.settlement_value is not None else 0.5
        brier_sum += (s.model_probability - actual) ** 2
    brier_score = brier_sum / total_with_outcome

    return CalibrationSummary(
        total_signals=total_signals,
        total_with_outcome=total_with_outcome,
        accuracy=accuracy,
        avg_predicted_edge=avg_predicted_edge,
        avg_actual_edge=avg_actual_edge,
        brier_score=brier_score,
    )


def build_dashboard_cache_payload(db: Session) -> Dict[str, Any]:
    """Run both queries (equity curve + calibration) and return the
    payload dict that get_cached_dashboard_data() will later return.

    Pure function; no caching, no I/O beyond the passed-in db Session.
    Useful for tests and for the dashboard's cache-miss fallback path."""
    return {
        "equity_curve": _build_equity_curve(db),
        "calibration": _compute_calibration_summary(db),
    }


def refresh_dashboard_cache(db: Session) -> None:
    """Build a fresh payload and atomically swap it into the cache.
    Call from scheduler.settlement_job after settlement work is done.

    Wrapped at the call site in try/except so a cache-refresh failure
    never blocks settlement bookkeeping. If this function raises, the
    cache is left at its prior contents (stale but valid)."""
    payload = build_dashboard_cache_payload(db)
    update_cached_dashboard_data(payload)


# ---------------------------------------------------------------------
# Slice P4: per-underlying microstructure cache. Distinct from the
# (payload, ts) cache above because the consumer pattern is different —
# multi-microstructure is keyed by underlying, with each underlying
# updated independently as the scan job processes it.
#
# Concurrency model: WHOLE-DICT SWAP. The module-level _micro_cache name
# is rebound to a brand-new dict on every update; readers either see the
# old dict or the new dict, never a partial state. This is GIL-safe
# (single-name binding is atomic) at the cost of building a new 4-key
# dict on every update — negligible.
#
# Why not in-place dict mutation? `_micro_cache[u] = (...)` in CPython
# is atomic at the bytecode level for individual key assignment, but
# whole-dict swap is more defensively correct: a reader iterating the
# dict (e.g., to enumerate underlyings) under in-place mutation could
# observe a transient half-updated state on some Python implementation
# or with some future change. Whole-dict swap is fully atomic regardless.
#
# Single-writer: scan_for_signals._scan_one_underlying is the only path
# that calls update_cached_micro. Multi-reader: any /api/dashboard or
# /api/microstructure request can call get_cached_micro.
# ---------------------------------------------------------------------
_micro_cache: Dict[str, Tuple[CryptoMicrostructure, datetime]] = {}


def get_cached_micro(underlying: str) -> Optional[Tuple[CryptoMicrostructure, datetime]]:
    """Return the latest cached (micro, timestamp) for the given underlying,
    or None if no scan has populated this underlying yet (e.g., bot just
    restarted and scan_and_trade_job hasn't fired its first cycle).

    Caller is expected to handle the None case via inline fallback —
    do NOT update the cache from the fallback path (single-writer
    invariant; only scan_for_signals writes)."""
    snapshot = _micro_cache  # atomic single-name read
    return snapshot.get(underlying.upper())


def update_cached_micro(
    underlying: str,
    micro: CryptoMicrostructure,
    timestamp: Optional[datetime] = None,
) -> None:
    """Atomically update the per-underlying micro cache. Builds a brand-new
    dict containing all existing entries plus the new one, then rebinds
    the module-level name to the new dict in a single GIL-atomic step.

    Single-writer pattern — only call from scheduler / scan code paths,
    never from request handlers."""
    global _micro_cache
    ts = timestamp if timestamp is not None else datetime.now(timezone.utc)
    new_cache = dict(_micro_cache)  # snapshot the existing entries
    new_cache[underlying.upper()] = (micro, ts)
    _micro_cache = new_cache  # atomic single-name rebind


# ---------------------------------------------------------------------
# Slice P5 (2026-04-26): per-underlying active-markets cache.
#
# T4 measured /api/dashboard's call to fetch_active_crypto_markets("BTC")
# at ~1217 ms — 90.1% of dashboard latency, dominating everything else
# by an order of magnitude. T5 confirmed it's real Polymarket gamma-api
# HTTP latency, not a measurement artifact. The list of active 5-minute
# markets changes slowly (Polymarket creates new windows roughly every
# 5 minutes), so the dashboard tolerates ~60 s staleness easily.
#
# Same architecture as P4: per-underlying dict, whole-dict-swap for
# atomicity, single-writer (scan) / multi-reader (dashboard), no locks.
# Scope: the scan populates the cache for every underlying it processes
# (BTC/ETH/SOL/XRP). The dashboard currently only reads BTC; ETH/SOL/XRP
# entries cost nothing to populate and future-proof the cache for any
# follow-up slice that adds a per-asset windows panel.
# ---------------------------------------------------------------------
_active_markets_cache: Dict[str, Tuple[List[CryptoUpDownMarket], datetime]] = {}


def get_cached_active_markets(
    underlying: str,
) -> Optional[Tuple[List[CryptoUpDownMarket], datetime]]:
    """Return the latest cached (markets, timestamp) for the given
    underlying, or None if the scan hasn't populated this underlying yet
    (e.g., bot just restarted before scan_and_trade_job's first cycle).

    Caller handles None via inline fallback — do NOT update the cache
    from the fallback path (single-writer invariant; only the scan
    writes)."""
    snapshot = _active_markets_cache  # atomic single-name read
    return snapshot.get(underlying.upper())


def update_cached_active_markets(
    underlying: str,
    markets: List[CryptoUpDownMarket],
    timestamp: Optional[datetime] = None,
) -> None:
    """Atomically update the per-underlying active-markets cache. Same
    whole-dict-swap pattern as update_cached_micro: build a brand-new
    dict containing all existing entries plus the new one, then rebind
    the module-level name in a single GIL-atomic step.

    Markets are stored as a defensive shallow copy (list(markets)) so
    a later in-place mutation by the writer cannot affect cached
    readers. The CryptoUpDownMarket dataclasses themselves are
    intentionally treated as immutable by convention — we don't deep-
    copy them because they're produced fresh from each gamma-api
    response and never mutated downstream.

    Single-writer pattern — only call from scheduler / scan code paths,
    never from request handlers."""
    global _active_markets_cache
    ts = timestamp if timestamp is not None else datetime.now(timezone.utc)
    new_cache = dict(_active_markets_cache)
    new_cache[underlying.upper()] = (list(markets), ts)
    _active_markets_cache = new_cache
