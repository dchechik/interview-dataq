import { useEffect, useMemo, useState } from 'react'

import { ApiError } from '../api/client'
import {
  useDatasets,
  useJoinCandidates,
  useJoinPreview,
  useOperation,
  useProfile,
} from '../api/hooks'
import type { JoinPreviewRequest } from '../api/types'

/**
 * Joining this dataset to another one, on a key you choose.
 *
 * The join op has always taken a key; until now only a suggester supplied one,
 * and a suggester proposes exactly one pair of columns that share a meaning.
 * That covers the common annotation and nothing else — not a key that needs two
 * columns, not an inner join, not a choice about which columns come across.
 *
 * Two things the form is built around:
 *
 * **The key is a list.** A table of per-`(country, action)` counts has neither
 * column unique on its own, so matching on either alone multiplies rows and
 * only the pair annotates them. Adding a second key column is one click, and it
 * is the answer to the failure people hit most.
 *
 * **The answer comes before the job.** A wrong key is cheap to find out about
 * and expensive to discover: the op writes the whole result before counting the
 * rows and refusing it. So every change to the key re-asks the preview, which
 * costs two counts and a `GROUP BY`, and the run button reports what will
 * happen rather than what happened.
 */
export function JoinPanel({
  datasetId,
  columns,
  seed,
  onJob,
}: {
  datasetId: string
  columns: string[]
  /** A suggested join to open with, when one was clicked. */
  seed: JoinSeed | null
  onJob: (id: string) => void
}) {
  const { data: datasets } = useDatasets()
  const { data: candidates } = useJoinCandidates(datasetId)
  const operation = useOperation()

  const [rightId, setRightId] = useState('')
  // null means untouched, which is what lets the proposal below show through.
  // Choosing a different right dataset returns to null, because the old key
  // named columns on a table that is no longer in the join.
  const [chosenKeys, setChosenKeys] = useState<Pair[] | null>(null)
  const [how, setHow] = useState<'left' | 'inner'>('left')
  const [prefix, setPrefix] = useState('')
  const [outputName, setOutputName] = useState('')
  const [allowFanout, setAllowFanout] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const { data: rightProfile } = useProfile(rightId || undefined)
  const rightColumns = useMemo(
    () => rightProfile?.columns.map((c) => c.name) ?? [],
    [rightProfile],
  )

  const candidate = candidates?.find((c) => c.dataset_id === rightId)
  const suggestedIds = new Set(candidates?.map((c) => c.dataset_id) ?? [])
  const others = (datasets ?? []).filter(
    (d) => d.id !== datasetId && !suggestedIds.has(d.id),
  )

  // A suggestion clicked elsewhere on the page lands here rather than firing.
  useEffect(() => {
    if (!seed) return
    setRightId(seed.rightId)
    setChosenKeys(seed.keys)
    setHow(seed.how)
    setPrefix(seed.prefix)
    setError(null)
  }, [seed])

  // What the semantic layer knows about this pair, shown until something is
  // chosen instead. Derived rather than written into state: a proposal that
  // writes itself back is indistinguishable afterwards from a decision.
  const proposed: Pair[] = candidate
    ? candidate.keys.slice(0, 1).map((k) => ({ left: k.left, right: k.right }))
    : [{ left: '', right: '' }]
  const keys = chosenKeys ?? proposed
  const usable = keys.filter((k) => k.left && k.right)
  const previewBody: JoinPreviewRequest | null =
    rightId && usable.length
      ? {
          right_dataset_id: rightId,
          params: { on: usable, how, prefix },
        }
      : null
  const { data: preview, error: previewError } = useJoinPreview(datasetId, previewBody)

  function selectRight(id: string) {
    setRightId(id)
    setChosenKeys(null)
    setAllowFanout(false)
    setError(null)
  }

  function setPair(i: number, patch: Partial<Pair>) {
    setChosenKeys(keys.map((k, j) => (j === i ? { ...k, ...patch } : k)))
  }

  async function run() {
    setError(null)
    try {
      const accepted = await operation.mutateAsync({
        op: 'join',
        inputs: [{ dataset_id: datasetId }, { dataset_id: rightId }],
        params: {
          on: usable,
          how,
          prefix,
          ...(allowFanout ? { allow_fanout: true } : {}),
        },
        ...(outputName ? { output_name: outputName } : {}),
      })
      onJob(accepted.job_id)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }

  const blocked = Boolean(preview?.collisions.length) || (preview?.fanout && !allowFanout)

  return (
    <div className="space-y-3">
      <select
        value={rightId}
        onChange={(e) => selectRight(e.target.value)}
        className="w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
      >
        <option value="">Choose a dataset to join…</option>
        {candidates && candidates.length > 0 && (
          <optgroup label="Shares a meaning with this one">
            {candidates.map((c) => (
              <option key={c.dataset_id} value={c.dataset_id}>
                {c.name} — {c.keys[0].reason} ({c.row_count.toLocaleString()} rows)
              </option>
            ))}
          </optgroup>
        )}
        {others.length > 0 && (
          /* Nothing in common by meaning is not the same as nothing in common.
             The semantic layer proposes; it does not decide what may be joined. */
          <optgroup label="Everything else">
            {others.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name} ({d.row_count.toLocaleString()} rows)
              </option>
            ))}
          </optgroup>
        )}
      </select>

      {rightId && (
        <>
          <div className="space-y-1.5">
            <p className="text-xs font-medium text-slate-600">Match rows where</p>
            {keys.map((k, i) => (
              <div key={i} className="flex items-center gap-1.5">
                <ColumnSelect
                  value={k.left}
                  options={columns}
                  onChange={(v) => setPair(i, { left: v })}
                />
                <span className="text-xs text-slate-400">=</span>
                <ColumnSelect
                  value={k.right}
                  options={rightColumns}
                  onChange={(v) => setPair(i, { right: v })}
                />
                {keys.length > 1 && (
                  <button
                    type="button"
                    onClick={() => setChosenKeys(keys.filter((_, j) => j !== i))}
                    className="rounded px-1 text-xs text-slate-400 hover:bg-slate-100 hover:text-slate-700"
                    title="Remove this pair"
                  >
                    ×
                  </button>
                )}
              </div>
            ))}
            <button
              type="button"
              onClick={() => setChosenKeys([...keys, { left: '', right: '' }])}
              className="text-xs text-slate-500 underline underline-offset-2 hover:text-slate-800"
            >
              Add a column to the key
            </button>
          </div>

          <div className="flex flex-wrap items-center gap-2 text-xs">
            <select
              value={how}
              onChange={(e) => setHow(e.target.value as 'left' | 'inner')}
              className="rounded border border-slate-300 px-1.5 py-1"
            >
              <option value="left">Keep every row (left)</option>
              <option value="inner">Keep matched rows only (inner)</option>
            </select>
            <input
              value={prefix}
              onChange={(e) => setPrefix(e.target.value)}
              placeholder="prefix"
              className="w-24 rounded border border-slate-300 px-1.5 py-1"
              title="Prepended to every column brought across"
            />
            <input
              value={outputName}
              onChange={(e) => setOutputName(e.target.value)}
              placeholder="new dataset name"
              className="min-w-0 flex-1 rounded border border-slate-300 px-1.5 py-1"
            />
          </div>

          <JoinOutcome preview={preview} error={previewError} />

          {preview?.fanout && (
            <label className="flex items-start gap-1.5 text-xs text-slate-600">
              <input
                type="checkbox"
                checked={allowFanout}
                onChange={(e) => setAllowFanout(e.target.checked)}
                className="mt-0.5"
              />
              {/* A one-to-many join is a real thing to want; it is just almost
                  never what someone means by "annotate". */}
              <span>
                Multiply the rows anyway — I want one row per match, not per{' '}
                {keys[0]?.left || 'row'}.
              </span>
            </label>
          )}

          <button
            type="button"
            onClick={run}
            disabled={!usable.length || blocked || operation.isPending}
            className="rounded bg-slate-900 px-3 py-1.5 text-sm text-white hover:bg-slate-800 disabled:opacity-40"
          >
            {operation.isPending ? 'Joining…' : 'Join'}
          </button>
        </>
      )}

      {error && <p className="rounded bg-rose-50 p-2 text-sm text-rose-800">{error}</p>}
    </div>
  )
}

