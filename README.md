# Prediction Market Trading Bot

A multi-strategy trading bot that identifies pricing inefficiencies in prediction markets. Combines **multi-crypto 5-minute microstructure analysis** with a **Monte Carlo barrier-option pricer** to trade on **Polymarket** and **Kalshi**. Features a professional React dashboard.

![Python](https://img.shields.io/badge/python-3.10+-blue) ![React](https://img.shields.io/badge/react-18+-61DAFB) ![TypeScript](https://img.shields.io/badge/typescript-5.0+-blue) ![License](https://img.shields.io/badge/license-MIT-green)

![Dashboard](docs/dashboard.png)

**100% free to run** - No paid APIs, no subscriptions. All data sources are free. Kalshi API key optional for Kalshi markets.

## Overview

### Strategy 1: Multi-Crypto 5-Minute Up/Down
Scans Polymarket 5-minute Up/Down markets on BTC, ETH, SOL, and XRP every 60 seconds. Uses real-time 1-minute candle data from Coinbase/Kraken/Binance to compute RSI, momentum, VWAP deviation, SMA crossover, and market skew as a weighted composite signal. Trades when edge > 2%.

### Strategy 2: Monte Carlo Barrier (Kalshi, pilot phase)
Scans Kalshi monthly barrier contracts (KXBTCMAXMON, KXBTCMINMON) every 10 minutes. Closed-form reflection-principle GBM pricing for one-touch barriers; EWMA vol estimation with calibration-adjusted fractional Kelly. Currently runs on a $1,000 pilot bankroll with a per-series concentration cap; expansion gated on realized-vs-predicted edge validation after first settlements.

### Key Features

- **Crypto Microstructure Analysis** - RSI, momentum (1m/5m/15m), VWAP, SMA crossover from real candle data (BTC/ETH/SOL/XRP)
- **Monte Carlo Barrier Pricing** - Closed-form GBM barrier valuations with EWMA vol; calibration-shrunk Kelly sizing
- **Multi-Platform Trading** - Polymarket (crypto 5-min) and Kalshi (barrier) simultaneously
- **Edge Detection** - Identifies mispriced markets across both strategies and platforms
- **Kelly Criterion Sizing** - Fractional Kelly (15%) position sizing with per-trade caps
- **Signal Calibration** - Tracks predictions vs outcomes with Brier score
- **Professional Dashboard** - React dashboard with real-time per-strategy + per-asset views
- **Simulation Mode** - Paper trading with virtual bankroll tracking and equity curves

## Quick Start

### 1. Backend Setup

```bash
cd kalshi-trading-bot

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run the backend
uvicorn backend.api.main:app --reload --port 8000
```

Backend will be at: http://localhost:8000
API docs at: http://localhost:8000/docs

### 2. Frontend Setup

```bash
cd frontend

# Install dependencies
npm install

# Run the frontend
npm run dev
```

Frontend will be at: http://localhost:5173

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                          FRONTEND                                │
│  React + TypeScript + TanStack Query + Tailwind                  │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐            │
│  │ Strategy │ │Per-Asset │ │ Signals  │ │  Trades  │            │
│  │  Cards   │ │ Micro    │ │  Table   │ │  Table   │            │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘            │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                          BACKEND                                 │
│  FastAPI + Python + SQLite + APScheduler                         │
│  ┌───────────┐ ┌───────────┐ ┌───────────┐ ┌───────────┐        │
│  │  Crypto   │ │Monte Carlo│ │  Signal   │ │Settlement │        │
│  │  Tech     │ │ Barrier   │ │ Scheduler │ │  Engine   │        │
│  └───────────┘ └───────────┘ └───────────┘ └───────────┘        │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                        DATA SOURCES                              │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐            │
│  │Coinbase/ │ │  yfinance│ │Polymarket│ │ Kalshi   │            │
│  │Kraken/   │ │ (index   │ │Gamma API │ │ Markets  │            │
│  │Binance   │ │  history)│ │          │ │  API     │            │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘            │
└──────────────────────────────────────────────────────────────────┘
```

## How It Works

### Crypto 5-Minute Strategy
1. Fetch 60 one-minute candles from Coinbase/Kraken/Binance (fallback chain) for each configured underlying (BTC, ETH, SOL, XRP)
2. Compute 5 indicators per underlying: RSI(14), Momentum(1m/5m/15m), VWAP deviation, SMA crossover, Market skew
3. Convergence filter: require 2+ of 4 indicators to agree
4. Weighted composite -> model UP probability (0.35-0.65 range)
5. Compare to Polymarket prices, trade the side with higher edge

### Monte Carlo Barrier Strategy
1. Fetch open Kalshi barrier markets (KXBTCMAXMON, KXBTCMINMON series)
2. Estimate EWMA vol + drift from daily-close history (Coinbase spot for crypto, yfinance for index series)
3. Price each contract via closed-form reflection-principle GBM for one-touch barriers; fall back to European pricing when rules are ambiguous
4. Compute edge = model_probability - ask_price, net of Kalshi fees + slippage
5. Trade when net edge >= 5% and quote hasn't drifted since scan
6. Pilot-sized: $1,000 bankroll, per-series concentration cap, max 5 trades per scan

### Edge Calculation
```
edge = model_probability - market_probability
```
Crypto 5-min signals require |edge| > 2%. MC barrier signals require net_edge >= 5%.

### Position Sizing (Fractional Kelly)
```
kelly = (win_prob * odds - lose_prob) / odds
position_size = kelly * 0.15 * bankroll
```
Capped at 5% of bankroll for crypto 5-min, and by MC_MAX_TRADE_SIZE_PCT/MC_MAX_PER_UNDERLYING_PCT/MC_MAX_ASSET_CLASS_PCT caps for MC barrier.

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/dashboard` | GET | All dashboard data in one call |
| `/api/btc/price` | GET | Current BTC price + momentum |
| `/api/btc/windows` | GET | Active BTC 5-min windows |
| `/api/signals` | GET | Current BTC trading signals |
| `/api/signals/actionable` | GET | BTC signals above threshold |
| `/api/kalshi/status` | GET | Kalshi API auth status + balance |
| `/api/mc/portfolio` | GET | MC barrier portfolio (pilot bankroll, open positions, next scan) |
| `/api/mc/signals` | GET | Recent MC signals (actionable + sub-threshold) |
| `/api/mc/trades` | GET | MC trades (filter by status) |
| `/api/assets/stats` | GET | Per-underlying trade aggregates for the crypto_tech brain |
| `/api/microstructure` | GET | Microstructure for one underlying (BTC/ETH/SOL/XRP) |
| `/api/trades` | GET | Trade history |
| `/api/stats` | GET | Bot statistics |
| `/api/calibration` | GET | Signal calibration data |
| `/api/run-scan` | POST | Trigger crypto + MC scan |
| `/api/simulate-trade` | POST | Simulate a BTC trade |
| `/api/settle-trades` | POST | Check settlements |
| `/api/bot/start` | POST | Start trading |
| `/api/bot/stop` | POST | Pause trading |
| `/api/bot/reset` | POST | Reset all trades |
| `/api/events` | GET | Event log |
| `/ws/events` | WS | Real-time event stream |

## Configuration

All settings in `backend/config.py`, overridable via environment variables:

### Crypto 5-Min Settings
| Setting | Default | Description |
|---------|---------|-------------|
| `SCAN_INTERVAL_SECONDS` | 60 | Crypto 5-min scan frequency |
| `MIN_EDGE_THRESHOLD` | 0.02 | Minimum edge (2%) |
| `MAX_ENTRY_PRICE` | 0.55 | Max entry price (55c) |
| `MAX_TRADE_SIZE` | 75.0 | Max $ per crypto 5-min trade |
| `KELLY_FRACTION` | 0.10 | Fractional Kelly multiplier |
| `CRYPTO_TECH_UNDERLYINGS` | BTC,ETH,SOL,XRP | Underlyings scanned per cycle |
| `MAX_PENDING_PER_UNDERLYING` | 8 | Per-underlying pending-trade cap |
| `MIN_MARKET_VOLUME_24H_USD` | 50.0 | Filter dead markets |

### Kalshi Settings
| Setting | Default | Description |
|---------|---------|-------------|
| `KALSHI_API_KEY_ID` | None | Kalshi API key ID |
| `KALSHI_PRIVATE_KEY_PATH` | None | Path to RSA private key PEM file |
| `KALSHI_ENABLED` | True | Enable/disable Kalshi market fetching |

### Monte Carlo Barrier Settings
| Setting | Default | Description |
|---------|---------|-------------|
| `MC_ENABLED` | True | Enable/disable MC brain |
| `MC_SCAN_INTERVAL_SECONDS` | 600 | MC scan frequency (10 min) |
| `MC_MIN_EDGE_THRESHOLD` | 0.05 | Minimum net edge (5%) |
| `MC_MAX_ENTRY_PRICE` | 0.75 | Max entry price (75c) |
| `MC_PILOT_BANKROLL_USD` | 1000.0 | Isolated pilot bankroll |
| `MC_MAX_OPEN_PER_SERIES` | 2 | Concentration cap per Kalshi series |
| `MC_QUOTE_DRIFT_TOLERANCE` | 0.02 | Skip trade if ask drifts > 2¢ between scan and fill |

### Risk Management
| Setting | Default | Description |
|---------|---------|-------------|
| `DAILY_LOSS_LIMIT` | 300.0 | Daily loss circuit breaker |
| `MAX_TOTAL_PENDING_TRADES` | 20 | Max open positions |
| `INITIAL_BANKROLL` | 10000.0 | Starting paper bankroll |

## Data Sources

| Source | Data | Used For | Auth |
|--------|------|----------|------|
| Coinbase | Crypto 1-min candles + daily closes | Crypto microstructure + MC history | None |
| Kraken | Crypto 1-min candles | Crypto fallback | None |
| Binance | Crypto 1-min candles | Crypto fallback | None |
| yfinance | Index daily closes | MC equity-index vol/drift estimation | None |
| Polymarket | Market prices + resolution | Crypto 5-min strategy | None |
| Kalshi | Monthly barrier markets (KXBTCMAXMON, KXBTCMINMON) | MC barrier strategy | RSA key (optional for public reads) |

## Project Structure

```
polymarket-kalshi-bot/
├── backend/
│   ├── api/
│   │   └── main.py                 # FastAPI routes + dashboard
│   ├── core/
│   │   ├── signals.py              # Crypto 5-min signal generation
│   │   ├── mc_signals.py           # Monte Carlo barrier signal generator
│   │   ├── mc_execution.py         # MC execution guards (quote drift, concentration)
│   │   ├── monte_carlo.py          # GBM sim + reflection-principle barrier pricing
│   │   ├── vol_estimator.py        # EWMA vol + drift estimation
│   │   ├── fees.py                 # Fee + slippage model (Polymarket / Kalshi)
│   │   ├── calibration.py          # Kelly scale from realized-vs-predicted edge
│   │   ├── settlement.py           # Trade settlement (routes by market_type)
│   │   └── scheduler.py            # Background jobs (crypto tech + MC)
│   ├── data/
│   │   ├── crypto.py               # Crypto price + microstructure (multi-asset)
│   │   ├── crypto_markets.py       # Polymarket 5-min market fetcher
│   │   ├── mc_markets.py           # Kalshi barrier market fetcher
│   │   ├── spot_prices.py          # Coinbase + yfinance spot adapters
│   │   ├── price_history.py        # Daily-close history adapters
│   │   ├── kalshi_client.py        # Kalshi API client (RSA-PSS auth)
│   │   └── markets.py              # Generic market wrapper
│   ├── models/
│   │   └── database.py             # SQLAlchemy models (market_type / underlying_asset)
│   └── config.py                   # All settings
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   │   ├── PortfolioHeader.tsx       # Multi-strategy portfolio summary
│   │   │   ├── StrategyCards.tsx         # Technical + MC strategy cards
│   │   │   ├── McDetailPanel.tsx         # MC barrier detail (timeline + series + calibration)
│   │   │   ├── MultiAssetMicrostructure.tsx # Per-asset RSI / momentum / vol
│   │   │   ├── EdgeDistribution.tsx      # Edge distribution chart
│   │   │   ├── CalibrationPanel.tsx      # Prediction accuracy tracking
│   │   │   ├── SignalsTable.tsx          # Filterable signals (chip-filtered by asset)
│   │   │   ├── TradesTable.tsx           # Filterable trades (chip-filtered by asset)
│   │   │   ├── TableFilterChips.tsx      # Shared chip filter row
│   │   │   ├── EquityChart.tsx           # P&L chart
│   │   │   └── Terminal.tsx              # Event log + controls
│   │   ├── hooks/useDashboard.ts         # /api/dashboard polling hook
│   │   ├── selectors/dashboardSelectors.ts # Pure data derivation
│   │   ├── App.tsx                  # Main dashboard layout
│   │   ├── api.ts                   # API client
│   │   └── types.ts                 # TypeScript interfaces
│   └── package.json
├── requirements.txt
├── run.py
└── README.md
```

## Disclaimer

This is a **simulation tool** for educational purposes. It does not place real trades or use real money. Past performance in simulation does not guarantee future results. Prediction markets involve risk of loss.

## License

MIT - do whatever you want with it.
