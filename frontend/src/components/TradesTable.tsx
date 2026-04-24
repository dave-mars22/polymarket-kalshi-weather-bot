import { formatDistanceToNow } from 'date-fns'
import { ArrowUpDown, ArrowUp, ArrowDown } from 'lucide-react'
import { useState, useMemo } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import type { Trade } from '../types'
import { platformStyles } from '../utils'
import {
  TableFilterChips,
  type TableFilter,
} from './TableFilterChips'

interface Props {
  trades: Trade[]
}

type SortKey = 'timestamp' | 'size' | 'pnl' | 'result'
type SortDir = 'asc' | 'desc'

// Muted per-asset color palette — mirror the mapping used by SignalsTable
// so a BTC row reads the same hue on both tables.
const ASSET_COLOR: Record<string, string> = {
  BTC: 'text-amber-400',
  ETH: 'text-blue-400',
  SOL: 'text-purple-400',
  XRP: 'text-cyan-400',
  MC: 'text-pink-400',
}

function AssetToken({
  underlyingAsset,
  isMc,
}: {
  underlyingAsset: string
  isMc: boolean
}) {
  const label = isMc ? 'MC' : underlyingAsset.toUpperCase()
  const color = ASSET_COLOR[label] ?? 'text-neutral-400'
  return (
    <span className={`text-[10px] font-bold uppercase tracking-wider tabular-nums ${color}`}>
      {label || '—'}
    </span>
  )
}

