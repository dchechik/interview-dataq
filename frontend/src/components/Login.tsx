import { useState } from 'react'

import { api, ApiError, setToken } from '../api/client'

/**
 * The sign-in screen.
 *
 * Replaces a `window.prompt` for a server-side token — which asked a person for
 * something only the deployer had, gave no way to tell two people apart, and
 * could not be revoked for one of them. A password buys a session; the session
 * is what every later request carries.
 *
 * Reloading rather than routing on success is deliberate: every query in the
 * app was fired without a credential and failed, so the cheapest correct thing
 * is to start the page over now that there is one.
 */
export function Login() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const { token } = await api.login(username, password)
      setToken(token)
      window.location.reload()
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 401
          ? 'Wrong username or password.'
          : e instanceof Error
            ? e.message
            : String(e),
      )
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 p-6">
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-lg border border-slate-200 bg-white p-6 shadow-sm"
      >
        <h1 className="text-lg font-semibold tracking-tight text-slate-900">DataQ</h1>
        <p className="mt-1 text-sm text-slate-500">Sign in to continue.</p>

        <label className="mt-5 block">
          <span className="mb-1 block text-sm font-medium text-slate-700">Username</span>
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoFocus
            className="w-full rounded border border-slate-300 px-3 py-1.5 text-sm"
          />
        </label>

        <label className="mt-3 block">
          <span className="mb-1 block text-sm font-medium text-slate-700">Password</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            className="w-full rounded border border-slate-300 px-3 py-1.5 text-sm"
          />
        </label>

        {error && (
          <p className="mt-3 rounded bg-rose-50 p-2 text-sm text-rose-800">{error}</p>
        )}

        <button
          type="submit"
          disabled={busy || !username || !password}
          className="mt-4 w-full rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-40"
        >
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}
