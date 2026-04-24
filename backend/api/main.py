"""FastAPI backend for BTC 5-min trading bot dashboard."""
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import asyncio
import json
import os
import re

from backend.config import settings
from backend.models.database import (
    get_db, init_db, SessionLocal,
    Signal, Trade, BotState, AILog, ScanLog
)
from backend.core.signals import scan_for_signals, TradingSignal
from backend.data.crypto_markets import fetch_active_crypto_markets, CryptoUpDownMarket
from backend.data.crypto import fetch_crypto_price, compute_crypto_microstructure

from pydantic import BaseModel

app = FastAPI(
    title="BTC 5-Min Trading Bot",
    description="Polymarket BTC Up/Down 5-minute market trading bot",
    version="3.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass


ws_manager = ConnectionManager()


# Pydantic response models
class BtcPriceResponse(BaseModel):
    price: float
    change_24h: float
    change_7d: float
    market_cap: float
    volume_24h: float
    last_updated: datetime


class BtcWindowResponse(BaseModel):
    slug: str
    market_id: str
    up_price: float
    down_price: float
    window_start: datetime
    window_end: datetime
    volume: float
    is_active: bool
    is_upcoming: bool
    time_until_end: float
    spread: float


class MicrostructureResponse(BaseModel):
    rsi: float = 50.0
    momentum_1m: float = 0.0
    momentum_5m: float = 0.0
    momentum_15m: float = 0.0
    vwap_deviation: float = 0.0
    sma_crossover: float = 0.0
    volatility: float = 0.0
    price: float = 0.0
    source: str = "unknown"


class SignalResponse(BaseModel):
    market_ticker: str
    market_title: str
    platform: str
    direction: str
    model_probability: float
    market_probability: float
    edge: float
    confidence: float
    suggested_size: float
    reasoning: str
    timestamp: datetime
    category: str = "crypto"
    event_slug: Optional[str] = None
    underlying_price: float = 0.0
    underlying_change_24h: float = 0.0
    window_end: Optional[datetime] = None
    actionable: bool = False
    # Slice D2: multi-strategy attribution fields. Populated from either the
    # live TradingSignal.underlying (BTC brain path) or the Signal DB row
    # (MC brain path). Optional so older clients ignoring them keep working.
    underlying_asset: Optional[str] = None
    asset_class: Optional[str] = None
    contract_style: Optional[str] = None


class TradeResponse(BaseModel):
    id: int
    market_ticker: str
    platform: str
    event_slug: Optional[str] = None
    direction: str
    entry_price: float
    size: float
    timestamp: datetime
    settled: bool
    result: str
    pnl: Optional[float]
    # Slice D2: multi-strategy attribution fields read directly from the
    # Trade row (all three columns already exist; see models/database.py).
    market_type: Optional[str] = None
    underlying_asset: Optional[str] = None
    asset_class: Optional[str] = None
    contract_style: Optional[str] = None


class BotStats(BaseModel):
    bankroll: float
    total_trades: int
    winning_trades: int
    win_rate: float
    total_pnl: float
    is_running: bool
    last_run: Optional[datetime]


class CalibrationBucket(BaseModel):
    bucket: str
    predicted_avg: float
    actual_rate: float
    count: int


class CalibrationSummary(BaseModel):
    total_signals: int
    total_with_outcome: int
    accuracy: float
    avg_predicted_edge: float
    avg_actual_edge: float
    brier_score: float


class MultiMicrostructure(BaseModel):
    """Per-underlying microstructure + spot price snapshot (slice D2).

    Parallel to the singular `microstructure` field on DashboardData which
    remains BTC-only for backward compatibility. Missing underlyings (fetch
    failure) are simply absent from the dicts rather than set to null.
    """
    microstructures: Dict[str, MicrostructureResponse] = {}
    prices: Dict[str, float] = {}


class PerStrategyStats(BaseModel):
    """Trade aggregates split by the strategy that produced them (slice D2).

    strategy is one of:
      - "crypto_tech"   (rows where trades.market_type = 'btc')
      - "monte_carlo"   (rows where trades.market_type = 'monte_carlo')

    `allocated_bankroll` is the pool each strategy draws from:
    BotState.bankroll for crypto_tech, the MC pilot constant for monte_carlo.
    `realized_bankroll` = allocated_bankroll adjusted by the strategy's own
    cumulative settled PnL (so the MC pilot's realized bank reflects only
    MC trades, not the shared pool).
    """
    strategy: str
    total_trades: int
    settled_trades: int
    pending_trades: int
    wins: int
    losses: int
    win_rate: Optional[float] = None
    total_pnl: float
    pnl_24h: float
    allocated_bankroll: float
    realized_bankroll: float


class PerAssetStats(BaseModel):
    """Crypto-tech trade aggregates split by underlying asset (slice D2).

    Only covers market_type='btc' rows. Legacy pre-slice-3e BTC trades with
    underlying_asset=NULL are excluded by design — see scheduler.py comment
    on the same filter. The response always includes an entry for every
    configured CRYPTO_TECH_UNDERLYINGS symbol, even when zero trades exist.
    """
    underlying: str
    total_trades: int
    settled_trades: int
    pending_trades: int
    wins: int
    losses: int
    win_rate: Optional[float] = None
    total_pnl: float
    pnl_24h: float
    last_signal_time: Optional[datetime] = None
    last_trade_time: Optional[datetime] = None


class McOpenPosition(BaseModel):
    trade_id: int
    market_ticker: str
    underlying: str
    direction: str                        # "yes" or "no"
    entry_price: float
    size: float
    model_probability: float
    timestamp: datetime
    expected_settlement: Optional[datetime] = None


class McPortfolioStatus(BaseModel):
    """MC-brain portfolio snapshot. Pilot bankroll is derived, not stored."""
    pilot_bankroll_target: float
    realized_pilot_bankroll: float
    open_positions: List[McOpenPosition]
    total_allocated: float
    signals_last_24h: int
    actionable_signals_last_24h: int
    next_scheduled_scan: Optional[datetime] = None


class DashboardData(BaseModel):
    stats: BotStats
    btc_price: Optional[BtcPriceResponse]
    microstructure: Optional[MicrostructureResponse] = None
    windows: List[BtcWindowResponse]
    active_signals: List[SignalResponse]
    recent_trades: List[TradeResponse]
    equity_curve: List[dict]
    calibration: Optional[CalibrationSummary] = None
    # Slice D2: additive per-strategy / per-asset visibility.
    multi_microstructure: Optional[MultiMicrostructure] = None
    per_strategy_stats: List[PerStrategyStats] = []
    per_asset_stats: List[PerAssetStats] = []
    mc_portfolio: Optional[McPortfolioStatus] = None


class EventResponse(BaseModel):
    timestamp: str
    type: str
    message: str
    data: dict = {}


# Startup / Shutdown
@app.on_event("startup")
async def startup():
    print("=" * 60)
    print("BTC 5-MIN TRADING BOT v3.0")
    print("=" * 60)
    print("Initializing database...")

    init_db()

    db = SessionLocal()
    try:
        state = db.query(BotState).first()
        if not state:
            state = BotState(
                bankroll=settings.INITIAL_BANKROLL,
                total_trades=0,
                winning_trades=0,
                total_pnl=0.0,
                is_running=True
            )
            db.add(state)
            db.commit()
            print(f"Created new bot state with ${settings.INITIAL_BANKROLL:,.2f} bankroll")
        else:
            state.is_running = True
            db.commit()
            print(f"Loaded bot state: Bankroll ${state.bankroll:,.2f}, P&L ${state.total_pnl:+,.2f}, {state.total_trades} trades")
    finally:
        db.close()

    print("")
    print("Configuration:")
    print(f"  - Simulation mode: {settings.SIMULATION_MODE}")
    print(f"  - Min edge threshold: {settings.MIN_EDGE_THRESHOLD:.0%}")
    print(f"  - Kelly fraction: {settings.KELLY_FRACTION:.0%}")
    print(f"  - Scan interval: {settings.SCAN_INTERVAL_SECONDS}s")
    print(f"  - Settlement interval: {settings.SETTLEMENT_INTERVAL_SECONDS}s")
    print("")

    from backend.core.scheduler import start_scheduler, log_event
    start_scheduler()
    log_event("success", "BTC 5-min trading bot initialized")

    print("Bot is now running!")
    print(f"  - BTC scan: every {settings.SCAN_INTERVAL_SECONDS}s (edge >= {settings.MIN_EDGE_THRESHOLD:.0%})")
    print(f"  - Settlement check: every {settings.SETTLEMENT_INTERVAL_SECONDS}s")
    print("=" * 60)


@app.on_event("shutdown")
async def shutdown():
    from backend.core.scheduler import stop_scheduler
    stop_scheduler()


# Core endpoints
@app.get("/")
async def root():
    return {"status": "ok", "message": "BTC 5-Min Trading Bot API v3.0", "simulation_mode": settings.SIMULATION_MODE}


@app.get("/api/health")
async def health():
    return {"status": "healthy"}


@app.get("/api/stats", response_model=BotStats)
async def get_stats(db: Session = Depends(get_db)):
    state = db.query(BotState).first()
    if not state:
        raise HTTPException(status_code=404, detail="Bot state not initialized")

    win_rate = state.winning_trades / state.total_trades if state.total_trades > 0 else 0

    return BotStats(
        bankroll=state.bankroll,
        total_trades=state.total_trades,
        winning_trades=state.winning_trades,
        win_rate=win_rate,
        total_pnl=state.total_pnl,
        is_running=state.is_running,
        last_run=state.last_run
    )


# BTC-specific endpoints
@app.get("/api/btc/price", response_model=Optional[BtcPriceResponse])
async def get_btc_price():
    """Get current BTC price and momentum data."""
    try:
        btc = await fetch_crypto_price("BTC")
        if not btc:
            return None

        return BtcPriceResponse(
            price=btc.current_price,
            change_24h=btc.change_24h,
            change_7d=btc.change_7d,
            market_cap=btc.market_cap,
            volume_24h=btc.volume_24h,
            last_updated=btc.last_updated
        )
    except Exception:
        return None


@app.get("/api/btc/windows", response_model=List[BtcWindowResponse])
async def get_btc_windows():
    """Get upcoming BTC 5-min windows with prices."""
    try:
        markets = await fetch_active_crypto_markets("BTC")
        return [
            BtcWindowResponse(
                slug=m.slug,
                market_id=m.market_id,
                up_price=m.up_price,
                down_price=m.down_price,
                window_start=m.window_start,
                window_end=m.window_end,
                volume=m.volume,
                is_active=m.is_active,
                is_upcoming=m.is_upcoming,
                time_until_end=m.time_until_end,
                spread=m.spread,
            )
            for m in markets
        ]
    except Exception:
        return []


@app.get("/api/signals", response_model=List[SignalResponse])
async def get_signals():
    """Get current BTC trading signals."""
    try:
        signals = await scan_for_signals()
        return [_signal_to_response(s) for s in signals]
    except Exception:
        return []


@app.get("/api/signals/actionable", response_model=List[SignalResponse])
async def get_actionable_signals():
    """Get only signals that pass the edge threshold."""
    try:
        signals = await scan_for_signals()
        actionable = [s for s in signals if s.passes_threshold]
        return [_signal_to_response(s) for s in actionable]
    except Exception:
        return []


def _signal_to_response(s: TradingSignal, actionable: bool = False) -> SignalResponse:
    # Slice D2: carry multi-asset attribution. TradingSignal.underlying is
    # already populated per-asset (BTC/ETH/SOL/XRP). contract_style is None
    # for the technical brain — those are plain European 5-min up/down.
    underlying = (s.underlying or "").upper() or None
    return SignalResponse(
        market_ticker=s.market.market_id,
        market_title=f"{underlying or 'BTC'} 5m - {s.market.slug}",
        platform="polymarket",
        direction=s.direction,
        model_probability=s.model_probability,
        market_probability=s.market_probability,
        edge=s.edge,
        confidence=s.confidence,
        suggested_size=s.suggested_size,
        reasoning=s.reasoning,
        timestamp=s.timestamp,
        category="crypto",
        event_slug=s.market.slug,
        underlying_price=s.underlying_price,
        underlying_change_24h=s.underlying_change_24h,
        window_end=s.market.window_end,
        actionable=actionable,
        underlying_asset=underlying,
        asset_class="crypto" if underlying else None,
        contract_style=None,
    )


def _db_signal_to_response(sig: Signal) -> SignalResponse:
    """Serialize a persisted Signal row (used by /api/mc/signals)."""
    return SignalResponse(
        market_ticker=sig.market_ticker,
        market_title=sig.market_ticker,  # no slug on DB row; ticker is the label
        platform=sig.platform or "",
        direction=sig.direction or "",
        model_probability=sig.model_probability or 0.0,
        market_probability=sig.market_price or 0.0,
        edge=sig.edge or 0.0,
        confidence=sig.confidence or 0.0,
        suggested_size=sig.suggested_size or 0.0,
        reasoning=sig.reasoning or "",
        timestamp=sig.timestamp,
        category=sig.asset_class or "",
        event_slug=None,
        underlying_price=0.0,
        underlying_change_24h=0.0,
        window_end=None,
        actionable=bool(sig.edge is not None and abs(sig.edge) >= (
            settings.MC_MIN_EDGE_THRESHOLD
            if sig.market_type == "monte_carlo"
            else settings.MIN_EDGE_THRESHOLD
        )),
        underlying_asset=sig.underlying_asset,
        asset_class=sig.asset_class,
        contract_style=sig.contract_style,
    )


def _trade_to_response(t: Trade) -> TradeResponse:
    """Serialize a Trade row; centralized so D2 attribution fields flow
    through every endpoint that returns trades."""
    return TradeResponse(
        id=t.id,
        market_ticker=t.market_ticker,
        platform=t.platform,
        event_slug=t.event_slug,
        direction=t.direction,
        entry_price=t.entry_price,
        size=t.size,
        timestamp=t.timestamp,
        settled=t.settled,
        result=t.result,
        pnl=t.pnl,
        market_type=t.market_type,
        underlying_asset=t.underlying_asset,
        asset_class=t.asset_class,
        contract_style=t.contract_style,
    )


@app.get("/api/trades", response_model=List[TradeResponse])
async def get_trades(
    limit: int = 50,
    status: Optional[str] = None,
    db: Session = Depends(get_db)
):
    query = db.query(Trade)
    if status:
        query = query.filter(Trade.result == status)
    trades = query.order_by(Trade.timestamp.desc()).limit(limit).all()

    return [_trade_to_response(t) for t in trades]


@app.get("/api/equity-curve")
async def get_equity_curve(db: Session = Depends(get_db)):
    trades = db.query(Trade).filter(Trade.settled == True).order_by(Trade.timestamp).all()

    curve = []
    cumulative_pnl = 0
    bankroll = settings.INITIAL_BANKROLL

    for trade in trades:
        if trade.pnl is not None:
            cumulative_pnl += trade.pnl
            curve.append({
                "timestamp": trade.timestamp.isoformat(),
                "pnl": cumulative_pnl,
                "bankroll": bankroll + cumulative_pnl,
                "trade_id": trade.id
            })

    return curve


@app.post("/api/simulate-trade")
async def simulate_trade(signal_ticker: str, db: Session = Depends(get_db)):
    from backend.core.scheduler import log_event

    signals = await scan_for_signals()
    signal = next((s for s in signals if s.market.market_id == signal_ticker), None)

    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found")

    state = db.query(BotState).first()
    if not state:
        raise HTTPException(status_code=500, detail="Bot state not initialized")

    entry_price = signal.market.up_price if signal.direction == "up" else signal.market.down_price

    trade = Trade(
        market_ticker=signal.market.market_id,
        platform="polymarket",
        event_slug=signal.market.slug,
        direction=signal.direction,
        entry_price=entry_price,
        size=min(signal.suggested_size, state.bankroll * 0.05),
        model_probability=signal.model_probability,
        market_price_at_entry=signal.market_probability,
        edge_at_entry=signal.edge
    )

    db.add(trade)
    state.total_trades += 1
    db.commit()

    log_event("trade", f"Manual BTC trade: {signal.direction.upper()} {signal.market.slug}")
    return {"status": "ok", "trade_id": trade.id, "size": trade.size}


@app.post("/api/run-scan")
async def run_scan(db: Session = Depends(get_db)):
    from backend.core.scheduler import run_manual_scan, log_event

    state = db.query(BotState).first()
    if state:
        state.last_run = datetime.utcnow()
        db.commit()

    log_event("info", "Manual scan triggered")
    await run_manual_scan()

    signals = await scan_for_signals()
    actionable = [s for s in signals if s.passes_threshold]

    result = {
        "status": "ok",
        "total_signals": len(signals),
        "actionable_signals": len(actionable),
        "timestamp": datetime.utcnow().isoformat(),
    }

    # Also run MC scan if enabled (mirrors the BTC pattern).
    if settings.MC_ENABLED:
        try:
            from backend.core.mc_signals import scan_for_mc_signals
            mc_signals = scan_for_mc_signals()
            mc_act = [s for s in mc_signals if s.passes_threshold]
            result["mc_signals"] = len(mc_signals)
            result["mc_actionable"] = len(mc_act)
        except Exception as e:
            log_event("warning", f"[MC] manual scan failed: {e}")
            result["mc_signals"] = 0
            result["mc_actionable"] = 0

    return result


@app.post("/api/settle-trades")
async def settle_trades_endpoint(db: Session = Depends(get_db)):
    from backend.core.settlement import settle_pending_trades, update_bot_state_with_settlements
    from backend.core.scheduler import log_event

    log_event("info", "Manual settlement triggered")

    settled = await settle_pending_trades(db)
    await update_bot_state_with_settlements(db, settled)

    return {
        "status": "ok",
        "settled_count": len(settled),
        "trades": [{"id": t.id, "result": t.result, "pnl": t.pnl} for t in settled]
    }


def _configured_tech_underlyings() -> List[str]:
    """Current CRYPTO_TECH_UNDERLYINGS list, upper-cased + de-duped."""
    raw = settings.CRYPTO_TECH_UNDERLYINGS or ""
    out: List[str] = []
    for part in raw.split(","):
        sym = part.strip().upper()
        if sym and sym not in out:
            out.append(sym)
    return out


# Kalshi monthly-style tickers embed the settlement date as YYMMMDD (e.g.
# KXBTCMAXMON-26APR30-8000000 -> 2026-04-30). For other shapes we return
# None rather than guess. Parsing must never raise on malformed input.
_KALSHI_DATE_RE = re.compile(r"(\d{2})([A-Z]{3})(\d{2})")
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _parse_kalshi_expected_settlement(ticker: str) -> Optional[datetime]:
    """Best-effort parse of a settlement date from a Kalshi ticker.

    Returns the end of the matched day (23:59:59 UTC) so callers have a
    conservative settlement estimate. Returns None when no YYMMMDD token
    is present — we never fabricate a date.
    """
    if not ticker:
        return None
    m = _KALSHI_DATE_RE.search(ticker)
    if not m:
        return None
    yy, mmm, dd = m.group(1), m.group(2), m.group(3)
    month = _MONTHS.get(mmm)
    if month is None:
        return None
    try:
        year = 2000 + int(yy)
        day = int(dd)
        return datetime(year, month, day, 23, 59, 59)
    except (ValueError, TypeError):
        return None


def _build_per_strategy_stats(db: Session) -> List[PerStrategyStats]:
    """Aggregate the Trade table by market_type for slice D2 portfolio views.

    crypto_tech is labeled as 'crypto_tech' in the response even though the
    DB column uses the legacy 'btc' value — the rename would require a
    migration, and this slice is additive-only.

    total_pnl reconciliation (slice D4.5):
      BotState.total_pnl is the single-pool running counter the settlement
      code and bankroll sizing use. In a clean bot SUM(Trade.pnl WHERE
      settled=True) would equal BotState.total_pnl, but this project's DB
      has a drift from a dev-phase bot_state zeroing that preserved trade
      rows. To keep the dashboard's per-strategy breakdown internally
      consistent (crypto_tech + monte_carlo sum == BotState.total_pnl)
      we:
        * trust SUM(Trade.pnl) for MC (its trades are all post-pilot, no
          drift possible)
        * derive crypto_tech.total_pnl as the residual
          (BotState.total_pnl − mc.total_pnl)
      pnl_24h stays trade-sourced for both strategies; the 24h window is
      tight enough that drift is unlikely to span it.
    """
    cutoff_24h = datetime.utcnow() - timedelta(hours=24)
    mc_total_pnl = float(
        db.query(func.coalesce(func.sum(Trade.pnl), 0.0))
        .filter(Trade.market_type == "monte_carlo", Trade.settled == True)  # noqa: E712
        .scalar() or 0.0
    )
    state = db.query(BotState).first()
    tech_allocated = float(state.bankroll) if state else float(settings.INITIAL_BANKROLL)
    bot_state_total_pnl = float(state.total_pnl) if state else 0.0

    out: List[PerStrategyStats] = []
    for strategy_label, market_type_value, allocated in (
        ("crypto_tech", "btc", tech_allocated),
        ("monte_carlo", "monte_carlo", float(settings.MC_PILOT_BANKROLL_USD)),
    ):
        base = db.query(Trade).filter(Trade.market_type == market_type_value)
        total = base.count()
        settled = base.filter(Trade.settled == True).count()  # noqa: E712
        pending = base.filter(Trade.settled == False).count()  # noqa: E712
        wins = base.filter(Trade.result == "win").count()
        losses = base.filter(Trade.result == "loss").count()
        if strategy_label == "monte_carlo":
            total_pnl = mc_total_pnl
        else:
            # Residual: keeps crypto_tech + monte_carlo == BotState.total_pnl
            # so the dashboard PORTFOLIO card matches the number the bot's
            # sizing logic reads. See module docstring above.
            total_pnl = bot_state_total_pnl - mc_total_pnl
        pnl_24h = float(
            db.query(func.coalesce(func.sum(Trade.pnl), 0.0))
            .filter(
                Trade.market_type == market_type_value,
                Trade.settled == True,  # noqa: E712
                Trade.settlement_time.isnot(None),
                Trade.settlement_time >= cutoff_24h,
            ).scalar() or 0.0
        )
        # For crypto_tech the shared bankroll already reflects realized PnL;
        # the MC pilot is notional so realized = target + MC PnL.
        if strategy_label == "crypto_tech":
            realized = allocated
        else:
            realized = allocated + mc_total_pnl
        win_rate = (wins / settled) if settled > 0 else None
        out.append(PerStrategyStats(
            strategy=strategy_label,
            total_trades=total,
            settled_trades=settled,
            pending_trades=pending,
            wins=wins,
            losses=losses,
            win_rate=win_rate,
            total_pnl=total_pnl,
            pnl_24h=pnl_24h,
            allocated_bankroll=allocated,
            realized_bankroll=realized,
        ))
    return out


def _build_per_asset_stats(db: Session) -> List[PerAssetStats]:
    """Per-underlying view of the crypto_tech brain.

    Filters on market_type='btc' AND underlying_asset IS NOT NULL; the
    pre-slice-3e BTC history with NULL underlying is excluded so counts
    don't double-attribute (matches the scheduler's allocation-query
    convention).
    """
    cutoff_24h = datetime.utcnow() - timedelta(hours=24)
    out: List[PerAssetStats] = []
    for underlying in _configured_tech_underlyings():
        base = db.query(Trade).filter(
            Trade.market_type == "btc",
            Trade.underlying_asset == underlying,
        )
        total = base.count()
        settled = base.filter(Trade.settled == True).count()  # noqa: E712
        pending = base.filter(Trade.settled == False).count()  # noqa: E712
        wins = base.filter(Trade.result == "win").count()
        losses = base.filter(Trade.result == "loss").count()
        total_pnl = float(
            db.query(func.coalesce(func.sum(Trade.pnl), 0.0))
            .filter(
                Trade.market_type == "btc",
                Trade.underlying_asset == underlying,
                Trade.settled == True,  # noqa: E712
            ).scalar() or 0.0
        )
        pnl_24h = float(
            db.query(func.coalesce(func.sum(Trade.pnl), 0.0))
            .filter(
                Trade.market_type == "btc",
                Trade.underlying_asset == underlying,
                Trade.settled == True,  # noqa: E712
                Trade.settlement_time.isnot(None),
                Trade.settlement_time >= cutoff_24h,
            ).scalar() or 0.0
        )
        last_trade = (
            db.query(Trade.timestamp)
            .filter(Trade.market_type == "btc", Trade.underlying_asset == underlying)
            .order_by(Trade.timestamp.desc())
            .first()
        )
        last_signal = (
            db.query(Signal.timestamp)
            .filter(Signal.market_type == "btc", Signal.underlying_asset == underlying)
            .order_by(Signal.timestamp.desc())
            .first()
        )
        win_rate = (wins / settled) if settled > 0 else None
        out.append(PerAssetStats(
            underlying=underlying,
            total_trades=total,
            settled_trades=settled,
            pending_trades=pending,
            wins=wins,
            losses=losses,
            win_rate=win_rate,
            total_pnl=total_pnl,
            pnl_24h=pnl_24h,
            last_signal_time=last_signal[0] if last_signal else None,
            last_trade_time=last_trade[0] if last_trade else None,
        ))
    return out


def _build_mc_portfolio_status(db: Session) -> McPortfolioStatus:
    """MC brain state: pilot bankroll (derived), open positions, 24h signal counts."""
    mc_total_pnl = float(
        db.query(func.coalesce(func.sum(Trade.pnl), 0.0))
        .filter(Trade.market_type == "monte_carlo", Trade.settled == True)  # noqa: E712
        .scalar() or 0.0
    )
    open_rows = (
        db.query(Trade)
        .filter(Trade.market_type == "monte_carlo", Trade.settled == False)  # noqa: E712
        .order_by(Trade.timestamp.desc())
        .all()
    )
    open_positions = [
        McOpenPosition(
            trade_id=t.id,
            market_ticker=t.market_ticker,
            underlying=t.underlying_asset or "",
            direction=t.direction or "",
            entry_price=t.entry_price or 0.0,
            size=t.size or 0.0,
            model_probability=t.model_probability or 0.0,
            timestamp=t.timestamp,
            expected_settlement=_parse_kalshi_expected_settlement(t.market_ticker),
        )
        for t in open_rows
    ]
    total_allocated = float(sum(p.size for p in open_positions))

    cutoff_24h = datetime.utcnow() - timedelta(hours=24)
    signals_last_24h = (
        db.query(Signal)
        .filter(Signal.market_type == "monte_carlo", Signal.timestamp >= cutoff_24h)
        .count()
    )
    actionable_last_24h = (
        db.query(Signal)
        .filter(
            Signal.market_type == "monte_carlo",
            Signal.timestamp >= cutoff_24h,
            func.abs(Signal.edge) >= settings.MC_MIN_EDGE_THRESHOLD,
        )
        .count()
    )

    # Scheduler's next_run_time is only available if the job is registered.
    next_scan: Optional[datetime] = None
    try:
        from backend.core.scheduler import scheduler as _sched
        if _sched is not None:
            job = _sched.get_job("mc_scan")
            if job is not None and job.next_run_time is not None:
                nrt = job.next_run_time
                next_scan = nrt.replace(tzinfo=None) if nrt.tzinfo else nrt
    except Exception:
        next_scan = None

    return McPortfolioStatus(
        pilot_bankroll_target=float(settings.MC_PILOT_BANKROLL_USD),
        realized_pilot_bankroll=float(settings.MC_PILOT_BANKROLL_USD) + mc_total_pnl,
        open_positions=open_positions,
        total_allocated=total_allocated,
        signals_last_24h=signals_last_24h,
        actionable_signals_last_24h=actionable_last_24h,
        next_scheduled_scan=next_scan,
    )


async def _build_multi_microstructure() -> MultiMicrostructure:
    """Fetch microstructure + spot price for every configured tech underlying.

    Runs all underlyings concurrently via asyncio.gather. Per-underlying
    failures are swallowed so one dead exchange adapter doesn't null the
    whole response — missing symbols are simply absent from the dicts.
    """
    underlyings = _configured_tech_underlyings()
    if not underlyings:
        return MultiMicrostructure()

    micro_tasks = [compute_crypto_microstructure(u) for u in underlyings]
    price_tasks = [fetch_crypto_price(u) for u in underlyings]
    micros, prices = await asyncio.gather(
        asyncio.gather(*micro_tasks, return_exceptions=True),
        asyncio.gather(*price_tasks, return_exceptions=True),
    )

    micro_map: Dict[str, MicrostructureResponse] = {}
    price_map: Dict[str, float] = {}
    for u, micro in zip(underlyings, micros):
        if isinstance(micro, Exception) or micro is None:
            continue
        micro_map[u] = MicrostructureResponse(
            rsi=micro.rsi,
            momentum_1m=micro.momentum_1m,
            momentum_5m=micro.momentum_5m,
            momentum_15m=micro.momentum_15m,
            vwap_deviation=micro.vwap_deviation,
            sma_crossover=micro.sma_crossover,
            volatility=micro.volatility,
            price=micro.price,
            source=micro.source,
        )
        # Micro already has a current_price; use it as the default spot so a
        # CoinGecko outage on one symbol still yields a usable price.
        price_map[u] = float(micro.price)
    for u, price in zip(underlyings, prices):
        if isinstance(price, Exception) or price is None:
            continue
        price_map[u] = float(price.current_price)

    return MultiMicrostructure(microstructures=micro_map, prices=price_map)


def _compute_calibration_summary(db: Session) -> Optional[CalibrationSummary]:
    """Compute calibration summary from settled signals."""
    total_signals = db.query(Signal).count()
    settled_signals = db.query(Signal).filter(Signal.outcome_correct.isnot(None)).all()

    if not settled_signals:
        if total_signals == 0:
            return None
        return CalibrationSummary(
            total_signals=total_signals,
            total_with_outcome=0,
            accuracy=0.0,
            avg_predicted_edge=0.0,
            avg_actual_edge=0.0,
            brier_score=0.0,
        )

    total_with_outcome = len(settled_signals)
    correct = sum(1 for s in settled_signals if s.outcome_correct)
    accuracy = correct / total_with_outcome if total_with_outcome > 0 else 0.0

    avg_predicted_edge = sum(abs(s.edge) for s in settled_signals) / total_with_outcome
    # Actual edge: for correct predictions, edge was real; for incorrect, edge was negative
    avg_actual_edge = sum(
        abs(s.edge) if s.outcome_correct else -abs(s.edge)
        for s in settled_signals
    ) / total_with_outcome

    # Brier score: mean squared error of probability forecasts
    # For each signal: (predicted_prob - actual_outcome)^2
    brier_sum = 0.0
    for s in settled_signals:
        # Model probability is for UP; actual is 1.0 if UP won, 0.0 if DOWN won
        actual = s.settlement_value if s.settlement_value is not None else 0.5
        brier_sum += (s.model_probability - actual) ** 2
    brier_score = brier_sum / total_with_outcome

    return CalibrationSummary(
        total_signals=total_signals,
        total_with_outcome=total_with_outcome,
        accuracy=accuracy,
        avg_predicted_edge=avg_predicted_edge,
        avg_actual_edge=avg_actual_edge,
        brier_score=brier_score,
    )


@app.get("/api/calibration")
async def get_calibration(db: Session = Depends(get_db)):
    """Return calibration data: predicted probability vs actual win rate."""
    signals = db.query(Signal).filter(Signal.outcome_correct.isnot(None)).all()

    if not signals:
        return {"buckets": [], "summary": None}

    # Bucket signals by model_probability into 5% bins
    from collections import defaultdict
    buckets_data = defaultdict(lambda: {"predicted_sum": 0.0, "correct": 0, "total": 0})

    for s in signals:
        # Bin by 5% increments
        bin_start = int(s.model_probability * 100 // 5) * 5
        bin_end = bin_start + 5
        bucket_key = f"{bin_start}-{bin_end}%"

        buckets_data[bucket_key]["predicted_sum"] += s.model_probability
        buckets_data[bucket_key]["total"] += 1
        if s.outcome_correct:
            buckets_data[bucket_key]["correct"] += 1

    buckets = []
    for bucket_key in sorted(buckets_data.keys()):
        d = buckets_data[bucket_key]
        buckets.append(CalibrationBucket(
            bucket=bucket_key,
            predicted_avg=d["predicted_sum"] / d["total"],
            actual_rate=d["correct"] / d["total"],
            count=d["total"],
        ))

    summary = _compute_calibration_summary(db)

    return {"buckets": buckets, "summary": summary}


# Kalshi endpoints
@app.get("/api/kalshi/status")
async def get_kalshi_status():
    """Test Kalshi API authentication and return connection status."""
    from backend.data.kalshi_client import KalshiClient, kalshi_credentials_present

    if not kalshi_credentials_present():
        return {
            "connected": False,
            "error": "Kalshi credentials not configured (KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH)",
        }

    try:
        client = KalshiClient()
        balance_data = await client.get_balance()
        return {
            "connected": True,
            "balance": balance_data,
        }
    except Exception as e:
        return {
            "connected": False,
            "error": str(e),
        }


@app.get("/api/events", response_model=List[EventResponse])
async def get_events(limit: int = 50):
    from backend.core.scheduler import get_recent_events
    events = get_recent_events(limit)
    return [
        EventResponse(
            timestamp=e["timestamp"],
            type=e["type"],
            message=e["message"],
            data=e.get("data", {})
        )
        for e in events
    ]


# Bot control
@app.post("/api/bot/start")
async def start_bot(db: Session = Depends(get_db)):
    from backend.core.scheduler import start_scheduler, log_event, is_scheduler_running

    state = db.query(BotState).first()
    if state:
        state.is_running = True
        db.commit()

    if not is_scheduler_running():
        start_scheduler()

    log_event("success", "Trading bot started")
    return {"status": "started", "is_running": True}


@app.post("/api/bot/stop")
async def stop_bot(db: Session = Depends(get_db)):
    from backend.core.scheduler import log_event

    state = db.query(BotState).first()
    if state:
        state.is_running = False
        db.commit()

    log_event("info", "Trading bot paused")
    return {"status": "stopped", "is_running": False}


@app.post("/api/bot/reset")
async def reset_bot(db: Session = Depends(get_db)):
    from backend.core.scheduler import log_event

    try:
        trades_deleted = db.query(Trade).delete()
        state = db.query(BotState).first()
        if state:
            state.bankroll = settings.INITIAL_BANKROLL
            state.total_trades = 0
            state.winning_trades = 0
            state.total_pnl = 0.0
            state.is_running = True

        ai_logs_deleted = db.query(AILog).delete()
        db.commit()

        log_event("success", f"Bot reset: {trades_deleted} trades deleted. Fresh start with ${settings.INITIAL_BANKROLL:,.2f}")

        return {
            "status": "reset",
            "trades_deleted": trades_deleted,
            "ai_logs_deleted": ai_logs_deleted,
            "new_bankroll": settings.INITIAL_BANKROLL
        }

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Reset failed: {e}")


@app.get("/api/dashboard", response_model=DashboardData)
async def get_dashboard(db: Session = Depends(get_db)):
    """Get all dashboard data in one call."""
    stats = await get_stats(db)

    # Fetch BTC price from microstructure first, fallback to CoinGecko
    btc_price_data = None
    micro_data = None
    try:
        micro = await compute_crypto_microstructure("BTC")
        if micro:
            micro_data = MicrostructureResponse(
                rsi=micro.rsi,
                momentum_1m=micro.momentum_1m,
                momentum_5m=micro.momentum_5m,
                momentum_15m=micro.momentum_15m,
                vwap_deviation=micro.vwap_deviation,
                sma_crossover=micro.sma_crossover,
                volatility=micro.volatility,
                price=micro.price,
                source=micro.source,
            )
            btc_price_data = BtcPriceResponse(
                price=micro.price,
                change_24h=micro.momentum_15m * 96,  # rough extrapolation
                change_7d=0,
                market_cap=0,
                volume_24h=0,
                last_updated=datetime.utcnow(),
            )
    except Exception:
        pass
    if not btc_price_data:
        try:
            btc = await fetch_crypto_price("BTC")
            if btc:
                btc_price_data = BtcPriceResponse(
                    price=btc.current_price,
                    change_24h=btc.change_24h,
                    change_7d=btc.change_7d,
                    market_cap=btc.market_cap,
                    volume_24h=btc.volume_24h,
                    last_updated=btc.last_updated
                )
        except Exception:
            pass

    # Fetch windows
    windows = []
    try:
        markets = await fetch_active_crypto_markets("BTC")
        windows = [
            BtcWindowResponse(
                slug=m.slug,
                market_id=m.market_id,
                up_price=m.up_price,
                down_price=m.down_price,
                window_start=m.window_start,
                window_end=m.window_end,
                volume=m.volume,
                is_active=m.is_active,
                is_upcoming=m.is_upcoming,
                time_until_end=m.time_until_end,
                spread=m.spread,
            )
            for m in markets
        ]
    except Exception:
        pass

    # Signals — return ALL signals, mark which are actionable
    signals = []
    try:
        raw_signals = await scan_for_signals()
        signals = [_signal_to_response(s, actionable=s.passes_threshold) for s in raw_signals]
    except Exception:
        pass

    # Recent trades
    trades = db.query(Trade).order_by(Trade.timestamp.desc()).limit(50).all()
    recent_trades = [_trade_to_response(t) for t in trades]

    # Equity curve
    equity_trades = db.query(Trade).filter(Trade.settled == True).order_by(Trade.timestamp).all()
    equity_curve = []
    cumulative_pnl = 0
    for trade in equity_trades:
        if trade.pnl is not None:
            cumulative_pnl += trade.pnl
            equity_curve.append({
                "timestamp": trade.timestamp.isoformat(),
                "pnl": cumulative_pnl,
                "bankroll": settings.INITIAL_BANKROLL + cumulative_pnl
            })

    # Calibration summary
    calibration = _compute_calibration_summary(db)

    # Slice D2: multi-asset + multi-strategy visibility. All four blocks
    # degrade to sensible empty values on failure so the dashboard never
    # breaks on a new field — contract preserved with older clients.
    try:
        multi_micro = await _build_multi_microstructure()
    except Exception:
        multi_micro = MultiMicrostructure()
    try:
        per_strategy = _build_per_strategy_stats(db)
    except Exception:
        per_strategy = []
    try:
        per_asset = _build_per_asset_stats(db)
    except Exception:
        per_asset = []
    try:
        mc_portfolio = _build_mc_portfolio_status(db)
    except Exception:
        mc_portfolio = None

    return DashboardData(
        stats=stats,
        btc_price=btc_price_data,
        microstructure=micro_data,
        windows=windows,
        active_signals=signals,
        recent_trades=recent_trades,
        equity_curve=equity_curve,
        calibration=calibration,
        multi_microstructure=multi_micro,
        per_strategy_stats=per_strategy,
        per_asset_stats=per_asset,
        mc_portfolio=mc_portfolio,
    )


# Slice D2: MC-specific read endpoints. All are pure DB queries with no
# network side effects — safe to poll independently of /api/dashboard.
@app.get("/api/mc/portfolio", response_model=McPortfolioStatus)
async def get_mc_portfolio(db: Session = Depends(get_db)):
    return _build_mc_portfolio_status(db)


@app.get("/api/mc/signals", response_model=List[SignalResponse])
async def get_mc_signals(limit: int = 50, db: Session = Depends(get_db)):
    rows = (
        db.query(Signal)
        .filter(Signal.market_type == "monte_carlo")
        .order_by(Signal.timestamp.desc())
        .limit(limit)
        .all()
    )
    return [_db_signal_to_response(s) for s in rows]


@app.get("/api/mc/trades", response_model=List[TradeResponse])
async def get_mc_trades(
    status: str = "all",  # "open" | "settled" | "all"
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(Trade).filter(Trade.market_type == "monte_carlo")
    if status == "open":
        query = query.filter(Trade.settled == False)  # noqa: E712
    elif status == "settled":
        query = query.filter(Trade.settled == True)  # noqa: E712
    rows = query.order_by(Trade.timestamp.desc()).limit(limit).all()
    return [_trade_to_response(t) for t in rows]


# Slice D2: per-underlying views for the crypto_tech brain.
@app.get("/api/assets/stats", response_model=List[PerAssetStats])
async def get_per_asset_stats(db: Session = Depends(get_db)):
    return _build_per_asset_stats(db)


@app.get("/api/microstructure", response_model=Optional[MicrostructureResponse])
async def get_microstructure_for_underlying(underlying: str = "BTC"):
    """Microstructure for a single underlying. Pass ?underlying=BTC/ETH/SOL/XRP."""
    sym = (underlying or "").upper().strip()
    if not sym:
        raise HTTPException(status_code=400, detail="underlying is required")
    try:
        micro = await compute_crypto_microstructure(sym)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        return None
    if micro is None:
        return None
    return MicrostructureResponse(
        rsi=micro.rsi,
        momentum_1m=micro.momentum_1m,
        momentum_5m=micro.momentum_5m,
        momentum_15m=micro.momentum_15m,
        vwap_deviation=micro.vwap_deviation,
        sma_crossover=micro.sma_crossover,
        volatility=micro.volatility,
        price=micro.price,
        source=micro.source,
    )


@app.websocket("/ws/events")
async def websocket_events(websocket: WebSocket):
    await ws_manager.connect(websocket)

    try:
        await websocket.send_json({
            "timestamp": datetime.utcnow().isoformat(),
            "type": "success",
            "message": "Connected to BTC trading bot"
        })

        from backend.core.scheduler import get_recent_events
        for event in get_recent_events(20):
            await websocket.send_json(event)

        last_event_count = len(get_recent_events(200))
        while True:
            await asyncio.sleep(2)

            current_events = get_recent_events(200)
            if len(current_events) > last_event_count:
                new_events = current_events[last_event_count - len(current_events):]
                for event in new_events:
                    await websocket.send_json(event)
                last_event_count = len(current_events)

            await websocket.send_json({
                "type": "heartbeat",
                "timestamp": datetime.utcnow().isoformat()
            })

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception:
        ws_manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
