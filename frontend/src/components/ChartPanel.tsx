import { useState } from 'react'

import { ChartEditor } from './ChartEditor'
import { ChartInspector } from './ChartInspector'
import { VizRenderer } from '../renderers'
import type { ChartSpec, RenderedViz } from '../api/types'

/**
 * One chart, with everything you can do to it.
 *
 * Shared by the suggested charts and the ones drawn by hand, so that a chart
 * you asked for is not a second-class citizen: same editor, same inspector,
 * same "pin it" and "keep it" actions. The local edits live here rather than in
 * the page because they belong to this panel's life -- remount it (a new chart
 * in the same slot) and the edits go with the chart they were edits to.
 */
export function ChartPanel({
  title,
  subtitle,
  viz,
  isLoading,
  error,
  onSaveDataset,
  onPin,
}: {
  title: string
  subtitle?: string
  viz?: RenderedViz
  isLoading?: boolean
  error?: unknown
  onSaveDataset?: (viz: RenderedViz) => void
  onPin?: (viz: RenderedViz) => void
}) {
  const [editing, setEditing] = useState(false)
  const [edited, setEdited] = useState<ChartSpec | null>(null)

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-4">
      <div className="mb-2 flex items-start justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
          {subtitle && <p className="text-xs text-slate-500">{subtitle}</p>}
        </div>
        {viz && (onSaveDataset || onPin) && (
          <div className="flex shrink-0 gap-1.5">
            {onSaveDataset && (
              <button
                type="button"
                onClick={() => onSaveDataset(viz)}
                title="Materialise this chart's query as a dataset"
                className="rounded border border-slate-300 px-2 py-1 text-xs hover:bg-slate-50"
              >
                Save as dataset
              </button>
            )}
            {onPin && (
              <button
                type="button"
                onClick={() => onPin(viz)}
                className="rounded border border-slate-300 px-2 py-1 text-xs hover:bg-slate-50"
              >
                Pin to dashboard
              </button>
            )}
          </div>
        )}
      </div>

      {viz?.spec.chart && (
        <div className="mb-2">
          <button
            type="button"
            onClick={() => setEditing((v) => !v)}
            className="text-xs text-slate-400 hover:text-slate-700"
          >
            {editing ? '▾ Hide chart controls' : '▸ Edit chart'}
          </button>
          {editing && (
            <div className="mt-1.5">
              <ChartEditor
                chart={edited ?? viz.spec.chart}
                columns={Object.keys(viz.data[0] ?? {})}
                onChange={setEdited}
                onReset={edited ? () => setEdited(null) : undefined}
              />
            </div>
          )}
        </div>
      )}

      {isLoading && <p className="p-6 text-center text-sm text-slate-500">Loading…</p>}
      {error != null && (
        <p className="rounded bg-rose-50 p-3 text-xs text-rose-800">
          {String((error as Error).message)}
        </p>
      )}
      {viz && (
        <>
          <VizRenderer
            spec={edited ? { ...viz.spec, chart: edited } : viz.spec}
            data={viz.data}
            height={viz.spec.renderer === 'maplibre' ? 420 : 280}
          />
          <ChartInspector
            data={viz.data}
            sql={viz.sql}
            query={viz.spec.query}
            chart={edited ?? viz.spec.chart}
            rawSpec={viz.spec.spec}
            rowCount={viz.row_count}
            elapsedMs={viz.elapsed_ms}
            truncated={viz.truncated}
          />
        </>
      )}
    </section>
  )
}
