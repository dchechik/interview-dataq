import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { api } from '../api/client'
import { useDataset, useProfile } from '../api/hooks'
import { ChartInspector } from '../components/ChartInspector'
import { NumberField } from '../components/NumberField'
import { VizRenderer } from '../renderers'
import type { AbnormalityOp, Filter } from '../api/types'

/**
 * The timeline view: what happened, to this thing, in this order.
 *
 * A chart page asks you to pick encodings; this one asks you to pick a subject.
 * That is the difference in the reading task — you arrive already knowing which
 * user or address you care about, and the controls are there to narrow to it.
 */

/** A subject filter under construction: pick a column, then a value. */
interface Pinned {
  column: string
  value: string
}

/** Which events get flagged. `column: ''` means flag nothing. */
interface FlagRule {
  column: string
  op: AbnormalityOp
  value: number
}

const FLAG_OPS: AbnormalityOp[] = ['<', '<=', '>', '>=', '==', '!=']

// DuckDB's numeric physical types. A rule is a threshold, so only these can
// carry one -- a timestamp column would compare, but not usefully.
const NUMERIC_TYPES = [
  'BIGINT', 'INTEGER', 'SMALLINT', 'TINYINT', 'HUGEINT',
  'UBIGINT', 'UINTEGER', 'USMALLINT', 'UTINYINT',
  'DOUBLE', 'FLOAT', 'REAL', 'DECIMAL', 'NUMERIC',
]

