"""Configuration settings for the BTC 5-min trading bot."""
import os
from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Database (SQLite for Phase 1, PostgreSQL for production)
    DATABASE_URL: str = "sqlite:///./tradingbot.db"

    # API Keys (optional)
    POLYMARKET_API_KEY: Optional[str] = None

    # Kalshi API
    KALSHI_API_KEY_ID: Optional[str] = None
    KALSHI_PRIVATE_KEY_PATH: Optional[str] = None
    KALSHI_ENABLED: bool = True

    # AI API Keys
    GROQ_API_KEY: Optional[str] = None

    # AI Model Configuration
    GROQ_MODEL: str = "llama-3.1-8b-instant"

    # AI Feature Flags
    AI_LOG_ALL_CALLS: bool = True
    AI_DAILY_BUDGET_USD: float = 1.0

    # Bot settings - BTC 5-MIN TRADING
    SIMULATION_MODE: bool = True
    INITIAL_BANKROLL: float = 10000.0
    KELLY_FRACTION: float = 0.10  # Fractional Kelly
    # Change 3: Calibration-adjusted Kelly
    MIN_TRADES_FOR_CALIBRATION: int = 100  # Trades needed before trusting calibration
    CALIBRATION_MAX_MULTIPLIER: float = 0.7  # Cap on Kelly scale-up

    # BTC 5-min specific settings
    SCAN_INTERVAL_SECONDS: int = 60  # Scan every minute
    SETTLEMENT_INTERVAL_SECONDS: int = 120  # Check settlements every 2 min
    # Min edge required to enter a BTC trade. Set to 5% based on a diagnostic
    # of 718 settled BTC trades (analysis run 2026-04-24):
    #   - trades with |edge| < 5%:  n=210, win rate 44.3%, cumulative -$150
    #   - trades with |edge| >= 5%: n=496, win rate ~52%, cumulative +$200
    # The 5% gate cleanly separates a losing population from a marginally
    # profitable one. The .env override has been 0.05 since approximately
    # 2026-04-21 (latest sub-5% trade in the DB is id=523 at 00:55 UTC that
    # day; Apr 22 onward the bot only generated >=5% trades). This source
    # default is synced to match so the runtime can't silently drift back
    # to 0.02 if .env is reset or recreated from .env.example.
    MIN_EDGE_THRESHOLD: float = 0.05
    MAX_ENTRY_PRICE: float = 0.55  # Enter up to 55c
    MAX_TRADES_PER_WINDOW: int = 1
    MAX_TOTAL_PENDING_TRADES: int = 20

    # Signal gates (Change 2a — tighter convergence)
    USE_STRICT_CONVERGENCE: bool = True

    # Risk management
    DAILY_LOSS_LIMIT: float = 300.0
    MAX_TRADE_SIZE: float = 75.0
    MIN_TIME_REMAINING: int = 60  # Don't trade windows closing in < 60s
    MAX_TIME_REMAINING: int = 1800  # Trade windows up to 30min out

    # Indicator weights for composite signal (must sum to ~1.0)
    WEIGHT_RSI: float = 0.20
    WEIGHT_MOMENTUM: float = 0.35
    WEIGHT_VWAP: float = 0.20
    WEIGHT_SMA: float = 0.15
    WEIGHT_MARKET_SKEW: float = 0.10

    # Volume filter
    MIN_MARKET_VOLUME: float = 100.0  # Low volume for 5-min markets

    # Slippage (basis points) used by fee-aware edge math.
    # Applies venue-wide: not BTC-specific — all Polymarket crypto 5-min
    # trades (BTC/ETH/SOL/XRP) share this slippage assumption.
    POLYMARKET_CRYPTO_SLIPPAGE_BPS: int = 10   # 0.10% slippage per side
    KALSHI_SLIPPAGE_BPS: int = 50              # 0.50% slippage per side

    # === MONTE CARLO BRAIN ===
    # (full config batch lands when scheduler wires in; these two ship now
    # because the data layer reads them directly)
    MC_HISTORICAL_CACHE_SECONDS: int = 3600   # 1 h — vol barely moves intraday
    MC_SPOT_CACHE_SECONDS: int = 30           # 30 s — fresh enough for daily markets
    MC_MAX_TIME_TO_EXPIRY_DAYS: float = 30.0  # skip contracts expiring beyond 30 days
    MC_ENABLED: bool = True
    MC_SCAN_INTERVAL_SECONDS: int = 600       # 10 min — MC markets don't need sub-minute scans
    # Simulation
    MC_NUM_PATHS: int = 10_000
    MC_VOL_LOOKBACK_DAYS: int = 30
    MC_USE_EWMA_VOL: bool = True
    MC_EWMA_LAMBDA: float = 0.94              # RiskMetrics standard
    MC_DRIFT_LOOKBACK_DAYS: int = 90
    MC_MIN_HISTORY_DAYS: int = 60
    # Edge gating
    MC_MIN_EDGE_THRESHOLD: float = 0.05
    MC_MAX_ENTRY_PRICE: float = 0.75
    MC_MIN_TIME_TO_EXPIRY_HOURS: float = 1.0
    # Position sizing — all as fractions of current (live) bankroll
    MC_MAX_TRADE_SIZE_PCT: float = 0.03
    MC_MAX_PER_UNDERLYING_PCT: float = 0.08
    MC_MAX_ASSET_CLASS_PCT: float = 0.15
    MC_MAX_TOTAL_ALLOCATION_PCT: float = 0.40
    MC_MAX_TRADES_PER_SCAN: int = 5
    # Pilot-phase bankroll for MC brain. Kept at $1000 until calibration
    # data validates realized-vs-predicted edge. DO NOT CHANGE without
    # reviewing at least 30 settled barrier trades.
    # Stopping criterion: after 90 days OR 30 settled barrier trades
    # (whichever first), if realized edge averages below 3pp, archive MC.
    MC_PILOT_BANKROLL_USD: float = 1000.0
    # Quote-refresh guard: skip a trade if the ask price has moved more
    # than this many dollars from the scan-time snapshot.
    MC_QUOTE_DRIFT_TOLERANCE: float = 0.02
    # MC concentration caps differentiated by cadence to balance data
    # accumulation rate against capital-at-risk duration. Daily contracts
    # settle within 24h, providing fast feedback on model accuracy — small
    # exposure increase is low risk. Monthly contracts hold capital for
    # weeks before settling, so the original cap of 2 is preserved to limit
    # blast radius if the model is wrong. Decision date: 2026-04-25, with
    # first MC settlements (April 30) still pending. This intentionally
    # accepts more daily-series exposure to accelerate data collection
    # ahead of the May 21 evaluation checkpoint. Slice S2.
    #
    # Cadence is looked up per Kalshi series_ticker via
    # backend.data.mc_markets.cadence_for_series; concentration check lives
    # in backend.core.mc_execution.cap_for_series.
    MC_MAX_OPEN_PER_SERIES_DAILY: int = 3
    MC_MAX_OPEN_PER_SERIES_MONTHLY: int = 2
    MC_MAX_OPEN_PER_SERIES_OTHER: int = 2

    # === MULTI-CRYPTO TECHNICAL BRAIN ===
    CRYPTO_TECH_ENABLED: bool = True
    # Comma-separated underlyings to scan. Parsed at scan time; asset-
    # agnostic signal logic so adding/removing entries is cheap. Slice
    # 3c adds ETH; 3d adds SOL + XRP after live verification.
    CRYPTO_TECH_UNDERLYINGS: str = "BTC,ETH,SOL,XRP"
    # Per-underlying pending-trade cap. Prevents any single crypto from
    # dominating the open-trade queue and concentrating risk. Mirrors MC's
    # per-series concentration cap pattern.
    MAX_PENDING_PER_UNDERLYING: int = 8
    # Minimum 24-hour volume ($USD) on a market before we produce a signal.
    # Filters dead markets so calibration data stays clean. Observed
    # volumes at slice 3d: BTC ~$116, ETH nearest ~$119 (upcoming $0),
    # SOL/XRP upcoming $0. Passes BTC + ETH-nearest; filters the rest.
    MIN_MARKET_VOLUME_24H_USD: float = 50.0

    class Config:
        env_file = ".env"


settings = Settings()
