import { useQuery } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'

import { api, ApiError } from '../api/client'
import type { BrowseEntry } from '../api/types'

/**
 * Server-side file picker.
 *
 * DuckDB reads data files in place, so what we need back is a path the *server*
 * can open. A browser's own file input cannot supply that — it hands over
 * contents, not a location — so this browses the server's filesystem instead,
 * and uploading is offered separately for when the file really is only on the
 * viewer's machine.
 */
export function FileBrowser({
  open,
  onClose,
  onSelect,
}: {
  open: boolean
  onClose: () => void
  onSelect: (uri: string) => void
}) {
  const [path, setPath] = useState<string | undefined>(undefined)
  const [showHidden, setShowHidden] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const dialogRef = useRef<HTMLDivElement>(null)

  const { data, isLoading, error: browseError } = useQuery({
    queryKey: ['browse', path, showHidden],
    queryFn: () => api.browse(path, showHidden),
    enabled: open,
  })

  // Esc closes, and focus moves into the dialog so keyboard users are not
  // stranded behind it.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    dialogRef.current?.focus()
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  const message = error ?? (browseError ? (browseError as Error).message : null)

  async function upload(file: File) {
    setError(null)
    setUploading(true)
    try {
      const res = await api.upload(file)
      onSelect(res.uri)
      onClose()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setUploading(false)
      if (fileInput.current) fileInput.current.value = ''
    }
  }

  function crumbs(full: string) {
    // Render the path as clickable segments, keeping the leading separator.
    const parts = full.split('/').filter(Boolean)
    return parts.map((part, i) => ({ name: part, path: '/' + parts.slice(0, i + 1).join('/') }))
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4"
      onClick={onClose}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="Choose a data file"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        className="flex max-h-[80vh] w-full max-w-2xl flex-col rounded-lg bg-white shadow-xl outline-none"
      >
        <div className="flex items-center gap-2 border-b border-slate-200 px-4 py-3">
          <h2 className="text-sm font-semibold text-slate-900">Choose a data file</h2>
          <button
            type="button"
            onClick={onClose}
            className="ml-auto rounded px-2 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
            aria-label="Close"
          >
            ×
          </button>
        </div>

        {/* roots */}
        <div className="flex flex-wrap gap-1.5 border-b border-slate-200 px-4 py-2">
          {data?.roots.map((r) => (
            <button
              key={r.path}
              type="button"
              onClick={() => setPath(r.path)}
              className={`rounded px-2 py-0.5 text-xs ${
                data?.path === r.path
                  ? 'bg-slate-900 text-white'
                  : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
              }`}
            >
              {r.name}
            </button>
          ))}
        </div>

        {/* breadcrumbs */}
        {data && (
          <div className="flex flex-wrap items-center gap-0.5 px-4 py-2 font-mono text-xs text-slate-500">
            {crumbs(data.path).map((c) => (
              <span key={c.path} className="flex items-center gap-0.5">
                <span className="text-slate-300">/</span>
                <button
                  type="button"
                  onClick={() => setPath(c.path)}
                  className="rounded px-1 hover:bg-slate-100 hover:text-slate-800"
                >
                  {c.name}
                </button>
              </span>
            ))}
          </div>
        )}

        {/* listing */}
        <div className="min-h-48 flex-1 overflow-auto border-y border-slate-200">
          {isLoading && <p className="p-6 text-center text-sm text-slate-500">Loading…</p>}
          {message && (
            <p className="m-4 rounded bg-rose-50 p-3 text-sm text-rose-800">{message}</p>
          )}

          {data?.parent && (
            <button
              type="button"
              onClick={() => setPath(data.parent!)}
              className="flex w-full items-center gap-2 px-4 py-1.5 text-left text-sm hover:bg-slate-50"
            >
              <span className="text-slate-400">↑</span>
              <span className="text-slate-600">..</span>
            </button>
          )}

          {data?.entries.map((entry: BrowseEntry) => (
            <button
              key={entry.path}
              type="button"
              onClick={() => {
                if (entry.is_dir) setPath(entry.path)
                else {
                  onSelect(entry.path)
                  onClose()
                }
              }}
              className="flex w-full items-center gap-2 px-4 py-1.5 text-left text-sm hover:bg-slate-50"
            >
              <span className={entry.is_dir ? 'text-amber-500' : 'text-slate-400'}>
                {entry.is_dir ? '▸' : '·'}
              </span>
              <span className={entry.is_dir ? 'text-slate-800' : 'text-slate-700'}>
                {entry.name}
              </span>
              {!entry.is_dir && (
                <>
                  <span className="rounded bg-slate-100 px-1 font-mono text-[10px] text-slate-500">
                    {entry.reader_id?.replace('read.', '')}
                  </span>
                  <span className="ml-auto font-mono text-xs tabular-nums text-slate-400">
                    {formatSize(entry.size)}
                  </span>
                </>
              )}
            </button>
          ))}

          {data && !data.entries.length && !data.parent && (
            <p className="p-6 text-center text-sm text-slate-500">
              No data files here.
            </p>
          )}
          {data && !data.entries.length && data.parent && (
            <p className="p-6 text-center text-sm text-slate-500">
              No CSV, Parquet or JSON files in this folder.
            </p>
          )}
          {data?.truncated && (
            <p className="px-4 py-2 text-xs text-amber-700">
              Showing the first 500 entries — open a subfolder to narrow it down.
            </p>
          )}
        </div>

        {/* footer */}
        <div className="flex flex-wrap items-center gap-3 px-4 py-3">
          <label className="flex items-center gap-1.5 text-xs text-slate-500">
            <input
              type="checkbox"
              checked={showHidden}
              onChange={(e) => setShowHidden(e.target.checked)}
              className="h-3.5 w-3.5"
            />
            Show hidden
          </label>

          <div className="ml-auto flex items-center gap-2">
            <span className="text-xs text-slate-400">Not on this machine?</span>
            <input
              ref={fileInput}
              type="file"
              accept=".csv,.tsv,.txt,.parquet,.pq,.json,.ndjson,.jsonl,.gz"
              onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])}
              className="hidden"
            />
            <button
              type="button"
              onClick={() => fileInput.current?.click()}
              disabled={uploading}
              className="rounded border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50 disabled:opacity-40"
            >
              {uploading ? 'Uploading…' : 'Upload a file'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

function formatSize(bytes: number | null): string {
  if (bytes === null) return ''
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let n = bytes
  let u = 0
  while (n >= 1024 && u < units.length - 1) {
    n /= 1024
    u++
  }
  return `${n < 10 && u > 0 ? n.toFixed(1) : Math.round(n)} ${units[u]}`
}
