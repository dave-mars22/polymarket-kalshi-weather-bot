export interface BtcPrice {
  price: number
  change_24h: number
  change_7d: number
  market_cap: number
  volume_24h: number
  last_updated: string
}

export interface Microstructure {
  rsi: number
  momentum_1m: number
  momentum_5m: number
  momentum_15m: number
  vwap_deviation: number
  sma_crossover: number
  volatility: number
  price: number
  source: string
}

export interface BtcWindow {
  slug: string
  market_id: string
  up_price: number
  down_price: number
  window_start: string
  window_end: string
  volume: number
  is_active: boolean
  is_upcoming: boolean
  time_until_end: number
  spread: number
}

export interface Signal {
  market_ticker: string
  market_title: string
  platform: string
  direction: string
  model_probability: number
  market_probability: number
  edge: number
  confidence: number
  suggested_size: number
  reasoning: string
  timestamp: string
  category: string
  event_slug?: string
  underlying_price: number
  underlying_change_24h: number
  window_end?: string
  actionable: boolean
  // Slice D2: multi-strategy attribution. Optional so older payloads
  // without these fields don't break consumers.
  underlying_asset?: string | null
  asset_class?: string | null
  contract_style?: string | null
}

export interface Trade {
  id: number
  market_ticker: string
  platform: string
  event_slug?: string | null
  direction: string
  entry_price: number
  size: number
  timestamp: string
  settled: boolean
  result: string
  pnl: number | null
  // Slice D2: multi-strategy attribution read from Trade row columns.
  market_type?: string | null
  underlying_asset?: string | null
  asset_class?: string | null
  contract_style?: string | null
}

export interface BotStats {
  bankroll: number
  total_trades: number
  winning_trades: number
  win_rate: number
  total_pnl: number
  is_running: boolean
  last_run: string | null
}

export interface EquityPoint {
  timestamp: string
  pnl: number
  bankroll: number
}

export interface CalibrationSummary {
  total_signals: number
  total_with_outcome: number
  accuracy: number
  avg_predicted_edge: number
  avg_actual_edge: number
  brier_score: number
}

// Slice D2: new per-strategy / per-asset / MC types. Defined here for
// type safety as the backend starts emitting them; no component consumes
// them yet (that ships in a later slice).
export interface MultiMicrostructure {
  microstructures: Record<string, Microstructure>
  prices: Record<string, number>
}

export interface PerStrategyStats {
  strategy: 'crypto_tech' | 'monte_carlo' | string
  total_trades: number
  settled_trades: number
  pending_trades: number
  wins: number
  losses: number
  win_rate: number | null
  total_pnl: number
  pnl_24h: number
  allocated_bankroll: number
  realized_bankroll: number
}

export interface PerAssetStats {
  underlying: string
  total_trades: number
  settled_trades: number
  pending_trades: number
  wins: number
  losses: number
  win_rate: number | null
  total_pnl: number
  pnl_24h: number
  last_signal_time: string | null
  last_trade_time: string | null
}

export interface McOpenPosition {
  trade_id: number
  market_ticker: string
  underlying: string
  direction: string
  entry_price: number
  size: number
  model_probability: number
  timestamp: string
  expected_settlement: string | null
}

export interface McPortfolioStatus {
  pilot_bankroll_target: number
  realized_pilot_bankroll: number
  open_positions: McOpenPosition[]
  total_allocated: number
  signals_last_24h: number
  actionable_signals_last_24h: number
  next_scheduled_scan: string | null
}

export interface DashboardData {
  stats: BotStats
  btc_price: BtcPrice | null
  microstructure: Microstructure | null
  windows: BtcWindow[]
  active_signals: Signal[]
  recent_trades: Trade[]
  equity_curve: EquityPoint[]
  calibration: CalibrationSummary | null
  // Slice D2: all optional — older backends / transient fetch failures
  // will simply omit them, and no current component reads them.
  multi_microstructure?: MultiMicrostructure | null
  per_strategy_stats?: PerStrategyStats[]
  per_asset_stats?: PerAssetStats[]
  mc_portfolio?: McPortfolioStatus | null
}