export interface Pair {
  left: string
  right: string
}

/** A suggested join, handed to the panel instead of being run immediately. */
export interface JoinSeed {
  rightId: string
  keys: Pair[]
  how: 'left' | 'inner'
  prefix: string
}

function ColumnSelect({
  value,
  options,
  onChange,
}: {
  value: string
  options: string[]
  onChange: (v: string) => void
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="min-w-0 flex-1 rounded border border-slate-300 px-1.5 py-1 text-xs"
    >
      <option value="">column…</option>
      {options.map((c) => (
        <option key={c} value={c}>
          {c}
        </option>
      ))}
    </select>
  )
}

/**
 * What the join will do, before it does it.
 *
 * Three things can be wrong with a key and each reads differently: it can
 * duplicate rows, match nothing, or bring across a name that is already taken.
 * Only the first two are about the data; the third is about the schema and has
 * a one-word fix, so it says so rather than just refusing.
 */
function JoinOutcome({
  preview,
  error,
}: {
  preview: import('../api/types').JoinPreview | undefined
  error: unknown
}) {
  if (error) {
    return (
      <p className="rounded bg-rose-50 p-2 text-xs text-rose-800">
        {error instanceof ApiError ? error.message : String(error)}
      </p>
    )
  }
  if (!preview) return null

  const rate = preview.sampled ? preview.matched / preview.sampled : 0
  const bad = preview.fanout || preview.collisions.length > 0
  const thin = !bad && preview.sampled > 0 && rate < 0.5
  const tone = bad
    ? 'border-rose-200 bg-rose-50 text-rose-900'
    : thin
      ? 'border-amber-200 bg-amber-50 text-amber-900'
      : 'border-slate-200 bg-slate-50 text-slate-600'

  return (
    <div className={`rounded border p-2 text-xs ${tone}`}>
      <p>
        {preview.left_rows.toLocaleString()} rows →{' '}
        <span className="font-medium">
          {preview.exact ? '' : 'about '}
          {preview.result_rows.toLocaleString()}
        </span>
        {preview.sampled > 0 && (
          <>
            {' · '}
            {preview.matched.toLocaleString()} of {preview.sampled.toLocaleString()}{' '}
            sampled match ({(rate * 100).toFixed(0)}%)
          </>
        )}
        {preview.columns_added.length > 0 && (
          <>
            {' · '}
            {preview.columns_added.length} column
            {preview.columns_added.length === 1 ? '' : 's'} added
          </>
        )}
      </p>
      {preview.notes.map((n) => (
        <p key={n} className="mt-1">
          {n}
        </p>
      ))}
    </div>
  )
}
