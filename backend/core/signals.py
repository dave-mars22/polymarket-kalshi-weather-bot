"""Signal generator for crypto 5-minute Up/Down markets on Polymarket.

Parameterized over `underlying` (BTC / ETH / SOL / XRP). Slice 3a-2
refactors this module to take underlying as an argument throughout; the
actual iteration over multiple underlyings lands in slice 3e. Until then,
scan_for_signals() scans BTC only (behavior-identical to pre-3a-2).
"""
import logging
from datetime import datetime
from typing import Optional, List
from dataclasses import dataclass, field
import asyncio

from backend.config import settings
from backend.core.fees import net_edge
from backend.core.calibration import get_calibration_multiplier
from backend.data.crypto_markets import CryptoUpDownMarket, fetch_active_crypto_markets
from backend.data.crypto import fetch_crypto_price, compute_crypto_microstructure
from backend.models.database import SessionLocal, Signal

logger = logging.getLogger("trading_bot")


@dataclass
class TradingSignal:
    """A trading signal for a crypto 5-min market.

    The underlying asset is identified by TradingSignal.underlying; the
    market object is now a generic CryptoUpDownMarket (same shape
    regardless of BTC/ETH/SOL/XRP). Field renames btc_price/btc_change_*
    → underlying_* land in slice 3e alongside the frontend update.
    """
    market: CryptoUpDownMarket

    # Core signal data
    model_probability: float = 0.5  # Our estimated probability of UP
    market_probability: float = 0.5  # Market's implied UP probability
    edge: float = 0.0                # Equals net_edge; kept for backward compat
    raw_edge: float = 0.0            # Before fees (for dashboard/debug)
    net_edge: float = 0.0            # After fees (used for gating)
    fee_cost: float = 0.0            # Dollar cost of fees + slippage
    direction: str = "up"  # "up" or "down"

    # Confidence and sizing
    confidence: float = 0.5
    kelly_fraction: float = 0.0
    suggested_size: float = 0.0

    # Metadata
    sources: List[str] = field(default_factory=list)
    reasoning: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)

    # BTC price context
    btc_price: float = 0.0
    btc_change_1h: float = 0.0
    btc_change_24h: float = 0.0

    @property
    def passes_threshold(self) -> bool:
        """Check if signal passes minimum edge threshold (after fees)."""
        return abs(self.net_edge) >= settings.MIN_EDGE_THRESHOLD


def calculate_edge(
    model_prob: float,
    market_price: float
) -> tuple[float, str]:
    """Calculate edge and determine direction."""
    up_edge = model_prob - market_price
    down_edge = (1 - model_prob) - (1 - market_price)

    if up_edge >= down_edge:
        return up_edge, "up"
    else:
        return down_edge, "down"


def calculate_kelly_size(
    edge: float,
    probability: float,
    market_price: float,
    direction: str,
    bankroll: float
) -> float:
    """Calculate position size using fractional Kelly criterion."""
    if direction == "up":
        win_prob = probability
        price = market_price
    else:
        win_prob = 1 - probability
        price = 1 - market_price

    if price <= 0 or price >= 1:
        return 0

    odds = (1 - price) / price
    lose_prob = 1 - win_prob
    kelly = (win_prob * odds - lose_prob) / odds
    kelly *= settings.KELLY_FRACTION

    max_fraction = 0.05
    kelly = min(kelly, max_fraction)
    kelly = max(kelly, 0)

    size = kelly * bankroll
    size = min(size, settings.MAX_TRADE_SIZE)

    return size


