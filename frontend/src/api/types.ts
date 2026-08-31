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
export type Renderer = 'vega-lite' | 'maplibre' | 'table' | 'timeline'
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
  /** A plugin's hint about the widget to use; currently only 'textarea'. */
  format?: string
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

export interface DatasetNode extends DatasetSummary {
  /** How this dataset was produced; null for an imported source. */
  derived_via: { op: string; plugin_id: string } | null
  /** For a join, the parents it does *not* nest under. */
  joined_with: { id: string; name: string }[]
  descendants: number
  children: DatasetNode[]
}

export interface RelatedDataset {
  id: string
  name: string
  kind: string
  op: string
  plugin_id: string
  role: string
  row_count?: number
}

/** One materialised version of a dataset. */
export interface DatasetVersion {
  version: number
  row_count: number
  created_at: string
  produced_by_step: string
  columns: number
  bytes: number
  /** What every query, chart and dashboard reads. Decided by the backend, which
      owns the rule about which versions may be reverted to or deleted. */
  is_current: boolean
}

/** What a delete actually removed. */
export interface DeleteResult {
  deleted: string[]
  datasets: { id: string; name: string; kind: string }[]
  versions: number
  bytes_freed: number
}

export interface Related {
  parents: RelatedDataset[]
  children: RelatedDataset[]
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

export interface FormatCandidate {
  format: string
  label: string
  success_rate: number
  example_input: string
  example_output: string
  /** Set when another format fits equally well and disagrees about the date. */
  conflict: string | null
}

/** A meaning a column can carry. Built in, or defined by someone here. */
export interface SemanticType {
  id: string
  title: string
  parent: string | null
  role: string
  joinable: boolean
  description: string
  /** False for the types plugins are written against; those cannot be edited. */
  custom: boolean
  /** How many columns carry it. Only counted for custom types. */
  in_use: number
}

export interface SemanticGuess {
  semantic_type: string
  confidence: number
  rationale: string
  detector_id: string
  /** Populated only for temporal columns: how to read them, best first. */
  formats: FormatCandidate[]
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
  /** A choice the importer had to make that the data could not settle. */
  warning: string | null
}

export interface DatasetProfile {
  dataset_id: string
  version: number
  row_count: number
  columns: ColumnProfile[]
}

export type TargetType =
  | 'VARCHAR' | 'BIGINT' | 'DOUBLE' | 'BOOLEAN' | 'DATE' | 'TIMESTAMP'

/** What to do with one column at import — the editable part of a proposal. */
export interface ColumnPlan {
  name: string
  /** null means "leave it as the reader produced it". */
  target_type?: TargetType | null
  /** strptime format, or epoch:s / epoch:ms / epoch:us. */
  format?: string | null
  semantic_type?: string | null
  role?: ColumnRole | null
  /** Set when a person changed this from what was proposed. */
  pinned?: boolean
}

export interface ColumnProposal {
  name: string
  /** What the reader would produce, untouched. */
  source_type: string
  proposed: ColumnPlan
  sample_values: unknown[]
  rationale: string
  formats: FormatCandidate[]
  /** Share of sampled values the proposed cast would keep. */
  parse_rate: number | null
  /** The data cannot settle this; a person must. */
  decision_required: boolean
  conflict: string | null
}

export interface ImportPlan {
  reader: string
  sampled_rows: number
  columns: ColumnProposal[]
  rows: unknown[][]
}

export interface ActorChoice {
  column: string
  distinct: number
  reason: string
}

export interface ProposedFeature {
  expression: string
  explains: string
}

/** A draft feature set, and the choices behind it. */
export interface FeatureProposal {
  actor: string | null
  actor_options: ActorChoice[]
  time_column: string | null
  window: string
  features: ProposedFeature[]
  /** Sorts this set costs — the number that predicts the wait. */
  distinct_windows: number
  /** Why there is nothing to propose, when there is nothing. */
  blocked: string | null
}

export interface BrowseEntry {
  name: string
  path: string
  is_dir: boolean
  size: number | null
  importable: boolean
  reader_id: string | null
}

export interface BrowseResult {
  path: string
  parent: string | null
  roots: { path: string; name: string }[]
  entries: BrowseEntry[]
  truncated: boolean
}

export interface UploadResult {
  uri: string
  name: string
  bytes: number
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

export type Mark =
  | 'bar' | 'line' | 'area' | 'point' | 'tick' | 'rect' | 'arc' | 'boxplot'
export type Channel =
  | 'x' | 'y' | 'color' | 'size' | 'shape' | 'opacity' | 'theta'
  | 'detail' | 'row' | 'column' | 'tooltip'
export type EncodingType = 'quantitative' | 'nominal' | 'ordinal' | 'temporal'

export interface Encoding {
  field: string
  type?: EncodingType | null
  aggregate?: string | null
  bin?: boolean | number | null
  sort?: string | null
  title?: string | null
  stack?: boolean | null
  /** Why the backend chose this type — shown in the inspector. */
  inferred_from?: string | null
}

/** The typed grammar. Resolved server-side, compiled to Vega-Lite client-side. */
export interface ChartSpec {
  mark: Mark
  encodings: Partial<Record<Channel, Encoding>>
  layers?: ChartSpec[]
  raw_vega_lite?: Record<string, unknown>
  description?: string
}

export type AbnormalityOp = '<' | '<=' | '>' | '>=' | '==' | '!='

export interface AbnormalityRule {
  column: string
  op: AbnormalityOp
  value: number
  label: string
  /** Why this rule was proposed, so a highlight never looks arbitrary. */
  rationale?: string
}

export interface EventAttribute {
  column: string
  label?: string | null
  /** Emphasised attributes read as part of the event; the rest are detail. */
  highlight: boolean
  /** Offer "show only this value" — set for columns naming a subject. */
  filterable: boolean
}

/** How to render a list of timed events. */
export interface TimelineSpec {
  time_column: string
  title_column?: string | null
  attributes: EventAttribute[]
  abnormality?: AbnormalityRule | null
  descending: boolean
  group_by_day: boolean
  description?: string
}

export interface VizSpec {
  renderer: Renderer
  title: string
  query: QuerySpec
  /** Preferred when present; `spec` is the pre-ChartSpec fallback. */
  chart?: ChartSpec | null
  /** Presentation for the timeline renderer. */
  timeline?: TimelineSpec | null
  spec: Record<string, unknown>
  animate?: { field: string; label: string; fps: number } | null
  description: string
}

export interface RenderedViz {
  spec: VizSpec
  data: Record<string, unknown>[]
  row_count: number
  /** The SQL actually executed, for the chart's debug view. */
  sql: string
  elapsed_ms: number
  truncated: boolean
}

/** One tool the agent may call. The description is what the model was told it does. */
export interface AgentTool {
  name: string
  description: string
  scope: 'read_only' | 'full'
}

export interface AgentEstimate {
  input_tokens: number
  /** True when counted by the API rather than approximated locally. */
  exact: boolean
  model: string
  tools: number
  max_turns: number
  first_request_usd: number
  worst_case_usd: number
  has_api_key: boolean
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

/** A column pair that could be part of a join key, and why it could. */
export interface JoinKeyCandidate {
  left: string
  right: string
  semantic_type: string
  reason: string
}

/** A dataset worth joining to, with the pairs that make it joinable. */
export interface JoinCandidate {
  dataset_id: string
  name: string
  kind: string
  row_count: number
  keys: JoinKeyCandidate[]
}

/**
 * What a join would do, measured before it runs. `result_rows` is exact when
 * the right side is unique on the key and an estimate otherwise — which is
 * exactly when it is worth reading.
 */
export interface JoinPreview {
  left_rows: number
  right_rows: number
  sampled: number
  matched: number
  duplicate_keys: number
  result_rows: number
  exact: boolean
  columns_added: string[]
  collisions: string[]
  fanout: boolean
  notes: string[]
}

export interface JoinPreviewRequest {
  right_dataset_id: string
  params: Record<string, unknown>
  left_version?: number | null
  right_version?: number | null
}

export interface OperationRequest {
  op: 'import' | 'transform' | 'aggregate' | 'join'
  plugin_id?: string
  inputs?: { dataset_id: string; version?: number | null }[]
  params?: Record<string, unknown>
  /** For op:'aggregate' — materialise this query instead of running a plugin. */
  from_query?: QuerySpec
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
