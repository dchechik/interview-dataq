import { useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { NavLink, Outlet } from 'react-router-dom'

import { api, setToken, setUnauthenticatedHandler } from './api/client'
import { Login } from './components/Login'

const linkClass = ({ isActive }: { isActive: boolean }) =>
  `rounded px-3 py-1.5 text-sm font-medium transition-colors ${
    isActive ? 'bg-slate-900 text-white' : 'text-slate-600 hover:bg-slate-100'
  }`

export function App() {
  const { data: health } = useQuery({ queryKey: ['health'], queryFn: api.health })
  // Set by the API client the first time a request comes back 401, which is
  // also how an expired session surfaces: the next request fails and the screen
  // comes back rather than the app silently showing nothing.
  const [signedOut, setSignedOut] = useState(false)
  useEffect(() => {
    setUnauthenticatedHandler(() => setSignedOut(true))
    return () => setUnauthenticatedHandler(null)
  }, [])

  // Only meaningful on an instance with auth configured; on a local one no
  // request ever 401s and this never renders.
  const { data: me } = useQuery({
    queryKey: ['me'],
    queryFn: api.me,
    retry: false,
    staleTime: Infinity,
  })

  if (signedOut) return <Login />

  return (
    <div className="min-h-full bg-slate-50">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-7xl items-center gap-6 px-6 py-3">
          <span className="text-lg font-semibold tracking-tight text-slate-900">DataQ</span>
          <nav className="flex gap-1">
            <NavLink to="/datasets" className={linkClass}>
              Datasets
            </NavLink>
            <NavLink to="/dashboards" className={linkClass}>
              Dashboards
            </NavLink>
            <NavLink to="/ask" className={linkClass}>
              Ask
            </NavLink>
          </nav>
          <div className="ml-auto flex items-center gap-3 text-xs text-slate-500">
            {health ? (
              <span>
                storage <code className="font-mono text-slate-700">{health.storage}</code> ·{' '}
                {health.plugins} plugins
              </span>
            ) : (
              <span>connecting…</span>
            )}
            {me?.username && (
              <>
                <span className="text-slate-400">·</span>
                <span className="text-slate-600">{me.username}</span>
                <button
                  type="button"
                  onClick={() => {
                    setToken(null)
                    window.location.reload()
                  }}
                  className="rounded border border-slate-300 px-2 py-0.5 hover:bg-slate-50"
                >
                  Sign out
                </button>
              </>
            )}
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-6 py-6">
        <Outlet />
      </main>
    </div>
  )
}
