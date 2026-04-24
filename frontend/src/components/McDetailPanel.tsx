import type { DashboardData, McOpenPosition } from '../types'
import {
  getMcPositionsBySeries,
  getMcSettledCount,
  getNextMcSettlement,
  getOpenMcPositions,
} from '../selectors/dashboardSelectors'
import { formatCurrency } from '../utils'

interface Props {
  data?: DashboardData
}

const EM_DASH = '—'

// Strip the familiar KXBTC series prefix so the ticker tail is readable
// in a narrow column. Leaves anything that doesn't start with KXBTC
// unchanged (e.g., KXINX-*, future non-BTC barriers).
function stripKalshiPrefix(ticker: string): string {
  return ticker.startsWith('KXBTC') ? ticker.slice(5) : ticker
}

// Duration formatter. Returns "Nd Yh" for >= 1 day, "Nh Mm" for >= 1 hour,
// "Nm" for minute-scale, "now" for <= 0.
function formatRemaining(ms: number): string {
  if (ms <= 0) return 'now'
  const days = Math.floor(ms / 86_400_000)
  const hours = Math.floor((ms % 86_400_000) / 3_600_000)
  const minutes = Math.floor((ms % 3_600_000) / 60_000)
  if (days >= 1) return `${days}d ${hours}h`
  if (hours >= 1) return `${hours}h ${minutes}m`
  return `${minutes}m`
}

function formatCountdown(iso: string | null): string {
  if (!iso) return 'unknown'
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return 'unknown'
  return formatRemaining(t - Date.now())
}

// ----------------------------------------------------------------
// Section wrappers — each has a small title row and a body, styled
// to match the surrounding panels (bg-[#0a0a0a], hairline borders).
// ----------------------------------------------------------------

function SectionFrame({
  title,
  right,
  children,
  className = '',
}: {
  title: string
  right?: React.ReactNode
  children: React.ReactNode
  className?: string
}) {
  return (
    <div className={`flex flex-col min-w-0 ${className}`}>
      <div className="flex items-center justify-between border-b border-neutral-800 px-2 py-0.5">
        <span className="text-[10px] text-neutral-500 uppercase tracking-wider font-mono">
          {title}
        </span>
        {right && <div className="flex items-center gap-1">{right}</div>}
      </div>
      <div className="flex-1 px-2 py-1 min-w-0">{children}</div>
    </div>
  )
}

// ----------------------------------------------------------------
// Section 1 — Settlement timeline
// ----------------------------------------------------------------

function SettlementTimeline({ data }: { data?: DashboardData }) {
  const opens = data ? getOpenMcPositions(data) : []
  const nextSettlement = data ? getNextMcSettlement(data) : null

  // Sort soonest-first. Positions without a parseable expected_settlement
  // go to the bottom so actionable items stay at the top.
  const sorted = [...opens].sort((a, b) => {
    const at = a.expected_settlement ? new Date(a.expected_settlement).getTime() : Infinity
    const bt = b.expected_settlement ? new Date(b.expected_settlement).getTime() : Infinity
    return at - bt
  })

  const headline = (() => {
    if (opens.length === 0) return 'NO OPEN POSITIONS'
    if (!nextSettlement) return 'SETTLEMENT DATE UNKNOWN'
    const remaining = nextSettlement.getTime() - Date.now()
    return `FIRST SETTLEMENT IN ${formatRemaining(remaining).toUpperCase()}`
  })()

  return (
    <SectionFrame
      title="Settlement Timeline"
      className="w-[35%] border-r border-neutral-800"
    >
      <div className="text-[11px] font-bold text-amber-400 uppercase tracking-wider tabular-nums mb-1.5">
        {headline}
      </div>
      {opens.length === 0 ? (
        <div className="text-[10px] text-neutral-600 uppercase tracking-wider py-1">
          {EM_DASH}
        </div>
      ) : (
        <div className="flex flex-col gap-0.5">
          {sorted.map(pos => (
            <PositionRow key={pos.trade_id} pos={pos} />
          ))}
        </div>
      )}
    </SectionFrame>
  )
}

function PositionRow({ pos }: { pos: McOpenPosition }) {
  const dirColor =
    pos.direction.toLowerCase() === 'yes' ? 'text-blue-400' : 'text-red-400'
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_36px_40px_40px_52px] items-center gap-1 text-[10px] tabular-nums border-b border-neutral-800/50 py-0.5">
      <span
        className="truncate text-neutral-300"
        title={pos.market_ticker}
      >
        {stripKalshiPrefix(pos.market_ticker)}
      </span>
      <span className={`font-bold uppercase text-right ${dirColor}`}>
        {pos.direction}
      </span>
      <span className="text-neutral-500 text-right">
        {(pos.entry_price * 100).toFixed(0)}¢
      </span>
      <span className="text-neutral-500 text-right">
        ${pos.size.toFixed(0)}
      </span>
      <span className="text-[9px] text-neutral-600 uppercase tracking-wider text-right">
        {formatCountdown(pos.expected_settlement)}
      </span>
    </div>
  )
}

