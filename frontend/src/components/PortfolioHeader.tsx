import { motion } from 'framer-motion'
import type { DashboardData } from '../types'
import {
  getAllAssetStats,
  getCombinedBankroll,
  getCombinedPnl,
  getCryptoTechStats,
  getMcPilotStatus,
  getMcStats,
  getPriceFor,
  getTotalPendingTrades,
} from '../selectors/dashboardSelectors'
import { formatCurrency } from '../utils'

interface Props {
  data?: DashboardData
}

// Order matches settings.CRYPTO_TECH_UNDERLYINGS. Kept inline rather than
// pulled from the payload so the strip shows a stable column set even when
// one underlying's microstructure fetch fails.
const UNDERLYINGS = ['BTC', 'ETH', 'SOL', 'XRP'] as const

const EM_DASH = '—'

function formatSpotPrice(price: number): string {
  // No decimals at BTC/ETH scale; 2 decimals at SOL scale; 4 at XRP scale.
  if (price >= 1000) {
    return `$${price.toLocaleString(undefined, { maximumFractionDigits: 0 })}`
  }
  if (price >= 10) {
    return `$${price.toFixed(2)}`
  }
  return `$${price.toFixed(4)}`
}

// Slice D4.5: removed the change-% column. The previous implementation
// extrapolated momentum_15m by ×96 into a "24h change" that was a noisy
// directional proxy, not a real 24h number — it often showed every asset
// down double-digits at the same time. Until the backend carries a real
// change_24h per underlying, we just show the spot. Absent > misleading.
function AssetTile({ data, underlying }: { data?: DashboardData; underlying: string }) {
  const price = data ? getPriceFor(data, underlying) : null
  return (
    <div className="flex items-center gap-1 px-1.5">
      <span className="text-[9px] font-bold text-neutral-500 uppercase tracking-wider">
        {underlying}
      </span>
      <span className="text-xs font-semibold tabular-nums text-neutral-100">
        {price != null ? formatSpotPrice(price) : EM_DASH}
      </span>
    </div>
  )
}

function GroupLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[9px] text-neutral-500 uppercase tracking-wider leading-none">
      {children}
    </div>
  )
}

function BigNumber({
  children,
  className = '',
}: {
  children: React.ReactNode
  className?: string
}) {
  return (
    <div className={`text-sm font-semibold tabular-nums leading-none ${className}`}>
      {children}
    </div>
  )
}

// Visible hairline pipe between secondary metrics. Matches the inline
// "muted divider" aesthetic used elsewhere (.terminal, .scan-line etc).
function MetricPipe() {
  return <span className="text-neutral-700 text-[10px]">|</span>
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
    <div className="flex flex-col justify-center gap-0.5 min-w-0">
      <GroupLabel>Portfolio</GroupLabel>
      <BigNumber className="text-neutral-100">
        {bank != null ? formatCurrency(bank) : EM_DASH}
      </BigNumber>
      <div className={`text-[10px] tabular-nums leading-none ${pnlColor}`}>
        {pnl != null ? formatCurrency(pnl, true) : EM_DASH}
      </div>
    </div>
  )
}

function TechnicalGroup({ data }: { data?: DashboardData }) {
  const tech = data ? getCryptoTechStats(data) : null
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
    <div className="flex flex-col justify-center gap-0.5 min-w-0">
      <GroupLabel>Technical</GroupLabel>
      <BigNumber className="text-neutral-100">
        {bank != null ? formatCurrency(bank) : EM_DASH}
      </BigNumber>
      <div className="flex items-center gap-1.5 text-[10px] tabular-nums leading-none">
        <span className={pnl24Color}>
          24h {pnl24 != null ? formatCurrency(pnl24, true) : EM_DASH}
        </span>
        <MetricPipe />
        <span className={wrColor}>
          W {winRate != null ? `${Math.round(winRate * 100)}%` : EM_DASH}
        </span>
        <MetricPipe />
        <span className="text-neutral-500">
          N {trades != null ? trades : EM_DASH}
        </span>
      </div>
    </div>
  )
}

function McGroup({ data }: { data?: DashboardData }) {
  const mc = data ? getMcStats(data) : null
  const pilot = data ? getMcPilotStatus(data) : null
  const openCount = data?.mc_portfolio?.open_positions.length ?? 0

  // IDLE = the MC brain has never taken a trade. Distinct from a run that
  // happens to be flat — show a single muted label, not a row of zeros.
  const isIdle = mc != null && mc.total_trades === 0

  const pnlColor =
    pilot == null || pilot.pnl === 0
      ? 'text-neutral-500'
      : pilot.pnl > 0
      ? 'text-green-500'
      : 'text-red-500'
  const openColor = openCount > 0 ? 'text-amber-400' : 'text-neutral-600'

  return (
    <div className="flex flex-col justify-center gap-0.5 min-w-0">
      <GroupLabel>MC Pilot</GroupLabel>
      {isIdle ? (
        <BigNumber className="text-neutral-600">IDLE</BigNumber>
      ) : (
        <>
          <BigNumber className="text-neutral-100">
            {pilot != null ? formatCurrency(pilot.realized) : EM_DASH}
          </BigNumber>
          <div className="flex items-center gap-1.5 text-[10px] tabular-nums leading-none">
            <span className={pnlColor}>
              PnL {pilot != null ? formatCurrency(pilot.pnl, true) : EM_DASH}
            </span>
            <MetricPipe />
            <span className={`${openColor} uppercase tracking-wider`}>
              {openCount} Open
            </span>
          </div>
        </>
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
      <div className="flex items-stretch gap-3 shrink-0">
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
