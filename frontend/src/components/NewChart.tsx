import { useState } from 'react'

import { usePlugins } from '../api/hooks'
import { SchemaForm } from './SchemaForm'
import type { PluginDescriptor } from '../api/types'

/**
 * Draw any chart, whether or not anything suggested it.
 *
 * Suggestions read semantic types, so a chart type is unreachable exactly when
 * detection missed -- a map needs `geo.lat`, and coordinates that arrived as
 * text have none. That made the suggester the only door to the whole class of
 * visualizer, which is the wrong shape: it is a good default, not a gate.
 *
 * So every visualizer is listed, including the ones whose preconditions this
 * dataset does not meet. They are marked with what they wanted rather than
 * hidden, because "needs a column meaning geo.lat" is the sentence that tells
 * you to go and set that meaning -- and picking the columns by hand works
 * regardless, since the form asks the dataset for its columns, not for types.
 *
 * The form itself is the plugin's own JSON Schema, so a new visualizer in the
 * backend is drawable here with no change to this file.
 */
export function NewChart({
  datasetId,
  columns,
  onDraw,
}: {
  datasetId: string
  columns: string[]
  onDraw: (pluginId: string, params: Record<string, unknown>) => void
}) {
  const { data: all } = usePlugins({ kind: 'visualizer' })
  const { data: applicable } = usePlugins({ kind: 'visualizer', applicable_to: datasetId })
  const [selected, setSelected] = useState('')
  const [params, setParams] = useState<Record<string, unknown>>({})

  const plugins = all ?? []
  const ready = new Set((applicable ?? []).map((p) => p.id))
  const plugin = plugins.find((p) => p.id === selected)

  // Only the required parameters gate the button. Everything else has a
  // default, and a chart drawn from defaults is the point of the exercise.
  const missing = (plugin?.params_schema.required ?? []).filter((key) => {
    const value = params[key]
    return (
      value === undefined ||
      value === null ||
      value === '' ||
      (Array.isArray(value) && value.length === 0)
    )
  })

  const wants = (p: PluginDescriptor) => p.accepts.semantic_types.join(' or ')

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold text-slate-900">New chart</h2>
        <select
          value={selected}
          onChange={(e) => {
            setSelected(e.target.value)
            setParams({})
          }}
          className="rounded border border-slate-300 px-2 py-1 text-sm"
        >
          <option value="">Choose a chart…</option>
          <optgroup label="Suits this dataset">
            {plugins
              .filter((p) => ready.has(p.id))
              .map((p) => (
                <option key={p.id} value={p.id}>
                  {p.title}
                </option>
              ))}
          </optgroup>
          {plugins.some((p) => !ready.has(p.id)) && (
            <optgroup label="Needs a column meaning something this dataset has not got">
              {plugins
                .filter((p) => !ready.has(p.id))
                .map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.title} — wants {wants(p)}
                  </option>
                ))}
            </optgroup>
          )}
        </select>
        <span className="text-xs text-slate-500">
          every chart the backend can draw, suggested or not
        </span>
      </div>

      {plugin && (
        <div className="mt-3 space-y-3">
          <p className="text-xs text-slate-500">{plugin.summary}</p>
          {!ready.has(plugin.id) && (
            <p className="rounded border border-amber-200 bg-amber-50 p-2 text-xs text-amber-900">
              Nothing in this dataset is typed <code className="font-mono">{wants(plugin)}</code>,
              so this chart was never suggested. Pick the columns yourself below — or set the
              meaning on the dataset page and it will be suggested from then on.
            </p>
          )}
          <SchemaForm
            schema={plugin.params_schema}
            value={params}
            onChange={setParams}
            columns={columns}
          />
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => onDraw(plugin.id, params)}
              disabled={missing.length > 0}
              className="rounded bg-slate-900 px-3 py-1.5 text-sm text-white hover:bg-slate-800 disabled:opacity-40"
            >
              Draw
            </button>
            {missing.length > 0 && (
              <span className="text-xs text-slate-500">
                needs {missing.join(', ')}
              </span>
            )}
          </div>
        </div>
      )}
    </section>
  )
}
