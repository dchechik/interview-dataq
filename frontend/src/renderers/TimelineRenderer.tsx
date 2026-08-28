import { useMemo, useState } from 'react'

import type { EventAttribute, TimelineSpec } from '../api/types'
import type { RendererProps } from './index'

/**
 * A list of timed events.
 *
 * The reading task is different from a chart's: you scan down looking for the
 * moment something changed. So the layout puts time in a fixed left column, the
 * headline where the eye lands, and secondary detail as chips — and anything
 * matching the abnormality rule gets a tint and a badge, because the whole point
 * of the view is that the odd event should find *you*.
 */

function formatTime(value: unknown): { day: string; clock: string } {
  const date = new Date(String(value))
  if (Number.isNaN(date.getTime())) return { day: '', clock: String(value ?? '') }
  return {
    day: date.toLocaleDateString(undefined, {
      weekday: 'short', day: 'numeric', month: 'short', year: 'numeric',
    }),
    clock: date.toLocaleTimeString(undefined, {
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    }),
  }
}

function display(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  if (typeof value === 'number') {
    return Number.isInteger(value) ? value.toLocaleString() : String(Number(value.toFixed(4)))
  }
  return String(value)
}

/** Does this row trip the abnormality rule? Evaluated here so a threshold can move without a refetch. */
function isAbnormal(row: Record<string, unknown>, spec: TimelineSpec): boolean {
  const rule = spec.abnormality
  if (!rule) return false
  const raw = row[rule.column]
  if (raw === null || raw === undefined) return false
  const value = Number(raw)
  if (!Number.isFinite(value)) return false
  switch (rule.op) {
    case '<': return value < rule.value
    case '<=': return value <= rule.value
    case '>': return value > rule.value
    case '>=': return value >= rule.value
    case '==': return value === rule.value
    case '!=': return value !== rule.value
    default: return false
  }
}

function Chip({
  attribute,
  value,
  onFilter,
}: {
  attribute: EventAttribute
  value: unknown
  onFilter?: (column: string, value: unknown) => void
}) {
  const label = attribute.label ?? attribute.column
  const clickable = attribute.filterable && onFilter && value != null
  const base = attribute.highlight
    ? 'bg-slate-800 text-white'
    : 'bg-slate-100 text-slate-600'

  const content = (
    <>
      <span className={attribute.highlight ? 'text-slate-400' : 'text-slate-400'}>
        {label}
      </span>{' '}
      <span className="font-mono">{display(value)}</span>
    </>
  )

  if (!clickable) {
    return <span className={`rounded px-1.5 py-0.5 text-xs ${base}`}>{content}</span>
  }
  return (
    <button
      type="button"
      onClick={() => onFilter(attribute.column, value)}
      title={`Show only ${attribute.column} = ${display(value)}`}
      className={`rounded px-1.5 py-0.5 text-xs hover:ring-2 hover:ring-blue-300 ${base}`}
    >
      {content}
    </button>
  )
}

export function TimelineRenderer({ spec, data, height = 420, onFilter }: RendererProps) {
  const timeline = spec.timeline
  const [onlyAbnormal, setOnlyAbnormal] = useState(false)

  // Day separators are derived here rather than by mutating a variable during
  // render: render has to be a pure function of its inputs, or a re-render can
  // put the headings in the wrong places.
  const events = useMemo(() => {
    if (!timeline) return []
    let previousDay = ''
    return data.map((row) => {
      const { day, clock } = formatTime(row[timeline.time_column])
      const startsDay = timeline.group_by_day && day !== previousDay
      previousDay = day
      return { row, day, clock, startsDay, abnormal: isAbnormal(row, timeline) }
    })
  }, [data, timeline])

  const abnormalCount = events.filter((e) => e.abnormal).length
  const shown = onlyAbnormal ? events.filter((e) => e.abnormal) : events

  if (!timeline) {
    return (
      <div className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-800">
        This panel has no timeline configuration.
      </div>
    )
  }
  if (!data.length) {
    return <div className="p-6 text-center text-sm text-slate-500">No events</div>
  }

  return (
    <div>
      {timeline.abnormality && (
        <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
          <button
            type="button"
            onClick={() => setOnlyAbnormal((v) => !v)}
            className={`rounded px-2 py-0.5 ${
              onlyAbnormal ? 'bg-rose-600 text-white' : 'bg-rose-50 text-rose-700'
            }`}
          >
            {abnormalCount} {timeline.abnormality.label}
          </button>
          <span className="text-slate-400">
            {timeline.abnormality.rationale ||
              `${timeline.abnormality.column} ${timeline.abnormality.op} ${timeline.abnormality.value}`}
          </span>
          {onlyAbnormal && (
            <button
              type="button"
              onClick={() => setOnlyAbnormal(false)}
              className="text-slate-500 underline"
            >
              show all {events.length}
            </button>
          )}
        </div>
      )}

      <div className="overflow-auto rounded border border-slate-200" style={{ maxHeight: height }}>
        {shown.map(({ row, day, clock, startsDay, abnormal }, i) => {
          // When the list is filtered to only the flagged events, the first row
          // shown always needs its heading even if it did not start a day in
          // the unfiltered list.
          const showDay = startsDay || (onlyAbnormal && i === 0)
          return (
            <div key={i}>
              {showDay && day && (
                <div className="sticky top-0 z-10 border-y border-slate-200 bg-slate-50 px-3 py-1 text-xs font-medium text-slate-600">
                  {day}
                </div>
              )}
              <div
                className={`flex gap-3 border-b border-slate-100 px-3 py-2 last:border-0 ${
                  abnormal ? 'bg-rose-50/70' : 'hover:bg-slate-50/60'
                }`}
              >
                {/* A rail, so the eye can run down the times. */}
                <div className="flex shrink-0 flex-col items-end">
                  <span className="font-mono text-xs tabular-nums text-slate-500">{clock}</span>
                </div>
                <div
                  className={`mt-1 h-2 w-2 shrink-0 rounded-full ${
                    abnormal ? 'bg-rose-500' : 'bg-slate-300'
                  }`}
                />

                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-baseline gap-2">
                    {timeline.title_column && (
                      <span className="font-medium text-slate-900">
                        {display(row[timeline.title_column])}
                      </span>
                    )}
                    {abnormal && timeline.abnormality && (
                      <span className="rounded bg-rose-600 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-white">
                        {timeline.abnormality.label}
                      </span>
                    )}
                  </div>
                  {timeline.attributes.length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {timeline.attributes.map((attribute) => (
                        <Chip
                          key={attribute.column}
                          attribute={attribute}
                          value={row[attribute.column]}
                          onFilter={onFilter}
                        />
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </div>
          )
        })}
      </div>

      <div className="mt-1 text-xs text-slate-500">
        {shown.length.toLocaleString()} event{shown.length === 1 ? '' : 's'}
        {abnormalCount > 0 && !onlyAbnormal && (
          <span className="text-rose-700"> · {abnormalCount} flagged</span>
        )}
      </div>
    </div>
  )
}
