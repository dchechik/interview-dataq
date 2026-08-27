import { useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { api, ApiError } from '../api/client'
import {
  keys,
  useLineage,
  useOperation,
  usePlugins,
  useProfile,
  useSuggestions,
  useVersions,
} from '../api/hooks'
import type { ColumnProfile, PluginDescriptor, Suggestion } from '../api/types'
import { JobProgress, StatusBadge } from '../components/JobProgress'
import { SchemaForm } from '../components/SchemaForm'

const ROLE_COLORS: Record<string, string> = {
  time: 'bg-violet-100 text-violet-800',
  measure: 'bg-emerald-100 text-emerald-800',
  geo: 'bg-sky-100 text-sky-800',
  key: 'bg-amber-100 text-amber-800',
  dimension: 'bg-slate-100 text-slate-700',
  ignore: 'bg-slate-100 text-slate-400',
}

const MODE_HINT: Record<string, string> = {
  pushdown: 'runs as one SQL statement',
  batch: 'streams rows through Python',
  external: 'makes network/LLM calls — cached and cost-capped',
  inspect: 'instant, read-only',
}

function ColumnRow({
  column,
  semanticTypes,
  onPin,
}: {
  column: ColumnProfile
  semanticTypes: string[]
  onPin: (name: string, type: string) => void
}) {
  const [open, setOpen] = useState(false)
  const s = column.stats
  return (
    <>
      <tr className="border-t border-slate-100 hover:bg-slate-50/60">
        <td className="px-3 py-1.5">
          <button type="button" onClick={() => setOpen((o) => !o)} className="text-left">
            <span className="font-medium text-slate-800">{column.name}</span>
          </button>
        </td>
        <td className="px-3 py-1.5 font-mono text-xs text-slate-500">{column.physical_type}</td>
        <td className="px-3 py-1.5">
          <select
            value={column.semantic_type ?? ''}
            onChange={(e) => onPin(column.name, e.target.value)}
            className={`rounded border-0 px-1.5 py-0.5 text-xs ${
              column.pinned ? 'bg-blue-100 text-blue-900' : 'bg-slate-100 text-slate-700'
            }`}
          >
            <option value="">—</option>
            {semanticTypes.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
          {column.pinned && <span className="ml-1 text-xs text-blue-600">pinned</span>}
        </td>
        <td className="px-3 py-1.5">
          <span className={`rounded px-1.5 py-0.5 text-xs ${ROLE_COLORS[column.role]}`}>
            {column.role}
          </span>
        </td>
        <td className="px-3 py-1.5 text-right font-mono text-xs text-slate-500">
          {s ? s.distinct_count.toLocaleString() : '—'}
        </td>
        <td className="px-3 py-1.5 text-right font-mono text-xs text-slate-500">
          {s ? `${Math.round(s.null_count / Math.max(1, s.row_count) * 100)}%` : '—'}
        </td>
      </tr>
      {open && s && (
        <tr className="bg-slate-50">
          <td colSpan={6} className="px-3 py-2 text-xs text-slate-600">
            <div className="mb-1">
              <span className="text-slate-500">range:</span>{' '}
              <code className="font-mono">
                {String(s.min)} … {String(s.max)}
              </code>
            </div>
            <div className="mb-1">
              <span className="text-slate-500">samples:</span>{' '}
              <code className="font-mono">
                {s.sample_values.slice(0, 8).map(String).join(', ')}
              </code>
            </div>
            {column.candidates.length > 0 && (
              <div>
                <span className="text-slate-500">detected:</span>{' '}
                {column.candidates.map((c) => (
                  <span key={c.semantic_type} className="mr-2">
                    {c.semantic_type} ({Math.round(c.confidence * 100)}%) — {c.rationale}
                  </span>
                ))}
              </div>
            )}
          </td>
        </tr>
      )}
    </>
  )
}

function RunPluginPanel({
  datasetId,
  plugins,
  columns,
  onJob,
}: {
  datasetId: string
  plugins: PluginDescriptor[]
  columns: string[]
  onJob: (id: string) => void
}) {
  const [selected, setSelected] = useState<string>('')
  const [params, setParams] = useState<Record<string, unknown>>({})
  const [error, setError] = useState<string | null>(null)
  const operation = useOperation()

  const plugin = plugins.find((p) => p.id === selected)
  const op = plugin?.kind === 'aggregator' ? 'aggregate' : 'transform'

  async function run() {
    if (!plugin) return
    setError(null)
    try {
      const accepted = await operation.mutateAsync({
        op,
        plugin_id: plugin.id,
        inputs: [{ dataset_id: datasetId }],
        params,
      })
      onJob(accepted.job_id)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }

  return (
    <div className="space-y-3">
      <select
        value={selected}
        onChange={(e) => {
          setSelected(e.target.value)
          setParams({})
        }}
        className="w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
      >
        <option value="">Choose an action…</option>
        <optgroup label="Transform">
          {plugins
            .filter((p) => p.kind === 'transform')
            .map((p) => (
              <option key={p.id} value={p.id}>
                {p.title}
              </option>
            ))}
        </optgroup>
        <optgroup label="Aggregate">
          {plugins
            .filter((p) => p.kind === 'aggregator')
            .map((p) => (
              <option key={p.id} value={p.id}>
                {p.title}
              </option>
            ))}
        </optgroup>
      </select>

      {plugin && (
        <>
          <p className="text-xs text-slate-500">
            {plugin.summary}
            <span className="ml-1 text-slate-400">
              ({plugin.mode} — {MODE_HINT[plugin.mode]})
            </span>
            {plugin.cost_class === 'expensive' && (
              <span className="ml-1 rounded bg-amber-100 px-1 text-amber-800">costs money</span>
            )}
          </p>
          {/* The form is generated from the plugin's JSON Schema: a new backend
              plugin gets a working UI with no frontend change. */}
          <SchemaForm
            schema={plugin.params_schema}
            value={params}
            onChange={setParams}
            columns={columns}
          />
          <button
            type="button"
            onClick={run}
            disabled={operation.isPending}
            className="rounded bg-slate-900 px-3 py-1.5 text-sm text-white hover:bg-slate-800 disabled:opacity-40"
          >
            Run
          </button>
        </>
      )}
      {error && <p className="rounded bg-rose-50 p-2 text-sm text-rose-800">{error}</p>}
    </div>
  )
}

function SuggestionCard({
  suggestion,
  datasetId,
  onJob,
}: {
  suggestion: Suggestion
  datasetId: string
  onJob: (id: string) => void
}) {
  const operation = useOperation()
  const [error, setError] = useState<string | null>(null)
  const action = suggestion.action as Record<string, unknown>
  const isInspect = action.op === 'inspect'

  async function apply() {
    setError(null)
    try {
      // A suggestion's action is an executable payload; replay it verbatim.
      const accepted = await operation.mutateAsync(action as never)
      onJob(accepted.job_id)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }

  return (
    <div className="rounded border border-slate-200 bg-white p-3">
      <div className="flex items-start justify-between gap-2">
        <span className="text-sm font-medium text-slate-800">{suggestion.title}</span>
        <span className="rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-600">
          {suggestion.kind}
        </span>
      </div>
      <p className="mt-1 text-xs text-slate-500">{suggestion.rationale}</p>
      <div className="mt-2">
        {isInspect ? (
          <Link
            to={`/datasets/${datasetId}/explore`}
            className="rounded border border-slate-300 px-2 py-1 text-xs hover:bg-slate-50"
          >
            View chart
          </Link>
        ) : (
          <button
            type="button"
            onClick={apply}
            disabled={operation.isPending}
            className="rounded bg-slate-900 px-2 py-1 text-xs text-white hover:bg-slate-800 disabled:opacity-40"
          >
            Create
          </button>
        )}
      </div>
      {error && <p className="mt-1 text-xs text-rose-700">{error}</p>}
    </div>
  )
}

export function DatasetPage() {
  const { id = '' } = useParams()
  const qc = useQueryClient()
  const { data: profile } = useProfile(id)
  const { data: versions } = useVersions(id)
  const { data: lineage } = useLineage(id)
  const { data: suggestions } = useSuggestions(id)
  const { data: applicable } = usePlugins({ applicable_to: id })
  const [jobId, setJobId] = useState<string | null>(null)
  const [semanticTypes, setSemanticTypes] = useState<string[]>([])

  useMemo(() => {
    api.semanticTypes().then((t) => setSemanticTypes(t.map((x) => x.id)))
  }, [])

  const columns = profile?.columns.map((c) => c.name) ?? []

  async function pin(column: string, type: string) {
    await api.pinColumnType(id, column, type || null)
    qc.invalidateQueries({ queryKey: keys.profile(id) })
    qc.invalidateQueries({ queryKey: keys.suggestions(id) })
  }

  if (!profile) return <p className="text-sm text-slate-500">Loading…</p>

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold text-slate-900">{id}</h1>
        <span className="text-sm text-slate-500">
          {profile.row_count.toLocaleString()} rows · v{profile.version} ·{' '}
          {profile.columns.length} columns
        </span>
        <div className="ml-auto flex gap-2">
          <Link
            to={`/datasets/${id}/query`}
            className="rounded border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50"
          >
            Query
          </Link>
          <Link
            to={`/datasets/${id}/explore`}
            className="rounded bg-slate-900 px-3 py-1.5 text-sm text-white hover:bg-slate-800"
          >
            Explore
          </Link>
        </div>
      </div>

      <JobProgress jobId={jobId} />

      <div className="grid gap-6 lg:grid-cols-3">
        <section className="lg:col-span-2">
          <h2 className="mb-2 text-base font-semibold text-slate-900">Schema</h2>
          <div className="overflow-hidden rounded-lg border border-slate-200 bg-white">
            <table className="w-full text-sm">
              <thead className="bg-slate-50 text-xs text-slate-600">
                <tr>
                  <th className="px-3 py-2 text-left font-medium">Column</th>
                  <th className="px-3 py-2 text-left font-medium">Type</th>
                  <th className="px-3 py-2 text-left font-medium">Meaning</th>
                  <th className="px-3 py-2 text-left font-medium">Role</th>
                  <th className="px-3 py-2 text-right font-medium">Distinct</th>
                  <th className="px-3 py-2 text-right font-medium">Null</th>
                </tr>
              </thead>
              <tbody>
                {profile.columns.map((c) => (
                  <ColumnRow
                    key={c.name}
                    column={c}
                    semanticTypes={semanticTypes}
                    onPin={pin}
                  />
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-1 text-xs text-slate-500">
            Click a column for stats. Changing “Meaning” pins it against re-detection.
          </p>
        </section>

        <div className="space-y-6">
          <section>
            <h2 className="mb-2 text-base font-semibold text-slate-900">Run an action</h2>
            <div className="rounded-lg border border-slate-200 bg-white p-3">
              <RunPluginPanel
                datasetId={id}
                plugins={applicable ?? []}
                columns={columns}
                onJob={setJobId}
              />
            </div>
          </section>

          <section>
            <h2 className="mb-2 text-base font-semibold text-slate-900">Suggestions</h2>
            <div className="space-y-2">
              {suggestions?.slice(0, 6).map((s, i) => (
                <SuggestionCard key={i} suggestion={s} datasetId={id} onJob={setJobId} />
              ))}
              {!suggestions?.length && (
                <p className="text-sm text-slate-500">No suggestions.</p>
              )}
            </div>
          </section>
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <section>
          <h2 className="mb-2 text-base font-semibold text-slate-900">Versions</h2>
          <div className="overflow-hidden rounded-lg border border-slate-200 bg-white">
            <table className="w-full text-sm">
              <tbody>
                {versions?.map((v) => (
                  <tr key={v.version} className="border-b border-slate-100 last:border-0">
                    <td className="px-3 py-1.5 font-medium">v{v.version}</td>
                    <td className="px-3 py-1.5 text-slate-600">
                      {v.row_count.toLocaleString()} rows
                    </td>
                    <td className="px-3 py-1.5 text-slate-500">{v.columns} cols</td>
                    <td className="px-3 py-1.5 text-xs text-slate-400">
                      {new Date(v.created_at).toLocaleString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section>
          <h2 className="mb-2 text-base font-semibold text-slate-900">Lineage</h2>
          <div className="space-y-1.5">
            {lineage?.map((s) => (
              <div
                key={s.id}
                className="flex items-center gap-2 rounded border border-slate-200 bg-white px-3 py-1.5 text-sm"
              >
                <span className="rounded bg-slate-100 px-1.5 py-0.5 text-xs">{s.op}</span>
                <code className="font-mono text-xs text-slate-700">{s.plugin_id || '—'}</code>
                <span className="text-xs text-slate-500">
                  {Object.entries(s.params)
                    .map(([k, v]) => `${k}=${String(v)}`)
                    .join(' ')}
                </span>
                <span className="ml-auto text-xs text-slate-400">
                  {s.rows.toLocaleString()} rows
                </span>
                <StatusBadge status={s.status} />
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  )
}
