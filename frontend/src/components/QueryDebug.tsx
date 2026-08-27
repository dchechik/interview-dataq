import { useState } from 'react'

import type { QuerySpec } from '../api/types'

/**
 * Shows what a chart was actually built from.
 *
 * A chart you cannot trace back to a query is hard to trust — especially here,
 * where the query was chosen by a suggester or an agent rather than by the
 * person looking at it. Collapsed by default so it never competes with the chart.
 */
export function QueryDebug({
  sql,
  spec,
  rowCount,
  elapsedMs,
  truncated,
}: {
  sql?: string
  spec?: QuerySpec
  rowCount?: number
  elapsedMs?: number
  truncated?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [tab, setTab] = useState<'sql' | 'spec'>('sql')

  if (!sql && !spec) return null

  return (
    <div className="mt-2 border-t border-slate-100 pt-1.5">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full items-center gap-1.5 text-left text-xs text-slate-400 hover:text-slate-600"
      >
        <span>{open ? '▾' : '▸'}</span>
        <span>Query</span>
        {rowCount != null && (
          <span className="tabular-nums">
            · {rowCount.toLocaleString()} rows
            {truncated && ' (truncated)'}
          </span>
        )}
        {elapsedMs != null && <span className="tabular-nums">· {elapsedMs}ms</span>}
      </button>

      {open && (
        <div className="mt-1.5">
          <div className="mb-1 flex gap-1">
            {(['sql', 'spec'] as const).map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => setTab(t)}
                className={`rounded px-2 py-0.5 text-xs ${
                  tab === t ? 'bg-slate-800 text-white' : 'bg-slate-100 text-slate-600'
                }`}
              >
                {t === 'sql' ? 'SQL' : 'QuerySpec'}
              </button>
            ))}
            <button
              type="button"
              onClick={() =>
                navigator.clipboard?.writeText(
                  tab === 'sql' ? (sql ?? '') : JSON.stringify(spec, null, 2),
                )
              }
              className="ml-auto rounded border border-slate-300 px-2 py-0.5 text-xs text-slate-600 hover:bg-slate-50"
            >
              Copy
            </button>
          </div>
          <pre className="max-h-56 overflow-auto rounded bg-slate-50 p-2 text-[11px] leading-relaxed text-slate-700">
            {tab === 'sql' ? (sql ?? 'not available') : JSON.stringify(spec, null, 2)}
          </pre>
        </div>
      )}
    </div>
  )
}
