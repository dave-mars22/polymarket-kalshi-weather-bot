"""Historical daily price fetchers for Monte Carlo vol/drift estimation.

Asset-class routing:
    - "crypto" -> Coinbase Exchange public API
Other asset classes (equity_index, etc.) will be added in later slices.

All fetchers return `list[tuple[date, float]]` oldest-first chronologically
so callers can pass `[close for _, close in bars]` straight into log_returns.

Callers that need fewer bars than requested receive an InsufficientHistoryError
rather than silent truncation — signal generators should loudly skip markets
with thin history rather than produce under-calibrated estimates.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import httpx

from backend.config import settings

logger = logging.getLogger("trading_bot")

# Module-level cache: (symbol, asset_class, days) -> (expiry_ts, bars)
_cache: Dict[Tuple[str, str, int], Tuple[float, List[Tuple[date, float]]]] = {}

# Coinbase public API: max 300 candles per request. Beyond that we'd need
# to paginate. Our MC lookbacks are ~90 days max, so one call is enough.
_COINBASE_MAX_CANDLES = 300


class PriceHistoryError(Exception):
    """Irrecoverable failure in historical price fetching."""


class InsufficientHistoryError(PriceHistoryError):
    """Fewer bars returned than requested."""


def fetch_daily_closes(
    symbol: str,
    asset_class: str,
    days: int,
    *,
    http_client: Optional[httpx.Client] = None,
) -> List[Tuple[date, float]]:
    """Fetch `days` daily closes for `symbol`/`asset_class`, oldest-first.

    Raises InsufficientHistoryError if the adapter returns fewer than `days`
    bars (callers should catch this and skip the market, not assume partial
    data is usable for vol estimation).

    `http_client` is an injection seam for tests — production callers should
    leave it None so the function constructs and closes its own client.
    """
    if days < 1:
        raise ValueError(f"days must be >= 1, got {days}")
    if days > _COINBASE_MAX_CANDLES:
        raise ValueError(
            f"days={days} exceeds Coinbase per-request cap of {_COINBASE_MAX_CANDLES}; "
            "pagination not yet implemented"
        )

    key = (symbol, asset_class, days)
    now = time.time()
    cached = _cache.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]

    if asset_class == "crypto":
        bars = _fetch_coinbase_daily(symbol, days, http_client=http_client)
    else:
        raise PriceHistoryError(f"Unsupported asset_class: {asset_class!r}")

    if len(bars) < days:
        raise InsufficientHistoryError(
            f"{asset_class}/{symbol}: got {len(bars)} bars, needed {days}"
        )

    expiry = now + settings.MC_HISTORICAL_CACHE_SECONDS
    _cache[key] = (expiry, bars)
    return bars


def _fetch_coinbase_daily(
    symbol: str, days: int, *, http_client: Optional[httpx.Client] = None
) -> List[Tuple[date, float]]:
    """Fetch daily candles from Coinbase Exchange public API.

    Response shape: [[timestamp, low, high, open, close, volume], ...] newest-first.
    We map to (date, close) and sort oldest-first.
    """
    end = datetime.now(timezone.utc)
    # Ask for a couple extra days of buffer, then trim.
    start = end - timedelta(days=days + 2)
    params = {
        "granularity": 86400,
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
    url = f"https://api.exchange.coinbase.com/products/{symbol}-USD/candles"

    client = http_client or httpx.Client(timeout=15.0)
    owns_client = http_client is None
    try:
        data = _get_with_retry(client, url, params=params)
    finally:
        if owns_client:
            client.close()

    if not isinstance(data, list):
        raise PriceHistoryError(
            f"Coinbase {symbol}: unexpected response type {type(data).__name__}"
        )
    if not data:
        raise PriceHistoryError(f"Coinbase {symbol}: empty candle response")

    bars: List[Tuple[date, float]] = []
    for row in data:
        if not isinstance(row, list) or len(row) < 5:
            continue
        try:
            ts = int(row[0])
            close = float(row[4])
        except (TypeError, ValueError):
            continue
        if close <= 0:
            continue
        d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        bars.append((d, close))

    bars.sort(key=lambda b: b[0])

    if len(bars) > days:
        bars = bars[-days:]

    return bars


def _get_with_retry(
    client: httpx.Client,
    url: str,
    *,
    params: Optional[dict] = None,
    max_retries: int = 3,
) -> object:
    """HTTP GET with exponential backoff on 429 responses.

    Respects Retry-After header when present. Raises for non-429 errors
    immediately. Raises after exhausting retries.
    """
    for attempt in range(max_retries + 1):
        response = client.get(url, params=params)
        if response.status_code == 429:
            if attempt >= max_retries:
                response.raise_for_status()
            retry_after = response.headers.get("Retry-After")
            if retry_after is not None:
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = 2.0 ** attempt
            else:
                delay = 2.0 ** attempt
            logger.warning(
                f"Rate limited on {url} (attempt {attempt + 1}/{max_retries + 1}); "
                f"sleeping {delay:.1f}s"
            )
            time.sleep(delay)
            continue
        response.raise_for_status()
        return response.json()
    raise PriceHistoryError("retry loop exhausted without return")


def _clear_cache() -> None:
    """Test-only helper to reset the module cache between tests."""
    _cache.clear()
