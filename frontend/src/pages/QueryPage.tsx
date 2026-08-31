import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { api, ApiError } from '../api/client'
import { useDataset, useProfile } from '../api/hooks'
import type { Filter, QueryResult, QuerySpec, Select } from '../api/types'
import { ChartInspector } from '../components/ChartInspector'
import { TableRenderer } from '../renderers/TableRenderer'

const OPS = ['=', '!=', '<', '<=', '>', '>=', 'in', 'contains', 'starts_with', 'is_null',
  'is_not_null'] as const
const AGGS = ['', 'count', 'count_distinct', 'sum', 'avg', 'min', 'max', 'median'] as const

export function QueryPage() {
  const { id = '' } = useParams()
  const { data: dataset } = useDataset(id)
  const { data: profile } = useProfile(id)
  const [filters, setFilters] = useState<Filter[]>([])
  const [groupBy, setGroupBy] = useState<string[]>([])
  const [selects, setSelects] = useState<Select[]>([])
  const [limit, setLimit] = useState(200)
  const [mode, setMode] = useState<'rows' | 'builder' | 'sql'>('rows')
  const [page, setPage] = useState(0)
  const [sql, setSql] = useState('')
  const [result, setResult] = useState<QueryResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  // The last text we put in the SQL box ourselves, so we can tell our own
  // seed from something you typed and only ever overwrite the former.
  const seededSql = useRef('')

  const columns = profile?.columns.map((c) => c.name) ?? []

  // In 'rows' mode nothing is configured: it is a plain paginated SELECT *, which
  // is what you want first when meeting an unfamiliar dataset.
  const spec: QuerySpec =
    mode === 'rows'
      ? { dataset: id, limit, offset: page * limit }
      : { dataset: id, filters, group_by: groupBy, select: selects, limit }

  const run = useCallback(
    async (override?: QuerySpec) => {
      setError(null)
      try {
        setResult(mode === 'sql' ? await api.sql(sql, limit) : await api.query(override ?? spec))
      } catch (e) {
        setResult(null)
        setError(e instanceof ApiError ? e.message : String(e))
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [mode, sql, limit, id, page, filters, groupBy, selects],
  )

  // Builder → SQL hands over the query you have built so far, so the SQL tab
  // opens on your work rather than a blank box: the same spec the Run button
  // would send, compiled by the backend (only it knows the source table) with
  // its literals inlined so the text is runnable as-is.
  const switchMode = async (m: typeof mode) => {
    const from = mode
    setMode(m)
    setResult(null)
    setError(null)
    if (m !== 'sql' || from !== 'builder') return
    if (sql !== '' && sql !== seededSql.current) return // you have edited it; leave it be
    try {
      const compiled = (await api.compileSql(spec)).sql
      seededSql.current = compiled
      setSql(compiled)
    } catch {
      // A spec that will not compile (no columns loaded yet, say) is not worth
      // an error banner over: the editor simply stays as it was.
    }
  }

  // Raw rows load on arrival and whenever the page changes, so exploring is just
  // clicking Next rather than composing a query first.
  useEffect(() => {
    if (mode === 'rows' && id) run({ dataset: id, limit, offset: page * limit })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, id, page, limit])

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <Link to={`/datasets/${id}`} className="text-sm text-slate-500 hover:text-slate-800">
          ← {dataset?.name ?? id}
        </Link>
        <h1 className="text-xl font-semibold text-slate-900">Query</h1>
        <div className="ml-auto flex rounded border border-slate-300">
          {(['rows', 'builder', 'sql'] as const).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => void switchMode(m)}
              className={`px-3 py-1 text-sm ${
                mode === m ? 'bg-slate-900 text-white' : 'text-slate-600'
              }`}
            >
              {m === 'rows' ? 'Rows' : m === 'builder' ? 'Builder' : 'SQL'}
            </button>
          ))}
        </div>
      </div>

      <div className="rounded-lg border border-slate-200 bg-white p-4">
        {mode === 'rows' ? (
          <div className="flex flex-wrap items-center gap-3">
            <span className="text-sm text-slate-600">
              Raw rows from <span className="font-medium">{dataset?.name ?? id}</span>
              {profile && (
                <span className="text-slate-400">
                  {' '}· {profile.row_count.toLocaleString()} rows ·{' '}
                  {profile.columns.length} columns
                </span>
              )}
            </span>
            <div className="ml-auto flex items-center gap-1.5">
              <button
                type="button"
                onClick={() => setPage((p) => Math.max(0, p - 1))}
                disabled={page === 0}
                className="rounded border border-slate-300 px-2 py-1 text-sm hover:bg-slate-50 disabled:opacity-40"
              >
                ← Prev
              </button>
              <span className="px-1 font-mono text-xs tabular-nums text-slate-500">
                {(page * limit + 1).toLocaleString()}–
                {(page * limit + (result?.row_count ?? 0)).toLocaleString()}
              </span>
              <button
                type="button"
                onClick={() => setPage((p) => p + 1)}
                // The last page is the one that comes back short.
                disabled={(result?.row_count ?? 0) < limit}
                className="rounded border border-slate-300 px-2 py-1 text-sm hover:bg-slate-50 disabled:opacity-40"
              >
                Next →
              </button>
              <label className="ml-2 text-xs text-slate-500">
                per page{' '}
                <select
                  value={limit}
                  onChange={(e) => {
                    setLimit(Number(e.target.value))
                    setPage(0)
                  }}
                  className="rounded border border-slate-300 px-1 py-0.5 text-xs"
                >
                  {[50, 100, 200, 500, 1000].map((n) => (
                    <option key={n}>{n}</option>
                  ))}
                </select>
              </label>
            </div>
          </div>
        ) : mode === 'sql' ? (
          <>
            <textarea
              className="h-32 w-full rounded border border-slate-300 p-2 font-mono text-sm"
              placeholder={`SELECT * FROM read_parquet('...') LIMIT 10`}
              value={sql}
              onChange={(e) => setSql(e.target.value)}
            />
            <p className="mt-1 text-xs text-slate-500">
              Read-only: a single SELECT, enforced by DuckDB&apos;s parser.
            </p>
          </>
        ) : (
          <div className="space-y-4">
            <div>
              <div className="mb-1 flex items-center gap-2">
                <span className="text-sm font-medium text-slate-700">Filters</span>
                <button
                  type="button"
                  onClick={() =>
                    setFilters([...filters, { column: columns[0] ?? '', op: '=', value: '' }])
                  }
                  className="rounded border border-slate-300 px-2 py-0.5 text-xs hover:bg-slate-50"
                >
                  + add
                </button>
              </div>
              {filters.map((f, i) => (
                <div key={i} className="mb-1 flex gap-2">
                  <select
                    value={f.column}
                    onChange={(e) =>
                      setFilters(filters.map((x, j) =>
                        j === i ? { ...x, column: e.target.value } : x))
                    }
                    className="rounded border border-slate-300 px-2 py-1 text-sm"
                  >
                    {columns.map((c) => (
                      <option key={c}>{c}</option>
                    ))}
                  </select>
                  <select
                    value={f.op}
                    onChange={(e) =>
                      setFilters(filters.map((x, j) =>
                        j === i ? { ...x, op: e.target.value } : x))
                    }
                    className="rounded border border-slate-300 px-2 py-1 text-sm"
                  >
                    {OPS.map((o) => (
                      <option key={o}>{o}</option>
                    ))}
                  </select>
                  {!f.op.startsWith('is_') && (
                    <input
                      value={String(f.value ?? '')}
                      onChange={(e) =>
                        setFilters(filters.map((x, j) =>
                          j === i
                            ? {
                                ...x,
                                value: x.op === 'in'
                                  ? e.target.value.split(',').map((s) => s.trim())
                                  : e.target.value,
                              }
                            : x))
                      }
                      placeholder={f.op === 'in' ? 'a, b, c' : 'value'}
                      className="flex-1 rounded border border-slate-300 px-2 py-1 text-sm"
                    />
                  )}
                  <button
                    type="button"
                    onClick={() => setFilters(filters.filter((_, j) => j !== i))}
                    className="px-2 text-sm text-slate-400 hover:text-rose-600"
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>

            <div>
              <span className="mb-1 block text-sm font-medium text-slate-700">Group by</span>
              <div className="flex flex-wrap gap-1.5">
                {columns.map((c) => (
                  <button
                    key={c}
                    type="button"
                    onClick={() =>
                      setGroupBy(groupBy.includes(c)
                        ? groupBy.filter((x) => x !== c)
                        : [...groupBy, c])
                    }
                    className={`rounded px-2 py-0.5 text-xs ${
                      groupBy.includes(c)
                        ? 'bg-slate-900 text-white'
                        : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
                    }`}
                  >
                    {c}
                  </button>
                ))}
              </div>
            </div>

            <div>
              <div className="mb-1 flex items-center gap-2">
                <span className="text-sm font-medium text-slate-700">Select</span>
                <button
                  type="button"
                  onClick={() => setSelects([...selects, { column: '*', agg: 'count' }])}
                  className="rounded border border-slate-300 px-2 py-0.5 text-xs hover:bg-slate-50"
                >
                  + add
                </button>
              </div>
              {selects.map((s, i) => (
                <div key={i} className="mb-1 flex gap-2">
                  <select
                    value={s.agg ?? ''}
                    onChange={(e) =>
                      setSelects(selects.map((x, j) =>
                        j === i ? { ...x, agg: e.target.value || null } : x))
                    }
                    className="rounded border border-slate-300 px-2 py-1 text-sm"
                  >
                    {AGGS.map((a) => (
                      <option key={a} value={a}>
                        {a || '(none)'}
                      </option>
                    ))}
                  </select>
                  <select
                    value={s.column}
                    onChange={(e) =>
                      setSelects(selects.map((x, j) =>
                        j === i ? { ...x, column: e.target.value } : x))
                    }
                    className="rounded border border-slate-300 px-2 py-1 text-sm"
                  >
                    <option value="*">*</option>
                    {columns.map((c) => (
                      <option key={c}>{c}</option>
                    ))}
                  </select>
                  <button
                    type="button"
                    onClick={() => setSelects(selects.filter((_, j) => j !== i))}
                    className="px-2 text-sm text-slate-400 hover:text-rose-600"
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          </div>
        )}

        {mode !== 'rows' && (
          <div className="mt-3 flex items-center gap-2">
            <button
              type="button"
              onClick={() => run()}
              className="rounded bg-slate-900 px-3 py-1.5 text-sm text-white hover:bg-slate-800"
            >
              Run
            </button>
            <label className="text-xs text-slate-500">
              limit{' '}
              <input
                type="number"
                value={limit}
                onChange={(e) => setLimit(Number(e.target.value))}
                className="w-20 rounded border border-slate-300 px-1.5 py-0.5 text-xs"
              />
            </label>
          </div>
        )}
      </div>

      {error && <p className="rounded bg-rose-50 p-3 text-sm text-rose-800">{error}</p>}

      {result && (
        <div>
          <p className="mb-1 text-xs text-slate-500">
            {result.row_count.toLocaleString()} rows in {result.elapsed_ms}ms
            {result.truncated && ' (truncated)'}
          </p>
          <TableRenderer
            spec={{ renderer: 'table', title: '', query: spec, spec: {}, description: '' }}
            data={result.rows.map((r) =>
              Object.fromEntries(result.columns.map((c, i) => [c, r[i]])),
            )}
            height={480}
          />
          <ChartInspector
            sql={result.sql}
            query={mode === 'sql' ? undefined : spec}
            rowCount={result.row_count}
            elapsedMs={result.elapsed_ms}
            truncated={result.truncated}
          />
        </div>
      )}
    </div>
  )
}