export function TradesTable({ trades }: Props) {
  const [sortKey, setSortKey] = useState<SortKey>('timestamp')
  const [sortDir, setSortDir] = useState<SortDir>('desc')
  const [activeFilter, setActiveFilter] = useState<TableFilter>('ALL')

  // Precompute attribution once. Chip semantics: BTC/ETH/SOL/XRP match
  // underlying_asset; MC matches market_type === 'monte_carlo'. Per the
  // spec's example, a row with underlying_asset='BTC' AND market_type=
  // 'monte_carlo' counts toward both BTC and MC chips (overlap is fine).
  const enriched = useMemo(() => {
    return trades.map(t => ({
      trade: t,
      underlyingAsset: (t.underlying_asset || 'BTC').toUpperCase(),
      isMc: t.market_type === 'monte_carlo',
    }))
  }, [trades])

  const counts = useMemo(() => {
    const c: Partial<Record<TableFilter, number>> = {
      ALL: enriched.length,
      BTC: 0,
      ETH: 0,
      SOL: 0,
      XRP: 0,
      MC: 0,
    }
    for (const e of enriched) {
      if (e.underlyingAsset === 'BTC') c.BTC = (c.BTC ?? 0) + 1
      if (e.underlyingAsset === 'ETH') c.ETH = (c.ETH ?? 0) + 1
      if (e.underlyingAsset === 'SOL') c.SOL = (c.SOL ?? 0) + 1
      if (e.underlyingAsset === 'XRP') c.XRP = (c.XRP ?? 0) + 1
      if (e.isMc) c.MC = (c.MC ?? 0) + 1
    }
    return c
  }, [enriched])

  const filtered = useMemo(() => {
    if (activeFilter === 'ALL') return enriched
    if (activeFilter === 'MC') return enriched.filter(e => e.isMc)
    return enriched.filter(e => e.underlyingAsset === activeFilter)
  }, [enriched, activeFilter])

  const handleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir(sortDir === 'asc' ? 'desc' : 'asc')
    } else {
      setSortKey(key)
      setSortDir('desc')
    }
  }

  const sortedTrades = useMemo(() => {
    return [...filtered].sort((a, b) => {
      let aVal: number | string, bVal: number | string
      switch (sortKey) {
        case 'timestamp':
          aVal = new Date(a.trade.timestamp).getTime()
          bVal = new Date(b.trade.timestamp).getTime()
          break
        case 'size':
          aVal = a.trade.size; bVal = b.trade.size; break
        case 'pnl':
          aVal = a.trade.pnl ?? 0; bVal = b.trade.pnl ?? 0; break
        case 'result':
          aVal = a.trade.result; bVal = b.trade.result; break
        default: return 0
      }
      if (typeof aVal === 'string') {
        return sortDir === 'asc'
          ? aVal.localeCompare(bVal as string)
          : (bVal as string).localeCompare(aVal)
      }
      return sortDir === 'asc' ? aVal - (bVal as number) : (bVal as number) - aVal
    })
  }, [filtered, sortKey, sortDir])

  const SortIcon = ({ column }: { column: SortKey }) => {
    if (sortKey !== column) return <ArrowUpDown className="w-2.5 h-2.5 text-neutral-600" />
    return sortDir === 'asc'
      ? <ArrowUp className="w-2.5 h-2.5 text-amber-500" />
      : <ArrowDown className="w-2.5 h-2.5 text-amber-500" />
  }

  const COL_COUNT = 8

  const emptyMessage = (() => {
    if (enriched.length === 0) return 'No trades yet'
    if (sortedTrades.length === 0) {
      return activeFilter === 'ALL'
        ? 'No trades match filter'
        : `No ${activeFilter} trades`
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
            <th className="py-1.5 px-1.5 font-medium w-5"></th>
            <th
              className="py-1.5 px-1.5 font-medium cursor-pointer hover:text-neutral-400"
              onClick={() => handleSort('result')}
            >
              <div className="flex items-center gap-0.5">
                St <SortIcon column="result" />
              </div>
            </th>
            <th className="py-1.5 px-1.5 font-medium w-10">Asset</th>
            <th className="py-1.5 px-1.5 font-medium">Market</th>
            <th className="py-1.5 px-1.5 font-medium text-center">Dir</th>
            <th
              className="py-1.5 px-1.5 font-medium text-right cursor-pointer hover:text-neutral-400"
              onClick={() => handleSort('size')}
            >
              <div className="flex items-center justify-end gap-0.5">
                Size <SortIcon column="size" />
              </div>
            </th>
            <th
              className="py-1.5 px-1.5 font-medium text-right cursor-pointer hover:text-neutral-400"
              onClick={() => handleSort('pnl')}
            >
              <div className="flex items-center justify-end gap-0.5">
                P&L <SortIcon column="pnl" />
              </div>
            </th>
            <th
              className="py-1.5 px-1.5 font-medium text-right cursor-pointer hover:text-neutral-400"
              onClick={() => handleSort('timestamp')}
            >
              <div className="flex items-center justify-end gap-0.5">
                Time <SortIcon column="timestamp" />
              </div>
            </th>
          </tr>
        </thead>
        <tbody>
          {emptyMessage ? (
            <tr>
              <td colSpan={COL_COUNT} className="py-8 text-center">
                <p className="text-xs text-neutral-600">{emptyMessage}</p>
                {enriched.length === 0 && (
                  <p className="text-[10px] mt-0.5 text-neutral-700">
                    Trades will appear here
                  </p>
                )}
              </td>
            </tr>
          ) : (
            <AnimatePresence>
              {sortedTrades.map(({ trade, underlyingAsset, isMc }, i) => {
                const isPending = trade.result === 'pending'
                const isWin = trade.result === 'win'
                const isUp = trade.direction === 'up'
                const style = platformStyles[trade.platform?.toLowerCase()]
                // Strip the per-asset 5m prefix so the Market cell stays
                // scannable regardless of which underlying's slug it is.
                // MC tickers don't match the pattern so this passes through.
                const prefix = `${underlyingAsset.toLowerCase()}-updown-5m-`
                const marketLabel = (trade.event_slug || trade.market_ticker).replace(prefix, '')

                return (
                  <motion.tr
                    key={trade.id}
                    initial={{ opacity: 0, y: 4 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ delay: i * 0.02 }}
                    className="border-b border-neutral-800/50 hover:bg-neutral-800/30 text-[11px]"
                  >
                    <td className="py-1 px-1.5">
                      {style && (
                        <span className={`platform-badge ${style.badge}`}>
                          {style.icon}
                        </span>
                      )}
                    </td>
                    <td className="py-1 px-1.5">
                      <span className={`text-[9px] font-medium uppercase ${
                        isPending ? 'text-amber-500' : isWin ? 'text-green-500' : 'text-red-500'
                      }`}>
                        {isPending ? 'PND' : isWin ? 'WIN' : 'LOSS'}
                      </span>
                    </td>
                    <td className="py-1 px-1.5">
                      <AssetToken underlyingAsset={underlyingAsset} isMc={isMc} />
                    </td>
                    <td className="py-1 px-1.5">
                      <span
                        className="text-neutral-400 truncate block max-w-[100px]"
                        title={trade.event_slug || trade.market_ticker}
                      >
                        {marketLabel}
                      </span>
                    </td>
                    <td className="py-1 px-1.5 text-center">
                      <span className={`text-[10px] font-semibold uppercase ${isUp ? 'text-green-500' : 'text-red-500'}`}>
                        {trade.direction}
                      </span>
                    </td>
                    <td className="py-1 px-1.5 text-right text-neutral-300 tabular-nums">
                      ${trade.size.toFixed(0)}
                    </td>
                    <td className="py-1 px-1.5 text-right">
                      {trade.pnl !== null ? (
                        <span className={`font-semibold tabular-nums ${
                          trade.pnl >= 0 ? 'text-green-500' : 'text-red-500'
                        }`}>
                          {trade.pnl >= 0 ? '+' : ''}${trade.pnl.toFixed(0)}
                        </span>
                      ) : (
                        <span className="text-neutral-600">-</span>
                      )}
                    </td>
                    <td className="py-1 px-1.5 text-right text-[10px] text-neutral-600 tabular-nums">
                      {formatDistanceToNow(new Date(trade.timestamp), { addSuffix: false })}
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
