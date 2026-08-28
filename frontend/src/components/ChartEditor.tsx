import type { Channel, ChartSpec, Encoding, Mark } from '../api/types'

/**
 * Edit a chart's grammar directly.
 *
 * Column pickers are populated from the query's **actual output columns**, so a
 * field cannot be mistyped — which is the whole reason the encodings are typed
 * rather than free-form JSON. Changes apply live; there is no Apply button
 * because seeing the chart move is the feedback.
 */

const MARKS: Mark[] = ['bar', 'line', 'area', 'point', 'tick', 'rect', 'arc', 'boxplot']

/** The channels worth exposing. The IR supports more; these carry most charts. */
const CHANNELS: { channel: Channel; label: string; hint: string }[] = [
  { channel: 'x', label: 'X', hint: 'horizontal position' },
  { channel: 'y', label: 'Y', hint: 'vertical position' },
  { channel: 'color', label: 'Color', hint: 'splits into series' },
  { channel: 'size', label: 'Size', hint: 'point or bar thickness' },
]

const AGGREGATES = ['', 'count', 'sum', 'mean', 'median', 'min', 'max'] as const

export function ChartEditor({
  chart,
  columns,
  onChange,
  onReset,
}: {
  chart: ChartSpec
  columns: string[]
  onChange: (next: ChartSpec) => void
  onReset?: () => void
}) {
  const setEncoding = (channel: Channel, patch: Partial<Encoding> | null) => {
    const next: ChartSpec = { ...chart, encodings: { ...chart.encodings } }
    if (patch === null) {
      delete next.encodings[channel]
    } else {
      const existing = next.encodings[channel]
      next.encodings[channel] = { ...(existing ?? { field: columns[0] }), ...patch }
    }
    onChange(next)
  }

  return (
    <div className="space-y-2 rounded border border-slate-200 bg-slate-50 p-3">
      <div className="flex items-center gap-2">
        <label className="text-xs font-medium text-slate-700">Mark</label>
        <select
          value={chart.mark}
          onChange={(e) => onChange({ ...chart, mark: e.target.value as Mark })}
          className="rounded border border-slate-300 px-2 py-0.5 text-xs"
        >
          {MARKS.map((m) => (
            <option key={m}>{m}</option>
          ))}
        </select>
        {onReset && (
          <button
            type="button"
            onClick={onReset}
            className="ml-auto rounded border border-slate-300 px-2 py-0.5 text-xs text-slate-600 hover:bg-white"
          >
            Reset
          </button>
        )}
      </div>

      <table className="w-full text-xs">
        <tbody>
          {CHANNELS.map(({ channel, label, hint }) => {
            const enc = chart.encodings[channel]
            return (
              <tr key={channel}>
                <td className="w-14 py-1 pr-2 font-medium text-slate-700" title={hint}>
                  {label}
                </td>
                <td className="py-1 pr-2">
                  <select
                    value={enc?.field ?? ''}
                    onChange={(e) =>
                      e.target.value
                        ? setEncoding(channel, { field: e.target.value, type: null })
                        : setEncoding(channel, null)
                    }
                    className="w-full rounded border border-slate-300 px-1.5 py-0.5 text-xs"
                  >
                    <option value="">— none —</option>
                    {columns.map((c) => (
                      <option key={c} value={c}>
                        {c}
                      </option>
                    ))}
                  </select>
                </td>
                <td className="w-24 py-1 pr-2">
                  {enc && (
                    <select
                      value={enc.aggregate ?? ''}
                      onChange={(e) =>
                        setEncoding(channel, { aggregate: e.target.value || null })
                      }
                      className="w-full rounded border border-slate-300 px-1.5 py-0.5 text-xs"
                    >
                      {AGGREGATES.map((a) => (
                        <option key={a} value={a}>
                          {a || 'no agg'}
                        </option>
                      ))}
                    </select>
                  )}
                </td>
                <td className="w-28 py-1 text-slate-400">
                  {/* The backend's inferred type, so a wrong axis is visible. */}
                  {enc?.type ?? ''}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>

      <p className="text-[11px] text-slate-400">
        Types are inferred from each column&apos;s meaning; pick a field to override the
        chart the suggester chose.
      </p>
    </div>
  )
}
