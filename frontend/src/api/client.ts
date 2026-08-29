import type {
  AgentEstimate,
  BrowseResult,
  Dashboard,
  DatasetNode,
  DatasetProfile,
  DatasetSummary,
  DeleteResult,
  Job,
  LineageStep,
  OperationAccepted,
  OperationRequest,
  PluginDescriptor,
  QueryResult,
  QuerySpec,
  Related,
  RenderedViz,
  Suggestion,
  UploadResult,
} from './types'

/** Same-origin in production; Vite proxies /api to uvicorn in dev. */
const BASE = '/api'

/**
 * Shared-token auth for hosted instances.
 *
 * A local instance sets no token and none of this engages. A deployed one
 * returns 401 until a token is supplied; we prompt once, keep it per-browser,
 * and attach it to every request. Kept in localStorage rather than a cookie
 * because the token also has to travel as a query parameter for EventSource,
 * which cannot set headers.
 */
const TOKEN_KEY = 'dataq.token'

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY)
  } catch {
    return null // private browsing, or storage disabled
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* nothing we can do; the header just will not persist */
  }
}

/** Append the token to a URL, for EventSource and other header-less clients. */
export function withToken(url: string): string {
  const token = getToken()
  if (!token) return url
  return `${url}${url.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}`
}

let promptInFlight = false

/** Ask once for the token, then reload so every query refetches with it. */
function promptForToken(): void {
  if (promptInFlight) return
  promptInFlight = true
  const entered = window.prompt(
    'This DataQ instance requires an access token.\n\n' +
      'It is the DATAQ_AUTH_TOKEN set on the server.',
  )
  promptInFlight = false
  if (entered) {
    setToken(entered.trim())
    window.location.reload()
  }
}

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
  const token = getToken()
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      ...(isForm ? {} : { 'Content-Type': 'application/json' }),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init?.headers ?? {}),
    },
  })
  if (res.status === 401) {
    // Either no token yet, or the stored one is stale.
    setToken(null)
    promptForToken()
    throw new ApiError('authentication required', 401)
  }
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
  datasetTree: () => request<DatasetNode[]>('/datasets/tree'),
  related: (id: string) => request<Related>(`/datasets/${id}/related`),
  dataset: (id: string) => request<DatasetSummary>(`/datasets/${id}`),
  deleteDataset: (id: string, cascade = false) =>
    request<DeleteResult>(`/datasets/${id}${cascade ? '?cascade=true' : ''}`, {
      method: 'DELETE',
    }),
  dependents: (id: string) =>
    request<{ id: string; name: string; kind: string }[]>(`/datasets/${id}/dependents`),
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

  // --- agent ---
  agentEstimate: (message: string, history: unknown[] = []) =>
    post<AgentEstimate>('/agent/estimate', { message, history }),

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
