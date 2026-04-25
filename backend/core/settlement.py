"""Trade settlement logic for BTC 5-min markets using Polymarket API, with Kalshi resolution helper for future reuse."""
import httpx
import json
import logging
from datetime import datetime, date
from typing import Optional, List, Tuple
from sqlalchemy.orm import Session

from backend.models.database import Trade, BotState, Signal

logger = logging.getLogger("trading_bot")


async def fetch_polymarket_resolution(market_id: str, event_slug: Optional[str] = None) -> Tuple[bool, Optional[float]]:
    """
    Fetch actual market resolution from Polymarket API.

    For BTC 5-min markets, uses event slug to find the market.

    Returns: (is_resolved, settlement_value)
        - settlement_value: 1.0 if Up won, 0.0 if Down won
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Try event slug first (more reliable for BTC 5-min markets)
            if event_slug:
                response = await client.get(
                    "https://gamma-api.polymarket.com/events",
                    params={"slug": event_slug}
                )
                response.raise_for_status()
                events = response.json()

                if events:
                    event = events[0] if isinstance(events, list) else events
                    markets = event.get("markets", [])
                    if markets:
                        return _parse_market_resolution(markets[0])

            # Fallback: try market ID directly
            url = f"https://gamma-api.polymarket.com/markets/{market_id}"
            response = await client.get(url)

            if response.status_code == 404:
                return await _search_market_in_events(market_id)

            response.raise_for_status()
            market = response.json()
            return _parse_market_resolution(market)

    except Exception as e:
        logger.warning(f"Failed to fetch resolution for {event_slug or market_id}: {e}")
        return False, None


async def _search_market_in_events(market_id: str) -> Tuple[bool, Optional[float]]:
    """Search for market in events (both active and closed)."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for closed in [True, False]:
                params = {
                    "closed": str(closed).lower(),
                    "limit": 200
                }
                response = await client.get(
                    "https://gamma-api.polymarket.com/events",
                    params=params
                )
                response.raise_for_status()
                events = response.json()

                for event in events:
                    for market in event.get("markets", []):
                        if str(market.get("id")) == str(market_id):
                            return _parse_market_resolution(market)

        return False, None

    except Exception as e:
        logger.warning(f"Failed to search for market {market_id}: {e}")
        return False, None


def _parse_market_resolution(market: dict) -> Tuple[bool, Optional[float]]:
    """
    Parse market data to determine if resolved and outcome.

    Handles both Yes/No and Up/Down outcomes.
    - outcomePrices[0] > 0.99 -> first outcome won (Yes or Up)
    - outcomePrices[0] < 0.01 -> second outcome won (No or Down)
    """
    is_closed = market.get("closed", False)

    if not is_closed:
        return False, None

    outcome_prices = market.get("outcomePrices", [])
    if not outcome_prices:
        return False, None

    try:
        if isinstance(outcome_prices, str):
            outcome_prices = json.loads(outcome_prices)

        first_price = float(outcome_prices[0]) if outcome_prices else 0.5

        if first_price > 0.99:
            # First outcome won (Up or Yes)
            logger.info(f"Market {market.get('id')} resolved: UP/YES won")
            return True, 1.0
        elif first_price < 0.01:
            # Second outcome won (Down or No)
            logger.info(f"Market {market.get('id')} resolved: DOWN/NO won")
            return True, 0.0
        else:
            return False, None

    except (ValueError, IndexError, TypeError) as e:
        logger.warning(f"Failed to parse outcome prices: {e}")
        return False, None


