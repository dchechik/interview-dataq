import type { RendererProps } from './index'

function display(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'number') {
    return Number.isInteger(value) ? value.toLocaleString() : value.toFixed(4)
  }
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

export function TableRenderer({ data, height = 320 }: RendererProps) {
  if (!data.length) {
    return <div className="p-6 text-center text-sm text-slate-500">No rows</div>
  }
  const columns = Object.keys(data[0])
  return (
    <div className="overflow-auto rounded border border-slate-200" style={{ maxHeight: height }}>
      <table className="w-full border-collapse text-sm">
        <thead className="sticky top-0 bg-slate-50">
          <tr>
            {columns.map((c) => (
              <th
                key={c}
                className="border-b border-slate-200 px-3 py-2 text-left font-medium text-slate-700"
              >
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.map((row, i) => (
            <tr key={i} className="odd:bg-white even:bg-slate-50/60">
              {columns.map((c) => (
                <td
                  key={c}
                  className="border-b border-slate-100 px-3 py-1.5 font-mono text-xs text-slate-700"
                >
                  {display(row[c])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
