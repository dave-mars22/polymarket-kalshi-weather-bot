"""Monte Carlo signal generator.

Ties market discovery + GBM simulation + vol/drift estimation + fee math +
calibration + bankroll-relative sizing into MonteCarloSignal objects the
scheduler can act on.

Key design notes:
- Markets sharing (underlying_asset, close_time) are batched: one GBM sim
  serves all of them. prob_above(threshold) is cheap (boolean array op).
- Both YES and NO sides are evaluated per market; whichever has higher
  net_edge wins. The other is discarded.
- Short-expiry (< 7 days) contracts force drift_used = 0 because the sample
  mean of log-returns is noise-dominated on short windows (SE ~ sigma/sqrt(n)).
- Sizing is bankroll-relative: the four caps (per-trade / per-underlying /
  per-asset-class / per-total) are computed at scan time from current
  BotState.bankroll, not from INITIAL_BANKROLL. Outstanding MC allocation
  from the Trade table eats into the three non-per-trade caps.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func

from backend.config import settings
from backend.core.calibration import get_calibration_multiplier
from backend.core.fees import get_fee_model
from backend.core.monte_carlo import (
    SimulationResult,
    prob_one_touch_above_analytic,
    prob_one_touch_below_analytic,
    simulate_terminal_prices,
)
from backend.core.vol_estimator import estimate as estimate_vol_drift
from backend.data.mc_markets import MonteCarloMarket, fetch_mc_markets
from backend.data.price_history import (
    InsufficientHistoryError,
    PriceHistoryError,
    fetch_daily_closes,
)
from backend.data.spot_prices import SpotPriceError, fetch_spot
from backend.models.database import BotState, SessionLocal, Trade

logger = logging.getLogger("trading_bot")

# Kalshi underlying symbol -> symbol expected by the price-data adapter.
# Crypto symbols map 1:1 to Coinbase product IDs (the data-layer dispatches
# by asset_class internally). Equity indices map to Yahoo Finance tickers.
_UNDERLYING_TO_SYMBOL: Dict[str, str] = {
    # Crypto (Coinbase)
    "BTC":  "BTC",
    "ETH":  "ETH",
    "SOL":  "SOL",
    "BCH":  "BCH",
    "AVAX": "AVAX",
    "SHIB": "SHIB",
    # Equity indices (yfinance)
    "SPX":  "^GSPC",
    "NDX":  "^NDX",
}

_PERIODS_PER_YEAR_BY_ASSET_CLASS: Dict[str, float] = {
    "crypto": 365.0,           # 24/7 trading
    "equity_index": 252.0,     # US trading days
}

# Contracts expiring within this window force drift_used = 0 (module docstring).
_SHORT_EXPIRY_DRIFT_CUTOFF_YEARS: float = 7.0 / 365.0


# -------------------------------------------------------------------------
# Data classes
# -------------------------------------------------------------------------

@dataclass(frozen=True)
class MonteCarloSignal:
    """Signal for one MC-priceable binary contract, one side picked."""
    market: MonteCarloMarket
    direction: str             # "YES" or "NO"
    model_probability: float   # GBM-derived P for the chosen side
    market_probability: float  # ask price for the chosen side (what we'd pay)
    raw_edge: float
    net_edge: float
    fee_cost: float
    passes_threshold: bool
    suggested_size: float
    reasoning: str
    spot_used: float
    vol_used: float
    drift_used: float
    years_to_expiry: float


@dataclass(frozen=True)
class _AllocationState:
    """Snapshot of MC exposure for the duration of one scan."""
    bankroll: float
    total_mc: float
    by_underlying: Dict[str, float]
    by_asset_class: Dict[str, float]


@dataclass(frozen=True)
class _SimBundle:
    """Per-group simulation plus the inputs used to build it."""
    sim: SimulationResult
    spot: float
    vol: float
    drift: float
    years_to_expiry: float


# -------------------------------------------------------------------------
# Public orchestrator
# -------------------------------------------------------------------------

def scan_for_mc_signals() -> List[MonteCarloSignal]:
    """Discover MC markets, simulate, emit signals (actionable + sub-threshold).

    Sub-threshold signals are included so the dashboard can render them.
    Returns [] if MC_ENABLED is False or no markets were discovered.
    """
    if not settings.MC_ENABLED:
        return []

    markets = fetch_mc_markets(["crypto"])
    if not markets:
        logger.info("MC scan: no markets discovered")
        return []

    alloc = _get_allocation_state()
    groups = _group_by_simulation_key(markets)

    signals: List[MonteCarloSignal] = []
    for (underlying, close_time), group in groups.items():
        try:
            sim_bundle = _simulate_group(
                underlying, close_time, asset_class=group[0].asset_class,
            )
        except (InsufficientHistoryError, SpotPriceError, PriceHistoryError, ValueError) as e:
            logger.info(
                f"MC skip group ({underlying}, {close_time.isoformat()}): "
                f"{type(e).__name__}: {e}"
            )
            continue

        for market in group:
            signal = _generate_signal_for_market(market, sim_bundle, alloc)
            if signal is not None:
                signals.append(signal)

    actionable = sum(1 for s in signals if s.passes_threshold)
    logger.info(f"MC scan: {len(signals)} signals ({actionable} actionable)")
    return signals


# -------------------------------------------------------------------------
# Simulation grouping + execution
# -------------------------------------------------------------------------

def _group_by_simulation_key(
    markets: List[MonteCarloMarket],
) -> Dict[Tuple[str, datetime], List[MonteCarloMarket]]:
    """Batch markets so those sharing (underlying, close_time) share a sim."""
    groups: Dict[Tuple[str, datetime], List[MonteCarloMarket]] = {}
    for m in markets:
        key = (m.underlying_asset, m.close_time)
        groups.setdefault(key, []).append(m)
    return groups


def _simulate_group(
    underlying: str,
    close_time: datetime,
    asset_class: str,
) -> _SimBundle:
    """Run one GBM simulation for a (underlying, close_time) group."""
    now = datetime.now(timezone.utc)
    years_to_expiry = (close_time - now).total_seconds() / (365.0 * 24 * 3600)
    if years_to_expiry <= 0:
        raise ValueError(f"close_time {close_time} already passed")

    price_symbol = _UNDERLYING_TO_SYMBOL.get(underlying)
    if price_symbol is None:
        raise ValueError(f"No spot/history source for underlying {underlying!r}")

    # Fetch enough history to cover the larger of vol/drift windows + buffer.
    request_days = max(
        settings.MC_MIN_HISTORY_DAYS,
        max(settings.MC_VOL_LOOKBACK_DAYS, settings.MC_DRIFT_LOOKBACK_DAYS) + 10,
    )
    history = fetch_daily_closes(price_symbol, asset_class, days=request_days)
    prices = [close for _, close in history]

    spot = fetch_spot(price_symbol, asset_class)

    periods_per_year = _PERIODS_PER_YEAR_BY_ASSET_CLASS.get(asset_class, 365.0)
    est = estimate_vol_drift(
        prices,
        periods_per_year=periods_per_year,
        use_ewma=settings.MC_USE_EWMA_VOL,
        ewma_lambda=settings.MC_EWMA_LAMBDA,
        vol_window=settings.MC_VOL_LOOKBACK_DAYS,
        drift_window=settings.MC_DRIFT_LOOKBACK_DAYS,
    )

    drift_used = (
        0.0 if years_to_expiry < _SHORT_EXPIRY_DRIFT_CUTOFF_YEARS else est.mu_annual
    )

    sim = simulate_terminal_prices(
        spot=spot,
        drift_annual=drift_used,
        vol_annual=est.sigma_annual,
        years_to_expiry=years_to_expiry,
        n_paths=settings.MC_NUM_PATHS,
    )

    return _SimBundle(
        sim=sim,
        spot=spot,
        vol=est.sigma_annual,
        drift=drift_used,
        years_to_expiry=years_to_expiry,
    )


# -------------------------------------------------------------------------
# Per-market signal generation
# -------------------------------------------------------------------------

def _generate_signal_for_market(
    market: MonteCarloMarket,
    sim_bundle: _SimBundle,
    alloc: _AllocationState,
) -> Optional[MonteCarloSignal]:
    """Return the better of the YES/NO candidates, or None if both filtered."""
    min_years = settings.MC_MIN_TIME_TO_EXPIRY_HOURS / (24.0 * 365.0)
    if sim_bundle.years_to_expiry < min_years:
        return None

    # Direction and contract_style together determine YES-probability math:
    #   european + above:   P(YES) = P(S_T > K)
    #   european + below:   P(YES) = P(S_T < K)
    #   european + between: P(YES) = P(K_low < S_T <= K_high)
    #   one_touch_above:    P(YES) = P(max_{t<=T} S_t >= K)   (barrier math)
    #   one_touch_below:    P(YES) = P(min_{t<=T} S_t <= K)   (barrier math)
    style = market.contract_style
    if style == "one_touch_above":
        p = prob_one_touch_above_analytic(
            spot=sim_bundle.spot,
            barrier=market.threshold,
            drift_annual=sim_bundle.drift,
            vol_annual=sim_bundle.vol,
            years_to_expiry=sim_bundle.years_to_expiry,
        )
        yes_model_p, no_model_p = p, 1.0 - p
    elif style == "one_touch_below":
        p = prob_one_touch_below_analytic(
            spot=sim_bundle.spot,
            barrier=market.threshold,
            drift_annual=sim_bundle.drift,
            vol_annual=sim_bundle.vol,
            years_to_expiry=sim_bundle.years_to_expiry,
        )
        yes_model_p, no_model_p = p, 1.0 - p
    elif market.direction == "above":
        p = sim_bundle.sim.prob_above(market.threshold)
        yes_model_p, no_model_p = p, 1.0 - p
    elif market.direction == "below":
        p = sim_bundle.sim.prob_above(market.threshold)
        yes_model_p, no_model_p = 1.0 - p, p
    elif market.direction == "between":
        if market.threshold_upper is None:
            return None  # defensive; parser should have enforced this
        p = sim_bundle.sim.prob_in_range(market.threshold, market.threshold_upper)
        yes_model_p, no_model_p = p, 1.0 - p
    else:
        return None

    yes_candidate = _evaluate_side(
        side="YES", market=market, model_p=yes_model_p, ask=market.yes_ask,
        sim_bundle=sim_bundle, alloc=alloc,
    )
    no_candidate = _evaluate_side(
        side="NO", market=market, model_p=no_model_p, ask=market.no_ask,
        sim_bundle=sim_bundle, alloc=alloc,
    )

    candidates = [c for c in (yes_candidate, no_candidate) if c is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.net_edge)


def _evaluate_side(
    side: str,
    market: MonteCarloMarket,
    model_p: float,
    ask: float,
    sim_bundle: _SimBundle,
    alloc: _AllocationState,
) -> Optional[MonteCarloSignal]:
    """Build a candidate signal for one side (YES or NO), or None if gated out."""
    if ask <= 0 or ask >= 1:
        return None
    if ask > settings.MC_MAX_ENTRY_PRICE:
        return None

    # Use per-trade cap as trial_size for fee math. Fee-per-notional is
    # mostly constant at a fixed price, so this is a good approximation.
    #
    # We call the fee model directly instead of fees.net_edge because
    # net_edge's sign-flipping convention produces nonsense for sides with
    # raw_edge <= 0 at very low entry prices (fees can exceed |raw|).
    # Here we evaluate each side independently: net_edge = raw_edge - fees,
    # always subtracting fees regardless of sign.
    trial_size = max(1.0, settings.MC_MAX_TRADE_SIZE_PCT * alloc.bankroll)
    fee_model = get_fee_model(
        venue="kalshi",
        btc_slippage_bps=settings.BTC_SLIPPAGE_BPS,
        kalshi_slippage_bps=settings.KALSHI_SLIPPAGE_BPS,
        market_type="monte_carlo",
    )
    fee_breakdown = fee_model.estimate_round_trip_cost(
        entry_price=ask, size_usd=trial_size,
    )
    fee_as_edge = fee_breakdown.total / trial_size if trial_size > 0 else 0.0
    raw_edge = model_p - ask
    net_edge_val = raw_edge - fee_as_edge

    size = _size_mc_trade(
        net_edge=net_edge_val,
        model_prob=model_p,
        entry_price=ask,
        bankroll=alloc.bankroll,
        underlying=market.underlying_asset,
        asset_class=market.asset_class,
        alloc=alloc,
    )

    passes = net_edge_val >= settings.MC_MIN_EDGE_THRESHOLD and size > 0

    reasoning = _build_reasoning(
        side=side, market=market, model_p=model_p, ask=ask,
        raw_edge=raw_edge, net_edge=net_edge_val, fee_cost=fee_breakdown.total,
        sim_bundle=sim_bundle, size=size, passes=passes,
    )

    return MonteCarloSignal(
        market=market,
        direction=side,
        model_probability=model_p,
        market_probability=ask,
        raw_edge=raw_edge,
        net_edge=net_edge_val,
        fee_cost=fee_breakdown.total,
        passes_threshold=passes,
        suggested_size=size,
        reasoning=reasoning,
        spot_used=sim_bundle.spot,
        vol_used=sim_bundle.vol,
        drift_used=sim_bundle.drift,
        years_to_expiry=sim_bundle.years_to_expiry,
    )


# -------------------------------------------------------------------------
# Sizing
# -------------------------------------------------------------------------

def _size_mc_trade(
    *,
    net_edge: float,
    model_prob: float,
    entry_price: float,
    bankroll: float,
    underlying: str,
    asset_class: str,
    alloc: _AllocationState,
) -> float:
    """Fractional Kelly, calibration-shrunk, clamped to four bankroll-pct caps."""
    if net_edge <= 0:
        return 0.0
    if not (0.0 < entry_price < 1.0) or not (0.0 < model_prob < 1.0):
        return 0.0

    # f* = p - q/b where b = (1-price)/price
    odds = (1.0 - entry_price) / entry_price
    lose_prob = 1.0 - model_prob
    kelly_full = (model_prob * odds - lose_prob) / odds
    if kelly_full <= 0:
        return 0.0

    kelly_frac = kelly_full * settings.KELLY_FRACTION
    kelly_frac *= get_calibration_multiplier("monte_carlo")
    if kelly_frac <= 0:
        return 0.0

    desired = kelly_frac * bankroll

    per_trade_cap = settings.MC_MAX_TRADE_SIZE_PCT * bankroll
    per_und_cap = settings.MC_MAX_PER_UNDERLYING_PCT * bankroll
    per_ac_cap = settings.MC_MAX_ASSET_CLASS_PCT * bankroll
    per_total_cap = settings.MC_MAX_TOTAL_ALLOCATION_PCT * bankroll

    und_headroom = max(0.0, per_und_cap - alloc.by_underlying.get(underlying, 0.0))
    ac_headroom = max(0.0, per_ac_cap - alloc.by_asset_class.get(asset_class, 0.0))
    total_headroom = max(0.0, per_total_cap - alloc.total_mc)

    size = min(desired, per_trade_cap, und_headroom, ac_headroom, total_headroom)
    return max(size, 0.0)


# -------------------------------------------------------------------------
# Reasoning string
# -------------------------------------------------------------------------

def _build_reasoning(
    *,
    side: str,
    market: MonteCarloMarket,
    model_p: float,
    ask: float,
    raw_edge: float,
    net_edge: float,
    fee_cost: float,
    sim_bundle: _SimBundle,
    size: float,
    passes: bool,
) -> str:
    """Human-readable summary; noted as ACTIONABLE or SUB-THRESHOLD up front."""
    hours = sim_bundle.years_to_expiry * 365.0 * 24.0
    drift_note = ""
    if (
        sim_bundle.years_to_expiry < _SHORT_EXPIRY_DRIFT_CUTOFF_YEARS
        and sim_bundle.drift == 0.0
    ):
        drift_note = " (drift=0 forced: <7d expiry)"
    status = "ACTIONABLE" if passes else "SUB-THRESHOLD"
    if market.direction == "between" and market.threshold_upper is not None:
        range_str = f"in [${market.threshold:,.2f}, ${market.threshold_upper:,.2f}]"
    else:
        range_str = f"${market.threshold:,.2f}"
    style_tag = f" [{market.contract_style}]" if market.contract_style != "european" else ""
    return (
        f"[{status}]{style_tag} {market.underlying_asset} {market.direction} "
        f"{range_str} expires in {hours:.1f}h | "
        f"spot=${sim_bundle.spot:,.2f} vol={sim_bundle.vol:.1%} "
        f"drift={sim_bundle.drift:+.1%}{drift_note} | "
        f"{settings.MC_NUM_PATHS:,} paths | "
        f"Side={side} model_p={model_p:.1%} ask={ask:.1%} "
        f"raw={raw_edge:+.2%} net={net_edge:+.2%} (fees ${fee_cost:.2f}) | "
        f"size=${size:.2f}"
    )


# -------------------------------------------------------------------------
# Allocation state (DB read)
# -------------------------------------------------------------------------

def _get_allocation_state() -> _AllocationState:
    """Snapshot current bankroll + outstanding MC allocations from the DB."""
    db = SessionLocal()
    try:
        state = db.query(BotState).first()
        bankroll = float(state.bankroll) if state else float(settings.INITIAL_BANKROLL)

        base_filter = [
            Trade.settled == False,
            Trade.market_type == "monte_carlo",
        ]

        total = float(
            db.query(func.coalesce(func.sum(Trade.size), 0.0))
            .filter(*base_filter)
            .scalar()
            or 0.0
        )

        und_rows = (
            db.query(Trade.underlying_asset, func.coalesce(func.sum(Trade.size), 0.0))
            .filter(*base_filter)
            .group_by(Trade.underlying_asset)
            .all()
        )
        by_underlying = {k: float(v) for k, v in und_rows if k is not None}

        ac_rows = (
            db.query(Trade.asset_class, func.coalesce(func.sum(Trade.size), 0.0))
            .filter(*base_filter)
            .group_by(Trade.asset_class)
            .all()
        )
        by_asset_class = {k: float(v) for k, v in ac_rows if k is not None}

        return _AllocationState(
            bankroll=bankroll,
            total_mc=total,
            by_underlying=by_underlying,
            by_asset_class=by_asset_class,
        )
    finally:
        db.close()