def _merge_settlement_features(trade: Trade, settlement_value: float) -> None:
    """Layer realized-outcome keys onto trade.features without overwriting
    signal/exec keys already captured at entry time.

    Records: settled_outcome (1/0), realized_pnl, settlement_timestamp,
    minutes_to_settlement, predicted_vs_realized (model_prob minus actual).
    """
    existing = dict(trade.features or {})
    now = datetime.utcnow()
    entry_ts = trade.timestamp
    minutes_to_settle: Optional[float] = None
    if entry_ts is not None:
        minutes_to_settle = round((now - entry_ts).total_seconds() / 60.0, 2)

    direction = (trade.direction or "").lower()
    if direction in ("up", "yes"):
        predicted_yes_p = trade.model_probability
    elif direction in ("down", "no"):
        predicted_yes_p = (
            1.0 - trade.model_probability
            if trade.model_probability is not None else None
        )
    else:
        predicted_yes_p = trade.model_probability

    predicted_vs_realized = None
    if predicted_yes_p is not None:
        predicted_vs_realized = round(predicted_yes_p - float(settlement_value), 6)

    outcome_patch = {
        "settled_outcome": float(settlement_value),
        "realized_pnl": None if trade.pnl is None else round(float(trade.pnl), 4),
        "settlement_timestamp": now.isoformat(),
        "minutes_to_settlement": minutes_to_settle,
        "predicted_vs_realized": predicted_vs_realized,
    }
    # Merge so existing signal/exec keys win on any (unexpected) collision;
    # outcome keys use distinct names so in practice both sets coexist.
    trade.features = {**outcome_patch, **existing}


def calculate_pnl(trade: Trade, settlement_value: float) -> float:
    """
    Calculate P&L for a trade given the settlement value.

    settlement_value: 1.0 if Up/Yes outcome, 0.0 if Down/No outcome

    Maps up->yes, down->no internally:
    - UP position wins when settlement = 1.0
    - DOWN position wins when settlement = 0.0
    """
    # Map up/down to yes/no logic
    direction = trade.direction
    if direction == "up":
        direction = "yes"
    elif direction == "down":
        direction = "no"

    if direction == "yes":
        if settlement_value == 1.0:
            pnl = trade.size * (1.0 - trade.entry_price)
        else:
            pnl = -trade.size * trade.entry_price
    else:  # NO / DOWN position
        if settlement_value == 0.0:
            pnl = trade.size * (1.0 - trade.entry_price)
        else:
            pnl = -trade.size * trade.entry_price

    return round(pnl, 2)


async def check_market_settlement(trade: Trade) -> Tuple[bool, Optional[float], Optional[float]]:
    """
    Check if a trade's market has settled. Routes by platform:
      - Kalshi -> _fetch_kalshi_resolution (signed auth not required for public markets)
      - Polymarket (and anything else) -> fetch_polymarket_resolution

    Returns: (is_settled, settlement_value, pnl)
    """
    platform = (getattr(trade, "platform", None) or "polymarket").lower()

    if platform == "kalshi":
        is_resolved, settlement_value = await _fetch_kalshi_resolution(trade.market_ticker)
    else:
        is_resolved, settlement_value = await fetch_polymarket_resolution(
            trade.market_ticker,
            event_slug=trade.event_slug,
        )

    if not is_resolved or settlement_value is None:
        return False, None, None

    pnl = calculate_pnl(trade, settlement_value)

    mapped_dir = "UP" if trade.direction in ("up", "yes") else "DOWN"
    outcome = "UP" if settlement_value == 1.0 else "DOWN"
    result = "WIN" if mapped_dir == outcome else "LOSS"

    logger.info(f"Trade {trade.id} ({platform}) settled: {mapped_dir} @ "
                f"{trade.entry_price:.2%} -> {result} P&L: ${pnl:+.2f}")

    return True, settlement_value, pnl


_KALSHI_PUBLIC_BASE = "https://api.elections.kalshi.com/trade-api/v2"