// ----------------------------------------------------------------
// Section 2 — Per-series breakdown
// ----------------------------------------------------------------

function PerSeriesBreakdown({ data }: { data?: DashboardData }) {
  const bySeries = data ? getMcPositionsBySeries(data) : {}
  const series = Object.entries(bySeries)

  return (
    <SectionFrame
      title="Per-Series Exposure"
      className="w-[25%] border-r border-neutral-800"
    >
      {series.length === 0 ? (
        <div className="text-[10px] text-neutral-600 uppercase tracking-wider py-1">
          No series exposure
        </div>
      ) : (
        <div className="flex flex-col gap-1.5">
          {series.map(([key, positions]) => {
            const positionCount = positions.length
            const invested = positions.reduce((sum, p) => sum + p.size, 0)
            // Potential payout per share = $1; shares = size / entry_price.
            // Summed across positions gives the "if everything wins" ceiling.
            const potential = positions.reduce(
              (sum, p) => sum + (p.entry_price > 0 ? p.size / p.entry_price : 0),
              0,
            )
            return (
              <div key={key} className="flex flex-col gap-0.5 min-w-0">
                <div
                  className="text-[10px] font-bold text-neutral-300 uppercase tracking-wider truncate"
                  title={key}
                >
                  {key}
                </div>
                <div className="flex flex-col gap-0 text-[10px] tabular-nums">
                  <div className="flex justify-between">
                    <span className="text-neutral-500">Positions</span>
                    <span className="text-neutral-200">{positionCount}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-neutral-500">Invested</span>
                    <span className="text-neutral-200">{formatCurrency(invested)}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-neutral-500">Max payout</span>
                    <span className="text-green-500">{formatCurrency(potential)}</span>
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </SectionFrame>
  )
}

// ----------------------------------------------------------------
// Section 3 — Calibration scaffold
// ----------------------------------------------------------------

function CalibrationScaffold({ data }: { data?: DashboardData }) {
  const settled = data ? getMcSettledCount(data) : 0

  return (
    <SectionFrame
      title="Calibration"
      right={
        <span className="text-[9px] text-neutral-600 tabular-nums">
          {settled} settled
        </span>
      }
      className="w-[40%]"
    >
      {settled === 0 ? (
        <div className="flex flex-col gap-1 text-[10px] text-neutral-500 leading-relaxed max-w-[440px]">
          <p>
            Model calibration will populate once MC contracts settle
            (first KXBTCMAXMON-26APR30 resolves on Apr&nbsp;30).
          </p>
          <p className="text-neutral-600">
            Each settled trade updates the probability-to-outcome mapping
            below — buckets 0-20% / 20-40% / 40-60% / 60-80% / 80-100% vs
            realized win rate, plus a Brier score for the MC predictions.
          </p>
        </div>
      ) : (
        // When settlements arrive this block will render the full table.
        // Keeping the placeholder path isolated so swapping in the
        // populated view is a single-component change.
        <CalibrationPopulated settled={settled} />
      )}
    </SectionFrame>
  )
}

// Rendered once at least one MC trade has settled. Per-bucket data is
// still fetched lazily (not in /api/dashboard today); this shell gives a
// counter and a hint so the user knows the path is live.
function CalibrationPopulated({ settled }: { settled: number }) {
  return (
    <div className="flex flex-col gap-1 text-[10px] text-neutral-500 leading-relaxed">
      <p className="text-neutral-400">
        {settled} MC trade{settled === 1 ? '' : 's'} settled. Calibration
        table populates here.
      </p>
      <p className="text-neutral-600">
        (Populated bucket data lands in a follow-up slice after Apr 30.)
      </p>
    </div>
  )
}

// ----------------------------------------------------------------
// Public entry
// ----------------------------------------------------------------

export function McDetailPanel({ data }: Props) {
  // If the backend never returned mc_portfolio, render a minimal empty
  // state instead of three half-filled sections.
  if (data && !data.mc_portfolio) {
    return (
      <div className="bg-[#0a0a0a] border border-neutral-800 px-2 py-2">
        <span className="text-[10px] text-neutral-500 uppercase tracking-wider">
          MC brain inactive
        </span>
      </div>
    )
  }

  return (
    <div className="flex items-stretch bg-[#0a0a0a] border border-neutral-800 min-h-[100px]">
      <SettlementTimeline data={data} />
      <PerSeriesBreakdown data={data} />
      <CalibrationScaffold data={data} />
    </div>
  )
}
