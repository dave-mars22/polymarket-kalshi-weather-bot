import { formatDistanceToNow } from 'date-fns'
import type {
  DashboardData,
  McOpenPosition,
  McPortfolioStatus,
  PerAssetStats,
  PerStrategyStats,
} from '../types'
import {
  getAllAssetStats,
  getCryptoTechStats,
  getMcPilotStatus,
  getMcStats,
  getOpenMcPositions,
} from '../selectors/dashboardSelectors'
import { formatCurrency } from '../utils'

interface Props {
  data?: DashboardData
}

const EM_DASH = '—'
const CRYPTO_TECH_UNDERLYINGS = ['BTC', 'ETH', 'SOL', 'XRP'] as const

// Slice D6 fix: backend Pydantic datetimes serialize as naive ISO
// ("2026-04-24T17:38:01.875164", no 'Z' or offset). JavaScript's
// `new Date()` parses such strings as LOCAL time, which made the
// "last trade X ago" display drift by the user's UTC offset (4h on
// EDT). Normalizing to UTC here — append 'Z' when no timezone marker is
// present — keeps the age math accurate regardless of the viewer's tz.
function parseBackendDate(iso: string): Date {
  const hasTz = /Z|[+-]\d{2}:?\d{2}$/.test(iso)
  return new Date(hasTz ? iso : `${iso}Z`)
}

// A settled-trade floor below which win rate is too noisy to trust.
// Aligns with the backend's MIN_TRADES_FOR_CALIBRATION intent (100) but
// relaxed for display — we still want to show ~10-sample rates with a
// muted color instead of '—'.
const WIN_RATE_MIN_SAMPLE = 10

// -------------------------------------------------------------------
// Tiny layout primitives
// -------------------------------------------------------------------

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <span className="text-[10px] text-neutral-400 uppercase tracking-wider font-mono">
      {children}
    </span>
  )
}

function StatTile({
  label,
  value,
  valueColor = 'text-neutral-200',
  sub,
  subColor,
}: {
  label: string
  value: React.ReactNode
  valueColor?: string
  sub?: React.ReactNode
  subColor?: string
}) {
  return (
    <div className="flex flex-col gap-0.5 min-w-0">
      <span className="text-[9px] text-neutral-500 uppercase tracking-wider leading-none">
        {label}
      </span>
      <span className={`text-sm font-semibold tabular-nums leading-none ${valueColor}`}>
        {value}
      </span>
      {sub !== undefined && (
        <span className={`text-[9px] tabular-nums leading-none ${subColor ?? 'text-neutral-600'}`}>
          {sub}
        </span>
      )}
    </div>
  )
}

function CardShell({
  title,
  right,
  children,
}: {
  title: string
  right?: React.ReactNode
  children: React.ReactNode
}) {
  // Equivalent of index.css .card: bg-[#0a0a0a] + border-neutral-800. We
  // use Tailwind directly because every other component in this codebase
  // does, and keeping one convention beats introducing the CSS-class path.
  return (
    <div className="flex-1 min-w-0 flex flex-col bg-[#0a0a0a] border border-neutral-800">
      <div className="flex items-center justify-between px-2 py-1 border-b border-neutral-800">
        <SectionLabel>{title}</SectionLabel>
        <div className="flex items-center gap-1.5">{right}</div>
      </div>
      <div className="flex-1 flex flex-col gap-2 px-2 py-2">{children}</div>
    </div>
  )
}

function pnlColor(value: number | null | undefined): string {
  if (value == null || value === 0) return 'text-neutral-500'
  return value > 0 ? 'text-green-500' : 'text-red-500'
}

function pnlText(value: number | null | undefined): string {
  return value != null ? formatCurrency(value, true) : EM_DASH
}

// -------------------------------------------------------------------
// Technical card: per_strategy_stats[crypto_tech] + per_asset_stats
// -------------------------------------------------------------------

function TechnicalStatus({ hasActionable }: { hasActionable: boolean }) {
  return hasActionable ? (
    <>
      <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
      <span className="text-[9px] font-bold uppercase tracking-wider text-amber-400">
        Active
      </span>
    </>
  ) : (
    <span className="text-[9px] font-medium uppercase tracking-wider text-neutral-600">
      Idle
    </span>
  )
}

function AssetPill({ stat }: { stat: PerAssetStats }) {
  const isIdle = stat.total_trades === 0
  if (isIdle) {
    return (
      <div className="flex items-center gap-1 px-1.5 py-0.5 border border-neutral-800">
        <span className="text-[9px] font-bold text-neutral-600 uppercase tracking-wider">
          {stat.underlying}
        </span>
        <span className="text-[9px] text-neutral-700 uppercase">Idle</span>
      </div>
    )
  }
  return (
    <div className="flex items-center gap-1 px-1.5 py-0.5 border border-neutral-800">
      <span className="text-[9px] font-bold text-neutral-300 uppercase tracking-wider">
        {stat.underlying}
      </span>
      <span className={`text-[10px] tabular-nums ${pnlColor(stat.total_pnl)}`}>
        {pnlText(stat.total_pnl)}
      </span>
      <span className="text-[9px] tabular-nums text-neutral-500">
        {stat.wins}W/{stat.losses}L
      </span>
    </div>
  )
}