async def _fetch_kalshi_resolution(
    ticker: str,
    *,
    http_client: Optional[httpx.AsyncClient] = None,
) -> Tuple[bool, Optional[float]]:
    """Fetch resolution status for a Kalshi market via the public market
    endpoint.

    Slice B1 fix: this function previously bailed via
    `kalshi_credentials_present()` and never made an API call, leaving
    every MC trade in pending state forever. The Kalshi
    /trade-api/v2/markets/{ticker} endpoint is publicly readable — no
    auth header required. Authenticated requests are only needed for
    writes (placing orders, querying private balance), not for reading
    public market resolution status. Verified via direct curl on
    2026-04-25; see commit body for the full bug story.

    Returns (is_resolved, settlement_value):
      - status == 'finalized' AND result == 'yes' → (True, 1.0)
      - status == 'finalized' AND result == 'no'  → (True, 0.0)
      - any other state (open, ambiguous, malformed, HTTP error)
        → (False, None) so the settlement loop tries again next cycle

    `http_client` is an injection seam for tests (matches the same
    pattern in mc_execution.fetch_current_ask and spot_prices.fetch_spot);
    production callers leave it None to get a fresh per-call client.
    """
    url = f"{_KALSHI_PUBLIC_BASE}/markets/{ticker}"
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=15.0)
    try:
        try:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
        finally:
            if owns_client:
                await client.aclose()

        # Response shape per Kalshi v2 docs and verified live: top-level
        # {"market": {...}}. Be tolerant of an unwrapped variant just in
        # case (some endpoints return the resource directly).
        market = data.get("market", data) if isinstance(data, dict) else {}
        status = market.get("status", "")
        result = market.get("result", "")

        # Strict equality — Kalshi's API contract is case-explicit.
        if status in ("finalized", "determined") and result:
            if result == "yes":
                return True, 1.0
            if result == "no":
                return True, 0.0

        return False, None

    except Exception as e:
        logger.warning(f"Failed to fetch Kalshi resolution for {ticker}: {e}")
        return False, None


async def settle_pending_trades(db: Session) -> List[Trade]:
    """
    Process all pending trades for settlement.
    Uses REAL market outcomes from Polymarket API.
    """
    try:
        pending = db.query(Trade).filter(Trade.settled == False).all()
    except Exception as e:
        logger.error(f"Failed to query pending trades: {e}")
        return []

    if not pending:
        logger.info("No pending trades to settle")
        return []

    logger.info(f"Checking {len(pending)} pending trades for settlement...")
    settled_trades = []

    for trade in pending:
        try:
            is_settled, settlement_value, pnl = await check_market_settlement(trade)

            if is_settled and settlement_value is not None:
                trade.settled = True
                trade.settlement_value = settlement_value
                trade.pnl = pnl
                trade.settlement_time = datetime.utcnow()

                if pnl is not None and pnl > 0:
                    trade.result = "win"
                elif pnl is not None and pnl < 0:
                    trade.result = "loss"
                else:
                    trade.result = "push"

                # Slice 4: merge realized outcome keys into trade.features.
                # Merge (not overwrite) so signal/exec features survive.
                # predicted_vs_realized = model_probability vs actual outcome
                # bit (1.0/0.0), expressed as signed error. Minutes to
                # settlement measured from entry to settlement_time.
                _merge_settlement_features(trade, settlement_value)

                settled_trades.append(trade)

                # Update linked Signal with actual outcome for calibration
                if trade.signal_id:
                    linked_signal = db.query(Signal).filter(Signal.id == trade.signal_id).first()
                    if linked_signal:
                        actual_outcome = "up" if settlement_value == 1.0 else "down"
                        linked_signal.actual_outcome = actual_outcome
                        linked_signal.outcome_correct = (linked_signal.direction == actual_outcome)
                        linked_signal.settlement_value = settlement_value
                        linked_signal.settled_at = datetime.utcnow()
        except Exception as e:
            logger.error(f"Failed to settle trade {trade.id}: {e}")
            continue

    if settled_trades:
        try:
            db.commit()
            logger.info(f"Settled {len(settled_trades)} trades")
        except Exception as e:
            logger.error(f"Failed to commit settlements: {e}")
            db.rollback()
            return []
    else:
        logger.info("No trades ready for settlement (markets still open)")

    return settled_trades


async def update_bot_state_with_settlements(db: Session, settled_trades: List[Trade]) -> None:
    """Update bot state with P&L from settled trades."""
    if not settled_trades:
        return

    try:
        state = db.query(BotState).first()
        if not state:
            logger.warning("Bot state not found")
            return

        for trade in settled_trades:
            if trade.pnl is not None:
                state.total_pnl += trade.pnl
                state.bankroll += trade.pnl
                if trade.result == "win":
                    state.winning_trades += 1

        db.commit()
        logger.info(f"Updated bot state: Bankroll ${state.bankroll:.2f}, P&L ${state.total_pnl:+.2f}")
    except Exception as e:
        logger.error(f"Failed to update bot state: {e}")
        db.rollback()
