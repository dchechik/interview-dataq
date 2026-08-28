/**
 * Compile a `ChartSpec` to Vega-Lite.
 *
 * The backend validates and types the spec (it has the semantic layer); this
 * turns the result into the JSON Vega-Lite wants. Keeping compilation on the
 * client preserves the rule that the backend never renders, and leaves room for
 * a non-Vega renderer to compile the same IR differently.
 */
import type { ChartSpec, Encoding } from '../api/types'

/** Deep merge, with `override` winning. Used for the raw_vega_lite escape hatch. */
function merge(
  base: Record<string, unknown>,
  override: Record<string, unknown>,
): Record<string, unknown> {
  const out: Record<string, unknown> = { ...base }
  for (const [key, value] of Object.entries(override)) {
    const existing = out[key]
    const bothPlainObjects =
      existing !== null &&
      value !== null &&
      typeof existing === 'object' &&
      typeof value === 'object' &&
      !Array.isArray(existing) &&
      !Array.isArray(value)
    out[key] = bothPlainObjects
      ? merge(existing as Record<string, unknown>, value as Record<string, unknown>)
      : value
  }
  return out
}

function compileEncoding(encoding: Encoding): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  // A count needs no field: `{aggregate: 'count'}` counts rows, and naming a
  // field would instead count that field's non-null values.
  if (encoding.aggregate !== 'count') out.field = encoding.field
  if (encoding.type) out.type = encoding.type
  if (encoding.aggregate) out.aggregate = encoding.aggregate
  if (encoding.bin != null && encoding.bin !== false) {
    out.bin = encoding.bin === true ? true : { maxbins: encoding.bin }
  }
  if (encoding.sort) out.sort = encoding.sort
  if (encoding.title) out.title = encoding.title
  if (encoding.stack != null) out.stack = encoding.stack
  return out
}

function compileUnit(chart: ChartSpec): Record<string, unknown> {
  const encoding: Record<string, unknown> = {}
  for (const [channel, enc] of Object.entries(chart.encodings ?? {})) {
    encoding[channel] = compileEncoding(enc)
  }
  const unit: Record<string, unknown> = { mark: chart.mark }
  if (Object.keys(encoding).length) unit.encoding = encoding
  if (!chart.raw_vega_lite) return unit

  // The escape hatch is merged last so a plugin can restyle a mark (a colour,
  // point markers) without restating the encodings.
  const merged = merge(unit, chart.raw_vega_lite)

  // ...but the mark *type* always comes from `chart.mark`. Letting raw override
  // it would give one property two sources of truth, and the symptom is subtle:
  // the editor's mark dropdown appears to do nothing.
  const rawMark = (chart.raw_vega_lite as { mark?: unknown }).mark
  if (rawMark && typeof rawMark === 'object' && !Array.isArray(rawMark)) {
    merged.mark = { ...(rawMark as Record<string, unknown>), type: chart.mark }
  } else {
    merged.mark = chart.mark
  }
  return merged
}

export function compileChartSpec(chart: ChartSpec): Record<string, unknown> {
  if (!chart.layers?.length) return compileUnit(chart)
  // Layered charts put shared encodings on the parent and marks in `layer`.
  return {
    layer: [compileUnit(chart), ...chart.layers.map(compileUnit)],
  }
}
