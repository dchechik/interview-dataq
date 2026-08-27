import { useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { useDatasets } from '../api/hooks'
import type { VizSpec } from '../api/types'
import { VizRenderer } from '../renderers'

interface Turn {
  type: 'text' | 'tool_use' | 'tool_result' | 'error' | 'done'
  text?: string
  tool_name?: string
  tool_input?: Record<string, unknown>
  tool_result?: unknown
}

const EXAMPLES = [
  'Which pickup zones are busiest on a Thursday night?',
  'Annotate each login with how common its country is, then show me the rarest.',
  'What are the most interesting patterns in this data?',
]

function ToolCall({ turn, result }: { turn: Turn; result?: Turn }) {
  const [open, setOpen] = useState(false)
  const failed =
    result?.tool_result != null &&
    typeof result.tool_result === 'object' &&
    'error' in (result.tool_result as object)

  return (
    <div className="rounded border border-slate-200 bg-slate-50 px-3 py-1.5 text-xs">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 text-left"
      >
        <span className={failed ? 'text-rose-600' : 'text-emerald-600'}>
          {result ? (failed ? '✗' : '✓') : '…'}
        </span>
        <code className="font-mono font-medium text-slate-700">{turn.tool_name}</code>
        <span className="truncate text-slate-500">
          {Object.entries(turn.tool_input ?? {})
            .map(([k, v]) => `${k}=${typeof v === 'object' ? '{…}' : String(v)}`)
            .join(' ')}
        </span>
        <span className="ml-auto text-slate-400">{open ? '−' : '+'}</span>
      </button>
      {open && (
        <pre className="mt-1 max-h-56 overflow-auto rounded bg-white p-2 text-[11px] text-slate-600">
          {JSON.stringify(turn.tool_input, null, 2)}
          {result ? `\n\n→ ${JSON.stringify(result.tool_result, null, 2)}` : ''}
        </pre>
      )}
    </div>
  )
}

/**
 * The agent chat. Turns arrive over SSE so tool calls appear as they run rather
 * than after the whole thing finishes -- the user watches the work.
 */
export function AgentPage() {
  const { id } = useParams()
  const { data: datasets } = useDatasets()
  const [turns, setTurns] = useState<Turn[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)

  const dataset = datasets?.find((d) => d.id === id)

  async function send(message: string) {
    if (!message.trim() || busy) return
    setBusy(true)
    setTurns((t) => [...t, { type: 'text', text: `**You:** ${message}` }])
    setInput('')

    const scoped = dataset
      ? `${message}\n\n(The user is looking at dataset ${dataset.id} — "${dataset.name}".)`
      : message

    try {
      const res = await fetch('/api/agent/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: scoped, history: [] }),
      })
      if (!res.body) throw new Error('no response body')

      // Parse the SSE stream by hand: fetch gives us a byte stream, and we want
      // POST semantics that EventSource cannot provide.
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const parts = buffer.split('\n\n')
        buffer = parts.pop() ?? ''
        for (const part of parts) {
          const line = part.split('\n').find((l) => l.startsWith('data: '))
          if (!line) continue
          const turn = JSON.parse(line.slice(6)) as Turn
          if (turn.type !== 'done') setTurns((t) => [...t, turn])
          scrollRef.current?.scrollTo({ top: 1e9 })
        }
      }
    } catch (e) {
      setTurns((t) => [...t, { type: 'error', text: String(e) }])
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        {id && (
          <Link to={`/datasets/${id}`} className="text-sm text-slate-500 hover:text-slate-800">
            ← {dataset?.name ?? id}
          </Link>
        )}
        <h1 className="text-xl font-semibold text-slate-900">Ask</h1>
        <span className="text-xs text-slate-500">
          the agent uses the same APIs as the UI
        </span>
      </div>

      <div
        ref={scrollRef}
        className="max-h-[60vh] space-y-2 overflow-auto rounded-lg border border-slate-200 bg-white p-4"
      >
        {!turns.length && (
          <div className="py-8 text-center">
            <p className="mb-3 text-sm text-slate-500">Ask a question about your data.</p>
            <div className="flex flex-wrap justify-center gap-2">
              {EXAMPLES.map((e) => (
                <button
                  key={e}
                  type="button"
                  onClick={() => send(e)}
                  className="rounded border border-slate-300 px-3 py-1.5 text-xs text-slate-600 hover:bg-slate-50"
                >
                  {e}
                </button>
              ))}
            </div>
          </div>
        )}

        {turns.map((turn, i) => {
          if (turn.type === 'tool_use') {
            const result = turns
              .slice(i + 1)
              .find((t) => t.type === 'tool_result' && t.tool_name === turn.tool_name)
            return <ToolCall key={i} turn={turn} result={result} />
          }
          if (turn.type === 'tool_result') {
            // Charts the agent built are worth showing, not just logging.
            const r = turn.tool_result as { spec?: VizSpec; sample?: unknown[] } | null
            if (turn.tool_name === 'render_viz' && r?.spec) {
              return (
                <div key={i} className="rounded border border-slate-200 p-3">
                  <p className="mb-2 text-xs font-medium text-slate-700">{r.spec.title}</p>
                  <VizRenderer
                    spec={r.spec}
                    data={(r.sample ?? []) as Record<string, unknown>[]}
                    height={200}
                  />
                  <p className="mt-1 text-xs text-slate-400">
                    preview of the agent&apos;s chart — open Explore for the full view
                  </p>
                </div>
              )
            }
            return null
          }
          if (turn.type === 'error') {
            return (
              <p key={i} className="rounded bg-rose-50 p-2 text-sm text-rose-800">
                {turn.text}
              </p>
            )
          }
          return (
            <p key={i} className="whitespace-pre-wrap text-sm text-slate-800">
              {turn.text}
            </p>
          )
        })}

        {busy && <p className="text-sm text-slate-400">thinking…</p>}
      </div>

      <div className="flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && send(input)}
          placeholder="Ask about your data…"
          disabled={busy}
          className="flex-1 rounded border border-slate-300 px-3 py-2 text-sm disabled:bg-slate-50"
        />
        <button
          type="button"
          onClick={() => send(input)}
          disabled={busy || !input.trim()}
          className="rounded bg-slate-900 px-4 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-40"
        >
          Send
        </button>
      </div>
    </div>
  )
}
