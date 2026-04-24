"""Signal generator for weather temperature markets using ensemble forecasts."""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from backend.config import settings
from backend.core.signals import calculate_edge, calculate_kelly_size
from backend.core.fees import net_edge
from backend.core.calibration import get_calibration_multiplier
from backend.data.weather import fetch_ensemble_forecast, EnsembleForecast, CITY_CONFIG
from backend.data.weather_markets import WeatherMarket, fetch_polymarket_weather_markets
from backend.models.database import SessionLocal, Signal

logger = logging.getLogger("trading_bot")


@dataclass
class WeatherTradingSignal:
    """A trading signal for a weather temperature market."""
    market: WeatherMarket

    # Core signal data
    model_probability: float = 0.5
    market_probability: float = 0.5
    edge: float = 0.0
    raw_edge: float = 0.0
    net_edge: float = 0.0
    fee_cost: float = 0.0
    direction: str = "yes"

    # Confidence and sizing
    confidence: float = 0.5
    kelly_fraction: float = 0.0
    suggested_size: float = 0.0

    # Metadata
    sources: List[str] = field(default_factory=list)
    reasoning: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)

    # Forecast context
    ensemble_mean: float = 0.0
    ensemble_std: float = 0.0
    ensemble_members: int = 0
    ensemble_agreement: float = 0.0

    @property
    def passes_threshold(self) -> bool:
        """Check if signal passes minimum edge threshold (after fees)."""
        return abs(self.net_edge) >= settings.WEATHER_MIN_EDGE_THRESHOLD


async def generate_weather_signal(market: WeatherMarket) -> Optional[WeatherTradingSignal]:
    """
    Generate a trading signal for a weather temperature market.

    Gates:
    - Fee-adjusted edge >= WEATHER_MIN_EDGE_THRESHOLD (Change 1b)
    - Entry price <= WEATHER_MAX_ENTRY_PRICE
    - Ensemble agreement >= WEATHER_MIN_ENSEMBLE_AGREEMENT (Change 2b)
      Kills trades where the ensemble is split near 50/50.
    """
    forecast = await fetch_ensemble_forecast(market.city_key, market.target_date)
    if not forecast or not forecast.member_highs:
        return None

    if market.metric == "high":
        if market.direction == "above":
            model_yes_prob = forecast.probability_high_above(market.threshold_f)
        else:
            model_yes_prob = forecast.probability_high_below(market.threshold_f)
    else:
        if market.direction == "above":
            model_yes_prob = forecast.probability_low_above(market.threshold_f)
        else:
            model_yes_prob = forecast.probability_low_below(market.threshold_f)

    model_yes_prob = max(0.05, min(0.95, model_yes_prob))

    market_yes_prob = market.yes_price

    raw_edge_val, direction_raw = calculate_edge(model_yes_prob, market_yes_prob)
    direction = "yes" if direction_raw == "up" else "no"

    entry_price = market.yes_price if direction == "yes" else market.no_price

    venue = "kalshi" if market.platform == "kalshi" else "polymarket"
    trial_size = min(settings.WEATHER_MAX_TRADE_SIZE, settings.INITIAL_BANKROLL * 0.05)
    _, net_edge_val, fee_breakdown = net_edge(
        model_prob=model_yes_prob,
        market_prob=market_yes_prob,
        entry_price=entry_price if entry_price > 0 else 0.5,
        size_usd=trial_size,
        venue=venue,
        market_type="weather",
        btc_slippage_bps=settings.BTC_SLIPPAGE_BPS,
        weather_slippage_bps=settings.WEATHER_SLIPPAGE_BPS,
    )
    fee_cost = fee_breakdown.total

    if market.metric == "high":
        members = forecast.member_highs
    else:
        members = forecast.member_lows

    above_count = sum(1 for m in members if m > market.threshold_f)
    agreement_frac = max(above_count, len(members) - above_count) / len(members)
    confidence = min(0.9, agreement_frac)

    # --- Change 2b: Ensemble extremity gate ---
    ensemble_is_extreme = agreement_frac >= settings.WEATHER_MIN_ENSEMBLE_AGREEMENT

    passes_price_filter = entry_price <= settings.WEATHER_MAX_ENTRY_PRICE
    passes_filters = passes_price_filter and ensemble_is_extreme

    edge = abs(net_edge_val) if raw_edge_val >= 0 else -abs(net_edge_val)

    if not passes_filters:
        edge = 0.0
        net_edge_val = 0.0
        raw_edge_val = 0.0

    bankroll = settings.INITIAL_BANKROLL
    suggested_size = calculate_kelly_size(
        edge=abs(edge),
        probability=model_yes_prob,
        market_price=market_yes_prob,
        direction=direction_raw,
        bankroll=bankroll,
    )
    # Calibration-adjusted Kelly (Change 3): shrink sizing if weather model has been overconfident
    calib_mult = get_calibration_multiplier("weather")
    suggested_size *= calib_mult
    suggested_size = min(suggested_size, settings.WEATHER_MAX_TRADE_SIZE)

    mean_val = forecast.mean_high if market.metric == "high" else forecast.mean_low
    std_val = forecast.std_high if market.metric == "high" else forecast.std_low

    filter_status = "ACTIONABLE" if passes_filters else "FILTERED"
    filter_notes = []
    if not passes_price_filter:
        filter_notes.append(f"entry {entry_price:.0%} > {settings.WEATHER_MAX_ENTRY_PRICE:.0%}")
    if not ensemble_is_extreme:
        filter_notes.append(f"agreement {agreement_frac:.0%} < {settings.WEATHER_MIN_ENSEMBLE_AGREEMENT:.0%}")
    filter_note = f" [{', '.join(filter_notes)}]" if filter_notes else ""

    reasoning = (
        f"[{filter_status}]{filter_note} "
        f"{market.city_name} {market.metric} {market.direction} {market.threshold_f:.0f}F on {market.target_date} | "
        f"Ensemble: {mean_val:.1f}F +/- {std_val:.1f}F ({forecast.num_members} members) | "
        f"Model YES: {model_yes_prob:.0%} vs Market: {market_yes_prob:.0%} | "
        f"Raw edge: {raw_edge_val:+.1%} | Net edge: {edge:+.1%} (fees ${fee_cost:.2f}) -> {direction.upper()} @ {entry_price:.0%} | "
        f"Agreement: {agreement_frac:.0%}"
    )

    return WeatherTradingSignal(
        market=market,
        model_probability=model_yes_prob,
        market_probability=market_yes_prob,
        edge=edge,
        raw_edge=raw_edge_val,
        net_edge=edge,
        fee_cost=fee_cost,
        direction=direction,
        confidence=confidence,
        kelly_fraction=suggested_size / bankroll if bankroll > 0 else 0,
        suggested_size=suggested_size,
        sources=[f"open_meteo_ensemble_{forecast.num_members}m"],
        reasoning=reasoning,
        ensemble_mean=mean_val,
        ensemble_std=std_val,
        ensemble_members=forecast.num_members,
        ensemble_agreement=agreement_frac,
    )


