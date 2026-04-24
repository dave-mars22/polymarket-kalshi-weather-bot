"""Live spot-price fetchers for Monte Carlo simulation.

Asset-class routing:
    - "crypto" -> Coinbase Exchange /ticker endpoint

Returns a single float price. Cached for MC_SPOT_CACHE_SECONDS (default 30 s)
to avoid hammering the exchange when multiple markets reference the same
underlying within one scan cycle.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Optional, Tuple

import httpx

from backend.config import settings

logger = logging.getLogger("trading_bot")

# Module-level cache: (symbol, asset_class) -> (expiry_ts, price)
_cache: Dict[Tuple[str, str], Tuple[float, float]] = {}


class SpotPriceError(Exception):
    """Irrecoverable failure in spot-price fetching."""


def fetch_spot(
    symbol: str,
    asset_class: str,
    *,
    http_client: Optional[httpx.Client] = None,
) -> float:
    """Fetch current spot price for `symbol` in `asset_class`.

    `http_client` is an injection seam for tests.
    """
    key = (symbol, asset_class)
    now = time.time()
    cached = _cache.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]

    if asset_class == "crypto":
        price = _fetch_coinbase_spot(symbol, http_client=http_client)
    else:
        raise SpotPriceError(f"Unsupported asset_class: {asset_class!r}")

    expiry = now + settings.MC_SPOT_CACHE_SECONDS
    _cache[key] = (expiry, price)
    return price


def _fetch_coinbase_spot(
    symbol: str, *, http_client: Optional[httpx.Client] = None
) -> float:
    """Fetch last trade price from Coinbase /products/{SYMBOL}-USD/ticker."""
    url = f"https://api.exchange.coinbase.com/products/{symbol}-USD/ticker"

    client = http_client or httpx.Client(timeout=10.0)
    owns_client = http_client is None
    try:
        data = _get_with_retry(client, url)
    finally:
        if owns_client:
            client.close()

    if not isinstance(data, dict):
        raise SpotPriceError(
            f"Coinbase {symbol}: unexpected response type {type(data).__name__}"
        )

    price_str = data.get("price")
    if price_str is None:
        raise SpotPriceError(f"Coinbase {symbol}: response missing 'price' field: {data}")

    try:
        price = float(price_str)
    except (TypeError, ValueError) as e:
        raise SpotPriceError(f"Coinbase {symbol}: invalid price {price_str!r}") from e

    if price <= 0:
        raise SpotPriceError(f"Coinbase {symbol}: non-positive price {price}")

    return price


def _get_with_retry(
    client: httpx.Client, url: str, *, max_retries: int = 3
) -> object:
    """HTTP GET with exponential backoff on 429 (same contract as price_history)."""
    for attempt in range(max_retries + 1):
        response = client.get(url)
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
    raise SpotPriceError("retry loop exhausted without return")


def _clear_cache() -> None:
    """Test-only helper to reset the module cache between tests."""
    _cache.clear()
