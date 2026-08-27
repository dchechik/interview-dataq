import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api, ApiError } from '../api/client'
import { useDatasets, useOperation } from '../api/hooks'
import { JobProgress } from '../components/JobProgress'

interface Preview {
  reader: string
  columns: string[]
  types: string[]
  rows: unknown[][]
}

export function DatasetsPage() {
  const { data: datasets, isLoading } = useDatasets()
  const operation = useOperation()
  const [uri, setUri] = useState('')
  const [name, setName] = useState('')
  const [preview, setPreview] = useState<Preview | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [jobId, setJobId] = useState<string | null>(null)

  async function doPreview() {
    setError(null)
    setPreview(null)
    try {
      setPreview(await api.preview(uri, 8))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }

  async function doImport() {
    setError(null)
    try {
      const accepted = await operation.mutateAsync({ op: 'import', uri, name: name || undefined })
      setJobId(accepted.job_id)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }

  return (
    <div className="space-y-6">
      <section className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="mb-3 text-base font-semibold text-slate-900">Import a dataset</h2>
        <div className="flex flex-wrap gap-2">
          <input
            className="min-w-96 flex-1 rounded border border-slate-300 px-3 py-1.5 text-sm"
            placeholder="/path/to/data.parquet, /path/to/data.csv, or a glob"
            value={uri}
            onChange={(e) => setUri(e.target.value)}
          />
          <input
            className="w-48 rounded border border-slate-300 px-3 py-1.5 text-sm"
            placeholder="name (optional)"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <button
            type="button"
            onClick={doPreview}
            disabled={!uri}
            className="rounded border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50 disabled:opacity-40"
          >
            Preview
          </button>
          <button
            type="button"
            onClick={doImport}
            disabled={!uri || operation.isPending}
            className="rounded bg-slate-900 px-3 py-1.5 text-sm text-white hover:bg-slate-800 disabled:opacity-40"
          >
            Import
          </button>
        </div>

        {error && (
          <p className="mt-2 rounded bg-rose-50 p-2 text-sm text-rose-800">{error}</p>
        )}

        {preview && (
          <div className="mt-3">
            <p className="mb-1 text-xs text-slate-500">
              Read by <code className="font-mono">{preview.reader}</code> ·{' '}
              {preview.columns.length} columns
            </p>
            <div className="overflow-auto rounded border border-slate-200">
              <table className="w-full text-xs">
                <thead className="bg-slate-50">
                  <tr>
                    {preview.columns.map((c, i) => (
                      <th key={c} className="px-2 py-1 text-left font-medium text-slate-700">
                        {c}
                        <span className="ml-1 font-mono font-normal text-slate-400">
                          {preview.types[i]}
                        </span>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {preview.rows.map((row, i) => (
                    <tr key={i} className="border-t border-slate-100">
                      {row.map((cell, j) => (
                        <td key={j} className="px-2 py-1 font-mono text-slate-600">
                          {cell === null ? '—' : String(cell)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        <div className="mt-3">
          <JobProgress jobId={jobId} />
        </div>
      </section>

      <section>
        <h2 className="mb-3 text-base font-semibold text-slate-900">Datasets</h2>
        {isLoading && <p className="text-sm text-slate-500">Loading…</p>}
        {datasets?.length === 0 && (
          <p className="rounded border border-dashed border-slate-300 p-6 text-center text-sm text-slate-500">
            No datasets yet. Import one above.
          </p>
        )}
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {datasets?.map((d) => (
            <Link
              key={d.id}
              to={`/datasets/${d.id}`}
              className="rounded-lg border border-slate-200 bg-white p-4 transition-shadow hover:shadow-sm"
            >
              <div className="flex items-start justify-between gap-2">
                <span className="font-medium text-slate-900">{d.name}</span>
                <span className="rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-600">
                  {d.kind}
                </span>
              </div>
              <p className="mt-1 text-xs text-slate-500">
                {d.row_count.toLocaleString()} rows · v{d.latest_version}
              </p>
              {d.description && (
                <p className="mt-1 line-clamp-2 text-xs text-slate-500">{d.description}</p>
              )}
            </Link>
          ))}
        </div>
      </section>
    </div>
  )
}
