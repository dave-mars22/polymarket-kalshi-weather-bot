import { ArrowUpDown, ArrowUp, ArrowDown } from 'lucide-react'
import { useState, useMemo } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import type { Signal } from '../types'
import { platformStyles } from '../utils'
import {
  TableFilterChips,
  type TableFilter,
} from './TableFilterChips'

interface Props {
  signals: Signal[]
  onSimulateTrade: (ticker: string) => void
  isSimulating: boolean
}

type SortKey = 'edge' | 'model_probability' | 'suggested_size'
type SortDir = 'asc' | 'desc'

interface UnifiedSignal {
  key: string
  ticker: string
  title: string
  platform: string
  direction: string
  edge: number
  modelProb: number
  marketProb: number
  confidence: number
  suggestedSize: number
  reasoning: string
  actionable: boolean
  // Slice D7: attribution for the ASSET column + chip filter. isMc is
  // set when the signal carries a contract_style (the MC brain's distinct
  // marker — European / one_touch_above / one_touch_below). In practice
  // /api/dashboard.active_signals contains only crypto_tech signals, so
  // the MC chip shows 0 here; the filter is future-proofed for when MC
  // signals eventually flow through the same shape.
  underlyingAsset: string
  isMc: boolean
}

// Muted per-asset color palette for the ASSET column. Scannability over
// decoration — one hue per asset so the eye can separate rows without
// reading text.
const ASSET_COLOR: Record<string, string> = {
  BTC: 'text-amber-400',
  ETH: 'text-blue-400',
  SOL: 'text-purple-400',
  XRP: 'text-cyan-400',
  MC: 'text-pink-400',
}

function AssetToken({ underlying, isMc }: { underlying: string; isMc: boolean }) {
  const label = isMc ? 'MC' : underlying.toUpperCase()
  const color = ASSET_COLOR[label] ?? 'text-neutral-400'
  return (
    <span className={`text-[10px] font-bold uppercase tracking-wider tabular-nums ${color}`}>
      {label || '—'}
    </span>
  )
}

function PlatformBadge({ platform }: { platform: string }) {
  const style = platformStyles[platform.toLowerCase()]
  if (!style) return null
  return (
    <span className={`platform-badge ${style.badge}`}>
      {style.icon}
    </span>
  )
}

function EdgeBar({ edge }: { edge: number }) {
  const absEdge = Math.abs(edge) * 100
  const width = Math.min(100, absEdge * 5)
  const color = edge > 0.05 ? '#22c55e' : edge > 0 ? '#22c55e80' : '#dc2626'
  return (
    <div className="edge-bar">
      <div className="edge-fill" style={{ width: `${width}%`, backgroundColor: color }} />
    </div>
  )
}

