import { useState } from 'react'

import type { ChartSpec, QuerySpec } from '../api/types'
import { compileChartSpec } from '../renderers/vegaLite'

/**
 * Shows what a chart was actually built from, and what it was drawn with.
 *
 * A chart you cannot trace back to its data is hard to trust — especially here,
 * where the query and the encodings were usually chosen by a suggester or an
 * agent rather than by the person reading the chart. Collapsed by default so it
 * never competes with the chart itself.
 */

type Tab = 'data' | 'sql' | 'query' | 'chart' | 'vega'

const TAB_LABELS: Record<Tab, string> = {
  data: 'Data',
  sql: 'SQL',
  query: 'QuerySpec',
  chart: 'ChartSpec',
  vega: 'Vega-Lite',
}

function display(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'number') {
    return Number.isInteger(value) ? value.toLocaleString() : String(Number(value.toFixed(6)))
  }
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

/** The rows the chart actually received — the fastest way to see why it looks wrong. */
function DataTable({ data }: { data: Record<string, unknown>[] }) {
  if (!data.length) {
    return <p className="p-3 text-center text-xs text-slate-500">The query returned no rows.</p>
  }
  const columns = Object.keys(data[0])
  return (
    <div className="max-h-56 overflow-auto">
      <table className="w-full border-collapse text-[11px]">
        <thead className="sticky top-0 bg-slate-100">
          <tr>
            {columns.map((c) => (
              <th key={c} className="px-2 py-1 text-left font-medium text-slate-700">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.slice(0, 200).map((row, i) => (
            <tr key={i} className="border-t border-slate-200">
              {columns.map((c) => (
                <td key={c} className="px-2 py-0.5 font-mono text-slate-600">
                  {display(row[c])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {data.length > 200 && (
        <p className="px-2 py-1 text-[11px] text-slate-400">
          showing the first 200 of {data.length.toLocaleString()} rows
        </p>
      )}
    </div>
  )
}

/** Which channel reads which column, and why it was typed the way it was. */
function ChartSummary({ chart }: { chart: ChartSpec }) {
  return (
    <div className="p-2">
      <p className="mb-1.5 text-[11px] text-slate-500">
        mark <code className="font-mono text-slate-800">{chart.mark}</code>
      </p>
      <table className="w-full text-[11px]">
        <tbody>
          {Object.entries(chart.encodings ?? {}).map(([channel, enc]) =>
            enc ? (
              <tr key={channel} className="border-t border-slate-200">
                <td className="py-0.5 pr-2 font-medium text-slate-700">{channel}</td>
                <td className="py-0.5 pr-2 font-mono text-slate-800">{enc.field}</td>
                <td className="py-0.5 pr-2 text-slate-600">{enc.type}</td>
                <td className="py-0.5 text-slate-400">
                  {enc.sort ? `sorted by ${enc.sort}` : ''}
                  {enc.inferred_from ? ` · ${enc.inferred_from}` : ''}
                </td>
              </tr>
            ) : null,
          )}
        </tbody>
      </table>
    </div>
  )
}

export function ChartInspector({
  data,
  sql,
  query,
  chart,
  rawSpec,
  rowCount,
  elapsedMs,
  truncated,
}: {
  data?: Record<string, unknown>[]
  sql?: string
  query?: QuerySpec
  chart?: ChartSpec | null
  rawSpec?: Record<string, unknown>
  rowCount?: number
  elapsedMs?: number
  truncated?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [tab, setTab] = useState<Tab>('data')

  if (!sql && !query && !chart && !data) return null

  const compiled = chart ? compileChartSpec(chart) : rawSpec
  const tabs: Tab[] = [
    ...(data ? (['data'] as Tab[]) : []),
    'sql',
    'query',
    ...(chart ? (['chart'] as Tab[]) : []),
    'vega',
  ]

  const textFor = (t: Tab): string => {
    if (t === 'sql') return sql ?? 'not available'
    if (t === 'query') return JSON.stringify(query, null, 2)
    if (t === 'chart') return JSON.stringify(chart, null, 2)
    if (t === 'vega') return JSON.stringify(compiled, null, 2)
    return JSON.stringify(data, null, 2)
  }

  return (
    <div className="mt-2 border-t border-slate-100 pt-1.5">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full items-center gap-1.5 text-left text-xs text-slate-400 hover:text-slate-600"
      >
        <span>{open ? '▾' : '▸'}</span>
        <span>Inspect</span>
        {rowCount != null && (
          <span className="tabular-nums">
            · {rowCount.toLocaleString()} rows{truncated && ' (truncated)'}
          </span>
        )}
        {elapsedMs != null && <span className="tabular-nums">· {elapsedMs}ms</span>}
      </button>

      {open && (
        <div className="mt-1.5">
          <div className="mb-1 flex flex-wrap gap-1">
            {tabs.map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => setTab(t)}
                className={`rounded px-2 py-0.5 text-xs ${
                  tab === t ? 'bg-slate-800 text-white' : 'bg-slate-100 text-slate-600'
                }`}
              >
                {TAB_LABELS[t]}
              </button>
            ))}
            <button
              type="button"
              onClick={() => navigator.clipboard?.writeText(textFor(tab))}
              className="ml-auto rounded border border-slate-300 px-2 py-0.5 text-xs text-slate-600 hover:bg-slate-50"
            >
              Copy
            </button>
          </div>

          <div className="overflow-hidden rounded bg-slate-50">
            {tab === 'data' && data ? (
              <DataTable data={data} />
            ) : tab === 'chart' && chart ? (
              <ChartSummary chart={chart} />
            ) : (
              <pre className="max-h-56 overflow-auto p-2 text-[11px] leading-relaxed text-slate-700">
                {textFor(tab)}
              </pre>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
