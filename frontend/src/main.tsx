import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'

import { App } from './App'
import { AgentPage } from './pages/AgentPage'
import './index.css'
import { DashboardsPage } from './pages/DashboardsPage'
import { DatasetPage } from './pages/DatasetPage'
import { DatasetsPage } from './pages/DatasetsPage'
import { ExplorePage } from './pages/ExplorePage'
import { QueryPage } from './pages/QueryPage'

const queryClient = new QueryClient({
  defaultOptions: { queries: { refetchOnWindowFocus: false, staleTime: 5_000 } },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<App />}>
            <Route index element={<Navigate to="/datasets" replace />} />
            <Route path="datasets" element={<DatasetsPage />} />
            <Route path="datasets/:id" element={<DatasetPage />} />
            <Route path="datasets/:id/query" element={<QueryPage />} />
            <Route path="datasets/:id/explore" element={<ExplorePage />} />
            <Route path="datasets/:id/ask" element={<AgentPage />} />
            <Route path="ask" element={<AgentPage />} />
            <Route path="dashboards" element={<DashboardsPage />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
