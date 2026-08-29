import { useEffect, useRef } from 'react'

import { api } from '../api/client'
import { useJobWatcher } from '../api/hooks'
import type { Job } from '../api/types'

const STATUS_STYLES: Record<string, string> = {
  queued: 'bg-slate-100 text-slate-700',
  running: 'bg-blue-100 text-blue-800',
  succeeded: 'bg-emerald-100 text-emerald-800',
  failed: 'bg-rose-100 text-rose-800',
  cancelled: 'bg-amber-100 text-amber-800',
}

export function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={`rounded px-2 py-0.5 text-xs font-medium ${
        STATUS_STYLES[status] ?? 'bg-slate-100 text-slate-700'
      }`}
    >
      {status}
    </span>
  )
}

export function JobProgress({
  jobId,
  onDone,
}: {
  jobId: string | null
  onDone?: (job: Job) => void
}) {
  const job = useJobWatcher(jobId)
  // Called from an effect, and only once: as a bare expression in the render
  // body this fired on every render for as long as the job stayed succeeded,
  // so an onDone that navigates or sets state ran repeatedly.
  const announced = useRef<string | null>(null)
  useEffect(() => {
    if (!job || job.status !== 'succeeded' || !onDone) return
    if (announced.current === job.id) return
    announced.current = job.id
    onDone(job)
  }, [job, onDone])

  if (!jobId || !job) return null

  const p = job.progress ?? {}
  const pct = p.pct ?? (job.status === 'succeeded' ? 100 : 0)
  const cost = p.cost

  return (
    <div className="rounded border border-slate-200 bg-white p-3 text-sm">
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="font-medium text-slate-800">{job.title}</span>
        <div className="flex items-center gap-2">
          <StatusBadge status={job.status} />
          {job.status === 'running' && (
            <button
              type="button"
              onClick={() => api.cancelJob(job.id)}
              className="rounded border border-slate-300 px-2 py-0.5 text-xs hover:bg-slate-50"
            >
              Cancel
            </button>
          )}
        </div>
      </div>

      <div className="h-1.5 w-full overflow-hidden rounded bg-slate-100">
        <div
          className={`h-full transition-all ${
            job.status === 'failed' ? 'bg-rose-500' : 'bg-blue-500'
          }`}
          style={{ width: `${Math.min(100, pct)}%` }}
        />
      </div>

      <div className="mt-1.5 flex flex-wrap gap-x-4 text-xs text-slate-600">
        {p.rows_done != null && (
          <span>
            {p.rows_done.toLocaleString()}
            {p.rows_total ? ` / ${p.rows_total.toLocaleString()}` : ''} rows
          </span>
        )}
        {p.rows_per_s ? <span>{Math.round(p.rows_per_s).toLocaleString()} rows/s</span> : null}
        {p.eta_s != null && <span>ETA {Math.round(p.eta_s)}s</span>}
        {cost && cost.calls > 0 && (
          <span>
            ${cost.usd.toFixed(4)} · {cost.calls} calls · {cost.cache_hits} cached
          </span>
        )}
      </div>

      {job.error && (
        <pre className="mt-2 overflow-auto rounded bg-rose-50 p-2 text-xs text-rose-800">
          {job.error}
        </pre>
      )}

      {job.logs?.length > 0 && (
        <details className="mt-2">
          <summary className="cursor-pointer text-xs text-slate-500">
            Log ({job.logs.length})
          </summary>
          <pre className="mt-1 max-h-40 overflow-auto rounded bg-slate-50 p-2 text-xs text-slate-600">
            {job.logs.join('\n')}
          </pre>
        </details>
      )}
    </div>
  )
}
