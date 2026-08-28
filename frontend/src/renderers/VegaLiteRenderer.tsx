import { useMemo } from 'react'
import { VegaEmbed } from 'react-vega'
import type { VisualizationSpec } from 'vega-embed'

import type { RendererProps } from './index'
import { compileChartSpec } from './vegaLite'

/**
 * Renders a chart with Vega-Lite.
 *
 * Prefers the typed `ChartSpec`, falling back to the raw `spec` dict for panels
 * saved to a dashboard before the grammar existed — dashboards persist the
 * recipe rather than a snapshot, so old panels must keep working.
 *
 * The backend never sets `data`, `width` or `height`; those are the client's
 * concern, which keeps a spec reusable at any size.
 */
export function VegaLiteRenderer({ spec, data, height = 320 }: RendererProps) {
  const vegaSpec = useMemo(() => {
    const chartPart = spec.chart
      ? compileChartSpec(spec.chart)
      : (spec.spec as Record<string, unknown>)
    return {
      $schema: 'https://vega.github.io/schema/vega-lite/v6.json',
      ...chartPart,
      data: { values: data },
      width: 'container',
      height,
      autosize: { type: 'fit', contains: 'padding' },
      config: {
        axis: { labelColor: '#475569', titleColor: '#334155', gridColor: '#e2e8f0' },
        view: { stroke: 'transparent' },
        font: 'ui-sans-serif, system-ui, sans-serif',
      },
    } as VisualizationSpec
  }, [spec.chart, spec.spec, data, height])

  if (!data.length) {
    return <div className="p-6 text-center text-sm text-slate-500">No data</div>
  }

  return (
    <VegaEmbed
      className="w-full"
      spec={vegaSpec}
      options={{ actions: false, renderer: 'canvas' }}
    />
  )
}
