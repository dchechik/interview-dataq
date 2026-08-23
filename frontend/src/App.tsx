import { useQuery } from '@tanstack/react-query'
import { NavLink, Outlet } from 'react-router-dom'

import { api } from './api/client'

const linkClass = ({ isActive }: { isActive: boolean }) =>
  `rounded px-3 py-1.5 text-sm font-medium transition-colors ${
    isActive ? 'bg-slate-900 text-white' : 'text-slate-600 hover:bg-slate-100'
  }`

export function App() {
  const { data: health } = useQuery({ queryKey: ['health'], queryFn: api.health })

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
          </nav>
          <div className="ml-auto text-xs text-slate-500">
            {health ? (
              <>
                storage <code className="font-mono text-slate-700">{health.storage}</code> ·{' '}
                {health.plugins} plugins
              </>
            ) : (
              'connecting…'
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
