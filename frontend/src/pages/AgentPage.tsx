import { useQuery } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { api, ApiError } from '../api/client'
import { useDataset } from '../api/hooks'
import type { AgentEstimate, AgentTool, VizSpec } from '../api/types'
import { VizRenderer } from '../renderers'

interface Turn {
  /** `user` is added here, not by the server — it is this side of the conversation. */
  type: 'user' | 'text' | 'tool_use' | 'tool_result' | 'error' | 'done'
  text?: string
  tool_name?: string
  tool_input?: Record<string, unknown>
  tool_result?: unknown
  /** When this turn reached the browser, so a call can report how long it took. */
  at?: number
}

/**
 * The run, regrouped into what the agent actually did.
 *
 * The stream is flat — text, tool_use, tool_result, text, … — but it is not
 * shapeless: the model emits its reasoning as text and then the calls that
 * reasoning leads to, so a text block followed by tool calls *is* the stated
 * intent for those calls. Recovering that pairing is what turns a log into
 * progress you can read.
 *
 * A text block with no calls after it is the answer, not an intent, and stays
 * in the conversation where the user is looking for it.
 */
interface Step {
  /** What the model said it was about to do. Absent when it called without narrating. */
  intent?: string
  calls: { use: Turn; result?: Turn }[]
}

interface Segment {
  question?: string
  steps: Step[]
  /** Prose and errors the user should read directly, in arrival order. */
  said: Turn[]
}

function segments(turns: Turn[]): Segment[] {
  const out: Segment[] = []
  let current: Segment = { steps: [], said: [] }
  // Held back until we know which it is: an intent if calls follow, otherwise
  // an answer. Nothing else can tell the two apart at the moment it arrives.
  let pending: string | undefined

  const flushPending = () => {
    if (pending !== undefined) {
      current.said.push({ type: 'text', text: pending })
      pending = undefined
    }
  }

  for (const turn of turns) {
    if (turn.type === 'user') {
      flushPending()
      if (current.question !== undefined || current.steps.length || current.said.length) {
        out.push(current)
      }
      current = { question: turn.text, steps: [], said: [] }
      continue
    }
    if (turn.type === 'text') {
      flushPending()
      pending = turn.text
      continue
    }
    if (turn.type === 'tool_use') {
      // The held text belongs to this call rather than to the conversation.
      if (pending !== undefined) {
        current.steps.push({ intent: pending, calls: [{ use: turn }] })
        pending = undefined
      } else {
        const last = current.steps.at(-1)
        // Several calls in one model turn share its intent, so they share a step.
        if (last && !last.calls.at(-1)?.result) last.calls.push({ use: turn })
        else current.steps.push({ calls: [{ use: turn }] })
      }
      continue
    }
    if (turn.type === 'tool_result') {
      for (const step of [...current.steps].reverse()) {
        const call = [...step.calls].reverse()
          .find((c) => c.use.tool_name === turn.tool_name && !c.result)
        if (call) {
          call.result = turn
          break
        }
      }
      continue
    }
    if (turn.type === 'error') {
      flushPending()
      current.said.push(turn)
    }
  }
  flushPending()
  out.push(current)
  return out.filter((s) => s.question !== undefined || s.steps.length || s.said.length)
}

function failed(result?: Turn): boolean {
  return (
    result?.tool_result != null &&
    typeof result.tool_result === 'object' &&
    'error' in (result.tool_result as object)
  )
}

/** `dataset_id=a1b2, limit=100` — enough to recognise the call without opening it. */
function summarise(input: Record<string, unknown> | undefined): string {
  return Object.entries(input ?? {})
    .map(([k, v]) => {
      if (v === null || v === undefined) return `${k}=—`
      if (Array.isArray(v)) return `${k}=[${v.length}]`
      if (typeof v === 'object') return `${k}={…}`
      const text = String(v)
      return `${k}=${text.length > 28 ? `${text.slice(0, 27)}…` : text}`
    })
    .join(', ')
}

const EXAMPLES = [
  'Which pickup zones are busiest on a Thursday night?',
  'Annotate each login with how common its country is, then show me the rarest.',
  'What are the most interesting patterns in this data?',
]