async def generate_crypto_tech_signal(
    market: CryptoUpDownMarket, underlying: str,
) -> Optional[TradingSignal]:
    """Generate a technical signal for a crypto 5-min Up/Down market.

    Indicator math and gates are identical across underlyings — only the
    data source varies. Gates (Change 2a — tighter):
    - Convergence: 3-of-5 indicators must agree on direction (was 2-of-4)
    - RSI neutrality filter: reject if |RSI - 50| < 5 (pure noise zone)
    - Momentum floor: at least one window must show |change| > 0.1%
    - Fee-adjusted edge: trade only if NET edge >= threshold
    - Entry price filter: only enter when price <= MAX_ENTRY_PRICE
    - Time window filter: MIN_TIME_REMAINING <= time_left <= MAX_TIME_REMAINING
    """
    underlying = underlying.upper()
    try:
        micro = await compute_crypto_microstructure(underlying)
    except Exception as e:
        logger.warning(f"Failed to compute {underlying} microstructure: {e}")
        return None

    if not micro:
        return None

    market_up_prob = market.up_price

    if market_up_prob < 0.02 or market_up_prob > 0.98:
        return None

    # --- Individual indicator signals ---

    # 1) RSI
    if micro.rsi < 30:
        rsi_signal = 0.5 + (30 - micro.rsi) / 30
    elif micro.rsi > 70:
        rsi_signal = -0.5 - (micro.rsi - 70) / 30
    elif micro.rsi < 45:
        rsi_signal = (45 - micro.rsi) / 30
    elif micro.rsi > 55:
        rsi_signal = -(micro.rsi - 55) / 30
    else:
        rsi_signal = 0.0
    rsi_signal = max(-1.0, min(1.0, rsi_signal))

    # 2) Momentum
    mom_blend = micro.momentum_1m * 0.5 + micro.momentum_5m * 0.35 + micro.momentum_15m * 0.15
    momentum_signal = max(-1.0, min(1.0, mom_blend / 0.10))

    # 3) VWAP deviation
    vwap_signal = max(-1.0, min(1.0, micro.vwap_deviation / 0.05))

    # 4) SMA crossover
    sma_signal = max(-1.0, min(1.0, micro.sma_crossover / 0.03))

    # 5) Market skew
    market_skew = market_up_prob - 0.50
    skew_signal = max(-1.0, min(1.0, -market_skew * 4))

    # --- Convergence filter (Change 2a: now counts all 5 indicators, stricter) ---
    indicator_signs = [
        rsi_signal,
        momentum_signal,
        vwap_signal,
        sma_signal,
        skew_signal,
    ]
    up_votes = sum(1 for s in indicator_signs if s > 0.05)
    down_votes = sum(1 for s in indicator_signs if s < -0.05)

    convergence_threshold = 3 if settings.USE_STRICT_CONVERGENCE else 2
    has_convergence = up_votes >= convergence_threshold or down_votes >= convergence_threshold

    # --- NEW: RSI neutrality filter ---
    rsi_is_neutral = abs(micro.rsi - 50) < 5

    # --- NEW: Momentum magnitude floor ---
    max_momentum_mag = max(
        abs(micro.momentum_1m),
        abs(micro.momentum_5m),
        abs(micro.momentum_15m),
    )
    momentum_has_signal = max_momentum_mag > 0.001

    # --- Weighted composite ---
    w = settings
    composite = (
        rsi_signal * w.WEIGHT_RSI
        + momentum_signal * w.WEIGHT_MOMENTUM
        + vwap_signal * w.WEIGHT_VWAP
        + sma_signal * w.WEIGHT_SMA
        + skew_signal * w.WEIGHT_MARKET_SKEW
    )

    model_up_prob = 0.50 + composite * 0.15
    model_up_prob = max(0.35, min(0.65, model_up_prob))

    # Raw edge
    raw_edge_val, direction = calculate_edge(model_up_prob, market_up_prob)

    # Entry price
    if direction == "up":
        entry_price = market_up_prob
    else:
        entry_price = market.down_price

    # Fee-adjusted edge
    trial_size = min(settings.MAX_TRADE_SIZE, settings.INITIAL_BANKROLL * 0.05)
    _, net_edge_val, fee_breakdown = net_edge(
        model_prob=model_up_prob,
        market_prob=market_up_prob,
        entry_price=entry_price if entry_price > 0 else 0.5,
        size_usd=trial_size,
        venue="polymarket",
        market_type="btc",
        btc_slippage_bps=settings.BTC_SLIPPAGE_BPS,
        kalshi_slippage_bps=settings.KALSHI_SLIPPAGE_BPS,
    )
    fee_cost = fee_breakdown.total

    # Time filter
    now = datetime.utcnow()
    window_end = market.window_end
    if window_end.tzinfo is not None:
        window_end = window_end.replace(tzinfo=None)
    time_remaining = (window_end - now).total_seconds()
    time_ok = settings.MIN_TIME_REMAINING <= time_remaining <= settings.MAX_TIME_REMAINING

    # Combined filter check
    passes_filters = (
        has_convergence
        and not rsi_is_neutral
        and momentum_has_signal
        and entry_price <= settings.MAX_ENTRY_PRICE
        and time_ok
    )

    # calculate_edge returns the direction-specific positive raw edge;
    # fees reduce it. If fees exceed raw edge, no profitable direction
    # exists — zero out to skip. (Earlier version used a sign-flip trick
    # on net_edge_val which broke when fees > |raw|; we now derive
    # direction-specific edge from fee_breakdown directly.)
    fee_as_edge = fee_breakdown.total / trial_size if trial_size > 0 else 0.0
    edge = max(0.0, raw_edge_val - fee_as_edge)
    net_edge_val = edge

    if not passes_filters:
        edge = 0.0
        net_edge_val = 0.0
        raw_edge_val = 0.0

    # Confidence
    vol_factor = min(1.0, micro.volatility / 0.05) if micro.volatility > 0 else 0.5
    convergence_strength = max(up_votes, down_votes) / 5.0
    confidence = min(0.8, 0.3 + convergence_strength * 0.3 + abs(composite) * 0.2) * vol_factor

    # Kelly sizing
    bankroll = settings.INITIAL_BANKROLL
    suggested_size = calculate_kelly_size(
        edge=abs(edge),
        probability=model_up_prob,
        market_price=market_up_prob,
        direction=direction,
        bankroll=bankroll,
    )
    # Calibration-adjusted Kelly (Change 3): shrink sizing if model has been overconfident
    calib_mult = get_calibration_multiplier("btc")
    suggested_size *= calib_mult

    # Build reasoning
    filter_status = "ACTIONABLE" if passes_filters else "FILTERED"
    filter_reasons = []
    if not has_convergence:
        filter_reasons.append(f"convergence {max(up_votes, down_votes)}/5 < {convergence_threshold}")
    if rsi_is_neutral:
        filter_reasons.append(f"RSI neutral ({micro.rsi:.0f}, needs |rsi-50|>=5)")
    if not momentum_has_signal:
        filter_reasons.append(f"momentum too weak (max={max_momentum_mag*100:.3f}%<0.1%)")
    if not time_ok:
        filter_reasons.append(f"time {time_remaining:.0f}s not in [{settings.MIN_TIME_REMAINING},{settings.MAX_TIME_REMAINING}]")
    if entry_price > settings.MAX_ENTRY_PRICE:
        filter_reasons.append(f"entry {entry_price:.0%} > {settings.MAX_ENTRY_PRICE:.0%}")
    filter_note = f" [{', '.join(filter_reasons)}]" if filter_reasons else ""

    reasoning = (
        f"[{filter_status}]{filter_note} "
        f"{underlying} ${micro.price:,.0f} | RSI:{micro.rsi:.0f} Mom1m:{micro.momentum_1m:+.3f}% "
        f"Mom5m:{micro.momentum_5m:+.3f}% VWAP:{micro.vwap_deviation:+.3f}% "
        f"SMA:{micro.sma_crossover:+.4f}% Vol:{micro.volatility:.4f}% | "
        f"Composite:{composite:+.3f} -> Model UP:{model_up_prob:.0%} vs Mkt:{market_up_prob:.0%} | "
        f"Raw edge:{raw_edge_val:+.1%} | Net edge:{edge:+.1%} (fees ${fee_cost:.2f}) -> {direction.upper()} @ {entry_price:.0%} | "
        f"Convergence:{max(up_votes, down_votes)}/5 | "
        f"Window ends: {market.window_end.strftime('%H:%M UTC')}"
    )

    return TradingSignal(
        market=market,
        model_probability=model_up_prob,
        market_probability=market_up_prob,
        edge=edge,
        raw_edge=raw_edge_val,
        net_edge=edge,
        fee_cost=fee_cost,
        direction=direction,
        confidence=confidence,
        kelly_fraction=suggested_size / bankroll if bankroll > 0 else 0,
        suggested_size=suggested_size,
        sources=[f"binance_microstructure_{micro.source}"],
        reasoning=reasoning,
        btc_price=micro.price,
        btc_change_1h=micro.momentum_5m * 12,
        btc_change_24h=micro.momentum_15m * 96,
    )