function TechnicalCard({ data }: { data?: DashboardData }) {
  const tech: PerStrategyStats | null = data ? getCryptoTechStats(data) : null
  const assetStats: PerAssetStats[] = data ? getAllAssetStats(data) : []

  // Pre-slot entries for every configured underlying so the pill row has a
  // stable column count even when one asset has never traded.
  const assetByUnderlying: Record<string, PerAssetStats | undefined> =
    Object.fromEntries(assetStats.map(s => [s.underlying.toUpperCase(), s]))
  const pillStats: PerAssetStats[] = CRYPTO_TECH_UNDERLYINGS.map(
    u =>
      assetByUnderlying[u] ?? {
        underlying: u,
        total_trades: 0,
        settled_trades: 0,
        pending_trades: 0,
        wins: 0,
        losses: 0,
        win_rate: null,
        total_pnl: 0,
        pnl_24h: 0,
        last_signal_time: null,
        last_trade_time: null,
      },
  )

  const hasActionable =
    (data?.active_signals ?? []).some(s => s.actionable) ?? false

  // Last activity across all underlyings; prefer most-recent settled trade.
  const lastTradeIso =
    assetStats
      .map(s => s.last_trade_time)
      .filter((t): t is string => !!t)
      .sort()
      .pop() ?? null
  const lastActivityText = lastTradeIso
    ? `${formatDistanceToNow(parseBackendDate(lastTradeIso))} ago`
    : 'no activity'

  const winRateDisplay = (() => {
    if (tech == null || tech.win_rate == null) return EM_DASH
    if (tech.settled_trades < WIN_RATE_MIN_SAMPLE) {
      // Too small a sample to trust; show but mute.
      return `${Math.round(tech.win_rate * 100)}%`
    }
    return `${Math.round(tech.win_rate * 100)}%`
  })()
  const winRateColor =
    tech == null || tech.win_rate == null
      ? 'text-neutral-500'
      : tech.settled_trades < WIN_RATE_MIN_SAMPLE
      ? 'text-neutral-500'
      : tech.win_rate >= 0.55
      ? 'text-green-500'
      : tech.win_rate >= 0.45
      ? 'text-yellow-500'
      : 'text-red-500'

  return (
    <CardShell
      title="Multi-Crypto Technical"
      right={<TechnicalStatus hasActionable={hasActionable} />}
    >
      <div className="flex items-start gap-4 flex-wrap">
        <StatTile
          label="Realized"
          value={tech != null ? formatCurrency(tech.realized_bankroll) : EM_DASH}
        />
        <StatTile
          label="24h PnL"
          value={pnlText(tech?.pnl_24h)}
          valueColor={pnlColor(tech?.pnl_24h)}
        />
        <StatTile
          label="Win Rate"
          value={<span className={winRateColor}>{winRateDisplay}</span>}
          sub={
            tech != null
              ? tech.settled_trades < WIN_RATE_MIN_SAMPLE
                ? `low sample · ${tech.settled_trades}`
                : `${tech.wins}W / ${tech.losses}L`
              : EM_DASH
          }
        />
        <StatTile
          label="Trades"
          value={
            tech != null ? `${tech.settled_trades} / ${tech.total_trades}` : EM_DASH
          }
          sub={
            tech != null && tech.pending_trades > 0
              ? `${tech.pending_trades} pending`
              : undefined
          }
          subColor={
            tech != null && tech.pending_trades > 0
              ? 'text-amber-400'
              : 'text-neutral-600'
          }
        />
      </div>

      <div className="flex items-center gap-1 flex-wrap">
        {pillStats.map(stat => (
          <AssetPill key={stat.underlying} stat={stat} />
        ))}
      </div>

      <div className="mt-auto text-[9px] text-neutral-600 uppercase tracking-wider">
        Last trade: {lastActivityText}
      </div>
    </CardShell>
  )
}

// -------------------------------------------------------------------
// MC barrier card: mc_portfolio + per_strategy_stats[monte_carlo]
// -------------------------------------------------------------------

function McStatus({ openCount }: { openCount: number }) {
  if (openCount <= 0) {
    return (
      <span className="text-[9px] font-medium uppercase tracking-wider text-neutral-600">
        Idle
      </span>
    )
  }
  return (
    <>
      <span className="w-1.5 h-1.5 rounded-full bg-amber-400" />
      <span className="text-[9px] font-bold uppercase tracking-wider text-amber-400 tabular-nums">
        {openCount} Open
      </span>
    </>
  )
}

function stripKalshiPrefix(ticker: string): string {
  // Per slice spec: just strip 'KXBTC' if present. Leaves the strike/date
  // tail in place so it remains uniquely identifiable.
  return ticker.startsWith('KXBTC') ? ticker.slice(5) : ticker
}

