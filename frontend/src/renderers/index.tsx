/**
 * The frontend renderer registry -- the mirror image of the backend plugin registry.
 *
 * The backend returns a `VizSpec` naming a `renderer`; this maps that name to a
 * component. Consequence: a new chart type that reuses an existing renderer is a
 * backend-only change. Only a genuinely new rendering technology touches this file.
 */
import type { ComponentType } from 'react'

import type { Renderer, VizSpec } from '../api/types'
import { MapLibreRenderer } from './MapLibreRenderer'
import { TableRenderer } from './TableRenderer'
import { TimelineRenderer } from './TimelineRenderer'
import { VegaLiteRenderer } from './VegaLiteRenderer'

export interface RendererProps {
  spec: VizSpec
  data: Record<string, unknown>[]
  height?: number
  /**
   * Drill down from a rendered value, when the host page can act on it.
   * The timeline uses this for "show only this user / IP"; renderers that have
   * nothing to drill into simply ignore it.
   */
  onFilter?: (column: string, value: unknown) => void
}

export const RENDERERS: Record<Renderer, ComponentType<RendererProps>> = {
  'vega-lite': VegaLiteRenderer,
  maplibre: MapLibreRenderer,
  table: TableRenderer,
  timeline: TimelineRenderer,
}

export function VizRenderer({ spec, data, height, onFilter }: RendererProps) {
  const Component = RENDERERS[spec.renderer]
  if (!Component) {
    return (
      <div className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-800">
        No renderer registered for <code>{spec.renderer}</code>.
      </div>
    )
  }
  return <Component spec={spec} data={data} height={height} onFilter={onFilter} />
}
