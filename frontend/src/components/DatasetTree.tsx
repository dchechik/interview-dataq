import { useState } from 'react'
import { Link } from 'react-router-dom'

import type { DatasetNode } from '../api/types'

/**
 * Datasets nested under the dataset they were derived from.
 *
 * A join has two parents but a tree node has one, so it nests under its left
 * input and names the other side inline — the second edge stays visible rather
 * than being silently dropped.
 */

const KIND_STYLES: Record<string, string> = {
  source: 'bg-slate-100 text-slate-600',
  derived: 'bg-sky-100 text-sky-700',
  aggregate: 'bg-violet-100 text-violet-700',
  join: 'bg-emerald-100 text-emerald-700',
}

/** What produced this node, in the vocabulary a reader recognises. */
function derivationLabel(node: DatasetNode): string | null {
  if (!node.derived_via) return null
  const { op, plugin_id } = node.derived_via
  if (op === 'join') return 'joined with'
  return plugin_id.replace(/^agg\./, '') || op
}

function Row({ node, depth }: { node: DatasetNode; depth: number }) {
  const [open, setOpen] = useState(true)
  const hasChildren = node.children.length > 0

  return (
    <>
      <div
        className="group flex items-center gap-2 border-b border-slate-100 py-2 pr-3 last:border-0 hover:bg-slate-50/70"
        style={{ paddingLeft: `${12 + depth * 22}px` }}
      >
        {/* Expander occupies the same width whether or not there are children,
            so names stay aligned down a column. */}
        {hasChildren ? (
          <button
            type="button"
            onClick={() => setOpen((o) => !o)}
            aria-expanded={open}
            aria-label={open ? 'Collapse' : 'Expand'}
            className="flex h-4 w-4 shrink-0 items-center justify-center rounded text-xs text-slate-400 hover:bg-slate-200 hover:text-slate-700"
          >
            {open ? '▾' : '▸'}
          </button>
        ) : (
          <span className="h-4 w-4 shrink-0" />
        )}

        {depth > 0 && <span className="shrink-0 text-slate-300">└</span>}

        <Link
          to={`/datasets/${node.id}`}
          className="truncate font-medium text-slate-900 hover:text-blue-700 hover:underline"
        >
          {node.name}
        </Link>

        <span
          className={`shrink-0 rounded px-1.5 py-0.5 text-xs ${
            KIND_STYLES[node.kind] ?? KIND_STYLES.source
          }`}
        >
          {node.kind}
        </span>

        {derivationLabel(node) && (
          <span className="shrink-0 truncate text-xs text-slate-500">
            via <code className="font-mono">{derivationLabel(node)}</code>
            {node.joined_with.map((j) => (
              <Link
                key={j.id}
                to={`/datasets/${j.id}`}
                className="ml-1 text-slate-600 hover:underline"
              >
                {j.name}
              </Link>
            ))}
          </span>
        )}

        <span className="ml-auto shrink-0 font-mono text-xs tabular-nums text-slate-500">
          {node.row_count.toLocaleString()} rows
        </span>
        <span className="shrink-0 font-mono text-xs text-slate-400">v{node.latest_version}</span>

        {hasChildren && !open && (
          <span className="shrink-0 rounded bg-slate-100 px-1.5 text-xs text-slate-500">
            +{node.descendants}
          </span>
        )}
      </div>

      {open && node.children.map((child) => (
        <Row key={child.id} node={child} depth={depth + 1} />
      ))}
    </>
  )
}

export function DatasetTree({ nodes }: { nodes: DatasetNode[] }) {
  if (!nodes.length) {
    return (
      <p className="rounded border border-dashed border-slate-300 p-6 text-center text-sm text-slate-500">
        No datasets yet. Import one above.
      </p>
    )
  }
  return (
    <div className="overflow-hidden rounded-lg border border-slate-200 bg-white">
      {nodes.map((node) => (
        <Row key={node.id} node={node} depth={0} />
      ))}
    </div>
  )
}
