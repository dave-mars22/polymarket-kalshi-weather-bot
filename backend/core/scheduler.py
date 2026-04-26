"""Background scheduler for BTC 5-min autonomous trading."""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import func
import logging

from backend.config import settings
from backend.models.database import BotState, SessionLocal, Signal, Trade
from backend.core.signals import scan_for_signals, update_cached_scan

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("trading_bot")

# Global scheduler instance
scheduler: Optional[AsyncIOScheduler] = None

# Event log for terminal display (in-memory, last 200 events)
event_log: List[dict] = []
MAX_LOG_SIZE = 200


def log_event(event_type: str, message: str, data: dict = None):
    """Log an event for terminal display.

    Slice D8 fix: timestamps use the same canonical UTC-with-Z format as
    the REST API (per slice D6.5). The WebSocket handler in backend/api/main.py
    sends these dicts directly via send_json, bypassing Pydantic — so
    without fixing here, WS-delivered events would have naive ISO strings
    and any future "time ago" display on them would drift by the viewer's
    tz offset. The REST /api/events endpoint is unaffected (it re-parses
    via EventResponse.timestamp: UTCDatetime).
    """
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "type": event_type,
        "message": message,
        "data": data or {}
    }
    event_log.append(event)

    while len(event_log) > MAX_LOG_SIZE:
        event_log.pop(0)

    log_func = {
        "error": logger.error,
        "warning": logger.warning,
        "success": logger.info,
        "info": logger.info,
        "data": logger.debug,
        "trade": logger.info
    }.get(event_type, logger.info)

    log_func(f"[{event_type.upper()}] {message}")


def get_recent_events(limit: int = 50) -> List[dict]:
    """Get recent events for terminal display."""
    return event_log[-limit:]


