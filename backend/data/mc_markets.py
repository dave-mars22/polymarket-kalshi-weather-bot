"""Monte Carlo market discovery.

Scans exchange APIs for binary price-continuous contracts that can be
priced with Geometric Brownian Motion, returning a list of
MonteCarloMarket dataclasses consumed by the signal generator.

Venue coverage:
    - Kalshi crypto daily series      (Slice 1d)
    - Kalshi equity-index daily series (Slice 2, SPX + NDX)

Not yet implemented:
    - Polymarket crypto / index markets
    - Hourly / weekly / monthly / annual Kalshi series (we restrict to daily
      because constant-vol GBM is weakest over long horizons and our scan
      cadence is too slow for intraday contracts)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import httpx

from backend.config import settings

logger = logging.getLogger("trading_bot")

KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# (series_ticker, underlying_symbol, asset_class)
# Only daily-frequency series. Monthly/annual/hourly deferred — they stress
# GBM's constant-vol assumption (long horizons) or overwhelm our scan
# cadence (short horizons).
KALSHI_CRYPTO_SERIES: List[Tuple[str, str, str]] = [
    # Daily
    ("KXBTCD",      "BTC",  "crypto"),
    ("KXBCH",       "BCH",  "crypto"),
    ("KXSHIBA",     "SHIB", "crypto"),
    ("KXAVAXD",     "AVAX", "crypto"),
    ("KXBTCMAXD",   "BTC",  "crypto"),
    # Monthly / annual (Slice 2.5 experiment)
    ("KXBTCMINMON", "BTC",  "crypto"),
    ("KXBTCMAXMON", "BTC",  "crypto"),
    ("KXBTCY",      "BTC",  "crypto"),
]

# SPX and NDX each expose a mix of legacy and KX-prefixed series that often
# emit identical contracts. The dedup pass in fetch_mc_markets collapses them
# by (underlying, direction, threshold, close_time).
KALSHI_EQUITY_INDEX_SERIES: List[Tuple[str, str, str]] = [
    # SPX daily
    ("INX",       "SPX", "equity_index"),
    ("INXU",      "SPX", "equity_index"),
    ("INXZ",      "SPX", "equity_index"),
    ("INXAB",     "SPX", "equity_index"),
    ("KXINX",     "SPX", "equity_index"),
    ("KXINXZ",    "SPX", "equity_index"),
    ("KXINXAB",   "SPX", "equity_index"),
    # SPX weekly / monthly (Slice 2.5 experiment)
    ("KXINXW",    "SPX", "equity_index"),
    ("KXINXM",    "SPX", "equity_index"),
    # NDX daily
    ("NASDAQ100",     "NDX", "equity_index"),
    ("NASDAQ100U",    "NDX", "equity_index"),
    ("NASDAQ100Z",    "NDX", "equity_index"),
    ("KXNASDAQ100",   "NDX", "equity_index"),
    ("KXNASDAQ100Z",  "NDX", "equity_index"),
    # NDX weekly / monthly / annual (Slice 2.5 experiment)
    ("KXNASDAQ100W",  "NDX", "equity_index"),
    ("NASDAQ100M",    "NDX", "equity_index"),
    ("NASDAQ100Y",    "NDX", "equity_index"),
]

# Combined set — fetch_mc_markets iterates and filters by asset_class arg.
KALSHI_SERIES: List[Tuple[str, str, str]] = (
    KALSHI_CRYPTO_SERIES + KALSHI_EQUITY_INDEX_SERIES
)

_KALSHI_PAGE_LIMIT = 200


@dataclass(frozen=True)
class MonteCarloMarket:
    """A binary contract we can price with GBM.

    contract_style:
      - "european":        YES resolves on terminal value (S_T vs threshold).
      - "one_touch_above": YES resolves if S EVER reaches threshold from below
                           at any point in [issuance, expiry]. Requires barrier
                           math (reflection-principle closed-form), not
                           European pricing — GBM terminal probability
                           systematically UNDER-estimates hit probability.
      - "one_touch_below": symmetric downside barrier.

    Default is "european". If rules_primary text can't be confidently
    classified as a barrier contract, we stay european to avoid mispricing.
    """
    ticker: str
    event_ticker: str
    venue: str                  # "kalshi" or "polymarket"
    underlying_asset: str       # e.g. "BTC"
    asset_class: str            # e.g. "crypto"
    direction: str              # "above" | "below" | "between"
    threshold: float
    close_time: datetime        # UTC
    yes_ask: float              # 0..1
    yes_bid: float
    no_ask: float
    no_bid: float
    raw_market: dict            # full API response, for debugging
    threshold_upper: Optional[float] = None  # only set for direction == "between"
    contract_style: str = "european"


# Barrier-language patterns. Order matters: we check for explicit barrier
# phrasing first. If none match, we stay "european".
_ONE_TOUCH_ABOVE_PATTERNS = [
    "ever above",
    "ever reach",
    "ever exceed",
    "at any point above",
    "at any time above",
    "touches",
    "hits",
]
_ONE_TOUCH_BELOW_PATTERNS = [
    "ever below",
    "ever drops below",
    "ever falls below",
    "at any point below",
    "at any time below",
]


def _classify_contract_style(
    rules_primary: Optional[str], direction: str,
) -> str:
    """Classify a contract as 'european', 'one_touch_above', or 'one_touch_below'.

    Conservative: returns 'european' if the rules text is missing or
    ambiguous. We prefer to skip a tradeable barrier contract than to
    misprice a European contract as a barrier.
    """
    if not rules_primary or direction == "between":
        # Between markets aren't barrier-style; they're range markets.
        return "european"
    text = rules_primary.lower()
    if direction == "above":
        for pat in _ONE_TOUCH_ABOVE_PATTERNS:
            if pat in text:
                return "one_touch_above"
        return "european"
    if direction == "below":
        for pat in _ONE_TOUCH_BELOW_PATTERNS:
            if pat in text:
                return "one_touch_below"
        return "european"
    return "european"


class MCMarketError(Exception):
    """Irrecoverable failure during market discovery."""


def fetch_mc_markets(
    asset_classes: List[str],
    *,
    http_client: Optional[httpx.Client] = None,
) -> List[MonteCarloMarket]:
    """Discover GBM-priceable binary contracts across configured venues.

    Args:
        asset_classes: which asset classes to scan (e.g. ["crypto"]).
            Series outside these classes are skipped.
        http_client: injection seam for tests; production callers leave None.

    Returns:
        Deduplicated list of MonteCarloMarket. Dedup key is
        (underlying_asset, direction, threshold, close_time) — first
        occurrence wins; later duplicates are logged at DEBUG and dropped.

    Error policy: if one series' fetch fails (network, 404, parse error),
    it's logged and skipped; other series continue. Only a programmer
    error escapes this function.
    """
    if not asset_classes:
        return []

    markets: List[MonteCarloMarket] = []
    seen: set = set()

    for series_ticker, underlying, asset_class in KALSHI_SERIES:
        if asset_class not in asset_classes:
            continue
        try:
            series_markets = _fetch_kalshi_series(
                series_ticker, underlying, asset_class, http_client=http_client,
            )
        except Exception as e:
            logger.warning(
                f"Failed to fetch Kalshi series {series_ticker}: "
                f"{type(e).__name__}: {e}. Skipping series."
            )
            continue

        for m in series_markets:
            key = (m.underlying_asset, m.direction, m.threshold, m.threshold_upper, m.close_time)
            if key in seen:
                logger.debug(
                    f"Deduplicated {m.ticker} (duplicate of earlier market "
                    f"with key {key})"
                )
                continue
            seen.add(key)
            markets.append(m)

    logger.info(
        f"MC market discovery: {len(markets)} unique markets across "
        f"{len([s for s in KALSHI_SERIES if s[2] in asset_classes])} series"
    )
    return markets


def _fetch_kalshi_series(
    series_ticker: str,
    underlying: str,
    asset_class: str,
    *,
    http_client: Optional[httpx.Client] = None,
) -> List[MonteCarloMarket]:
    """Fetch all open markets for one Kalshi series with pagination."""
    client = http_client or httpx.Client(timeout=15.0)
    owns_client = http_client is None

    results: List[MonteCarloMarket] = []
    cursor: Optional[str] = None
    now = datetime.now(timezone.utc)
    max_close_time = now + timedelta(days=settings.MC_MAX_TIME_TO_EXPIRY_DAYS)

    try:
        while True:
            params = {
                "series_ticker": series_ticker,
                "status": "open",
                "limit": _KALSHI_PAGE_LIMIT,
            }
            if cursor:
                params["cursor"] = cursor

            response = client.get(f"{KALSHI_BASE_URL}/markets", params=params)
            response.raise_for_status()
            data = response.json()

            raw_markets = data.get("markets", [])
            for raw in raw_markets:
                parsed = _parse_kalshi_market(
                    raw, underlying, asset_class, now, max_close_time,
                )
                if parsed is not None:
                    results.append(parsed)

            cursor = data.get("cursor")
            # Stop when cursor missing/empty OR the page was empty (defensive
            # against a non-null cursor with 0 markets, which would infinite-loop).
            if not cursor or not raw_markets:
                break
    finally:
        if owns_client:
            client.close()

    return results


def _parse_kalshi_market(
    raw: dict,
    underlying: str,
    asset_class: str,
    now: datetime,
    max_close_time: datetime,
) -> Optional[MonteCarloMarket]:
    """Parse a Kalshi /markets record into MonteCarloMarket or None if filtered."""
    if raw.get("status") != "active":
        return None

    close_time = _parse_iso_utc(raw.get("close_time"))
    if close_time is None or close_time <= now or close_time >= max_close_time:
        return None

    strike_type = raw.get("strike_type")
    threshold_upper: Optional[float] = None
    if strike_type == "greater":
        direction = "above"
        threshold_raw = raw.get("floor_strike")
    elif strike_type == "less":
        direction = "below"
        threshold_raw = raw.get("cap_strike")
    elif strike_type == "between":
        direction = "between"
        threshold_raw = raw.get("floor_strike")
        upper_raw = raw.get("cap_strike")
        if upper_raw is None:
            return None
        try:
            threshold_upper = float(upper_raw)
        except (TypeError, ValueError):
            return None
    else:
        # Unknown strike_type: skip defensively.
        return None

    if threshold_raw is None:
        return None
    try:
        threshold = float(threshold_raw)
    except (TypeError, ValueError):
        return None

    # For between markets: enforce ordering
    if direction == "between" and threshold_upper is not None and threshold >= threshold_upper:
        return None

    # Prices are dollar strings like "0.0100" (1c) .. "1.0000" (100c).
    try:
        yes_ask = float(raw.get("yes_ask_dollars", "0"))
        yes_bid = float(raw.get("yes_bid_dollars", "0"))
        no_ask = float(raw.get("no_ask_dollars", "0"))
        no_bid = float(raw.get("no_bid_dollars", "0"))
    except (TypeError, ValueError):
        return None

    ticker = raw.get("ticker")
    if not ticker:
        return None

    contract_style = _classify_contract_style(raw.get("rules_primary"), direction)

    return MonteCarloMarket(
        ticker=ticker,
        event_ticker=raw.get("event_ticker", ""),
        venue="kalshi",
        underlying_asset=underlying,
        asset_class=asset_class,
        direction=direction,
        threshold=threshold,
        threshold_upper=threshold_upper,
        close_time=close_time,
        yes_ask=yes_ask,
        yes_bid=yes_bid,
        no_ask=no_ask,
        no_bid=no_bid,
        raw_market=raw,
        contract_style=contract_style,
    )


def _parse_iso_utc(s: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (possibly 'Z'-suffixed) into a UTC datetime."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