function formatSettlementCountdown(iso: string | null): string {
  if (!iso) return 'settlement unknown'
  const then = parseBackendDate(iso).getTime()
  if (Number.isNaN(then)) return 'settlement unknown'
  const diffMs = then - Date.now()
  if (diffMs <= 0) return 'settling'
  const days = Math.floor(diffMs / 86_400_000)
  const hours = Math.floor((diffMs % 86_400_000) / 3_600_000)
  if (days >= 1) return `settles in ${days}d ${hours}h`
  const minutes = Math.floor(diffMs / 60_000)
  if (hours >= 1) return `settles in ${hours}h ${minutes % 60}m`
  return `settles in ${minutes}m`
}

function OpenPositionRow({ pos }: { pos: McOpenPosition }) {
  const dirColor =
    pos.direction.toLowerCase() === 'yes' ? 'text-blue-400' : 'text-red-400'
  return (
    <div className="flex items-center gap-2 text-[10px] tabular-nums border-b border-neutral-800/50 py-0.5">
      <span
        className="truncate text-neutral-300 max-w-[150px]"
        title={pos.market_ticker}
      >
        {stripKalshiPrefix(pos.market_ticker)}
      </span>
      <span className={`font-bold uppercase ${dirColor}`}>{pos.direction}</span>
      <span className="text-neutral-500">@ {(pos.entry_price * 100).toFixed(0)}¢</span>
      <span className="text-neutral-500">${pos.size.toFixed(0)}</span>
      <span className="text-neutral-500">
        model {Math.round(pos.model_probability * 100)}%
      </span>
      <span className="ml-auto text-[9px] text-neutral-600 uppercase tracking-wider">
        {formatSettlementCountdown(pos.expected_settlement)}
      </span>
    </div>
  )
}

function formatNextScan(iso: string | null | undefined): string {
  if (!iso) return 'scan due'
  const then = parseBackendDate(iso).getTime()
  if (Number.isNaN(then)) return 'scan due'
  const diffMs = then - Date.now()
  if (diffMs <= 0) return 'scan due'
  const minutes = Math.round(diffMs / 60_000)
  if (minutes < 1) return 'next scan in <1 min'
  return `next scan in ${minutes} min`
}

function McBarrierCard({ data }: { data?: DashboardData }) {
  const mc: PerStrategyStats | null = data ? getMcStats(data) : null
  const pilot = data ? getMcPilotStatus(data) : null
  const portfolio: McPortfolioStatus | null = data?.mc_portfolio ?? null
  const openPositions: McOpenPosition[] = data ? getOpenMcPositions(data) : []
  const displayed = openPositions.slice(0, 5)
  const overflow = Math.max(0, openPositions.length - displayed.length)

  return (
    <CardShell
      title="Monte Carlo Barrier"
      right={<McStatus openCount={openPositions.length} />}
    >
      <div className="flex items-start gap-4 flex-wrap">
        <StatTile
          label="Pilot Bankroll"
          value={pilot != null ? formatCurrency(pilot.realized) : EM_DASH}
          sub={
            pilot != null ? `target ${formatCurrency(pilot.target)}` : undefined
          }
        />
        <StatTile
          label="Total PnL"
          value={pnlText(mc?.total_pnl)}
          valueColor={pnlColor(mc?.total_pnl)}
          sub={
            mc != null && mc.pending_trades > 0
              ? `${mc.pending_trades} pending`
              : undefined
          }
          subColor={
            mc != null && mc.pending_trades > 0
              ? 'text-amber-400'
              : 'text-neutral-600'
          }
        />
        <StatTile
          label="Signals 24h"
          value={
            portfolio != null ? portfolio.signals_last_24h.toLocaleString() : EM_DASH
          }
        />
        <StatTile
          label="Actionable 24h"
          value={
            portfolio != null
              ? portfolio.actionable_signals_last_24h.toLocaleString()
              : EM_DASH
          }
          valueColor={
            portfolio != null && portfolio.actionable_signals_last_24h > 0
              ? 'text-amber-400'
              : 'text-neutral-200'
          }
        />
      </div>

      <div className="flex flex-col min-w-0">
        {displayed.length === 0 ? (
          <div className="text-[10px] uppercase tracking-wider text-neutral-600 py-1">
            No open positions
          </div>
        ) : (
          <>
            {displayed.map(pos => (
              <OpenPositionRow key={pos.trade_id} pos={pos} />
            ))}
            {overflow > 0 && (
              <div className="text-[9px] text-neutral-600 uppercase tracking-wider pt-0.5">
                +{overflow} more
              </div>
            )}
          </>
        )}
      </div>

      <div className="mt-auto text-[9px] text-neutral-600 uppercase tracking-wider">
        {formatNextScan(portfolio?.next_scheduled_scan)}
      </div>
    </CardShell>
  )
}

// -------------------------------------------------------------------
// Public entry point
// -------------------------------------------------------------------

export function StrategyCards({ data }: Props) {
  return (
    <div className="flex items-stretch gap-2">
      <TechnicalCard data={data} />
      <McBarrierCard data={data} />
    </div>
  )
}
