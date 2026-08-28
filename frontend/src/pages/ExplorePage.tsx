import { useQueries, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { api } from '../api/client'
import { keys, useDataset, useOperation, useSuggestions } from '../api/hooks'
import type { ChartSpec, RenderedViz, Suggestion } from '../api/types'
import { ChartEditor } from '../components/ChartEditor'
import { ChartInspector } from '../components/ChartInspector'
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
  // Local edits to a suggested chart, by panel index. Kept here rather than in
  // the panel so "Reset" can drop back to whatever the suggester proposed.
  const [edited, setEdited] = useState<Record<number, ChartSpec>>({})
  const [editing, setEditing] = useState<number | null>(null)
  const operation = useOperation()

  /**
   * Materialise what a chart drew. The chart already has a QuerySpec; this just
   * persists it, which is what turns a transient picture into a dataset that
   * carries semantic types and can be joined.
   */
  async function saveAsDataset(viz: RenderedViz) {
    if (!viz.spec.query.group_by?.length && !viz.spec.query.time_bucket) {
      setSaved('That chart shows raw rows, so there is nothing to roll up.')
      setTimeout(() => setSaved(null), 3000)
      return
    }
    const accepted = await operation.mutateAsync({
      op: 'aggregate',
      inputs: [{ dataset_id: id }],
      from_query: viz.spec.query,
    })
    qc.invalidateQueries({ queryKey: keys.datasets })
    qc.invalidateQueries({ queryKey: ['related'] })
    setSaved(`saved as a dataset (job ${accepted.job_id.slice(0, 6)})`)
    setTimeout(() => setSaved(null), 4000)
  }

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
                  <div className="flex shrink-0 gap-1.5">
                    <button
                      type="button"
                      onClick={() => saveAsDataset(q.data)}
                      title="Materialise this chart's query as a dataset"
                      className="rounded border border-slate-300 px-2 py-1 text-xs hover:bg-slate-50"
                    >
                      Save as dataset
                    </button>
                    <button
                      type="button"
                      onClick={() => saveToDashboard(q.data)}
                      className="rounded border border-slate-300 px-2 py-1 text-xs hover:bg-slate-50"
                    >
                      Pin to dashboard
                    </button>
                  </div>
                )}
              </div>
              {q.data?.spec.chart && (
                <div className="mb-2">
                  <button
                    type="button"
                    onClick={() => setEditing(editing === i ? null : i)}
                    className="text-xs text-slate-400 hover:text-slate-700"
                  >
                    {editing === i ? '▾ Hide chart controls' : '▸ Edit chart'}
                  </button>
                  {editing === i && (
                    <div className="mt-1.5">
                      <ChartEditor
                        chart={edited[i] ?? q.data.spec.chart}
                        columns={Object.keys(q.data.data[0] ?? {})}
                        onChange={(next) => setEdited((e) => ({ ...e, [i]: next }))}
                        onReset={
                          edited[i]
                            ? () =>
                                setEdited((e) => {
                                  const { [i]: _drop, ...rest } = e
                                  return rest
                                })
                            : undefined
                        }
                      />
                    </div>
                  )}
                </div>
              )}
              {q.isLoading && <p className="p-6 text-center text-sm text-slate-500">Loading…</p>}
              {q.error && (
                <p className="rounded bg-rose-50 p-3 text-xs text-rose-800">
                  {String((q.error as Error).message)}
                </p>
              )}
              {q.data && (
                <>
                  <VizRenderer
                    spec={edited[i] ? { ...q.data.spec, chart: edited[i] } : q.data.spec}
                    data={q.data.data}
                    height={q.data.spec.renderer === 'maplibre' ? 420 : 280}
                  />
                  <ChartInspector
                    data={q.data.data}
                    sql={q.data.sql}
                    query={q.data.spec.query}
                    chart={edited[i] ?? q.data.spec.chart}
                    rawSpec={q.data.spec.spec}
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
