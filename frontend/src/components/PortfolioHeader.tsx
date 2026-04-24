import { motion } from 'framer-motion'
import type { DashboardData } from '../types'
import {
  getAllAssetStats,
  getCombinedBankroll,
  getCombinedPnl,
  getCryptoTechStats,
  getMcPilotStatus,
  getMcStats,
  getMicrostructureFor,
  getPriceFor,
  getTotalPendingTrades,
} from '../selectors/dashboardSelectors'

interface Props {
  data?: DashboardData
}

// Order matches settings.CRYPTO_TECH_UNDERLYINGS. Kept inline rather than
// pulled from the payload so the strip shows a stable column set even when
// one underlying's microstructure fetch fails.
const UNDERLYINGS = ['BTC', 'ETH', 'SOL', 'XRP'] as const

const EM_DASH = '—'

function formatSpotPrice(price: number): string {
  // Match the existing BTC tile's style: no decimals at BTC/ETH scale,
  // 2 decimals at SOL scale, 4 decimals at XRP/sub-$10 scale.
  if (price >= 1000) {
    return `$${price.toLocaleString(undefined, { maximumFractionDigits: 0 })}`
  }
  if (price >= 10) {
    return `$${price.toFixed(2)}`
  }
  return `$${price.toFixed(4)}`
}

function formatBankShort(value: number): string {
  const abs = Math.abs(value)
  if (abs >= 1000) return `$${(value / 1000).toFixed(1)}K`
  return `$${value.toFixed(0)}`
}

function formatPnlShort(value: number): string {
  const abs = Math.abs(value)
  const sign = value > 0 ? '+' : value < 0 ? '-' : ''
  if (abs >= 1000) return `${sign}$${(abs / 1000).toFixed(1)}K`
  return `${sign}$${abs.toFixed(0)}`
}

function AssetTile({ data, underlying }: { data?: DashboardData; underlying: string }) {
  const price = data ? getPriceFor(data, underlying) : null
  const micro = data ? getMicrostructureFor(data, underlying) : null
  // Extrapolation of 15m momentum out to 24h is what the old BTC tile did.
  // It's a proxy, not a real change_24h — but it gives directional color.
  const change = micro ? micro.momentum_15m * 96 : null

  return (
    <div className="flex items-center gap-1 px-1.5">
      <span className="text-[9px] font-bold text-neutral-500 uppercase tracking-wider">
        {underlying}
      </span>
      <span className="text-xs font-semibold tabular-nums text-neutral-100">
        {price != null ? formatSpotPrice(price) : EM_DASH}
      </span>
      {change != null && (
        <span
          className={`text-[9px] tabular-nums ${
            change >= 0 ? 'text-green-500' : 'text-red-500'
          }`}
        >
          {change >= 0 ? '+' : ''}
          {change.toFixed(2)}%
        </span>
      )}
    </div>
  )
}

function GroupLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[9px] text-neutral-500 uppercase tracking-wider leading-none mb-0.5">
      {children}
    </div>
  )
}

function PortfolioGroup({ data }: { data?: DashboardData }) {
  const bank = data ? getCombinedBankroll(data) : null
  const pnl = data ? getCombinedPnl(data) : null
  const pnlColor =
    pnl == null || pnl === 0
      ? 'text-neutral-500'
      : pnl > 0
      ? 'text-green-500 glow-green'
      : 'text-red-500 glow-red'

  return (
    <div className="flex flex-col justify-center min-w-0">
      <GroupLabel>Portfolio</GroupLabel>
      <div className="flex items-baseline gap-1.5 leading-none">
        <span className="text-sm font-bold tabular-nums text-neutral-100">
          {bank != null ? formatBankShort(bank) : EM_DASH}
        </span>
        <span className={`text-[10px] tabular-nums ${pnlColor}`}>
          {pnl != null ? formatPnlShort(pnl) : EM_DASH}
        </span>
      </div>
    </div>
  )
}

