"""Market data types and fetching — crypto 5-min on Polymarket.

NOTE: this module currently has no external callers and is kept only
to avoid breaking historical imports. Flagged for removal in a later
cleanup slice.
"""
import logging
from datetime import datetime
from typing import Optional, List
from dataclasses import dataclass

from backend.data.crypto_markets import CryptoUpDownMarket, fetch_active_crypto_markets

logger = logging.getLogger(__name__)


@dataclass
class MarketData:
    """Structured market data."""
    platform: str
    ticker: str
    title: str
    category: str
    subcategory: Optional[str]

    yes_price: float  # 0-1 (Up price for crypto markets)
    no_price: float   # (Down price for crypto markets)
    volume: float
    settlement_time: Optional[datetime]

    threshold: Optional[float] = None
    direction: Optional[str] = None

    event_slug: Optional[str] = None
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None


def crypto_market_to_market_data(
    m: CryptoUpDownMarket, underlying: str,
) -> MarketData:
    """Convert a CryptoUpDownMarket to the generic MarketData format."""
    return MarketData(
        platform="polymarket",
        ticker=m.market_id,
        title=f"{underlying} Up or Down 5m - {m.slug}",
        category="crypto",
        subcategory=f"{underlying.lower()}-5m",
        yes_price=m.up_price,
        no_price=m.down_price,
        volume=m.volume,
        settlement_time=m.window_end,
        event_slug=m.slug,
        window_start=m.window_start,
        window_end=m.window_end,
    )


async def fetch_all_markets(underlying: str = "BTC", **kwargs) -> List[MarketData]:
    """Fetch all active 5-min crypto markets for `underlying`."""
    markets = await fetch_active_crypto_markets(underlying)
    return [crypto_market_to_market_data(m, underlying) for m in markets]