async def scan_and_trade_job():
    """
    Background job: Scan BTC 5-min markets, generate signals, execute trades.
    Runs every minute.
    """
    log_event("info", "Scanning BTC 5-min markets...")

    try:
        signals = await scan_for_signals()
        # Slice P2: publish to the in-process cache so /api/dashboard
        # can serve from this list instead of running its own ~4s scan.
        # Single-writer pattern; see signals.py docstring.
        update_cached_scan(signals)
        actionable = [s for s in signals if s.passes_threshold]

        log_event("data", f"Found {len(signals)} signals, {len(actionable)} actionable", {
            "total_signals": len(signals),
            "actionable": len(actionable),
        })

        if not actionable:
            log_event("info", "No actionable BTC signals")
            return

        db = SessionLocal()
        try:
            state = db.query(BotState).first()
            if not state:
                log_event("error", "Bot state not initialized")
                return

            if not state.is_running:
                log_event("info", "Bot is paused, skipping trades")
                return

            MAX_TRADES_PER_SCAN = 2
            MIN_TRADE_SIZE = 1
            MAX_TRADE_FRACTION = 0.03  # 3% max per trade
            MAX_TOTAL_PENDING = settings.MAX_TOTAL_PENDING_TRADES

            # --- Daily loss circuit breaker ---
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            daily_pnl = db.query(func.coalesce(func.sum(Trade.pnl), 0.0)).filter(
                Trade.settled == True,
                Trade.settlement_time >= today_start
            ).scalar()

            if daily_pnl <= -settings.DAILY_LOSS_LIMIT:
                log_event("warning", f"Daily loss limit hit: ${daily_pnl:.2f} (limit: -${settings.DAILY_LOSS_LIMIT:.0f}). Stopping trades.")
                return

            total_pending = db.query(Trade).filter(Trade.settled == False).count()
            if total_pending >= MAX_TOTAL_PENDING:
                log_event("info", f"Max pending trades reached ({total_pending}/{MAX_TOTAL_PENDING})")
                return

            # Multi-asset trading enabled in slice 3e. Any crypto in
            # settings.CRYPTO_TECH_UNDERLYINGS (BTC/ETH/SOL/XRP) can execute
            # trades; the trade row records underlying_asset/asset_class for
            # per-asset analytics and allocation queries.
            #
            # Note on historical data: the ~600 BTC trades created before
            # slice 3e have underlying_asset=NULL. This is intentional —
            # they all resolved with market_type="btc" which is sufficient
            # for calibration. Do not backfill NULLs; multi-asset allocation
            # queries (slice 3f) filter on underlying_asset IS NOT NULL to
            # avoid mixing old and new rows.
            trades_executed = 0
            for signal in actionable[:MAX_TRADES_PER_SCAN]:
                # Per-underlying pending cap (slice 3f). Prevents any one
                # crypto (particularly high-vol SOL or if one asset starts
                # firing often) from monopolizing the open-trade queue.
                # Filters on Trade.underlying_asset, which is NULL for
                # legacy pre-slice-3e BTC rows — those are excluded from
                # the count, which is fine: they're all settled anyway.
                pending_same_underlying = db.query(Trade).filter(
                    Trade.settled == False,  # noqa: E712
                    Trade.underlying_asset == signal.underlying,
                ).count()
                if pending_same_underlying >= settings.MAX_PENDING_PER_UNDERLYING:
                    log_event(
                        "info",
                        f"skip {signal.market.slug}: "
                        f"{pending_same_underlying} pending {signal.underlying} trades "
                        f"(cap {settings.MAX_PENDING_PER_UNDERLYING})",
                    )
                    continue

                # Check if we already have a trade for this market window
                existing = db.query(Trade).filter(
                    Trade.event_slug == signal.market.slug,
                    Trade.settled == False
                ).first()

                if existing:
                    continue

                trade_size = min(signal.suggested_size, state.bankroll * MAX_TRADE_FRACTION)
                trade_size = max(trade_size, MIN_TRADE_SIZE)

                if state.bankroll < MIN_TRADE_SIZE:
                    log_event("warning", f"Bankroll too low: ${state.bankroll:.2f}")
                    break

                if trades_executed >= MAX_TRADES_PER_SCAN:
                    break

                # Map up/down to yes/no for storage
                entry_price = signal.market.up_price if signal.direction == "up" else signal.market.down_price

                # Slice 4: inherit signal features, layer on execution
                # context (wall-clock, queue depth, bankroll). Merged in
                # this order so signal keys win if they ever collide.
                now_utc = datetime.utcnow()
                trade_features = {
                    "exec_hour_utc": now_utc.hour,
                    "exec_minute": now_utc.minute,
                    "exec_day_of_week": now_utc.weekday(),
                    "exec_total_pending": total_pending,
                    "exec_pending_this_underlying": pending_same_underlying,
                    "exec_bankroll_at_entry": round(state.bankroll, 2),
                    **dict(signal.features),
                }

                trade = Trade(
                    market_ticker=signal.market.market_id,
                    platform="polymarket",
                    event_slug=signal.market.slug,
                    market_type="btc",              # calibration bucket; shared across all cryptos
                    underlying_asset=signal.underlying,
                    asset_class="crypto",
                    direction=signal.direction,
                    entry_price=entry_price,
                    size=trade_size,
                    model_probability=signal.model_probability,
                    market_price_at_entry=signal.market_probability,
                    edge_at_entry=signal.edge,
                    features=trade_features,
                )

                db.add(trade)
                db.flush()  # get trade.id

                # Link trade to the most recent matching Signal and mark it executed
                matching_signal = db.query(Signal).filter(
                    Signal.market_ticker == signal.market.market_id,
                    Signal.executed == False,
                ).order_by(Signal.timestamp.desc()).first()
                if matching_signal:
                    matching_signal.executed = True
                    trade.signal_id = matching_signal.id

                state.total_trades += 1
                trades_executed += 1

                log_event("trade",
                    f"{signal.underlying} {signal.direction.upper()} ${trade_size:.0f} @ {entry_price:.0%} | {signal.market.slug}",
                    {
                        "slug": signal.market.slug,
                        "underlying": signal.underlying,
                        "direction": signal.direction,
                        "size": trade_size,
                        "edge": signal.edge,
                        "entry_price": entry_price,
                        "underlying_price": signal.underlying_price,
                    }
                )

            state.last_run = datetime.utcnow()
            db.commit()

            if trades_executed > 0:
                log_event("success", f"Executed {trades_executed} crypto trade(s)")
            else:
                log_event("info", "No new trades executed")

        finally:
            db.close()

    except Exception as e:
        log_event("error", f"Scan error: {str(e)}")
        logger.exception("Error in scan_and_trade_job")


