import { useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { api } from '../api/client'
import {
  keys,
  useDataset,
  useOperation,
  useProfile,
  useSuggestions,
} from '../api/hooks'
import type { RenderedViz, Suggestion } from '../api/types'
import { ChartPanel } from '../components/ChartPanel'
import { NewChart } from '../components/NewChart'

/**
 * Charts come from backend suggestions, and from whatever else you ask for.
 *
 * The page has no hard-coded idea of what a taxi or an auth log looks like: it
 * renders whatever the suggesters propose, through whichever renderer each
 * VizSpec names. But suggestions are inferred from semantic types, so on their
 * own they make an undetected column into a missing capability -- hence the
 * builder at the top, which reaches every visualizer the backend has.
 */
export function ExplorePage() {
  const { id = '' } = useParams()
  const qc = useQueryClient()
  const { data: dataset } = useDataset(id)
  const { data: profile } = useProfile(id)
  const { data: suggestions } = useSuggestions(id, 'viz')
  const [saved, setSaved] = useState<string | null>(null)
  // The chart asked for by hand, if any. One at a time: it is a workbench, and
  // anything worth keeping goes to a dashboard.
  const [drawn, setDrawn] = useState<{ pluginId: string; params: Record<string, unknown> } | null>(
    null,
  )
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

  const drawnViz = useQuery({
    queryKey: ['viz', id, drawn?.pluginId, drawn?.params],
    queryFn: () =>
      api.inspect({ plugin_id: drawn!.pluginId, dataset_id: id, params: drawn!.params }),
    enabled: Boolean(drawn),
    retry: false,
  })

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

      <NewChart
        datasetId={id}
        columns={(profile?.columns ?? []).map((c) => c.name)}
        onDraw={(pluginId, params) => setDrawn({ pluginId, params })}
      />

      {drawn && (
        <ChartPanel
          // Remounted per drawn chart, so chart-level edits never outlive the
          // chart they were made to.
          key={`${drawn.pluginId}:${JSON.stringify(drawn.params)}`}
          title={drawnViz.data?.spec.title || 'New chart'}
          subtitle={drawn.pluginId}
          viz={drawnViz.data}
          isLoading={drawnViz.isLoading}
          error={drawnViz.error}
          onSaveDataset={saveAsDataset}
          onPin={saveToDashboard}
        />
      )}

      {!vizSuggestions.length && (
        <p className="rounded border border-dashed border-slate-300 p-6 text-center text-sm text-slate-500">
          Nothing suggested itself for this dataset — draw one above.
        </p>
      )}

      <div className="grid gap-4 xl:grid-cols-2">
        {vizSuggestions.map((s, i) => (
          <ChartPanel
            key={i}
            title={s.title}
            subtitle={s.rationale}
            viz={rendered[i].data}
            isLoading={rendered[i].isLoading}
            error={rendered[i].error}
            onSaveDataset={saveAsDataset}
            onPin={saveToDashboard}
          />
        ))}
      </div>
    </div>
  )
}
