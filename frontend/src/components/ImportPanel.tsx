import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { api, ApiError } from '../api/client'
import { useOperation } from '../api/hooks'
import type {
  ColumnPlan,
  ColumnProposal,
  ColumnRole,
  ImportPlan,
  Job,
  TargetType,
} from '../api/types'
import { FileBrowser } from './FileBrowser'
import { JobProgress } from './JobProgress'

/**
 * Importing a dataset, with the column types shown before they are frozen.
 *
 * A column's physical type is decided by the reader the moment the file is
 * read, is copied into every record of the dataset, and can never be changed
 * afterwards — the only escape is a transform that adds a second column beside
 * it. Meaning and role, by contrast, are worked out later and stay editable
 * forever. So the one decision that cannot be revisited was the one nobody was
 * ever shown, which is how a column of dates ends up stored as text.
 *
 * The plan arrives already decided. The normal path is to look at it and press
 * Import; the only thing that stops is a column the data genuinely cannot
 * settle, and then it says which readings are on offer and what each would mean.
 */

const TARGET_TYPES: TargetType[] = [
  'VARCHAR', 'BIGINT', 'DOUBLE', 'BOOLEAN', 'DATE', 'TIMESTAMP',
]
const ROLES: ColumnRole[] = ['dimension', 'measure', 'time', 'key', 'geo', 'ignore']

/** Friendlier than the SQL spelling, for a table a non-technical user reads. */
const TYPE_LABELS: Record<string, string> = {
  VARCHAR: 'text',
  BIGINT: 'whole number',
  DOUBLE: 'number',
  BOOLEAN: 'true/false',
  DATE: 'date',
  TIMESTAMP: 'date + time',
}

function typeLabel(sql: string): string {
  const key = sql.toUpperCase()
  return TYPE_LABELS[key] ?? sql.toLowerCase()
}

const cell = 'px-2 py-1.5 align-top'
const control = 'w-full rounded border border-slate-300 bg-white px-1.5 py-1 text-xs'