async def mc_scan_and_trade_job():
    """MC brain scan + execute loop. Runs every MC_SCAN_INTERVAL_SECONDS.

    Flow:
      1. scan_for_mc_signals() -> all signals (actionable + sub-threshold)
      2. Persist ALL signals to Signal table (calibration tracking)
      3. For each actionable signal (up to MC_MAX_TRADES_PER_SCAN):
           a. Per-series concentration cap check
           b. Quote refresh (skip if ask drifted > MC_QUOTE_DRIFT_TOLERANCE)
           c. Create Trade row, link to Signal
      4. Log [MC]-prefixed events via existing log_event helper
    """
    if not settings.MC_ENABLED:
        return

    log_event("info", "[MC] Scanning Monte Carlo markets...")

    try:
        from backend.core.mc_signals import scan_for_mc_signals, persist_mc_signals
        from backend.core.mc_execution import (
            cap_for_series,
            concentration_cap_exceeded,
            fetch_current_ask,
            quote_drifted,
            series_ticker_of,
        )

        signals = scan_for_mc_signals()
        actionable = [s for s in signals if s.passes_threshold]
        log_event("data", f"[MC] scan: {len(signals)} signals, {len(actionable)} actionable", {
            "total": len(signals),
            "actionable": len(actionable),
        })

        # Persist every signal (actionable + sub-threshold) for calibration.
        try:
            written = persist_mc_signals(signals)
            if written:
                log_event("data", f"[MC] persisted {written} new signal rows")
        except Exception as e:
            log_event("warning", f"[MC] persist failed: {e}")

        if not actionable:
            log_event("info", "[MC] No actionable signals to execute")
            return

        db = SessionLocal()
        try:
            state = db.query(BotState).first()
            if not state:
                log_event("error", "[MC] Bot state not initialized")
                return
            if not state.is_running:
                log_event("info", "[MC] Bot paused, skipping execution")
                return

            trades_executed = 0
            for signal in actionable[: settings.MC_MAX_TRADES_PER_SCAN]:
                ticker = signal.market.ticker

                # Guard 1: per-series concentration (cap is cadence-specific
                # post-S2 — see mc_execution.cap_for_series).
                if concentration_cap_exceeded(db, ticker):
                    series = series_ticker_of(ticker)
                    log_event(
                        "info",
                        f"[MC] skip {ticker}: concentration cap in series {series} "
                        f"(>= {cap_for_series(series)} open)",
                    )
                    continue

                # Guard 2: quote refresh
                current_ask = fetch_current_ask(ticker, signal.direction)
                if current_ask is None:
                    log_event(
                        "info",
                        f"[MC] skip {ticker}: quote refresh failed (no current ask)",
                    )
                    continue
                if quote_drifted(signal.market_probability, current_ask):
                    log_event(
                        "info",
                        f"[MC] skip {ticker}: quote drifted "
                        f"{signal.market_probability:.2%} -> {current_ask:.2%} "
                        f"(> {settings.MC_QUOTE_DRIFT_TOLERANCE:.2%})",
                    )
                    continue

                # Guard 3: dedup against any existing open trade on same ticker
                existing = db.query(Trade).filter(
                    Trade.market_ticker == ticker,
                    Trade.settled == False,  # noqa: E712
                ).first()
                if existing:
                    continue

                # Create the trade. Use refreshed ask as the fill price.
                size = min(signal.suggested_size,
                           settings.MC_MAX_TRADE_SIZE_PCT * settings.MC_PILOT_BANKROLL_USD)
                if size < 1.0:
                    log_event("info", f"[MC] skip {ticker}: size ${size:.2f} below $1 floor")
                    continue

                # Slice 4: inherit signal features, layer on execution
                # context. For MC, also capture the refreshed ask so we can
                # later audit how much the quote shifted between scan and
                # fill. Bankroll here = pilot bankroll (what sizing uses).
                now_utc = datetime.utcnow()
                mc_total_pending = db.query(Trade).filter(
                    Trade.settled == False,  # noqa: E712
                    Trade.market_type == "monte_carlo",
                ).count()
                mc_pending_this_und = db.query(Trade).filter(
                    Trade.settled == False,  # noqa: E712
                    Trade.market_type == "monte_carlo",
                    Trade.underlying_asset == signal.market.underlying_asset,
                ).count()
                trade_features = {
                    "exec_hour_utc": now_utc.hour,
                    "exec_minute": now_utc.minute,
                    "exec_day_of_week": now_utc.weekday(),
                    "exec_total_pending": mc_total_pending,
                    "exec_pending_this_underlying": mc_pending_this_und,
                    "exec_bankroll_at_entry": round(
                        float(settings.MC_PILOT_BANKROLL_USD), 2
                    ),
                    "exec_refreshed_ask": round(current_ask, 6),
                    **dict(signal.features),
                }

                trade = Trade(
                    market_ticker=ticker,
                    platform=signal.market.venue,
                    event_slug=signal.market.event_ticker,
                    market_type="monte_carlo",
                    underlying_asset=signal.market.underlying_asset,
                    asset_class=signal.market.asset_class,
                    contract_style=signal.market.contract_style,
                    direction=signal.direction.lower(),  # "yes" / "no"
                    entry_price=current_ask,  # the refreshed price we'd pay
                    size=size,
                    model_probability=signal.model_probability,
                    market_price_at_entry=current_ask,
                    edge_at_entry=signal.net_edge,
                    features=trade_features,
                )
                db.add(trade)
                db.flush()

                # Link to the most recent matching Signal row (if any).
                linked = (
                    db.query(Signal)
                    .filter(
                        Signal.market_ticker == ticker,
                        Signal.market_type == "monte_carlo",
                        Signal.executed == False,  # noqa: E712
                    )
                    .order_by(Signal.timestamp.desc())
                    .first()
                )
                if linked:
                    linked.executed = True
                    trade.signal_id = linked.id

                state.total_trades += 1
                trades_executed += 1
                log_event(
                    "trade",
                    f"[MC] {ticker} {signal.direction} ${size:.2f} @ {current_ask:.2%} "
                    f"(net_edge {signal.net_edge:+.2%})",
                    {
                        "ticker": ticker,
                        "side": signal.direction,
                        "size": size,
                        "ask": current_ask,
                        "net_edge": signal.net_edge,
                        "contract_style": signal.market.contract_style,
                    },
                )

            state.last_run = datetime.utcnow()
            db.commit()

            if trades_executed:
                log_event("success", f"[MC] executed {trades_executed} trade(s)")
            else:
                log_event("info", "[MC] no trades executed this cycle")
        finally:
            db.close()

    except Exception as e:
        log_event("error", f"[MC] scan error: {str(e)}")
        logger.exception("Error in mc_scan_and_trade_job")