/**
 * One call: which tool, with what arguments, and — from the tool surface the
 * model itself was handed — what that tool does.
 *
 * The description is not decoration. "It called run_query" says nothing about
 * whether that was a reasonable thing to do; "Run a structured query, validated
 * against the schema" is the missing half, and it is already served by
 * GET /api/agent/tools, which nothing was reading.
 */
function ToolCall({
  use,
  result,
  tool,
}: {
  use: Turn
  result?: Turn
  tool?: AgentTool
}) {
  const [open, setOpen] = useState(false)
  const bad = failed(result)
  const elapsed = result?.at && use.at ? result.at - use.at : undefined

  return (
    <div className="rounded border border-slate-200 bg-white">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-start gap-2 px-2 py-1.5 text-left hover:bg-slate-50"
      >
        <span
          className={`mt-px shrink-0 ${bad ? 'text-rose-600' : result ? 'text-emerald-600' : 'text-slate-400'}`}
          aria-label={bad ? 'failed' : result ? 'done' : 'running'}
        >
          {result ? (bad ? '✗' : '✓') : '…'}
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-baseline gap-x-1.5">
            <code className="font-mono text-xs font-medium text-slate-800">
              {use.tool_name}
            </code>
            <code className="min-w-0 truncate font-mono text-[11px] text-slate-500">
              {summarise(use.tool_input)}
            </code>
          </span>
          {tool && (
            <span className="mt-0.5 line-clamp-2 text-[11px] text-slate-500">
              {tool.description}
            </span>
          )}
        </span>
        {elapsed !== undefined && (
          <span className="shrink-0 font-mono text-[11px] tabular-nums text-slate-400">
            {elapsed < 1000 ? `${elapsed}ms` : `${(elapsed / 1000).toFixed(1)}s`}
          </span>
        )}
        <span className="shrink-0 text-slate-400">{open ? '−' : '+'}</span>
      </button>

      {open && (
        <div className="border-t border-slate-100 px-2 py-1.5">
          {tool && <p className="mb-1.5 text-[11px] text-slate-600">{tool.description}</p>}
          <p className="text-[11px] font-medium text-slate-500">Parameters</p>
          <pre className="mt-0.5 max-h-40 overflow-auto rounded bg-slate-50 p-2 text-[11px] text-slate-700">
            {JSON.stringify(use.tool_input ?? {}, null, 2)}
          </pre>
          <p className="mt-1.5 text-[11px] font-medium text-slate-500">
            {result ? (bad ? 'Error' : 'Result') : 'Still running…'}
          </p>
          {result && (
            <pre
              className={`mt-0.5 max-h-56 overflow-auto rounded p-2 text-[11px] ${
                bad ? 'bg-rose-50 text-rose-800' : 'bg-slate-50 text-slate-700'
              }`}
            >
              {JSON.stringify(result.tool_result, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  )
}

/**
 * The run's work, collapsed into one thing you can open.
 *
 * Open while it runs, because watching the agent work is the point — the tool
 * calls are how you tell a good answer from a plausible one. Closed once it is
 * done, because by then the answer below it is what you came for. Either way
 * the header keeps saying what happened, so collapsing loses no information the
 * user has to reopen it to recover.
 */
function AgentProgress({ steps, tools }: { steps: Step[]; tools: AgentTool[] }) {
  const calls = steps.flatMap((s) => s.calls)
  const running = calls.some((c) => !c.result)
  const errors = calls.filter((c) => failed(c.result)).length
  // Keyed on `running` so the panel opens itself when work starts and folds
  // away when it ends -- while still honouring a click either way, since the
  // click updates the same state.
  const [open, setOpen] = useState(running)
  const [touched, setTouched] = useState(false)
  const shown = touched ? open : running

  const byName = new Map(tools.map((t) => [t.name, t]))

  return (
    <div className="rounded-lg border border-slate-200 bg-slate-50">
      <button
        type="button"
        onClick={() => {
          setTouched(true)
          setOpen(!shown)
        }}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
      >
        <span className="text-xs text-slate-400">{shown ? '▾' : '▸'}</span>
        <span className="text-xs font-medium text-slate-700">
          {running ? 'Working' : 'Agent progress'}
        </span>
        <span className="text-xs text-slate-500">
          {calls.length} {calls.length === 1 ? 'call' : 'calls'}
          {steps.length > 1 && ` · ${steps.length} steps`}
          {errors > 0 && (
            <span className="text-rose-600"> · {errors} failed</span>
          )}
        </span>
        {running && (
          <span className="truncate font-mono text-[11px] text-slate-400">
            {calls.find((c) => !c.result)?.use.tool_name}…
          </span>
        )}
        <span className="ml-auto text-[11px] text-slate-400">
          {shown ? 'hide' : 'show'}
        </span>
      </button>

      {shown && (
        <ol className="space-y-2 border-t border-slate-200 px-3 py-2">
          {steps.map((step, i) => (
            <li key={i} className="flex gap-2">
              <span className="mt-0.5 shrink-0 font-mono text-[11px] tabular-nums text-slate-400">
                {i + 1}
              </span>
              <div className="min-w-0 flex-1 space-y-1">
                {step.intent && (
                  // What the model said it was doing, in its own words. Left in
                  // the trace rather than the conversation: it explains the
                  // calls beneath it and reads as noise anywhere else.
                  <p className="whitespace-pre-wrap text-xs text-slate-600">{step.intent}</p>
                )}
                {step.calls.map((call, j) => (
                  <ToolCall
                    key={j}
                    use={call.use}
                    result={call.result}
                    tool={byName.get(call.use.tool_name ?? '')}
                  />
                ))}
              </div>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

/**
 * Shown before any request is sent, because the run costs real money and the
 * user is the one paying. The first-request count is what we can state exactly;
 * the run total depends on how many tools the model decides to call, so it is
 * given as a ceiling rather than a promise.
 */
function ConfirmRun({
  estimate,
  message,
  onConfirm,
  onCancel,
}: {
  estimate: AgentEstimate
  message: string
  onConfirm: () => void
  onCancel: () => void
}) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4"
      onClick={onCancel}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Confirm agent run"
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-md rounded-lg bg-white p-5 shadow-xl"
      >
        <h2 className="text-base font-semibold text-slate-900">Run the agent?</h2>
        <p className="mt-1 truncate text-sm text-slate-500">“{message}”</p>

        <dl className="mt-4 space-y-1.5 text-sm">
          <div className="flex justify-between">
            <dt className="text-slate-500">Tokens in first request</dt>
            <dd className="font-mono tabular-nums text-slate-800">
              {estimate.input_tokens.toLocaleString()}
              {!estimate.exact && <span className="ml-1 text-amber-600">approx</span>}
            </dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-slate-500">That request costs</dt>
            <dd className="font-mono tabular-nums text-slate-800">
              ${estimate.first_request_usd.toFixed(4)}
            </dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-slate-500">Whole run, at most</dt>
            <dd className="font-mono tabular-nums font-medium text-slate-900">
              ${estimate.worst_case_usd.toFixed(2)}
            </dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-slate-500">Model</dt>
            <dd className="font-mono text-xs text-slate-600">{estimate.model}</dd>
          </div>
        </dl>

        <p className="mt-3 text-xs text-slate-500">
          The agent may call up to {estimate.max_turns} rounds of its {estimate.tools} tools,
          resending the conversation each time — so the real cost lands between these two
          figures.
          {!estimate.exact && ' Token count is approximate: the API could not be reached to count it exactly.'}
        </p>

        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="rounded border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className="rounded bg-slate-900 px-3 py-1.5 text-sm text-white hover:bg-slate-800"
          >
            Run it
          </button>
        </div>
      </div>
    </div>
  )
}

/**
 * The agent chat. Turns arrive over SSE so tool calls appear as they run rather
 * than after the whole thing finishes -- the user watches the work.
 */
export function AgentPage() {
  const { id } = useParams()

  const [turns, setTurns] = useState<Turn[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [pending, setPending] = useState<{ message: string; estimate: AgentEstimate } | null>(
    null,
  )
  const scrollRef = useRef<HTMLDivElement>(null)

  // Fetched directly rather than scanned out of the list, so a deep link
  // resolves the name without waiting for every dataset.
  const { data: dataset } = useDataset(id)

  // The tool surface is fixed per deployment, so it caches forever. It is what
  // lets a call in the trace say what it does, not just what it is called.
  const { data: toolRows } = useQuery({
    queryKey: ['agent-tools'],
    queryFn: api.agentTools,
    staleTime: Infinity,
  })
  const tools = toolRows ?? []

  /** Price the run and ask, rather than spending the user's money on their behalf. */
  async function ask(message: string) {
    if (!message.trim() || busy) return
    try {
      const estimate = await api.agentEstimate(message)
      setPending({ message, estimate })
    } catch (e) {
      setTurns((t) => [
        ...t,
        { type: 'error', text: e instanceof ApiError ? e.message : String(e) },
      ])
    }
  }

  async function send(message: string) {
    if (!message.trim() || busy) return
    setBusy(true)
    setTurns((t) => [...t, { type: 'user', text: message, at: Date.now() }])
    setInput('')

    const scoped = dataset
      ? `${message}\n\n(The user is looking at dataset ${dataset.id} — "${dataset.name}".)`
      : message

    try {
      const res = await api.agentChat(scoped)
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
          // Stamped on arrival: the server sends no timings, and the gap
          // between a call and its result is the only per-call duration
          // anything here can honestly report.
          if (turn.type !== 'done') setTurns((t) => [...t, { ...turn, at: Date.now() }])
          scrollRef.current?.scrollTo({ top: 1e9 })
        }
      }
    } catch (e) {
      // The server's `detail`, not "[object Object]" -- the same treatment the
      // estimate call gives it.
      setTurns((t) => [
        ...t,
        { type: 'error', text: e instanceof ApiError ? e.message : String(e) },
      ])
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-4">
      {pending && (
        <ConfirmRun
          estimate={pending.estimate}
          message={pending.message}
          onCancel={() => setPending(null)}
          onConfirm={() => {
            const { message } = pending
            setPending(null)
            send(message)
          }}
        />
      )}
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
                  onClick={() => ask(e)}
                  className="rounded border border-slate-300 px-3 py-1.5 text-xs text-slate-600 hover:bg-slate-50"
                >
                  {e}
                </button>
              ))}
            </div>
          </div>
        )}

        {segments(turns).map((segment, i) => (
          <div key={i} className="space-y-2">
            {segment.question !== undefined && (
              <p className="ml-auto w-fit max-w-[80%] rounded-lg bg-slate-100 px-3 py-1.5 text-sm text-slate-800">
                {segment.question}
              </p>
            )}

            {segment.steps.length > 0 && (
              <AgentProgress steps={segment.steps} tools={tools} />
            )}

            {/* Charts the agent built are the work's output, not a record of
                it, so they stay in the conversation rather than folding away
                inside the trace. */}
            {segment.steps
              .flatMap((step) => step.calls)
              .filter((call) => call.use.tool_name === 'render_viz')
              .map((call, j) => {
                const r = call.result?.tool_result as
                  | { spec?: VizSpec; sample?: unknown[] }
                  | null
                if (!r?.spec) return null
                return (
                  <div key={j} className="rounded border border-slate-200 p-3">
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
              })}

            {segment.said.map((turn, j) =>
              turn.type === 'error' ? (
                <p key={j} className="rounded bg-rose-50 p-2 text-sm text-rose-800">
                  {turn.text}
                </p>
              ) : (
                <p key={j} className="whitespace-pre-wrap text-sm text-slate-800">
                  {turn.text}
                </p>
              ),
            )}
          </div>
        ))}

        {busy && <p className="text-sm text-slate-400">thinking…</p>}
      </div>

      <div className="flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && ask(input)}
          placeholder="Ask about your data…"
          disabled={busy}
          className="flex-1 rounded border border-slate-300 px-3 py-2 text-sm disabled:bg-slate-50"
        />
        <button
          type="button"
          onClick={() => ask(input)}
          disabled={busy || !input.trim()}
          className="rounded bg-slate-900 px-4 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-40"
        >
          Send
        </button>
      </div>
    </div>
  )
}
