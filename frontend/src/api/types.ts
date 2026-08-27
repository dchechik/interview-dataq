/**
 * Types mirroring the backend contracts.
 *
 * `npm run gen:types` regenerates `schema.d.ts` from the live OpenAPI document;
 * these hand-written aliases cover the shapes the UI actually manipulates and
 * keep call sites readable.
 */

export type PluginKind =
  | 'reader' | 'detector' | 'transform' | 'aggregator' | 'suggester' | 'visualizer'
export type ExecMode = 'pushdown' | 'batch' | 'external' | 'inspect'
export type Renderer = 'vega-lite' | 'maplibre' | 'table'
export type ColumnRole = 'dimension' | 'measure' | 'time' | 'key' | 'geo' | 'ignore'
export type JobStatus = 'queued' | 'running' | 'paused' | 'succeeded' | 'failed' | 'cancelled'

export interface JsonSchema {
  type?: string
  title?: string
  description?: string
  properties?: Record<string, JsonSchema>
  required?: string[]
  items?: JsonSchema
  enum?: unknown[]
  anyOf?: JsonSchema[]
  default?: unknown
  minimum?: number
  maximum?: number
  $defs?: Record<string, JsonSchema>
  $ref?: string
}

export interface PluginDescriptor {
  id: string
  kind: PluginKind
  mode: ExecMode
  version: string
  title: string
  summary: string
  cost_class: 'free' | 'cheap' | 'expensive'
  params_schema: JsonSchema
  accepts: { semantic_types: string[]; dataset_kinds: string[]; min_rows: number }
  produces: { semantic_types: string[]; dataset_kind: string | null; description: string }
}

export interface DatasetSummary {
  id: string
  name: string
  kind: string
  description: string
  source_uri: string
  latest_version: number
  row_count: number
  created_at: string
}

export interface ColumnStats {
  name: string
  physical_type: string
  row_count: number
  null_count: number
  distinct_count: number
  min: unknown
  max: unknown
  sample_values: unknown[]
  top_values: [unknown, number][]
}

export interface SemanticGuess {
  semantic_type: string
  confidence: number
  rationale: string
  detector_id: string
}

export interface ColumnProfile {
  name: string
  physical_type: string
  semantic_type: string | null
  confidence: number
  role: ColumnRole
  pinned: boolean
  stats: ColumnStats | null
  candidates: SemanticGuess[]
}

export interface DatasetProfile {
  dataset_id: string
  version: number
  row_count: number
  columns: ColumnProfile[]
}

export interface Suggestion {
  title: string
  rationale: string
  kind: string
  score: number
  action: Record<string, unknown>
}

export interface Filter {
  column: string
  op: string
  value?: unknown
}

export interface Select {
  column: string
  agg?: string | null
  alias?: string | null
}

export interface QuerySpec {
  dataset: string
  version?: number | null
  filters?: Filter[]
  time_bucket?: { column: string; interval: string; alias?: string } | null
  select?: Select[]
  group_by?: string[]
  order_by?: { column: string; desc: boolean }[]
  limit?: number
  offset?: number
}

export interface QueryResult {
  columns: string[]
  types: string[]
  rows: unknown[][]
  row_count: number
  truncated: boolean
  sql: string
  elapsed_ms: number
}

export interface VizSpec {
  renderer: Renderer
  title: string
  query: QuerySpec
  spec: Record<string, unknown>
  animate?: { field: string; label: string; fps: number } | null
  description: string
}

export interface RenderedViz {
  spec: VizSpec
  data: Record<string, unknown>[]
  row_count: number
}

export interface JobProgress {
  rows_done?: number
  rows_total?: number
  pct?: number | null
  rows_per_s?: number
  eta_s?: number | null
  cost?: { calls: number; tokens_in: number; tokens_out: number; usd: number; cache_hits: number }
}

export interface JobStep {
  id: string
  op: string
  plugin_id: string
  status: string
  rows_committed: number
  parts_committed: number
  cost: Record<string, number>
  outputs: { dataset_id: string; version: number }[]
  error: string
}

export interface Job {
  id: string
  title: string
  status: JobStatus
  progress: JobProgress
  logs: string[]
  error: string
  created_at: string
  finished_at: string | null
  steps?: JobStep[]
}

export interface OperationRequest {
  op: 'import' | 'transform' | 'aggregate' | 'join'
  plugin_id?: string
  inputs?: { dataset_id: string; version?: number | null }[]
  params?: Record<string, unknown>
  uri?: string
  name?: string
  output_name?: string
  dry_run?: boolean
  max_cost_usd?: number | null
}

export interface OperationAccepted {
  job_id: string
  step_id: string
  dataset_id: string
}

export interface Dashboard {
  id: string
  name: string
  description: string
  panels: VizSpec[]
  updated_at: string
}

export interface LineageStep {
  id: string
  op: string
  plugin_id: string
  params: Record<string, unknown>
  inputs: unknown[]
  outputs: { dataset_id: string; version: number }[]
  status: string
  rows: number
  cost: Record<string, number>
  created_at: string
}

export const TERMINAL_STATUSES: JobStatus[] = ['succeeded', 'failed', 'cancelled']