async def settlement_job():
    """
    Background job: Check and settle pending trades.
    Runs every 2 minutes (BTC 5-min markets resolve fast).
    """
    log_event("info", "Checking BTC trade settlements...")

    try:
        from backend.core.settlement import settle_pending_trades, update_bot_state_with_settlements
        from backend.core.dashboard_cache import refresh_dashboard_cache

        db = SessionLocal()
        try:
            pending_count = db.query(Trade).filter(Trade.settled == False).count()

            if pending_count == 0:
                log_event("data", "No pending trades to settle")
                # Slice P3: still refresh the dashboard cache even when
                # nothing settled this cycle. Keeps the cache warm across
                # bot restarts (it populates within one settlement_job
                # tick of startup, ~2 min) and absorbs any out-of-band
                # calibration updates.
                _refresh_dashboard_cache_safe(db, refresh_dashboard_cache)
                return

            log_event("data", f"Processing {pending_count} pending trades")

            settled = await settle_pending_trades(db)

            if settled:
                await update_bot_state_with_settlements(db, settled)

                wins = sum(1 for t in settled if t.result == "win")
                losses = sum(1 for t in settled if t.result == "loss")
                total_pnl = sum(t.pnl for t in settled if t.pnl is not None)

                log_event("success", f"Settled {len(settled)} trades: {wins}W/{losses}L, P&L: ${total_pnl:.2f}", {
                    "settled_count": len(settled),
                    "wins": wins,
                    "losses": losses,
                    "pnl": total_pnl
                })

                for trade in settled:
                    result_prefix = "+" if trade.pnl and trade.pnl > 0 else ""
                    log_event("data", f"  {trade.event_slug}: {trade.result.upper()} {result_prefix}${trade.pnl:.2f}")
            else:
                log_event("info", "No trades ready for settlement")

            # Slice P3: refresh dashboard cache after settlement work.
            # Wrapped so a cache failure can't cascade into settlement
            # rollback or false-error logs. The cache being stale is
            # strictly better than the settlement loop failing.
            _refresh_dashboard_cache_safe(db, refresh_dashboard_cache)

        finally:
            db.close()

    except Exception as e:
        log_event("error", f"Settlement error: {str(e)}")
        logger.exception("Error in settlement_job")