function TechnicalGroup({ data }: { data?: DashboardData }) {
  const tech = data ? getCryptoTechStats(data) : null
  // When the shared bankroll already reflects realized PnL (crypto_tech's
  // case per D2 helper), we surface realized_bankroll + the 24h slice and
  // cumulative win rate. Graceful '—' when per_strategy_stats is empty.
  const bank = tech?.realized_bankroll ?? null
  const pnl24 = tech?.pnl_24h ?? null
  const winRate = tech?.win_rate ?? null
  const trades = tech?.total_trades ?? null

  const pnl24Color =
    pnl24 == null || pnl24 === 0
      ? 'text-neutral-500'
      : pnl24 > 0
      ? 'text-green-500'
      : 'text-red-500'
  const wrColor =
    winRate == null
      ? 'text-neutral-600'
      : winRate >= 0.55
      ? 'text-green-500'
      : winRate >= 0.45
      ? 'text-yellow-500'
      : 'text-red-500'

  return (
    <div className="flex flex-col justify-center min-w-0">
      <GroupLabel>Technical</GroupLabel>
      <div className="flex items-baseline gap-1.5 leading-none">
        <span className="text-sm font-semibold tabular-nums text-neutral-100">
          {bank != null ? formatBankShort(bank) : EM_DASH}
        </span>
        <span className={`text-[10px] tabular-nums ${pnl24Color}`}>
          {pnl24 != null ? formatPnlShort(pnl24) : EM_DASH}
          <span className="text-neutral-600 ml-0.5">24h</span>
        </span>
        <span className={`text-[10px] tabular-nums ${wrColor}`}>
          {winRate != null ? `${Math.round(winRate * 100)}%W` : EM_DASH}
        </span>
        <span className="text-[10px] tabular-nums text-neutral-600">
          {trades != null ? `${trades}T` : EM_DASH}
        </span>
      </div>
    </div>
  )
}

function McGroup({ data }: { data?: DashboardData }) {
  const mc = data ? getMcStats(data) : null
  const pilot = data ? getMcPilotStatus(data) : null
  const openCount = data?.mc_portfolio?.open_positions.length ?? 0

  // IDLE state: the MC brain has never taken a trade. Show a single muted
  // label instead of a row of zeros so it's visually distinct from a run
  // that happens to be flat.
  const isIdle = mc != null && mc.total_trades === 0

  const pnlColor =
    pilot == null || pilot.pnl === 0
      ? 'text-neutral-500'
      : pilot.pnl > 0
      ? 'text-green-500'
      : 'text-red-500'

  return (
    <div className="flex flex-col justify-center min-w-0">
      <GroupLabel>MC Pilot</GroupLabel>
      {isIdle ? (
        <div className="text-sm font-medium text-neutral-600 tabular-nums leading-none">
          IDLE
        </div>
      ) : (
        <div className="flex items-baseline gap-1.5 leading-none">
          <span className="text-sm font-semibold tabular-nums text-neutral-100">
            {pilot != null ? formatBankShort(pilot.realized) : EM_DASH}
          </span>
          <span className={`text-[10px] tabular-nums ${pnlColor}`}>
            {pilot != null ? formatPnlShort(pilot.pnl) : EM_DASH}
          </span>
          <span
            className={`text-[10px] uppercase tracking-wider tabular-nums ${
              openCount > 0 ? 'text-amber-400' : 'text-neutral-600'
            }`}
          >
            {openCount} Open
          </span>
        </div>
      )}
    </div>
  )
}

function PendingBadge({ data }: { data?: DashboardData }) {
  const pending = data ? getTotalPendingTrades(data) : null
  const alert = pending != null && pending > 10
  return (
    <div
      className={`flex items-center gap-1 px-1.5 py-0.5 border shrink-0 ${
        alert
          ? 'border-amber-500/30 bg-amber-500/5 text-amber-400'
          : 'border-neutral-800 text-neutral-500'
      }`}
    >
      <span className="text-[9px] uppercase tracking-wider">Pending</span>
      <span className="text-xs font-semibold tabular-nums">
        {pending != null ? pending : EM_DASH}
      </span>
      {/* Optional per-asset stats peek: total settled across crypto_tech
          assets — a quick sanity dot that the aggregation is alive. Keeps
          the badge tight; no breakdown here. */}
      {data && getAllAssetStats(data).length > 0 && (
        <span className="w-1 h-1 rounded-full bg-green-500/60" />
      )}
    </div>
  )
}

export function PortfolioHeader({ data }: Props) {
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className="flex-1 flex items-center gap-2 min-w-0 overflow-hidden"
    >
      {/* Asset prices strip — compact status bar, not a detail panel. */}
      <div className="flex items-center divide-x divide-neutral-800 shrink-0">
        {UNDERLYINGS.map(u => (
          <AssetTile key={u} data={data} underlying={u} />
        ))}
      </div>

      {/* Elastic spacer so summary + pending hug the Scan button side. */}
      <div className="flex-1" />

      {/* Three strategy groups separated by hairline dividers. */}
      <div className="flex items-stretch gap-2 shrink-0">
        <PortfolioGroup data={data} />
        <div className="w-px bg-neutral-800 self-stretch" />
        <TechnicalGroup data={data} />
        <div className="w-px bg-neutral-800 self-stretch" />
        <McGroup data={data} />
      </div>

      <PendingBadge data={data} />
    </motion.div>
  )
}
