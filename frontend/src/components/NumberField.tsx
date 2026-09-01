import { useState } from 'react'

/**
 * A number input that lets you type a number.
 *
 * The value is a number, but that is not what the user is typing. Parsing on
 * every keystroke and feeding the result back turns "0." into "0" before the
 * next digit arrives, so a threshold like 0.005 can only be reached with the
 * spinner -- and a cleared box springs back to a value the user just deleted.
 * The typed text is therefore local state; the parsed number is published
 * alongside it, and the parent's value is adopted only when it changes to
 * something this field did not publish.
 */
export function NumberField({
  value,
  onChange,
  className = '',
  step,
  min,
  max,
}: {
  /** null renders an empty box -- an optional parameter nobody has set. */
  value: number | null
  onChange: (next: number | null) => void
  className?: string
  step?: number | string
  min?: number
  max?: number
}) {
  const external = value === null ? '' : String(value)
  const [state, setState] = useState({ text: external, published: external })
  if (state.published !== external) setState({ text: external, published: external })

  return (
    <input
      type="number"
      step={step}
      min={min}
      max={max}
      className={className}
      value={state.text}
      onChange={(e) => {
        const text = e.target.value
        // An emptied box means "unset". A half-typed number ("-", "0.") means
        // nothing yet, so it is kept on screen and simply not published: the
        // last complete value stands until the next one is.
        if (text.trim() === '') {
          setState({ text, published: '' })
          if (value !== null) onChange(null)
          return
        }
        const parsed = Number(text)
        if (!Number.isFinite(parsed)) {
          setState({ text, published: state.published })
          return
        }
        setState({ text, published: String(parsed) })
        if (parsed !== value) onChange(parsed)
      }}
    />
  )
}
