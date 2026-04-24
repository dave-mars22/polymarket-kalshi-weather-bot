"""Execution-time guards for MC trades.

Two guards are enforced in the scheduler between scan and order creation:

1. Per-series concentration cap: maximum MC_MAX_OPEN_PER_SERIES open
   positions per Kalshi series_ticker. Prevents stacking too much exposure
   on a single monthly event (e.g., all KXBTCMAXMON-T80000 / T82500 / T85000
   resolving on the same day).

2. Quote refresh: market prices at scan time can be stale by the time the
   scheduler actually creates a trade. Before persisting a Trade row we
   re-fetch current market state from Kalshi and skip if the ask has drifted
   by more than MC_QUOTE_DRIFT_TOLERANCE dollars.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from backend.config import settings
from backend.data.mc_markets import KALSHI_BASE_URL
from backend.models.database import Trade

logger = logging.getLogger("trading_bot")


def series_ticker_of(market_ticker: str) -> str:
    """Extract the Kalshi series_ticker (everything before the first '-')."""
    return market_ticker.split("-", 1)[0]


def count_open_mc_trades_in_series(db, series_ticker: str) -> int:
    """Count unsettled MC trades whose ticker starts with the given series."""
    prefix = f"{series_ticker}-%"
    return (
        db.query(Trade)
        .filter(
            Trade.settled == False,  # noqa: E712
            Trade.market_type == "monte_carlo",
            Trade.market_ticker.like(prefix),
        )
        .count()
    )


def concentration_cap_exceeded(db, market_ticker: str) -> bool:
    """True if placing a new trade in this series would exceed the cap."""
    series = series_ticker_of(market_ticker)
    open_n = count_open_mc_trades_in_series(db, series)
    return open_n >= settings.MC_MAX_OPEN_PER_SERIES


def fetch_current_ask(
    ticker: str,
    side: str,
    *,
    http_client: Optional[httpx.Client] = None,
) -> Optional[float]:
    """Re-fetch current Kalshi ask for the YES or NO side of one market.

    Returns the current ask as a float in [0, 1], or None on any failure
    (network, non-200, malformed response). Callers should treat None as
    "don't trade this one now" rather than swallow and proceed.
    """
    url = f"{KALSHI_BASE_URL}/markets/{ticker}"
    client = http_client or httpx.Client(timeout=10.0)
    owns = http_client is None
    try:
        r = client.get(url)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception as e:
        logger.debug(f"fetch_current_ask({ticker}): {e}")
        return None
    finally:
        if owns:
            client.close()

    market = data.get("market", data)
    field = "yes_ask_dollars" if side.upper() == "YES" else "no_ask_dollars"
    val = market.get(field)
    if val is None:
        return None
    try:
        ask = float(val)
    except (TypeError, ValueError):
        return None
    if not (0.0 < ask < 1.0):
        return None
    return ask


def quote_drifted(
    scan_time_ask: float, current_ask: float
) -> bool:
    """True if the current ask is more than MC_QUOTE_DRIFT_TOLERANCE from scan."""
    return abs(current_ask - scan_time_ask) > settings.MC_QUOTE_DRIFT_TOLERANCE