export function TimelinePage() {
  const { id = '' } = useParams()
  const { data: dataset } = useDataset(id)
  const { data: profile } = useProfile(id)

  const [timeColumn, setTimeColumn] = useState<string>('')
  const [titleColumn, setTitleColumn] = useState<string>('')
  const [pinned, setPinned] = useState<Pinned[]>([])
  const [limit, setLimit] = useState(200)
  // null until the user takes the rule over; the backend infers one until then.
  const [flag, setFlag] = useState<FlagRule | null>(null)

  // Default the columns from the semantic layer as soon as the profile lands:
  // the time column is whichever column *means* a time, not one named "ts".
  const timeCandidates = useMemo(
    () => (profile?.columns ?? []).filter((c) => c.role === 'time').map((c) => c.name),
    [profile],
  )
  const titleCandidates = useMemo(
    () =>
      (profile?.columns ?? [])
        .filter((c) => c.semantic_type === 'categorical')
        .map((c) => c.name),
    [profile],
  )
  const effectiveTime = timeColumn || timeCandidates[0] || ''
  const effectiveTitle = titleColumn || titleCandidates[0] || ''

  const filters: Filter[] = pinned
    .filter((p) => p.column && p.value !== '')
    .map((p) => ({ column: p.column, op: '=', value: p.value }))

  const params = useMemo(
    () => ({
      time_column: effectiveTime,
      title_column: effectiveTitle || null,
      filters,
      limit,
      // Untouched, the rule is the backend's to infer. Once touched it is sent
      // whole -- including the parts that came from the inference -- because a
      // half-specified rule would be re-inferred against the other half.
      ...(flag === null
        ? {}
        : flag.column === ''
          ? { abnormality_enabled: false }
          : {
              abnormality_column: flag.column,
              abnormality_op: flag.op,
              abnormality_value: flag.value,
            }),
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [effectiveTime, effectiveTitle, JSON.stringify(filters), limit, JSON.stringify(flag)],
  )

  const { data, isLoading, error } = useQuery({
    queryKey: ['timeline', id, params],
    queryFn: () => api.inspect({ plugin_id: 'viz.timeline', dataset_id: id, params }),
    enabled: Boolean(id && effectiveTime),
  })

  /** Clicking a chip pins that value — "show me only this address". */
  function pin(column: string, value: unknown) {
    setPinned((current) =>
      current.some((p) => p.column === column)
        ? current.map((p) => (p.column === column ? { ...p, value: String(value) } : p))
        : [...current, { column, value: String(value) }],
    )
  }

  // Every column, not just the subject-shaped ones. Narrowing to "who" is the
  // common case, but a computed feature is a measure -- and "first time this
  // recipient saw this country" is exactly the sort of thing you filter a
  // timeline down to, so excluding measures put that out of reach.
  const filterable = (profile?.columns ?? [])
    .filter((c) => c.role !== 'time')
    .map((c) => c.name)

  // Anything numeric can carry a threshold. The backend still *prefers* a
  // rarity column when nobody has chosen, but preferring is not the same as
  // being the only option: "flag the slow requests" is the same control.
  const flagCandidates = (profile?.columns ?? [])
    .filter(
      (c) =>
        c.role !== 'ignore' &&
        NUMERIC_TYPES.some((t) => c.physical_type.toUpperCase().startsWith(t)),
    )
    .map((c) => c.name)

  // What the controls show: the user's rule, else the one the backend inferred,
  // else nothing chosen yet.
  const inferred = data?.spec.timeline?.abnormality
  const shown: FlagRule =
    flag ??
    (inferred
      ? { column: inferred.column, op: inferred.op, value: inferred.value }
      : { column: '', op: '<', value: 0.01 })

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Link to={`/datasets/${id}`} className="text-sm text-slate-500 hover:text-slate-800">
          ← {dataset?.name ?? id}
        </Link>
        <h1 className="text-xl font-semibold text-slate-900">Timeline</h1>
        {data && (
          <span className="text-sm text-slate-500">
            {data.row_count.toLocaleString()} events · {data.elapsed_ms}ms
          </span>
        )}
      </div>

      {!timeCandidates.length && profile && (
        <p className="rounded border border-dashed border-slate-300 p-6 text-center text-sm text-slate-500">
          This dataset has no timestamp column, so there is nothing to lay out in time.
          Parse one with <code className="font-mono">normalize.timestamp</code> first.
        </p>
      )}

      {timeCandidates.length > 0 && (
        <div className="space-y-3 rounded-lg border border-slate-200 bg-white p-4">
          <div className="flex flex-wrap items-end gap-3">
            <label className="text-xs text-slate-600">
              <span className="mb-1 block font-medium">Time</span>
              <select
                value={effectiveTime}
                onChange={(e) => setTimeColumn(e.target.value)}
                className="rounded border border-slate-300 px-2 py-1 text-sm"
              >
                {timeCandidates.map((c) => (
                  <option key={c}>{c}</option>
                ))}
              </select>
            </label>

            <label className="text-xs text-slate-600">
              <span className="mb-1 block font-medium">Headline</span>
              <select
                value={effectiveTitle}
                onChange={(e) => setTitleColumn(e.target.value)}
                className="rounded border border-slate-300 px-2 py-1 text-sm"
              >
                <option value="">— none —</option>
                {titleCandidates.map((c) => (
                  <option key={c}>{c}</option>
                ))}
              </select>
            </label>

            <label className="text-xs text-slate-600">
              <span className="mb-1 block font-medium">Events</span>
              <select
                value={limit}
                onChange={(e) => setLimit(Number(e.target.value))}
                className="rounded border border-slate-300 px-2 py-1 text-sm"
              >
                {[50, 200, 500, 1000].map((n) => (
                  <option key={n}>{n}</option>
                ))}
              </select>
            </label>

            <button
              type="button"
              onClick={() => setPinned((c) => [...c, { column: filterable[0] ?? '', value: '' }])}
              className="rounded border border-slate-300 px-2 py-1 text-xs hover:bg-slate-50"
            >
              + filter
            </button>
          </div>

          {pinned.map((p, i) => (
            <div key={i} className="flex flex-wrap items-center gap-2">
              <select
                value={p.column}
                onChange={(e) =>
                  setPinned((c) =>
                    c.map((x, j) => (j === i ? { ...x, column: e.target.value } : x)),
                  )
                }
                className="rounded border border-slate-300 px-2 py-1 text-sm"
              >
                {filterable.map((c) => (
                  <option key={c}>{c}</option>
                ))}
              </select>
              <span className="text-sm text-slate-400">=</span>
              <input
                value={p.value}
                onChange={(e) =>
                  setPinned((c) => c.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))
                }
                placeholder="value"
                className="flex-1 rounded border border-slate-300 px-2 py-1 text-sm"
              />
              <button
                type="button"
                onClick={() => setPinned((c) => c.filter((_, j) => j !== i))}
                className="px-2 text-sm text-slate-400 hover:text-rose-600"
              >
                ×
              </button>
            </div>
          ))}

          {flagCandidates.length > 0 && (
            <div className="flex flex-wrap items-center gap-2 border-t border-slate-100 pt-3 text-xs">
              <span className="font-medium text-slate-600">Flag when</span>
              <select
                value={shown.column}
                // A new column takes the rule's shape with it: the operator and
                // the threshold that suited a share are meaningless on a
                // duration, so they go back to the backend's choosing.
                onChange={(e) =>
                  setFlag(
                    e.target.value === shown.column
                      ? shown
                      : { column: e.target.value, op: '<', value: 0.01 },
                  )
                }
                className="rounded border border-slate-300 px-2 py-0.5 font-mono"
              >
                <option value="">— nothing —</option>
                {flagCandidates.map((c) => (
                  <option key={c}>{c}</option>
                ))}
              </select>
              {shown.column && (
                <>
                  <select
                    value={shown.op}
                    onChange={(e) =>
                      setFlag({ ...shown, op: e.target.value as AbnormalityOp })
                    }
                    className="rounded border border-slate-300 px-1.5 py-0.5 font-mono"
                  >
                    {FLAG_OPS.map((o) => (
                      <option key={o}>{o}</option>
                    ))}
                  </select>
                  <NumberField
                    value={shown.value}
                    // Clearing the box leaves the rule where it was rather
                    // than flagging on an empty threshold.
                    onChange={(v) => v !== null && setFlag({ ...shown, value: v })}
                    step="any"
                    className="w-24 rounded border border-slate-300 px-1.5 py-0.5"
                  />
                </>
              )}
              <span className="text-slate-400">{inferred?.rationale}</span>
              {flag !== null && (
                <button
                  type="button"
                  onClick={() => setFlag(null)}
                  className="text-slate-500 underline"
                >
                  reset
                </button>
              )}
            </div>
          )}
        </div>
      )}

      {isLoading && <p className="text-sm text-slate-500">Loading…</p>}
      {error && (
        <p className="rounded bg-rose-50 p-3 text-sm text-rose-800">
          {String((error as Error).message)}
        </p>
      )}

      {data && (
        <div className="rounded-lg border border-slate-200 bg-white p-4">
          <VizRenderer spec={data.spec} data={data.data} height={620} onFilter={pin} />
          <ChartInspector
            data={data.data}
            sql={data.sql}
            query={data.spec.query}
            rowCount={data.row_count}
            elapsedMs={data.elapsed_ms}
            truncated={data.truncated}
          />
        </div>
      )}
    </div>
  )
}
