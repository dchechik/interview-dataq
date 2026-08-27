import type {
  BrowseResult,
  Dashboard,
  DatasetProfile,
  DatasetSummary,
  Job,
  LineageStep,
  OperationAccepted,
  OperationRequest,
  PluginDescriptor,
  QueryResult,
  QuerySpec,
  RenderedViz,
  Suggestion,
  UploadResult,
} from './types'

/** Same-origin in production; Vite proxies /api to uvicorn in dev. */
const BASE = '/api'

export class ApiError extends Error {
  // Declared explicitly rather than as a constructor parameter property, which
  // `erasableSyntaxOnly` forbids.
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  // FormData must set its own Content-Type so the multipart boundary is included;
  // forcing application/json here would make the upload unparseable.
  const isForm = init?.body instanceof FormData
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      ...(isForm ? {} : { 'Content-Type': 'application/json' }),
      ...(init?.headers ?? {}),
    },
  })
  if (!res.ok) {
    // FastAPI puts the useful message in `detail`; surface it rather than a bare code.
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(detail, res.status)
  }
  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

const post = <T,>(path: string, body: unknown) =>
  request<T>(path, { method: 'POST', body: JSON.stringify(body) })

export const api = {
  health: () => request<{ status: string; storage: string; plugins: number }>('/health'),

  // --- plugins ---
  plugins: (params: { kind?: string; mode?: string; applicable_to?: string } = {}) => {
    const q = new URLSearchParams()
    if (params.kind) q.set('kind', params.kind)
    if (params.mode) q.set('mode', params.mode)
    if (params.applicable_to) q.set('applicable_to', params.applicable_to)
    const qs = q.toString()
    return request<PluginDescriptor[]>(`/plugins${qs ? `?${qs}` : ''}`)
  },
  semanticTypes: () =>
    request<
      { id: string; title: string; parent: string | null; role: string; joinable: boolean }[]
    >('/semantic-types'),

  // --- datasets ---
  datasets: () => request<DatasetSummary[]>('/datasets'),
  dataset: (id: string) => request<DatasetSummary>(`/datasets/${id}`),
  deleteDataset: (id: string) => request<{ deleted: string }>(`/datasets/${id}`, { method: 'DELETE' }),
  profile: (id: string, version?: number) =>
    request<DatasetProfile>(`/datasets/${id}/profile${version ? `?version=${version}` : ''}`),
  versions: (id: string) =>
    request<{ version: number; row_count: number; created_at: string; columns: number }[]>(
      `/datasets/${id}/versions`,
    ),
  lineage: (id: string) => request<LineageStep[]>(`/datasets/${id}/lineage`),
  suggestions: (id: string, kind?: string) =>
    request<Suggestion[]>(`/datasets/${id}/suggestions${kind ? `?kind=${kind}` : ''}`),
  pinColumnType: (id: string, column: string, semanticType: string | null, role?: string) => {
    const q = new URLSearchParams()
    if (semanticType) q.set('semantic_type', semanticType)
    if (role) q.set('role', role)
    return post<unknown>(`/datasets/${id}/columns/${encodeURIComponent(column)}/type?${q}`, {})
  },

  // --- sources ---
  browse: (path?: string, showHidden = false) => {
    const q = new URLSearchParams()
    if (path) q.set('path', path)
    if (showHidden) q.set('show_hidden', 'true')
    const qs = q.toString()
    return request<BrowseResult>(`/sources/browse${qs ? `?${qs}` : ''}`)
  },
  upload: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    // No Content-Type header: the browser must set the multipart boundary.
    return request<UploadResult>('/sources/upload', { method: 'POST', body: form })
  },
  preview: (uri: string, limit = 20) =>
    post<{ reader: string; columns: string[]; types: string[]; rows: unknown[][] }>(
      '/sources/preview',
      { uri, limit },
    ),

  // --- operations ---
  operation: (req: OperationRequest) => post<OperationAccepted>('/operations', req),
  inspect: (req: {
    plugin_id: string
    dataset_id: string
    version?: number | null
    params?: Record<string, unknown>
    limit?: number | null
  }) => post<RenderedViz>('/inspect', req),

  // --- query ---
  query: (spec: QuerySpec) => post<QueryResult>('/query', spec),
  sql: (sql: string, limit = 1000) => post<QueryResult>('/query/sql', { sql, limit }),

  // --- jobs ---
  jobs: (limit = 50) => request<Job[]>(`/jobs?limit=${limit}`),
  job: (id: string) => request<Job>(`/jobs/${id}`),
  cancelJob: (id: string) => post<{ cancel_requested: boolean }>(`/jobs/${id}/cancel`, {}),

  // --- dashboards ---
  dashboards: () => request<Dashboard[]>('/dashboards'),
  dashboard: (id: string) => request<Dashboard>(`/dashboards/${id}`),
  saveDashboard: (d: { id?: string; name: string; description?: string; panels: unknown[] }) =>
    post<{ id: string; name: string }>('/dashboards', d),
}