async def scan_for_signals() -> List[TradingSignal]:
    """Scan crypto 5-min markets and generate signals.

    TODO(slice 3e): iterate over settings.CRYPTO_TECH_UNDERLYINGS instead
    of hardcoding BTC. Kept BTC-only in 3a-2 so behavior is identical to
    pre-refactor; the parameterization groundwork is in place.
    """
    signals = []

    logger.info("=" * 50)
    logger.info("CRYPTO 5-MIN SCAN: Fetching markets from Polymarket...")

    # TODO(slice 3e): loop over settings.CRYPTO_TECH_UNDERLYINGS
    underlying = "BTC"
    try:
        markets = await fetch_active_crypto_markets(underlying)
    except Exception as e:
        logger.error(f"Failed to fetch {underlying} markets: {e}")
        markets = []

    logger.info(f"Found {len(markets)} active {underlying} 5-min markets")

    for market in markets:
        try:
            signal = await generate_crypto_tech_signal(market, underlying)
            if signal:
                signals.append(signal)
        except Exception as e:
            logger.debug(f"Signal generation failed for {market.slug}: {e}")

        await asyncio.sleep(0.1)

    signals.sort(key=lambda s: abs(s.edge), reverse=True)

    actionable = [s for s in signals if s.passes_threshold]
    logger.info(f"=" * 50)
    logger.info(f"SCAN COMPLETE: {len(signals)} signals, {len(actionable)} actionable")

    for signal in actionable[:5]:
        logger.info(f"  {signal.market.slug}")
        logger.info(f"    Edge: {signal.edge:+.1%} -> {signal.direction.upper()} @ ${signal.suggested_size:.2f}")

    _persist_signals(signals)

    return signals


