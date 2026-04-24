// Pure data derivation over DashboardData. No React, no hooks, no JSX —
// just typed functions components can call for multi-strategy / multi-asset
// views. Backward-compatible fallbacks let these selectors work against a
// pre-slice-D2 backend payload (the new fields are optional on the wire).
import type {
  DashboardData,
  McOpenPosition,
  Microstructure,
  PerAssetStats,
  PerStrategyStats,
  Signal,
  Trade,
} from '../types'

export function getCryptoTechStats(data: DashboardData): PerStrategyStats | null {
  const stats = data.per_strategy_stats ?? []
  return stats.find(s => s.strategy === 'crypto_tech') ?? null
}

export function getMcStats(data: DashboardData): PerStrategyStats | null {
  const stats = data.per_strategy_stats ?? []
  return stats.find(s => s.strategy === 'monte_carlo') ?? null
}

export function getAssetStats(
  data: DashboardData, underlying: string,
): PerAssetStats | null {
  const stats = data.per_asset_stats ?? []
  const target = underlying.toUpperCase()
  return stats.find(a => a.underlying.toUpperCase() === target) ?? null
}

export function getAllAssetStats(data: DashboardData): PerAssetStats[] {
  return data.per_asset_stats ?? []
}

// Backward-compat: if multi_microstructure is absent (older backend), fall
// back to the singular data.microstructure when the caller asks for BTC.
export function getMicrostructureFor(
  data: DashboardData, underlying: string,
): Microstructure | null {
  const target = underlying.toUpperCase()
  const multi = data.multi_microstructure?.microstructures
  if (multi && multi[target]) return multi[target]
  if (target === 'BTC' && data.microstructure) return data.microstructure
  return null
}

export function getPriceFor(
  data: DashboardData, underlying: string,
): number | null {
  const target = underlying.toUpperCase()
  const prices = data.multi_microstructure?.prices
  if (prices && typeof prices[target] === 'number') return prices[target]
  if (target === 'BTC' && data.btc_price) return data.btc_price.price
  return null
}

export function getOpenMcPositions(data: DashboardData): McOpenPosition[] {
  return data.mc_portfolio?.open_positions ?? []
}

export function getMcPilotStatus(
  data: DashboardData,
): { target: number; realized: number; pnl: number } | null {
  const mc = data.mc_portfolio
  if (!mc) return null
  return {
    target: mc.pilot_bankroll_target,
    realized: mc.realized_pilot_bankroll,
    pnl: mc.realized_pilot_bankroll - mc.pilot_bankroll_target,
  }
}

// Portfolio-level bankroll view: the shared BotState bankroll (which the
// crypto-tech brain draws from) PLUS the MC pilot's realized notional.
// Falls back to `data.stats.bankroll` alone if per-strategy data missing.
export function getCombinedBankroll(data: DashboardData): number {
  const tech = getCryptoTechStats(data)
  const mc = getMcStats(data)
  if (!tech && !mc) return data.stats?.bankroll ?? 0
  const techBank = tech?.realized_bankroll ?? data.stats?.bankroll ?? 0
  const mcBank = mc?.realized_bankroll ?? 0
  return techBank + mcBank
}

export function getCombinedPnl(data: DashboardData): number {
  const tech = getCryptoTechStats(data)
  const mc = getMcStats(data)
  if (!tech && !mc) return data.stats?.total_pnl ?? 0
  return (tech?.total_pnl ?? 0) + (mc?.total_pnl ?? 0)
}

export function getTotalPendingTrades(data: DashboardData): number {
  const stats = data.per_strategy_stats ?? []
  if (stats.length === 0) {
    // Fallback: count pending rows in recent_trades.
    return (data.recent_trades ?? []).filter(t => !t.settled).length
  }
  return stats.reduce((sum, s) => sum + s.pending_trades, 0)
}

// Groups signals by underlying_asset. Signals without attribution land
// under "UNKNOWN" so the caller can decide whether to surface or drop them.
export function getActionableSignalsByAsset(
  data: DashboardData,
): Record<string, Signal[]> {
  const out: Record<string, Signal[]> = {}
  for (const sig of data.active_signals ?? []) {
    const key = (sig.underlying_asset || '').toUpperCase() || 'UNKNOWN'
    if (!out[key]) out[key] = []
    out[key].push(sig)
  }
  return out
}

export function getRecentTradesByAsset(
  data: DashboardData,
): Record<string, Trade[]> {
  const out: Record<string, Trade[]> = {}
  for (const trade of data.recent_trades ?? []) {
    const key = (trade.underlying_asset || '').toUpperCase() || 'UNKNOWN'
    if (!out[key]) out[key] = []
    out[key].push(trade)
  }
  return out
}

// market_type preserves the DB label ("btc" for crypto-tech, "monte_carlo"
// for MC). Trades without market_type land under "unknown".
export function getRecentTradesByStrategy(
  data: DashboardData,
): Record<string, Trade[]> {
  const out: Record<string, Trade[]> = {}
  for (const trade of data.recent_trades ?? []) {
    const key = trade.market_type || 'unknown'
    if (!out[key]) out[key] = []
    out[key].push(trade)
  }
  return out
}
