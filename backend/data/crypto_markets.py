"""Crypto 5-minute Up/Down market fetcher for Polymarket.

Parameterized over `underlying` (e.g. "BTC", "ETH", "SOL", "XRP"). The
slug pattern and validator are driven by `_SLUG_TEMPLATES`. Slice 3a-2
introduces this module with only BTC populated; ETH/SOL/XRP entries are
added in slice 3c/3d after live verification of Polymarket's slug format.
"""
import httpx
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional, List, Tuple, Pattern
from dataclasses import dataclass

logger = logging.getLogger("trading_bot")

GAMMA_API = "https://gamma-api.polymarket.com"

# Per-underlying slug templates. Each entry: (slug_prefix, strict_regex).
# Slice 3c will add ETH; 3d adds SOL and XRP after live verification.
# Keys are upper-case underlying symbols.
_SLUG_TEMPLATES: dict[str, Tuple[str, Pattern]] = {
    "BTC": ("btc-updown-5m-", re.compile(r"^btc-updown-5m-\d{10}$")),
}


def _template_for(underlying: str) -> Tuple[str, Pattern]:
    """Return (slug_prefix, slug_regex) for the given underlying, or raise."""
    t = _SLUG_TEMPLATES.get(underlying.upper())
    if t is None:
        raise ValueError(
            f"No slug template registered for underlying {underlying!r}. "
            f"Known: {sorted(_SLUG_TEMPLATES)}"
        )
    return t


def is_valid_crypto_slug(slug: str, underlying: str) -> bool:
    """Return True only if slug matches the configured pattern for underlying."""
    _, rx = _template_for(underlying)
    return bool(rx.match(slug))


@dataclass
class CryptoUpDownMarket:
    """A single crypto 5-minute Up/Down market on Polymarket.

    Structure identical across underlyings (BTC/ETH/SOL/XRP); the underlying
    is identified by the slug prefix and by the explicit `underlying` field
    on TradingSignal when a signal is generated from this market.
    """
    slug: str
    market_id: str
    up_price: float
    down_price: float
    window_start: datetime
    window_end: datetime
    volume: float
    closed: bool

    @property
    def event_slug(self) -> str:
        return self.slug

    @property
    def spread(self) -> float:
        return abs(1.0 - self.up_price - self.down_price)

    @property
    def time_until_end(self) -> float:
        """Seconds until this window ends."""
        now = datetime.now(timezone.utc)
        return (self.window_end - now).total_seconds()

    @property
    def is_active(self) -> bool:
        """Window is currently in progress."""
        now = datetime.now(timezone.utc)
        return self.window_start <= now <= self.window_end and not self.closed

    @property
    def is_upcoming(self) -> bool:
        """Window hasn't started yet."""
        now = datetime.now(timezone.utc)
        return now < self.window_start and not self.closed


def _round_to_5min(ts: float) -> int:
    """Round a unix timestamp down to the nearest 5-minute boundary."""
    return int(ts) // 300 * 300


def _compute_window_slugs(underlying: str, count: int = 5) -> List[str]:
    """Compute event slugs for the current and upcoming 5-min windows.

    Slug pattern (per underlying): {prefix}{unix_timestamp} where timestamp
    is the END of the 5-min window.
    """
    prefix, _ = _template_for(underlying)
    now = time.time()
    current_boundary = _round_to_5min(now)
    next_boundary = current_boundary + 300

    slugs = []
    for i in range(count):
        end_ts = next_boundary + (i * 300)
        slugs.append(f"{prefix}{end_ts}")
    return slugs


def _parse_event_to_crypto_market(event: dict) -> Optional[CryptoUpDownMarket]:
    """Parse a Polymarket event into a CryptoUpDownMarket."""
    markets = event.get("markets", [])
    if not markets:
        return None

    market = markets[0]

    # Parse outcome prices
    outcome_prices = market.get("outcomePrices", "")
    up_price = 0.5
    down_price = 0.5
    if outcome_prices:
        try:
            prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
            if isinstance(prices, list) and len(prices) >= 2:
                up_price = float(prices[0])
                down_price = float(prices[1])
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    # Parse timestamps
    slug = event.get("slug", "")
    start_str = event.get("startDate") or market.get("startDate")
    end_str = event.get("endDate") or market.get("endDate")

    window_start = datetime.now(timezone.utc)
    window_end = datetime.now(timezone.utc)

    if start_str:
        try:
            window_start = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            pass

    if end_str:
        try:
            window_end = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            pass

    return CryptoUpDownMarket(
        slug=slug,
        market_id=str(market.get("id", "")),
        up_price=up_price,
        down_price=down_price,
        window_start=window_start,
        window_end=window_end,
        volume=float(market.get("volume", 0) or 0),
        closed=bool(market.get("closed", False) or event.get("closed", False)),
    )


