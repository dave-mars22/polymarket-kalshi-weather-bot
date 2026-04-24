// Shared filter-chip row for SignalsTable and TradesTable (slice D7).
// Chips are plain text with an amber underline on the active chip — not
// bordered buttons — to match the Bloomberg-terminal aesthetic elsewhere.
// Counts render next to each label so activity is visible without
// clicking through.
export type TableFilter = 'ALL' | 'BTC' | 'ETH' | 'SOL' | 'XRP' | 'MC'

export const TABLE_FILTER_CHIPS: readonly TableFilter[] = [
  'ALL',
  'BTC',
  'ETH',
  'SOL',
  'XRP',
  'MC',
] as const

interface Props {
  activeFilter: TableFilter
  onFilterChange: (filter: TableFilter) => void
  counts: Partial<Record<TableFilter, number>>
}

export function TableFilterChips({ activeFilter, onFilterChange, counts }: Props) {
  return (
    <div className="flex items-center gap-3 px-2 py-1 border-b border-neutral-800/60">
      {TABLE_FILTER_CHIPS.map(chip => {
        const active = activeFilter === chip
        const count = counts[chip] ?? 0
        return (
          <button
            key={chip}
            type="button"
            onClick={() => onFilterChange(chip)}
            className={`flex items-center gap-1 py-0.5 text-[10px] uppercase tracking-wider transition-colors border-b ${
              active
                ? 'text-amber-400 border-amber-400/60'
                : 'text-neutral-500 hover:text-neutral-300 border-transparent'
            }`}
          >
            <span>{chip}</span>
            <span
              className={`tabular-nums ${
                active ? 'text-amber-400/80' : 'text-neutral-600'
              }`}
            >
              {count}
            </span>
          </button>
        )
      })}
    </div>
  )
}
