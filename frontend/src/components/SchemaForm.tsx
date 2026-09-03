/**
 * Renders a form from a plugin's JSON Schema.
 *
 * This is the payoff of the single-descriptor design: adding a plugin to the
 * backend gives it a working UI with no frontend change at all. The schema comes
 * straight from the plugin's Pydantic `Params` model via `GET /api/plugins`.
 */
import { useState } from 'react'

import { NumberField } from './NumberField'
import type { JsonSchema } from '../api/types'

interface Props {
  schema: JsonSchema
  value: Record<string, unknown>
  onChange: (next: Record<string, unknown>) => void
  /** Column names, so column-typed fields become a picker instead of free text. */
  columns?: string[]
}

/** Pydantic renders optional fields as `anyOf: [T, null]`; unwrap to the real type. */
function effective(schema: JsonSchema): JsonSchema {
  if (schema.anyOf) {
    const real = schema.anyOf.find((s) => s.type !== 'null')
    if (real) return { ...real, description: schema.description, default: schema.default }
  }
  return schema
}

/** `hour_of_day` reads as "hour of day" in a menu without losing the sent value. */
function humanize(value: unknown): string {
  return String(value).replace(/_/g, ' ')
}

/**
 * A list of strings edited as text -- one per line, or comma separated.
 *
 * The field's value is a list, but a list is not what the user is typing. The
 * normalisation the backend wants (trim each entry, drop the empty ones) cannot
 * run on every keystroke: pressing Enter makes an empty last line, which the
 * filter deleted before the re-render, so a new line could never be started --
 * and a comma vanished the moment it was typed. The typed text is therefore
 * local state and the parsed list is published alongside it; the parent's value
 * is adopted only when it changes to something this field did not publish,
 * which is how re-seeding the form from a suggestion still works.
 */
function ListField({
  value,
  onChange,
  multiline = false,
  placeholder,
}: {
  value: unknown
  onChange: (next: string[]) => void
  multiline?: boolean
  placeholder: string
}) {
  const sep = multiline ? '\n' : ', '
  const parse = (text: string) =>
    text
      .split(multiline ? '\n' : ',')
      .map((s) => s.trim())
      .filter(Boolean)

  const external = Array.isArray(value) ? value.join(sep) : String(value ?? '')
  const [state, setState] = useState({ text: external, published: external })
  if (state.published !== external) setState({ text: external, published: external })

  const edit = (text: string) => {
    const lines = parse(text)
    setState({ text, published: lines.join(sep) })
    onChange(lines)
  }

  const className = multiline
    ? 'w-full rounded border border-slate-300 px-2 py-1.5 font-mono text-xs'
    : 'w-full rounded border border-slate-300 px-2 py-1.5 text-sm'

  if (!multiline) {
    return (
      <input
        type="text"
        className={className}
        placeholder={placeholder}
        value={state.text}
        onChange={(e) => edit(e.target.value)}
      />
    )
  }
  return (
    <textarea
      rows={Math.max(4, state.text.split('\n').length + 1)}
      spellCheck={false}
      className={className}
      placeholder={placeholder}
      value={state.text}
      onChange={(e) => edit(e.target.value)}
    />
  )
}

function looksLikeColumn(name: string): boolean {
  return (
    name === 'column' ||
    name.endsWith('_column') ||
    name === 'dimension' ||
    name === 'measure' ||
    name === 'series' ||
    // Not spelled `*_column`, but a column all the same -- and typing a column
    // name by hand into a chart form is how you get a chart that will not draw.
    name === 'animate_by'
  )
}

export function SchemaForm({ schema, value, onChange, columns = [] }: Props) {
  const properties = schema.properties ?? {}
  const required = new Set(schema.required ?? [])
  const entries = Object.entries(properties)

  if (!entries.length) {
    return <p className="text-sm text-slate-500">No configuration needed.</p>
  }

  const set = (key: string, v: unknown) => onChange({ ...value, [key]: v })

  return (
    <div className="space-y-3">
      {entries.map(([key, raw]) => {
        const field = effective(raw)
        const current = value[key] ?? field.default ?? ''
        const label = field.title ?? key
        const isColumn = looksLikeColumn(key) && columns.length > 0

        return (
          <label key={key} className="block">
            <span className="mb-1 block text-sm font-medium text-slate-700">
              {label}
              {required.has(key) && <span className="ml-0.5 text-rose-600">*</span>}
            </span>

            {isColumn ? (
              <select
                className="w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
                value={String(current ?? '')}
                onChange={(e) => set(key, e.target.value || null)}
              >
                <option value="">—</option>
                {columns.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            ) : field.enum ? (
              <select
                className="w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
                value={String(current ?? '')}
                // An optional enum must be un-settable, so "" maps back to null
                // rather than being sent as an empty string the backend rejects.
                onChange={(e) => set(key, e.target.value === '' ? null : e.target.value)}
              >
                {!required.has(key) && <option value="">— none —</option>}
                {field.enum.map((o) => (
                  <option key={String(o)} value={String(o)}>
                    {humanize(o)}
                  </option>
                ))}
              </select>
            ) : field.type === 'boolean' ? (
              <input
                type="checkbox"
                checked={Boolean(current)}
                onChange={(e) => set(key, e.target.checked)}
                className="h-4 w-4"
              />
            ) : field.type === 'integer' || field.type === 'number' ? (
              <NumberField
                className="w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
                value={current === '' || current === null ? null : Number(current)}
                min={field.minimum}
                max={field.maximum}
                onChange={(v) => set(key, v)}
              />
            ) : field.type === 'array' && field.format === 'textarea' ? (
              // A list where each entry is a line of notation rather than a
              // word: feature expressions, one per line. Comma-separating them
              // would be unreadable, and would collide with the commas inside
              // 'by user, activity_type'.
              <ListField
                multiline
                value={current}
                onChange={(v) => set(key, v)}
                placeholder={'count() by user, activity_type over 30d\ndays_since_last() by user'}
              />
            ) : field.type === 'array' ? (
              <ListField
                value={current}
                onChange={(v) => set(key, v)}
                placeholder="comma separated"
              />
            ) : (
              <input
                type="text"
                className="w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
                value={current === null ? '' : String(current)}
                onChange={(e) => set(key, e.target.value === '' ? null : e.target.value)}
              />
            )}

            {field.description && (
              <span className="mt-0.5 block text-xs text-slate-500">{field.description}</span>
            )}
          </label>
        )
      })}
    </div>
  )
}
