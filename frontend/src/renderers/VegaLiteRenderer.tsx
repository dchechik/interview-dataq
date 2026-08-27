import { useMemo } from 'react'
import { VegaEmbed } from 'react-vega'
import type { VisualizationSpec } from 'vega-embed'

import type { RendererProps } from './index'

/**
 * Renders the Vega-Lite fragment the backend produced.
 *
 * The backend never sets `data`, `width` or `height` -- those are the client's
 * concern, which keeps a VizSpec reusable at any size. vega-embed takes data
 * inline in the spec, so rows are injected here.
 */
export function VegaLiteRenderer({ spec, data, height = 320 }: RendererProps) {
  const vegaSpec = useMemo(
    () =>
      ({
        $schema: 'https://vega.github.io/schema/vega-lite/v6.json',
        ...spec.spec,
        data: { values: data },
        width: 'container',
        height,
        autosize: { type: 'fit', contains: 'padding' },
        config: {
          axis: { labelColor: '#475569', titleColor: '#334155', gridColor: '#e2e8f0' },
          view: { stroke: 'transparent' },
          font: 'ui-sans-serif, system-ui, sans-serif',
        },
      }) as VisualizationSpec,
    [spec.spec, data, height],
  )

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