def _persist_signals(signals: list):
    """Save signals with non-zero edge to DB, deduplicating on (market_ticker, timestamp)."""
    to_save = [s for s in signals if abs(s.edge) > 0]
    if not to_save:
        return

    db = SessionLocal()
    try:
        for signal in to_save:
            existing = db.query(Signal).filter(
                Signal.market_ticker == signal.market.market_id,
                Signal.timestamp >= signal.timestamp.replace(second=0, microsecond=0),
            ).first()
            if existing:
                continue

            db_signal = Signal(
                market_ticker=signal.market.market_id,
                platform="polymarket",
                timestamp=signal.timestamp,
                direction=signal.direction,
                model_probability=signal.model_probability,
                market_price=signal.market_probability,
                edge=signal.edge,
                confidence=signal.confidence,
                kelly_fraction=signal.kelly_fraction,
                suggested_size=signal.suggested_size,
                sources=signal.sources,
                reasoning=signal.reasoning,
                executed=False,
            )
            db.add(db_signal)

        db.commit()
    except Exception as e:
        logger.warning(f"Failed to persist signals: {e}")
        db.rollback()
    finally:
        db.close()


async def get_actionable_signals() -> List[TradingSignal]:
    """Get only signals that pass the edge threshold."""
    all_signals = await scan_for_signals()
    return [s for s in all_signals if s.passes_threshold]


if __name__ == "__main__":
    async def test():
        print("Scanning crypto 5-min markets for signals...")
        signals = await scan_for_signals()
        print(f"\nFound {len(signals)} total signals")

        actionable = [s for s in signals if s.passes_threshold]
        print(f"Actionable signals (>{settings.MIN_EDGE_THRESHOLD:.0%} edge): {len(actionable)}")

        for signal in actionable[:5]:
            print(f"\n{signal.market.slug}")
            print(f"  Price: ${signal.btc_price:,.0f} ({signal.btc_change_24h:+.2f}%)")
            print(f"  Model UP: {signal.model_probability:.1%} vs Market UP: {signal.market_probability:.1%}")
            print(f"  Edge: {signal.edge:+.1%} -> {signal.direction.upper()}")
            print(f"  Size: ${signal.suggested_size:.2f}")

    asyncio.run(test())
