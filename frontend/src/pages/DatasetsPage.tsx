import { useState } from 'react'
import { Link } from 'react-router-dom'

import { useDatasetTree, useDatasets } from '../api/hooks'
import { ImportPanel } from '../components/ImportPanel'
import { DatasetTree } from '../components/DatasetTree'

export function DatasetsPage() {
  const { data: datasets, isLoading } = useDatasets()
  const { data: tree } = useDatasetTree()
  const [view, setView] = useState<'tree' | 'grid'>('tree')

  return (
    <div className="space-y-6">
      <ImportPanel />

      <section>
        <div className="mb-3 flex items-center gap-3">
          <h2 className="text-base font-semibold text-slate-900">Datasets</h2>
          <span className="text-xs text-slate-500">
            aggregates and joins nest under the dataset they came from
          </span>
          <div className="ml-auto flex rounded border border-slate-300">
            {(['tree', 'grid'] as const).map((v) => (
              <button
                key={v}
                type="button"
                onClick={() => setView(v)}
                className={`px-3 py-1 text-xs ${
                  view === v ? 'bg-slate-900 text-white' : 'text-slate-600 hover:bg-slate-50'
                }`}
              >
                {v === 'tree' ? 'Tree' : 'Grid'}
              </button>
            ))}
          </div>
        </div>

        {isLoading && <p className="text-sm text-slate-500">Loading…</p>}

        {view === 'tree' ? (
          tree && <DatasetTree nodes={tree} />
        ) : (
          <>
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
          </>
        )}
      </section>
    </div>
  )
}