def _refresh_dashboard_cache_safe(db, refresh_fn):
    """Slice P3 helper: invoke refresh_dashboard_cache(db) and absorb
    any exception. The dashboard cache is best-effort — if it fails,
    /api/dashboard falls back to its inline-build path on the next
    request. No reason to let a cache failure surface as a settlement
    error in the logs."""
    try:
        refresh_fn(db)
    except Exception as e:
        logger.warning(f"[dashboard cache] refresh failed (non-fatal): {e}")


async def heartbeat_job():
    """Periodic heartbeat. Runs every minute."""
    db = None
    try:
        db = SessionLocal()
        state = db.query(BotState).first()
        pending = db.query(Trade).filter(Trade.settled == False).count()

        if state is None:
            log_event("warning", "Heartbeat: Bot state not initialized")
            return

        log_event("data", f"Heartbeat: {pending} pending trades, bankroll: ${state.bankroll:.2f}", {
            "pending_trades": pending,
            "bankroll": state.bankroll,
            "is_running": state.is_running
        })
    except Exception as e:
        log_event("warning", f"Heartbeat failed: {str(e)}")
    finally:
        if db:
            db.close()


def start_scheduler():
    """Start the background scheduler for BTC 5-min trading."""
    global scheduler

    if scheduler is not None and scheduler.running:
        log_event("warning", "Scheduler already running")
        return

    scheduler = AsyncIOScheduler()

    scan_seconds = settings.SCAN_INTERVAL_SECONDS
    settle_seconds = settings.SETTLEMENT_INTERVAL_SECONDS

    # Scan BTC markets every minute
    scheduler.add_job(
        scan_and_trade_job,
        IntervalTrigger(seconds=scan_seconds),
        id="market_scan",
        replace_existing=True,
        max_instances=1
    )

    # Check settlements every 2 minutes
    scheduler.add_job(
        settlement_job,
        IntervalTrigger(seconds=settle_seconds),
        id="settlement_check",
        replace_existing=True,
        max_instances=1
    )

    # Heartbeat every minute
    scheduler.add_job(
        heartbeat_job,
        IntervalTrigger(minutes=1),
        id="heartbeat",
        replace_existing=True,
        max_instances=1
    )

    # MC brain job: runs independently on MC_SCAN_INTERVAL_SECONDS.
    # Gated by settings.MC_ENABLED; does nothing if disabled.
    if settings.MC_ENABLED:
        mc_interval = settings.MC_SCAN_INTERVAL_SECONDS
        scheduler.add_job(
            mc_scan_and_trade_job,
            IntervalTrigger(seconds=mc_interval),
            id="mc_scan",
            replace_existing=True,
            max_instances=1,
        )

    scheduler.start()
    log_event("success", "trading scheduler started", {
        "btc_scan_interval": f"{scan_seconds}s",
        "settlement_interval": f"{settle_seconds}s",
        "btc_min_edge": f"{settings.MIN_EDGE_THRESHOLD:.0%}",
        "mc_enabled": settings.MC_ENABLED,
        "mc_scan_interval": (
            f"{settings.MC_SCAN_INTERVAL_SECONDS}s"
            if settings.MC_ENABLED else "disabled"
        ),
    })

    asyncio.create_task(scan_and_trade_job())
    if settings.MC_ENABLED:
        asyncio.create_task(mc_scan_and_trade_job())


def stop_scheduler():
    """Stop the background scheduler."""
    global scheduler

    if scheduler is None or not scheduler.running:
        log_event("info", "Scheduler not running")
        return

    scheduler.shutdown(wait=False)
    scheduler = None
    log_event("info", "Scheduler stopped")


def is_scheduler_running() -> bool:
    """Check if scheduler is currently running."""
    return scheduler is not None and scheduler.running


async def run_manual_scan():
    """Trigger a manual market scan."""
    log_event("info", "Manual scan triggered")
    await scan_and_trade_job()


async def run_manual_settlement():
    """Trigger a manual settlement check."""
    log_event("info", "Manual settlement triggered")
    await settlement_job()