function ColumnPlanRow({
  proposal,
  plan,
  onChange,
  semanticTypes,
  needsChoice,
}: {
  proposal: ColumnProposal
  plan: ColumnPlan
  onChange: (next: ColumnPlan) => void
  semanticTypes: string[]
  needsChoice: boolean
}) {
  // Any edit marks the column as overridden, which is what freezes it against
  // re-detection. Accepting a proposal untouched leaves detection in charge.
  const set = (patch: Partial<ColumnPlan>) =>
    onChange({ ...plan, ...patch, pinned: true })
  const target = plan.target_type ?? null
  const temporal = target === 'TIMESTAMP' || target === 'DATE'

  return (
    <>
      <tr className={`border-t border-slate-100 ${needsChoice ? 'bg-amber-50' : ''}`}>
        <td className={`${cell} font-medium text-slate-800`}>{proposal.name}</td>
        <td className={`${cell} text-slate-500`}>{typeLabel(proposal.source_type)}</td>

        <td className={cell}>
          <select
            className={control}
            value={target ?? ''}
            onChange={(e) =>
              set({
                target_type: (e.target.value || null) as TargetType | null,
                // A format is meaningless once the target is not temporal, and
                // the backend refuses the combination rather than ignoring it.
                format: e.target.value === 'TIMESTAMP' || e.target.value === 'DATE'
                  ? plan.format
                  : null,
              })
            }
          >
            <option value="">as read ({typeLabel(proposal.source_type)})</option>
            {TARGET_TYPES.map((t) => (
              <option key={t} value={t}>{typeLabel(t)}</option>
            ))}
          </select>
        </td>

        <td className={cell}>
          {temporal && proposal.formats.length > 0 ? (
            <select
              className={`${control} ${needsChoice ? 'border-amber-400' : ''}`}
              value={plan.format ?? ''}
              onChange={(e) => set({ format: e.target.value || null })}
            >
              {needsChoice && <option value="">— choose —</option>}
              {proposal.formats.map((f) => (
                <option key={f.format} value={f.format}>
                  {f.label}
                  {f.example_output ? ` → ${f.example_output}` : ''}
                </option>
              ))}
            </select>
          ) : (
            <span className="text-slate-300">—</span>
          )}
        </td>

        <td className={cell}>
          <select
            className={control}
            value={plan.semantic_type ?? ''}
            onChange={(e) => set({ semantic_type: e.target.value || null })}
          >
            <option value="">—</option>
            {semanticTypes.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
        </td>

        <td className={cell}>
          <select
            className={control}
            value={plan.role ?? ''}
            onChange={(e) => set({ role: (e.target.value || null) as ColumnRole | null })}
          >
            <option value="">—</option>
            {ROLES.map((r) => (
              <option key={r} value={r}>{r}</option>
            ))}
          </select>
        </td>

        <td className={`${cell} font-mono text-[11px] text-slate-500`}>
          {proposal.sample_values.slice(0, 2).map(String).join(', ')}
        </td>
      </tr>

      {(needsChoice || proposal.rationale) && (
        <tr className={needsChoice ? 'bg-amber-50' : ''}>
          <td />
          <td colSpan={6} className="px-2 pb-1.5 text-[11px]">
            {needsChoice ? (
              <span className="text-amber-900">
                Both readings fit every value: {proposal.conflict}. Pick one.
              </span>
            ) : (
              <span className="text-slate-500">
                {proposal.rationale}
                {proposal.parse_rate !== null && proposal.parse_rate < 1 && (
                  <span className="text-amber-700">
                    {' '}· {Math.round((1 - proposal.parse_rate) * 100)}% of sampled
                    values would not convert and become empty
                  </span>
                )}
              </span>
            )}
          </td>
        </tr>
      )}
    </>
  )
}

export function ImportPanel() {
  const navigate = useNavigate()
  const operation = useOperation()
  const [uri, setUri] = useState('')
  const [name, setName] = useState('')
  const [plan, setPlan] = useState<ImportPlan | null>(null)
  const [edits, setEdits] = useState<Record<string, ColumnPlan>>({})
  const [error, setError] = useState<string | null>(null)
  const [jobId, setJobId] = useState<string | null>(null)
  const [browsing, setBrowsing] = useState(false)
  const [planning, setPlanning] = useState(false)

  const { data: semanticTypeRows } = useQuery({
    queryKey: ['semantic-types'],
    queryFn: api.semanticTypes,
    staleTime: Infinity,
  })
  const semanticTypes = semanticTypeRows?.map((t) => t.id) ?? []

  async function buildPlan(target = uri) {
    setError(null)
    setPlan(null)
    setJobId(null)
    setPlanning(true)
    try {
      const next = await api.planImport(target)
      setPlan(next)
      setEdits(
        Object.fromEntries(
          next.columns.map((c) => [
            c.name,
            // An undecided column starts blank, so Import stays disabled until
            // somebody actually chooses rather than accepting a coin toss.
            c.decision_required ? { ...c.proposed, format: null } : c.proposed,
          ]),
        ),
      )
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setPlanning(false)
    }
  }

  /** Picking a file fills the path, names the dataset after it, and plans it. */
  function handlePicked(picked: string) {
    setUri(picked)
    if (!name) {
      const base = picked.split('/').pop() ?? ''
      setName(base.replace(/\.(gz)$/, '').replace(/\.[^.]+$/, ''))
    }
    buildPlan(picked)
  }

  async function goToNewDataset(job: Job) {
    try {
      const full = await api.job(job.id)
      const dataset = full.steps?.[0]?.outputs?.[0]?.dataset_id
      if (dataset) navigate(`/datasets/${dataset}`)
    } catch {
      // Landing on the list is a fine outcome; it refreshes either way.
    }
  }

  const undecided = (plan?.columns ?? []).filter(
    (c) => c.decision_required && !edits[c.name]?.format,
  )

  async function doImport() {
    setError(null)
    try {
      const accepted = await operation.mutateAsync({
        op: 'import',
        uri,
        name: name || undefined,
        params: plan ? { columns: Object.values(edits) } : undefined,
      })
      setJobId(accepted.job_id)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-4">
      <h2 className="mb-3 text-base font-semibold text-slate-900">Import a dataset</h2>

      <div className="flex flex-wrap gap-2">
        <div className="flex min-w-96 flex-1 rounded border border-slate-300 focus-within:ring-1 focus-within:ring-slate-400">
          <input
            className="flex-1 rounded-l px-3 py-1.5 text-sm outline-none"
            placeholder="Choose a file, or type a path or glob"
            value={uri}
            onChange={(e) => {
              setUri(e.target.value)
              // A plan for the previous file would be a lie about this one.
              setPlan(null)
            }}
          />
          <button
            type="button"
            onClick={() => setBrowsing(true)}
            className="rounded-r border-l border-slate-300 bg-slate-50 px-3 text-sm text-slate-700 hover:bg-slate-100"
          >
            Browse…
          </button>
        </div>
        <input
          className="w-48 rounded border border-slate-300 px-3 py-1.5 text-sm"
          placeholder="name (optional)"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <button
          type="button"
          onClick={() => buildPlan()}
          disabled={!uri || planning}
          className="rounded border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50 disabled:opacity-40"
        >
          {planning ? 'Reading…' : plan ? 'Re-read' : 'Read columns'}
        </button>
      </div>

      {error && <p className="mt-2 rounded bg-rose-50 p-2 text-sm text-rose-800">{error}</p>}

      {plan && (
        <div className="mt-3">
          <p className="mb-1 text-xs text-slate-500">
            Read by <code className="font-mono">{plan.reader}</code> ·{' '}
            {plan.columns.length} columns · types worked out from{' '}
            {plan.sampled_rows.toLocaleString()} sampled rows
          </p>

          <div className="overflow-auto rounded border border-slate-200">
            <table className="w-full text-xs">
              <thead className="bg-slate-50 text-slate-600">
                <tr>
                  <th className="px-2 py-1.5 text-left font-medium">Column</th>
                  <th className="px-2 py-1.5 text-left font-medium">Found</th>
                  <th className="px-2 py-1.5 text-left font-medium">Import as</th>
                  <th className="px-2 py-1.5 text-left font-medium">Reading</th>
                  <th className="px-2 py-1.5 text-left font-medium">Meaning</th>
                  <th className="px-2 py-1.5 text-left font-medium">Role</th>
                  <th className="px-2 py-1.5 text-left font-medium">Sample</th>
                </tr>
              </thead>
              <tbody>
                {plan.columns.map((c) => (
                  <ColumnPlanRow
                    key={c.name}
                    proposal={c}
                    plan={edits[c.name] ?? c.proposed}
                    onChange={(next) => setEdits((e) => ({ ...e, [c.name]: next }))}
                    semanticTypes={semanticTypes}
                    needsChoice={c.decision_required && !edits[c.name]?.format}
                  />
                ))}
              </tbody>
            </table>
          </div>

          <div className="mt-3 flex items-center gap-3">
            <button
              type="button"
              onClick={doImport}
              disabled={undecided.length > 0 || operation.isPending}
              className="rounded bg-slate-900 px-3 py-1.5 text-sm text-white hover:bg-slate-800 disabled:opacity-40"
            >
              Import
            </button>
            {undecided.length > 0 && (
              <span className="text-xs text-amber-800">
                Choose how to read {undecided.map((c) => c.name).join(', ')} first.
              </span>
            )}
          </div>
        </div>
      )}

      <div className="mt-3">
        {/* The streamed job payload carries no steps, so the finished job is
            fetched to learn which dataset it created. */}
        <JobProgress jobId={jobId} onDone={goToNewDataset} />
      </div>

      <FileBrowser open={browsing} onClose={() => setBrowsing(false)} onSelect={handlePicked} />
    </section>
  )
}
