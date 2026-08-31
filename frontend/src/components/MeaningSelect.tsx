import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { api, ApiError } from '../api/client'
import type { SemanticType } from '../api/types'

/**
 * Choosing what a column means, including meanings that do not exist yet.
 *
 * The built-in types are the ones a detector can recognise from values alone,
 * which leaves out almost everything specific to an organisation: machine
 * names, cost centres, badge numbers, queue names. Those columns arrive with no
 * meaning at all, and a column with no meaning matches no plugin's accepted
 * types and joins to nothing — so the two most useful columns in a browsing log
 * can be the two DataQ has least to say about.
 *
 * Naming one is a claim only a person can make, and it pays off across datasets
 * rather than within one: calling `pc` here and `device` there both
 * `machine.name` is what makes the join suggester offer to link them. So the
 * picker can define as well as select, and it does it in place rather than
 * sending anyone to a settings page in the middle of an import.
 *
 * The parent is the part that matters and the part nobody would think to fill
 * in, so it is pre-chosen. A type descending from nothing matches no accepted
 * type anywhere, which would leave the column worse off than unlabelled.
 */

const NEW = ' new'

/**
 * Which existing meaning a new one should sit under, given how the column is
 * stored. The server applies the same defaulting and is the authority; this
 * copy exists only so the dropdown can show the answer before it is submitted.
 */
export function parentForPhysicalType(physical: string | null | undefined): string {
  const pt = (physical ?? '').toUpperCase()
  if (/^(TIMESTAMP|DATE|TIME)/.test(pt)) return 'temporal'
  if (/^(BIGINT|INTEGER|SMALLINT|TINYINT|HUGEINT|DOUBLE|FLOAT|DECIMAL|REAL)/.test(pt))
    return 'numeric'
  if (pt.startsWith('BOOL')) return 'boolean'
  // Text defaults to `categorical` rather than `text`: it is joinable, more
  // plugins accept it, and it descends from text anyway.
  return 'categorical'
}

export function useSemanticTypes() {
  return useQuery({
    queryKey: ['semantic-types'],
    queryFn: api.semanticTypes,
    staleTime: Infinity,
  })
}

export function MeaningSelect({
  value,
  onChange,
  types,
  physicalType,
  className,
}: {
  value: string | null
  onChange: (next: string | null) => void
  types: SemanticType[]
  /** How the column is stored, used to pre-choose a parent for a new meaning. */
  physicalType?: string | null
  className?: string
}) {
  const qc = useQueryClient()
  const [defining, setDefining] = useState(false)
  const [id, setId] = useState('')
  const [parent, setParent] = useState(() => parentForPhysicalType(physicalType))
  const [joinable, setJoinable] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const create = useMutation({
    mutationFn: () => api.createSemanticType({ id: id.trim(), parent, joinable }),
    onSuccess: async (created) => {
      await qc.invalidateQueries({ queryKey: ['semantic-types'] })
      onChange(created.id)
      setDefining(false)
      setId('')
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : String(e)),
  })

  const builtin = types.filter((t) => !t.custom)
  const custom = types.filter((t) => t.custom)

  return (
    <>
      <select
        className={className}
        value={value ?? ''}
        onChange={(e) => {
          if (e.target.value === NEW) {
            setError(null)
            setParent(parentForPhysicalType(physicalType))
            setDefining(true)
            return
          }
          onChange(e.target.value || null)
        }}
      >
        <option value="">&mdash;</option>
        {custom.length > 0 && (
          <optgroup label="Yours">
            {custom.map((t) => (
              <option key={t.id} value={t.id}>{t.id}</option>
            ))}
          </optgroup>
        )}
        <optgroup label="Built in">
          {builtin.map((t) => (
            <option key={t.id} value={t.id}>{t.id}</option>
          ))}
        </optgroup>
        <option value={NEW}>+ new meaning</option>
      </select>

      {defining && (
        <div className="mt-1 rounded border border-slate-300 bg-white p-2 text-xs shadow-sm">
          <input
            autoFocus
            className="w-full rounded border border-slate-300 px-1.5 py-1 font-mono text-xs"
            placeholder="machine.name"
            value={id}
            onChange={(e) => {
              setId(e.target.value)
              setError(null)
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && id.trim()) create.mutate()
              if (e.key === 'Escape') setDefining(false)
            }}
          />
          <label className="mt-1.5 flex items-center gap-1.5 text-slate-600">
            <span className="whitespace-nowrap">a kind of</span>
            <select
              className="min-w-0 flex-1 rounded border border-slate-300 px-1 py-0.5"
              value={parent}
              onChange={(e) => setParent(e.target.value)}
            >
              {types.map((t) => (
                <option key={t.id} value={t.id}>{t.id}</option>
              ))}
            </select>
          </label>
          <label className="mt-1 flex items-center gap-1.5 text-slate-600">
            <input
              type="checkbox"
              checked={joinable}
              onChange={(e) => setJoinable(e.target.checked)}
            />
            <span>columns sharing it can be joined</span>
          </label>
          <p className="mt-1 text-[11px] leading-snug text-slate-500">
            Give the same meaning to a column in another dataset and DataQ will
            offer to join them.
          </p>
          {error && <p className="mt-1 text-[11px] text-rose-700">{error}</p>}
          <div className="mt-1.5 flex gap-1.5">
            <button
              type="button"
              disabled={!id.trim() || create.isPending}
              onClick={() => create.mutate()}
              className="rounded bg-slate-900 px-2 py-0.5 text-white disabled:opacity-40"
            >
              {create.isPending ? 'Creating' : 'Create'}
            </button>
            <button
              type="button"
              onClick={() => setDefining(false)}
              className="rounded border border-slate-300 px-2 py-0.5 hover:bg-slate-50"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </>
  )
}
