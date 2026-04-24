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
    BTC_PRICE_SOURCE: str = "coinbase"
    MIN_EDGE_THRESHOLD: float = 0.02  # 2% edge required — these are 50/50 markets
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

    # Slippage (basis points) used by fee-aware edge math
    BTC_SLIPPAGE_BPS: int = 10      # 0.10% slippage per side for BTC/Polymarket
    KALSHI_SLIPPAGE_BPS: int = 50   # 0.50% slippage per side for Kalshi markets

    # === MONTE CARLO BRAIN ===
    # (full config batch lands when scheduler wires in; these two ship now
    # because the data layer reads them directly)
    MC_HISTORICAL_CACHE_SECONDS: int = 3600   # 1 h — vol barely moves intraday
    MC_SPOT_CACHE_SECONDS: int = 30           # 30 s — fresh enough for daily markets

    class Config:
        env_file = ".env"


settings = Settings()