async def fetch_crypto_market_by_slug(
    slug: str, underlying: str
) -> Optional[CryptoUpDownMarket]:
    """Fetch a single crypto 5-min market by its event slug.

    Validates the slug against the underlying's pattern before fetching;
    mismatches are rejected silently at DEBUG level.
    """
    if not is_valid_crypto_slug(slug, underlying):
        logger.debug(f"Rejected invalid {underlying} slug: {slug}")
        return None

    url = f"{GAMMA_API}/events"
    params = {"slug": slug}

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            events = response.json()

            if not events:
                return None

            event = events[0] if isinstance(events, list) else events
            return _parse_event_to_crypto_market(event)

        except Exception as e:
            logger.debug(f"Failed to fetch {underlying} market {slug}: {e}")
            return None


async def fetch_active_crypto_markets(underlying: str) -> List[CryptoUpDownMarket]:
    """Fetch current and upcoming 5-min markets on Polymarket for `underlying`.

    Strategy: compute expected slugs from current time and fetch them,
    plus do a series search as fallback.
    """
    prefix, _ = _template_for(underlying)
    markets: List[CryptoUpDownMarket] = []
    seen_slugs: set = set()

    # Method 1: Compute expected slugs and fetch directly
    expected_slugs = _compute_window_slugs(underlying, count=6)
    for slug in expected_slugs:
        market = await fetch_crypto_market_by_slug(slug, underlying)
        if market and market.slug not in seen_slugs:
            seen_slugs.add(market.slug)
            markets.append(market)

    # Method 2: Search by series slug prefix as fallback/supplement
    # The `slug_contains` filter uses the prefix without trailing timestamp.
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{GAMMA_API}/events",
                params={
                    "active": "true",
                    "closed": "false",
                    "slug_contains": prefix.rstrip("-"),
                    "limit": 20,
                },
            )
            response.raise_for_status()
            events = response.json()

            for event in events:
                market = _parse_event_to_crypto_market(event)
                if (
                    market
                    and market.slug not in seen_slugs
                    and is_valid_crypto_slug(market.slug, underlying)
                ):
                    seen_slugs.add(market.slug)
                    markets.append(market)

    except Exception as e:
        logger.debug(f"{underlying} series search fallback failed: {e}")

    # Sort by window end time (soonest first) and filter out already-closed
    markets.sort(key=lambda m: m.window_end)
    markets = [m for m in markets if not m.closed]

    logger.info(f"Fetched {len(markets)} active {underlying} 5-min markets")
    return markets


async def fetch_crypto_market_for_settlement(
    slug: str,
) -> Optional[CryptoUpDownMarket]:
    """Fetch a crypto 5-min market for settlement (includes closed markets).

    No `underlying` parameter needed: the slug itself encodes the asset, and
    settlement only cares about the resolution outcome which is shared-shape.
    """
    url = f"{GAMMA_API}/events"
    params = {"slug": slug}

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            events = response.json()

            if not events:
                return None

            event = events[0] if isinstance(events, list) else events
            return _parse_event_to_crypto_market(event)

        except Exception as e:
            logger.warning(f"Failed to fetch market for settlement {slug}: {e}")
            return None


if __name__ == "__main__":
    import asyncio

    async def test():
        print("Fetching active BTC 5-min markets...")
        markets = await fetch_active_crypto_markets("BTC")
        print(f"Found {len(markets)} markets")

        for m in markets:
            print(f"\n  {m.slug}")
            print(f"  Up: {m.up_price:.2%} | Down: {m.down_price:.2%}")
            print(f"  Window: {m.window_start} -> {m.window_end}")
            print(f"  Volume: ${m.volume:,.0f}")
            print(f"  Active: {m.is_active} | Upcoming: {m.is_upcoming}")

    asyncio.run(test())
