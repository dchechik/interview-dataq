import { useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import { api } from '../api/client'

/**
 * The draft the feature editor opens with.
 *
 * Knowing the feature language exists is not the same as knowing what to write.
 * The useful expressions follow from the table rather than from imagination —
 * pick whoever acts, pick the clock, and then every category gets the same
 * questions asked of it — so the box arrives filled in.
 *
 * The one thing the table cannot settle is *who acts*. Two email columns can be
 * indistinguishable by type and by statistics, and whether behaviour is
 * per-recipient or per-sender is a question about intent. So the guess is shown
 * rather than applied silently, and changing it rewrites the draft.
 */
export function FeatureDraft({
  datasetId,
  onUse,
  hasEdits,
}: {
  datasetId: string
  onUse: (expressions: string[]) => void
  /** True once the user has typed; the draft stops overwriting them. */
  hasEdits: boolean
}) {
  const [actor, setActor] = useState<string | null>(null)
  const [window, setWindow] = useState('30d')

  const { data: plan, isLoading } = useQuery({
    queryKey: ['feature-plan', datasetId, actor, window],
    queryFn: () => api.featurePlan(datasetId, actor ?? undefined, window),
  })

  // Fill the box on arrival, and again whenever the actor or window changes —
  // but never over something the user has typed.
  useEffect(() => {
    if (!plan || hasEdits || !plan.features.length) return
    onUse(plan.features.map((f) => f.expression))
    // onUse identity changes every render in the parent; depending on it would
    // re-fill the box on every keystroke.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plan, hasEdits])

  if (isLoading) {
    return <p className="text-xs text-slate-500">Reading the table…</p>
  }
  if (!plan) return null

  if (plan.blocked) {
    return (
      <div className="rounded border border-amber-200 bg-amber-50 p-2.5 text-xs text-amber-900">
        {plan.blocked}
      </div>
    )
  }

  return (
    <div className="rounded border border-slate-200 bg-slate-50 p-2.5 text-xs">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
        <span className="font-medium text-slate-700">Measure behaviour per</span>
        <select
          value={plan.actor ?? ''}
          onChange={(e) => setActor(e.target.value)}
          className="rounded border border-slate-300 bg-white px-1.5 py-0.5 text-xs"
        >
          {plan.actor_options.map((o) => (
            <option key={o.column} value={o.column}>
              {o.column} — {o.reason}
            </option>
          ))}
        </select>
        <span className="text-slate-500">over the last</span>
        <select
          value={window}
          onChange={(e) => setWindow(e.target.value)}
          className="rounded border border-slate-300 bg-white px-1.5 py-0.5 text-xs"
        >
          {['7d', '30d', '90d', '1y'].map((w) => (
            <option key={w}>{w}</option>
          ))}
        </select>
        {hasEdits && (
          <button
            type="button"
            onClick={() => onUse(plan.features.map((f) => f.expression))}
            className="ml-auto rounded border border-slate-300 bg-white px-2 py-0.5 hover:bg-slate-100"
          >
            Reset to draft
          </button>
        )}
      </div>

      <p className="mt-1.5 text-slate-500">
        {plan.features.length} suggested, timed by{' '}
        <code className="font-mono">{plan.time_column}</code> · costs{' '}
        {plan.distinct_windows} pass{plan.distinct_windows === 1 ? '' : 'es'} over the
        data. Edit freely below — the draft is a starting point, not a recipe.
      </p>

      <details className="mt-1.5">
        <summary className="cursor-pointer text-slate-500">
          What each one gives you
        </summary>
        <ul className="mt-1 space-y-0.5">
          {plan.features.map((f) => (
            <li key={f.expression} className="text-slate-600">
              <code className="font-mono text-[11px] text-slate-800">{f.expression}</code>
              <span className="text-slate-500"> — {f.explains}</span>
            </li>
          ))}
        </ul>
      </details>
    </div>
  )
}