async def scan_for_weather_signals() -> List[WeatherTradingSignal]:
    """Scan weather markets and generate ensemble-based signals."""
    signals = []

    city_keys = [c.strip() for c in settings.WEATHER_CITIES.split(",") if c.strip()]

    logger.info("=" * 50)
    logger.info("WEATHER SCAN: Fetching temperature markets...")

    markets = []

    try:
        poly_markets = await fetch_polymarket_weather_markets(city_keys)
        markets.extend(poly_markets)
        logger.info(f"Polymarket: {len(poly_markets)} weather markets")
    except Exception as e:
        logger.error(f"Failed to fetch Polymarket weather markets: {e}")

    if settings.KALSHI_ENABLED:
        try:
            from backend.data.kalshi_client import kalshi_credentials_present
            from backend.data.kalshi_markets import fetch_kalshi_weather_markets
            if kalshi_credentials_present():
                kalshi_markets = await fetch_kalshi_weather_markets(city_keys)
                markets.extend(kalshi_markets)
                logger.info(f"Kalshi: {len(kalshi_markets)} weather markets")
        except Exception as e:
            logger.error(f"Failed to fetch Kalshi weather markets: {e}")

    logger.info(f"Found {len(markets)} total weather temperature markets")

    for market in markets:
        try:
            signal = await generate_weather_signal(market)
            if signal:
                signals.append(signal)
        except Exception as e:
            logger.debug(f"Weather signal generation failed for {market.title}: {e}")

    signals.sort(key=lambda s: abs(s.edge), reverse=True)

    actionable = [s for s in signals if s.passes_threshold]
    logger.info(f"WEATHER SCAN COMPLETE: {len(signals)} signals, {len(actionable)} actionable")

    for signal in actionable[:5]:
        logger.info(f"  {signal.market.city_name}: {signal.market.metric} {signal.market.direction} "
                     f"{signal.market.threshold_f:.0f}F | Edge: {signal.edge:+.1%} | Agreement: {signal.ensemble_agreement:.0%}")

    _persist_weather_signals(signals)

    return signals


def _persist_weather_signals(signals: list):
    """Save weather signals to DB for calibration tracking."""
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
                platform=signal.market.platform,
                market_type="weather",
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
        logger.warning(f"Failed to persist weather signals: {e}")
        db.rollback()
    finally:
        db.close()
