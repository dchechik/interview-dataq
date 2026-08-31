import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { api, ApiError } from '../api/client'
import {
  keys,
  useDataset,
  useLineage,
  useOperation,
  usePlugins,
  useProfile,
  useRelated,
  useSuggestions,
  useVersions,
} from '../api/hooks'
import type {
  ColumnProfile,
  DatasetVersion,
  FormatCandidate,
  PluginDescriptor,
  RelatedDataset,
  SemanticType,
  Suggestion,
} from '../api/types'
import { FeatureDraft } from '../components/FeatureDraft'
import { JobProgress, StatusBadge } from '../components/JobProgress'
import { JoinPanel } from '../components/JoinPanel'
import type { JoinSeed } from '../components/JoinPanel'
import { MeaningSelect, useSemanticTypes } from '../components/MeaningSelect'
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

/** How profiling worked out this column reads, if it is temporal. */
function dateFormats(column: ColumnProfile): FormatCandidate[] {
  return column.candidates.find((c) => c.formats.length > 0)?.formats ?? []
}

const ROLES = ['dimension', 'measure', 'time', 'key', 'geo', 'ignore']

function ColumnRow({
  column,
  semanticTypes,
  onPin,
  onPinRole,
}: {
  column: ColumnProfile
  semanticTypes: SemanticType[]
  onPin: (name: string, type: string) => void
  onPinRole: (name: string, role: string) => void
}) {
  const [open, setOpen] = useState(false)
  const s = column.stats
  return (
    <>
      <tr className="border-t border-slate-100 hover:bg-slate-50/60">
        <td className="px-3 py-1.5">
          <button type="button" onClick={() => setOpen((o) => !o)} className="text-left">
            <span className="font-medium text-slate-800">{column.name}</span>
            {column.warning && (
              // The importer made a choice the data could not settle. Marked on
              // the row itself, because a warning only reachable by expanding
              // the column is a warning nobody sees.
              <span
                className="ml-1.5 rounded bg-amber-100 px-1 py-0.5 text-[10px] font-medium text-amber-900"
                title={column.warning}
              >
                check format
              </span>
            )}
          </button>
        </td>
        <td className="px-3 py-1.5 font-mono text-xs text-slate-500">{column.physical_type}</td>
        <td className="px-3 py-1.5">
          <MeaningSelect
            value={column.semantic_type ?? null}
            onChange={(type) => onPin(column.name, type ?? '')}
            types={semanticTypes}
            physicalType={column.physical_type}
            className={`rounded border-0 px-1.5 py-0.5 text-xs ${
              column.pinned ? 'bg-blue-100 text-blue-900' : 'bg-slate-100 text-slate-700'
            }`}
          />
          {column.pinned && <span className="ml-1 text-xs text-blue-600">pinned</span>}
        </td>
        <td className="px-3 py-1.5">
          {/* Editable because a role is a claim about what the column can do,
              and detection can get it wrong. pinColumnType has always accepted
              one; nothing ever sent it. */}
          <select
            value={column.role}
            onChange={(e) => onPinRole(column.name, e.target.value)}
            className={`rounded border-0 px-1.5 py-0.5 text-xs ${ROLE_COLORS[column.role]}`}
          >
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
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
            {column.warning && (
              <div className="mb-2 rounded border border-amber-200 bg-amber-50 p-2 text-amber-900">
                {column.warning}
              </div>
            )}
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
            {dateFormats(column).length > 0 && (
              <div className="mt-1">
                <span className="text-slate-500">reads as:</span>{' '}
                {dateFormats(column).map((f) => (
                  <span key={f.format} className="mr-2">
                    <code className="font-mono">{f.format}</code> ({f.label}
                    {f.success_rate < 1 ? `, ${Math.round(f.success_rate * 100)}%` : ''})
                    {f.example_input && (
                      <span className="text-slate-500">
                        {' '}
                        — {f.example_input} → {f.example_output}
                      </span>
                    )}
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
  // Once the user has typed, the draft stops overwriting them — changing the
  // actor should re-propose, but not silently discard what they wrote.
  const [edited, setEdited] = useState(false)
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
          setEdited(false)
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
          {/* The one plugin whose form opens with something in it. Its input is
              a language, and a blank box asking for one is a worse prompt than
              a draft you can edit. */}
          {plugin.id === 'enrich.features' && (
            <FeatureDraft
              datasetId={datasetId}
              hasEdits={edited}
              onUse={(expressions) => {
                setEdited(false)
                setParams((p) => ({ ...p, features: expressions }))
              }}
            />
          )}
          {/* The form is generated from the plugin's JSON Schema: a new backend
              plugin gets a working UI with no frontend change. */}
          <SchemaForm
            schema={plugin.params_schema}
            value={params}
            onChange={(next) => {
              setEdited(true)
              setParams(next)
            }}
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
  onRefine,
}: {
  suggestion: Suggestion
  datasetId: string
  onJob: (id: string) => void
  /** Load a join suggestion into the join panel rather than running it. */
  onRefine: (seed: JoinSeed) => void
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

  // A join suggestion is a guess at one column pair. It is often the right one,
  // and when it is not, the useful thing is to start from it rather than to be
  // told to build the whole join again by hand.
  const joinSeed = (): JoinSeed | null => {
    const inputs = action.inputs as { dataset_id: string }[] | undefined
    const params = (action.params ?? {}) as Record<string, unknown>
    const right = inputs?.[1]?.dataset_id
    if (action.op !== 'join' || !right) return null
    const on = (params.on as { left: string; right: string }[] | undefined) ?? [
      { left: String(params.left_column ?? ''), right: String(params.right_column ?? '') },
    ]
    return {
      rightId: right,
      keys: on,
      how: params.how === 'inner' ? 'inner' : 'left',
      prefix: String(params.prefix ?? ''),
    }
  }
  const seed = joinSeed()

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
          <div className="flex gap-1.5">
            <button
              type="button"
              onClick={apply}
              disabled={operation.isPending}
              className="rounded bg-slate-900 px-2 py-1 text-xs text-white hover:bg-slate-800 disabled:opacity-40"
            >
              {action.op === 'transform' ? 'Add columns' : 'Create'}
            </button>
            {seed && (
              <button
                type="button"
                onClick={() => onRefine(seed)}
                className="rounded border border-slate-300 px-2 py-1 text-xs hover:bg-slate-50"
              >
                Edit
              </button>
            )}
          </div>
        )}
      </div>
      {error && <p className="mt-1 text-xs text-rose-700">{error}</p>}
    </div>
  )
}

/**
 * Deleting a dataset, with its consequences shown before the button is armed.
 *
 * Deletion is the one action here that re-running a step cannot undo, so it is
 * the one that has to say what it will do first: how many rows go, how much
 * disk comes back, and -- the part that is easy to miss -- which other datasets
 * were built from this one. The backend refuses to strand those, so the dialog
 * asks rather than discovering it in an error.
 */
function DeleteDataset({
  datasetId,
  name,
  rowCount,
}: {
  datasetId: string
  name: string
  rowCount: number
}) {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Only asked for once the dialog opens: there is no reason to walk the
  // derivation tree of every dataset somebody merely looks at.
  const { data: dependents } = useQuery({
    queryKey: ['dependents', datasetId],
    queryFn: () => api.dependents(datasetId),
    enabled: open,
  })

  async function remove() {
    setBusy(true)
    setError(null)
    try {
      await api.deleteDataset(datasetId, Boolean(dependents?.length))
      qc.invalidateQueries({ queryKey: keys.datasets })
      qc.invalidateQueries({ queryKey: ['related'] })
      navigate('/datasets')
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
      setBusy(false)
    }
  }

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="rounded border border-slate-300 px-3 py-1.5 text-sm text-slate-600 hover:border-rose-300 hover:bg-rose-50 hover:text-rose-700"
      >
        Delete
      </button>
    )
  }

  const alsoGoing = dependents ?? []
  return (
    // basis-full breaks it onto its own line: the header is a wrapping flex
    // row, and left inline the panel squeezes the nav buttons into columns.
    <div className="w-full basis-full rounded-lg border border-rose-200 bg-rose-50 p-3">
      <p className="text-sm text-rose-900">
        Delete <span className="font-medium">{name}</span> and its{' '}
        {rowCount.toLocaleString()} rows? This cannot be undone.
      </p>

      {alsoGoing.length > 0 && (
        <div className="mt-2 text-sm text-rose-900">
          <p>
            {alsoGoing.length} dataset{alsoGoing.length > 1 ? 's were' : ' was'} built
            from this one and will be deleted too:
          </p>
          <ul className="mt-1 space-y-0.5">
            {alsoGoing.map((d) => (
              <li key={d.id} className="font-medium">
                · {d.name}{' '}
                <span className="font-normal text-rose-700">({d.kind})</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {error && <p className="mt-2 text-sm text-rose-800">{error}</p>}

      <div className="mt-3 flex gap-2">
        <button
          type="button"
          onClick={remove}
          disabled={busy}
          className="rounded bg-rose-600 px-3 py-1.5 text-sm text-white hover:bg-rose-700 disabled:opacity-40"
        >
          {busy
            ? 'Deleting…'
            : alsoGoing.length
              ? `Delete all ${alsoGoing.length + 1}`
              : 'Delete'}
        </button>
        <button
          type="button"
          onClick={() => {
            setOpen(false)
            setError(null)
          }}
          disabled={busy}
          className="rounded border border-rose-300 px-3 py-1.5 text-sm text-rose-800 hover:bg-rose-100"
        >
          Cancel
        </button>
      </div>
    </div>
  )
}

/**
 * The version history, with the two things you can do to it.
 *
 * Revert copies an older version forward as a new one rather than moving a
 * pointer back, so the row you reverted *from* is still listed afterwards and
 * the revert can itself be reverted. That is why the button says "Restore"
 * rather than "Roll back": nothing is being unwound.
 *
 * Delete is only offered on older rows. The current version is what every
 * query, chart and dashboard reads, and its number is the one the next write
 * counts from — the backend refuses it, and an armed button that always errors
 * is a worse explanation than no button.
 */
function VersionHistory({
  datasetId,
  versions,
  onJob,
}: {
  datasetId: string
  versions: DatasetVersion[]
  onJob: (id: string) => void
}) {
  const qc = useQueryClient()
  // One row at a time is confirming, so the panel cannot be open twice.
  const [confirming, setConfirming] = useState<number | null>(null)
  const [busy, setBusy] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function restore(version: number) {
    setError(null)
    setBusy(version)
    try {
      const accepted = await api.revertDataset(datasetId, version)
      onJob(accepted.job_id)
      // The new version only exists once the job finishes; the watcher
      // invalidates the dataset caches then. This is just the list itself.
      qc.invalidateQueries({ queryKey: keys.versions(datasetId) })
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  async function remove(version: number) {
    setError(null)
    setBusy(version)
    try {
      await api.deleteVersion(datasetId, version)
      setConfirming(null)
      qc.invalidateQueries({ queryKey: keys.versions(datasetId) })
      qc.invalidateQueries({ queryKey: keys.lineage(datasetId) })
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  return (
    <>
      <div className="overflow-hidden rounded-lg border border-slate-200 bg-white">
        <table className="w-full text-sm">
          <tbody>
            {versions.map((v) => (
              <tr key={v.version} className="border-b border-slate-100 last:border-0">
                <td className="px-3 py-1.5 font-medium">
                  v{v.version}
                  {v.is_current && (
                    <span className="ml-1.5 rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] font-medium text-emerald-800">
                      current
                    </span>
                  )}
                </td>
                <td className="px-3 py-1.5 text-slate-600">
                  {v.row_count.toLocaleString()} rows
                </td>
                <td className="px-3 py-1.5 text-slate-500">{v.columns} cols</td>
                <td className="px-3 py-1.5 text-xs text-slate-400">
                  {new Date(v.created_at).toLocaleString()}
                </td>
                <td className="px-3 py-1.5 text-right whitespace-nowrap">
                  {!v.is_current && (
                    <>
                      <button
                        type="button"
                        onClick={() => restore(v.version)}
                        disabled={busy != null}
                        className="rounded border border-slate-300 px-2 py-0.5 text-xs hover:bg-slate-50 disabled:opacity-40"
                        title={`Copy v${v.version} forward as the current version`}
                      >
                        {busy === v.version && confirming == null ? 'Restoring…' : 'Restore'}
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          setError(null)
                          setConfirming(v.version)
                        }}
                        disabled={busy != null}
                        className="ml-1.5 rounded border border-slate-300 px-2 py-0.5 text-xs text-slate-600 hover:border-rose-300 hover:bg-rose-50 hover:text-rose-700 disabled:opacity-40"
                      >
                        Delete
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {confirming != null && (
        <div className="mt-2 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900">
          <p>
            Delete <span className="font-medium">v{confirming}</span> and free its
            storage? The data goes; the step that produced it stays in the lineage,
            and no other version is touched.
          </p>
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              onClick={() => remove(confirming)}
              disabled={busy != null}
              className="rounded bg-rose-600 px-3 py-1 text-xs text-white hover:bg-rose-700 disabled:opacity-40"
            >
              {busy === confirming ? 'Deleting…' : `Delete v${confirming}`}
            </button>
            <button
              type="button"
              onClick={() => setConfirming(null)}
              disabled={busy != null}
              className="rounded border border-rose-300 px-3 py-1 text-xs text-rose-800 hover:bg-rose-100"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {error && <p className="mt-2 rounded bg-rose-50 p-2 text-sm text-rose-800">{error}</p>}

      {versions.length > 1 && (
        <p className="mt-1 text-xs text-slate-500">
          Restoring copies an older version forward as a new one, so nothing is
          lost and the restore can be undone the same way.
        </p>
      )}
    </>
  )
}


function RelatedRow({
  item,
  direction,
}: {
  item: RelatedDataset
  direction: 'parent' | 'child'
}) {
  return (
    <Link
      to={`/datasets/${item.id}`}
      className="flex items-center gap-2 rounded border border-slate-200 bg-white px-3 py-1.5 text-sm hover:bg-slate-50"
    >
      <span className="text-slate-400">{direction === 'parent' ? '↑' : '↳'}</span>
      <span className="font-medium text-slate-800">{item.name}</span>
      <span
        className={`rounded px-1.5 py-0.5 text-xs ${
          RELATED_KIND_STYLES[item.kind] ?? 'bg-slate-100 text-slate-600'
        }`}
      >
        {item.kind}
      </span>
      <span className="truncate text-xs text-slate-500">
        {item.role === 'joined' ? 'joined in' : item.plugin_id || item.op}
      </span>
      {item.row_count != null && (
        <span className="ml-auto font-mono text-xs tabular-nums text-slate-400">
          {item.row_count.toLocaleString()} rows
        </span>
      )}
    </Link>
  )
}

const RELATED_KIND_STYLES: Record<string, string> = {
  source: 'bg-slate-100 text-slate-600',
  derived: 'bg-sky-100 text-sky-700',
  aggregate: 'bg-violet-100 text-violet-700',
  join: 'bg-emerald-100 text-emerald-700',
}

export function DatasetPage() {
  const { id = '' } = useParams()
  const qc = useQueryClient()
  const { data: dataset } = useDataset(id)
  const { data: profile } = useProfile(id)
  const { data: versions } = useVersions(id)
  const { data: lineage } = useLineage(id)
  const { data: related } = useRelated(id)
  const { data: suggestions } = useSuggestions(id)
  const { data: applicable } = usePlugins({ applicable_to: id })
  const [jobId, setJobId] = useState<string | null>(null)
  const [joinSeed, setJoinSeed] = useState<JoinSeed | null>(null)

  // The semantic type vocabulary is static per deployment, so it caches well.
  const { data: semanticTypeRows } = useSemanticTypes()
  const semanticTypes = semanticTypeRows ?? []

  const columns = profile?.columns.map((c) => c.name) ?? []

  async function pin(column: string, type: string) {
    await api.pinColumnType(id, column, type || null)
    qc.invalidateQueries({ queryKey: keys.profile(id) })
    qc.invalidateQueries({ queryKey: keys.suggestions(id) })
  }

  async function pinRole(column: string, role: string) {
    const current = profile?.columns.find((c) => c.name === column)
    await api.pinColumnType(id, column, current?.semantic_type ?? null, role)
    qc.invalidateQueries({ queryKey: keys.profile(id) })
    qc.invalidateQueries({ queryKey: keys.suggestions(id) })
  }

  if (!profile) return <p className="text-sm text-slate-500">Loading…</p>

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold text-slate-900">
          {dataset?.name ?? 'Dataset'}
        </h1>
        {dataset && dataset.kind !== 'source' && (
          <span className="rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-600">
            {dataset.kind}
          </span>
        )}
        <code
          className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-xs text-slate-500"
          title="Dataset id"
        >
          {id}
        </code>
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
            className="rounded border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50"
          >
            Explore
          </Link>
          {profile.columns.some((c) => c.role === 'time') && (
            <Link
              to={`/datasets/${id}/timeline`}
              className="rounded border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50"
            >
              Timeline
            </Link>
          )}
          <Link
            to={`/datasets/${id}/ask`}
            className="rounded bg-slate-900 px-3 py-1.5 text-sm text-white hover:bg-slate-800"
          >
            Ask
          </Link>
        </div>
        {/* Outside the button row, so the confirmation panel wraps onto its own
            line instead of squeezing the nav links into columns. */}
        <DeleteDataset
          datasetId={id}
          name={dataset?.name ?? id}
          rowCount={profile.row_count}
        />
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
                    onPinRole={pinRole}
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
            <h2 className="mb-2 text-base font-semibold text-slate-900">
              Join another dataset
            </h2>
            <div className="rounded-lg border border-slate-200 bg-white p-3">
              <JoinPanel
                datasetId={id}
                columns={columns}
                seed={joinSeed}
                onJob={setJobId}
              />
            </div>
          </section>

          <section>
            <h2 className="mb-2 text-base font-semibold text-slate-900">Suggestions</h2>
            <div className="space-y-2">
              {suggestions?.slice(0, 6).map((s, i) => (
                <SuggestionCard
                  key={i}
                  suggestion={s}
                  datasetId={id}
                  onJob={setJobId}
                  onRefine={setJoinSeed}
                />
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
          <VersionHistory datasetId={id} versions={versions ?? []} onJob={setJobId} />
        </section>

        <section>
          <h2 className="mb-2 text-base font-semibold text-slate-900">Related datasets</h2>
          {!related?.parents.length && !related?.children.length && (
            <p className="rounded border border-dashed border-slate-300 p-4 text-center text-sm text-slate-500">
              Nothing derived from this yet — try a suggested aggregate.
            </p>
          )}

          {related && related.parents.length > 0 && (
            <div className="mb-3">
              <p className="mb-1 text-xs uppercase tracking-wide text-slate-500">Derived from</p>
              <div className="space-y-1.5">
                {related.parents.map((p) => (
                  <RelatedRow key={`${p.id}-${p.role}`} item={p} direction="parent" />
                ))}
              </div>
            </div>
          )}

          {related && related.children.length > 0 && (
            <div>
              <p className="mb-1 text-xs uppercase tracking-wide text-slate-500">
                Derived from this ({related.children.length})
              </p>
              <div className="space-y-1.5">
                {related.children.map((c) => (
                  <RelatedRow key={`${c.id}-${c.role}`} item={c} direction="child" />
                ))}
              </div>
            </div>
          )}
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