export function SignalsTable({ signals, onSimulateTrade, isSimulating }: Props) {
  const [sortKey, setSortKey] = useState<SortKey>('edge')
  const [sortDir, setSortDir] = useState<SortDir>('desc')
  const [expandedKey, setExpandedKey] = useState<string | null>(null)
  const [activeFilter, setActiveFilter] = useState<TableFilter>('ALL')

  const unified: UnifiedSignal[] = useMemo(() => {
    return signals.map(s => {
      const underlyingAsset = (s.underlying_asset || 'BTC').toUpperCase()
      const assetPrefix = underlyingAsset.toLowerCase()
      const raw = s.event_slug || s.market_ticker
      const title = raw.replace(`${assetPrefix}-updown-5m-`, '')
      return {
        key: `${assetPrefix}-${s.market_ticker}`,
        ticker: s.market_ticker,
        title,
        platform: s.platform || 'polymarket',
        direction: s.direction,
        edge: s.edge,
        modelProb: s.model_probability,
        marketProb: s.market_probability,
        confidence: s.confidence,
        suggestedSize: s.suggested_size,
        reasoning: s.reasoning,
        actionable: s.actionable,
        underlyingAsset,
        isMc: !!s.contract_style,
      }
    })
  }, [signals])

  // Counts per chip. Chip semantics match TradesTable so the same row
  // can show up in multiple chips (e.g. a future MC-BTC signal counts
  // toward both BTC and MC), mirroring the spec's example where per-chip
  // sums exceed ALL.
  const counts = useMemo(() => {
    const c: Partial<Record<TableFilter, number>> = {
      ALL: unified.length,
      BTC: 0,
      ETH: 0,
      SOL: 0,
      XRP: 0,
      MC: 0,
    }
    for (const u of unified) {
      if (u.underlyingAsset === 'BTC') c.BTC = (c.BTC ?? 0) + 1
      if (u.underlyingAsset === 'ETH') c.ETH = (c.ETH ?? 0) + 1
      if (u.underlyingAsset === 'SOL') c.SOL = (c.SOL ?? 0) + 1
      if (u.underlyingAsset === 'XRP') c.XRP = (c.XRP ?? 0) + 1
      if (u.isMc) c.MC = (c.MC ?? 0) + 1
    }
    return c
  }, [unified])

  const filtered = useMemo(() => {
    if (activeFilter === 'ALL') return unified
    if (activeFilter === 'MC') return unified.filter(u => u.isMc)
    return unified.filter(u => u.underlyingAsset === activeFilter)
  }, [unified, activeFilter])

  const handleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir(sortDir === 'asc' ? 'desc' : 'asc')
    } else {
      setSortKey(key)
      setSortDir('desc')
    }
  }

  const sorted = useMemo(() => {
    return [...filtered].sort((a, b) => {
      if (a.actionable !== b.actionable) return a.actionable ? -1 : 1
      let aVal: number, bVal: number
      switch (sortKey) {
        case 'edge':
          aVal = Math.abs(a.edge); bVal = Math.abs(b.edge); break
        case 'model_probability':
          aVal = a.modelProb; bVal = b.modelProb; break
        case 'suggested_size':
          aVal = a.suggestedSize; bVal = b.suggestedSize; break
        default: return 0
      }
      return sortDir === 'asc' ? aVal - bVal : bVal - aVal
    })
  }, [filtered, sortKey, sortDir])

  const SortIcon = ({ column }: { column: SortKey }) => {
    if (sortKey !== column) return <ArrowUpDown className="w-2.5 h-2.5 text-neutral-600" />
    return sortDir === 'asc'
      ? <ArrowUp className="w-2.5 h-2.5 text-amber-500" />
      : <ArrowDown className="w-2.5 h-2.5 text-amber-500" />
  }

  // Column count kept in one place so the empty-state colspan tracks any
  // future column add/remove.
  const COL_COUNT = 9

  const emptyMessage = (() => {
    if (unified.length === 0) return 'No signals generated'
    if (sorted.length === 0) {
      return activeFilter === 'ALL'
        ? 'No signals match filter'
        : `No ${activeFilter} signals`
    }
    return null
  })()

  return (
    <div>
      <TableFilterChips
        activeFilter={activeFilter}
        onFilterChange={setActiveFilter}
        counts={counts}
      />
      <table className="w-full">
        <thead className="sticky top-0 bg-[#0a0a0a] z-10">
          <tr className="text-neutral-600 text-left text-[10px] border-b border-neutral-800">
            <th className="py-1.5 px-1.5 font-medium w-6"></th>
            <th className="py-1.5 px-1.5 font-medium w-10">Asset</th>
            <th className="py-1.5 px-1.5 font-medium">Signal</th>
            <th className="py-1.5 px-1.5 font-medium text-center w-8">Dir</th>
            <th
              className="py-1.5 px-1.5 font-medium text-right cursor-pointer hover:text-neutral-400"
              onClick={() => handleSort('edge')}
            >
              <div className="flex items-center justify-end gap-0.5">
                Edge <SortIcon column="edge" />
              </div>
            </th>
            <th className="py-1.5 px-1.5 font-medium text-right w-10"></th>
            <th
              className="py-1.5 px-1.5 font-medium text-right cursor-pointer hover:text-neutral-400"
              onClick={() => handleSort('model_probability')}
            >
              <div className="flex items-center justify-end gap-0.5">
                Mod <SortIcon column="model_probability" />
              </div>
            </th>
            <th
              className="py-1.5 px-1.5 font-medium text-right cursor-pointer hover:text-neutral-400"
              onClick={() => handleSort('suggested_size')}
            >
              <div className="flex items-center justify-end gap-0.5">
                Size <SortIcon column="suggested_size" />
              </div>
            </th>
            <th className="py-1.5 px-1.5 font-medium text-right w-10"></th>
          </tr>
        </thead>
        <tbody>
          {emptyMessage ? (
            <tr>
              <td colSpan={COL_COUNT} className="py-8 text-center">
                <p className="text-xs text-neutral-600">{emptyMessage}</p>
                {unified.length === 0 && (
                  <p className="text-[10px] mt-0.5 text-neutral-700">
                    Run a scan or wait for next cycle
                  </p>
                )}
              </td>
            </tr>
          ) : (
            <AnimatePresence>
              {sorted.map((sig, i) => {
                const isExpanded = expandedKey === sig.key
                const isUp = sig.direction === 'up' || sig.direction === 'above'

                return (
                  <motion.tr
                    key={sig.key}
                    initial={{ opacity: 0, y: 4 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ delay: i * 0.02 }}
                    className={`border-b border-neutral-800/50 hover:bg-neutral-800/30 text-[11px] cursor-pointer ${
                      sig.actionable ? '' : 'opacity-40'
                    }`}
                    onClick={() => setExpandedKey(isExpanded ? null : sig.key)}
                  >
                    <td className="py-1 px-1.5">
                      <PlatformBadge platform={sig.platform} />
                    </td>
                    <td className="py-1 px-1.5">
                      <AssetToken underlying={sig.underlyingAsset} isMc={sig.isMc} />
                    </td>
                    <td className="py-1 px-1.5">
                      <span className="text-neutral-400 truncate block max-w-[110px]" title={sig.title}>
                        {sig.title}
                      </span>
                    </td>
                    <td className="py-1 px-1.5 text-center">
                      <span className={`text-[10px] font-semibold uppercase ${isUp ? 'text-green-500' : 'text-red-500'}`}>
                        {sig.direction}
                      </span>
                    </td>
                    <td className="py-1 px-1.5 text-right">
                      <span className={`font-semibold tabular-nums ${
                        sig.edge > 0 ? 'text-green-500' : sig.edge < 0 ? 'text-red-500' : 'text-neutral-600'
                      }`}>
                        {sig.edge === 0 ? '-' : `${Math.abs(sig.edge * 100).toFixed(1)}%`}
                      </span>
                    </td>
                    <td className="py-1 px-1.5">
                      <EdgeBar edge={sig.edge} />
                    </td>
                    <td className="py-1 px-1.5 text-right text-neutral-300 tabular-nums">
                      {(sig.modelProb * 100).toFixed(0)}%
                    </td>
                    <td className="py-1 px-1.5 text-right text-blue-400 tabular-nums">
                      {sig.suggestedSize > 0 ? `$${sig.suggestedSize.toFixed(0)}` : '-'}
                    </td>
                    <td className="py-1 px-1.5 text-right">
                      {sig.actionable && (
                        <button
                          onClick={(e) => { e.stopPropagation(); onSimulateTrade(sig.ticker) }}
                          disabled={isSimulating}
                          className="px-1.5 py-0.5 text-[8px] font-medium uppercase bg-amber-500/10 text-amber-400 border border-amber-500/20 hover:bg-amber-500/20 disabled:opacity-50"
                        >
                          Trade
                        </button>
                      )}
                    </td>
                  </motion.tr>
                )
              })}
            </AnimatePresence>
          )}
        </tbody>
      </table>
    </div>
  )
}
