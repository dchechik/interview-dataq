import { useQueries } from '@tanstack/react-query'
import { useState } from 'react'

import { api } from '../api/client'
import { useDashboards } from '../api/hooks'
import { ChartInspector } from '../components/ChartInspector'
import { VizRenderer } from '../renderers'

/**
 * A dashboard is a saved list of VizSpecs. Each panel re-runs its own query on
 * load, so a dashboard always shows current data rather than a frozen snapshot.
 */
export function DashboardsPage() {
  const { data: dashboards } = useDashboards()
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const active = dashboards?.find((d) => d.id === selectedId) ?? dashboards?.[0] ?? null

  const panels = useQueries({
    queries: (active?.panels ?? []).map((panel, i) => ({
      queryKey: ['panel', active?.id, i],
      queryFn: () => api.query(panel.query),
    })),
  })

  if (!dashboards?.length) {
    return (
      <p className="rounded border border-dashed border-slate-300 p-8 text-center text-sm text-slate-500">
        No dashboards yet. Save a chart from a dataset&apos;s Explore page.
      </p>
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-xl font-semibold text-slate-900">Dashboards</h1>
        <div className="ml-auto flex gap-1">
          {dashboards.map((d) => (
            <button
              key={d.id}
              type="button"
              onClick={() => setSelectedId(d.id)}
              className={`rounded px-3 py-1.5 text-sm ${
                active?.id === d.id
                  ? 'bg-slate-900 text-white'
                  : 'border border-slate-300 text-slate-600 hover:bg-slate-50'
              }`}
            >
              {d.name}
              <span className="ml-1.5 text-xs opacity-70">{d.panels.length}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        {active?.panels.map((panel, i) => {
          const q = panels[i]
          const rows = q.data
            ? q.data.rows.map((r) =>
                Object.fromEntries(q.data!.columns.map((c, j) => [c, r[j]])),
              )
            : []
          return (
            <section key={i} className="rounded-lg border border-slate-200 bg-white p-4">
              <h2 className="mb-2 text-sm font-semibold text-slate-900">{panel.title}</h2>
              {q.isLoading && <p className="p-6 text-center text-sm text-slate-500">Loading…</p>}
              {q.error && (
                <p className="rounded bg-rose-50 p-3 text-xs text-rose-800">
                  {String((q.error as Error).message)}
                </p>
              )}
              {q.data && (
                <>
                  <VizRenderer
                    spec={panel}
                    data={rows}
                    height={panel.renderer === 'maplibre' ? 380 : 260}
                  />
                  <ChartInspector
                    data={rows}
                    sql={q.data.sql}
                    query={panel.query}
                    chart={panel.chart}
                    rawSpec={panel.spec}
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
