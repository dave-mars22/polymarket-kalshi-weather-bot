import type { DashboardData, Microstructure } from '../types'
import {
  getMicrostructureFor,
  getPriceFor,
} from '../selectors/dashboardSelectors'

interface Props {
  data?: DashboardData
}

const UNDERLYINGS = ['BTC', 'ETH', 'SOL', 'XRP'] as const

const EM_DASH = '—'

function formatSpotPrice(price: number): string {
  if (price >= 1000) {
    return `$${price.toLocaleString(undefined, { maximumFractionDigits: 0 })}`
  }
  if (price >= 10) {
    return `$${price.toFixed(2)}`
  }
  return `$${price.toFixed(4)}`
}

function rsiColor(rsi: number): string {
  if (rsi < 30) return 'text-green-500'
  if (rsi > 70) return 'text-red-500'
  return 'text-neutral-400'
}

// Indicator rendered when a tile's microstructure is missing entirely
// (fetch failure for that underlying). Keeps the row visible so the
// column count stays stable; all metric cells become em-dashes.
function MissingRow({ underlying }: { underlying: string }) {
  return (
    <div className="grid grid-cols-[36px_minmax(0,1fr)_48px_60px_52px] items-center gap-1 px-1.5 py-1 border-b border-neutral-800/60 last:border-b-0">
      <span className="text-[10px] font-bold text-neutral-600 uppercase tracking-wider">
        {underlying}
      </span>
      <span className="text-xs text-neutral-600 tabular-nums">{EM_DASH}</span>
      <span className="text-[10px] text-neutral-700 tabular-nums text-right">{EM_DASH}</span>
      <span className="text-[10px] text-neutral-700 tabular-nums text-right">{EM_DASH}</span>
      <span className="text-[9px] text-neutral-700 tabular-nums text-right">{EM_DASH}</span>
    </div>
  )
}

function AssetRow({
  underlying,
  price,
  micro,
}: {
  underlying: string
  price: number | null
  micro: Microstructure | null
}) {
  if (!micro) {
    return <MissingRow underlying={underlying} />
  }

  // momentum_5m and volatility are stored as percentages (e.g. 0.12 = 0.12%).
  // Existing MicrostructurePanel uses the same unit; we mirror that so the
  // numeric scale is familiar to anyone cross-checking the old panel.
  const momColor =
    micro.momentum_5m === 0
      ? 'text-neutral-500'
      : micro.momentum_5m > 0
      ? 'text-green-500'
      : 'text-red-500'
  const momSign = micro.momentum_5m >= 0 ? '+' : ''

  return (
    <div className="grid grid-cols-[36px_minmax(0,1fr)_48px_60px_52px] items-center gap-1 px-1.5 py-1 border-b border-neutral-800/60 last:border-b-0">
      <span className="text-[10px] font-bold text-neutral-300 uppercase tracking-wider">
        {underlying}
      </span>
      <span className="text-xs font-semibold tabular-nums text-neutral-100 truncate">
        {price != null ? formatSpotPrice(price) : formatSpotPrice(micro.price)}
      </span>
      <span
        className={`text-[10px] tabular-nums text-right ${rsiColor(micro.rsi)}`}
        title={`RSI 14-period: ${micro.rsi.toFixed(1)}`}
      >
        RSI {micro.rsi.toFixed(0)}
      </span>
      <span
        className={`text-[10px] tabular-nums text-right ${momColor}`}
        title="5-minute momentum"
      >
        {momSign}
        {micro.momentum_5m.toFixed(2)}%
      </span>
      <span
        className="text-[9px] tabular-nums text-right text-neutral-600"
        title="1-minute return stdev (last 30 candles)"
      >
        σ {micro.volatility.toFixed(3)}
      </span>
    </div>
  )
}

// Picks a canonical source label. If every tile reports the same source
// (common case: all 'coinbase'), show that. Mixed sources mean at least one
// adapter failed over — show 'multi' so the user knows the label isn't a
// single authoritative value.
function resolveSource(micros: Array<Microstructure | null>): string {
  const sources = new Set(
    micros.filter((m): m is Microstructure => m != null).map(m => m.source),
  )
  if (sources.size === 0) return 'unknown'
  if (sources.size === 1) return Array.from(sources)[0]
  return 'multi'
}

export function MultiAssetMicrostructure({ data }: Props) {
  // Collect every tile's micro + price up front so the header can pick a
  // canonical source from the same set the rows render.
  const tiles = UNDERLYINGS.map(u => ({
    underlying: u,
    price: data ? getPriceFor(data, u) : null,
    micro: data ? getMicrostructureFor(data, u) : null,
  }))
  const source = resolveSource(tiles.map(t => t.micro))

  return (
    <div>
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-[10px] text-neutral-500 uppercase tracking-wider">
          Microstructure
        </span>
        <span className="text-[9px] text-neutral-600 tabular-nums">{source}</span>
      </div>
      <div>
        {tiles.map(t => (
          <AssetRow
            key={t.underlying}
            underlying={t.underlying}
            price={t.price}
            micro={t.micro}
          />
        ))}
      </div>
    </div>
  )
}
