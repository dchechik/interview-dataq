import { useQueries, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { api } from '../api/client'
import { keys, useDataset, useSuggestions } from '../api/hooks'
import type { RenderedViz, Suggestion } from '../api/types'
import { QueryDebug } from '../components/QueryDebug'
import { VizRenderer } from '../renderers'

/**
 * Charts come entirely from backend suggestions: the page has no hard-coded idea
 * of what a taxi or an auth log looks like. It renders whatever the suggesters
 * propose, through whichever renderer each VizSpec names.
 */
export function ExplorePage() {
  const { id = '' } = useParams()
  const qc = useQueryClient()
  const { data: dataset } = useDataset(id)
  const { data: suggestions } = useSuggestions(id, 'viz')
  const [saved, setSaved] = useState<string | null>(null)

  const vizSuggestions = useMemo<Suggestion[]>(
    () => (suggestions ?? []).filter((s) => s.action?.op === 'inspect').slice(0, 6),
    [suggestions],
  )

  const rendered = useQueries({
    queries: vizSuggestions.map((s) => {
      const action = s.action as { plugin_id: string; params: Record<string, unknown> }
      return {
        queryKey: ['viz', id, action.plugin_id, action.params],
        queryFn: () =>
          api.inspect({
            plugin_id: action.plugin_id,
            dataset_id: id,
            params: action.params,
          }),
      }
    }),
  })

  async function saveToDashboard(viz: RenderedViz) {
    const existing = await api.dashboards()
    const target = existing.find((d) => d.name === 'Explore')
    await api.saveDashboard({
      id: target?.id,
      name: 'Explore',
      panels: [...(target?.panels ?? []), viz.spec],
    })
    qc.invalidateQueries({ queryKey: keys.dashboards })
    setSaved(viz.spec.title)
    setTimeout(() => setSaved(null), 2000)
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <Link to={`/datasets/${id}`} className="text-sm text-slate-500 hover:text-slate-800">
          ← {dataset?.name ?? id}
        </Link>
        <h1 className="text-xl font-semibold text-slate-900">Explore</h1>
        {saved && (
          <span className="rounded bg-emerald-100 px-2 py-0.5 text-xs text-emerald-800">
            saved “{saved}” to Explore dashboard
          </span>
        )}
        <Link
          to="/dashboards"
          className="ml-auto rounded border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50"
        >
          Dashboards
        </Link>
      </div>

      {!vizSuggestions.length && (
        <p className="rounded border border-dashed border-slate-300 p-6 text-center text-sm text-slate-500">
          No chart suggestions for this dataset.
        </p>
      )}

      <div className="grid gap-4 xl:grid-cols-2">
        {vizSuggestions.map((s, i) => {
          const q = rendered[i]
          return (
            <section key={i} className="rounded-lg border border-slate-200 bg-white p-4">
              <div className="mb-2 flex items-start justify-between gap-2">
                <div>
                  <h2 className="text-sm font-semibold text-slate-900">{s.title}</h2>
                  <p className="text-xs text-slate-500">{s.rationale}</p>
                </div>
                {q.data && (
                  <button
                    type="button"
                    onClick={() => saveToDashboard(q.data)}
                    className="shrink-0 rounded border border-slate-300 px-2 py-1 text-xs hover:bg-slate-50"
                  >
                    Save
                  </button>
                )}
              </div>
              {q.isLoading && <p className="p-6 text-center text-sm text-slate-500">Loading…</p>}
              {q.error && (
                <p className="rounded bg-rose-50 p-3 text-xs text-rose-800">
                  {String((q.error as Error).message)}
                </p>
              )}
              {q.data && (
                <>
                  <VizRenderer
                    spec={q.data.spec}
                    data={q.data.data}
                    height={q.data.spec.renderer === 'maplibre' ? 420 : 280}
                  />
                  <QueryDebug
                    sql={q.data.sql}
                    spec={q.data.spec.query}
                    rowCount={q.data.row_count}
                    elapsedMs={q.data.elapsed_ms}
                    truncated={q.data.truncated}
                  />
                </>
              )}
            </section>
          )
        })}
      </div>
    </div>
  )
}
