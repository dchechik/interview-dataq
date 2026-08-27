# DataQ

Analysis of medium-to-large datasets (1–10 GB) for non-technical users, built as a
framework of pluggable components. Import a file, get its columns typed by
*meaning* rather than storage, then query, chart, aggregate and join it — with
every capability reachable from both the UI and an AI agent.

```bash
make setup          # install backend (uv) + frontend (npm) deps
make demo           # generate sample taxi + auth-log datasets
make dev            # api on :8000, web on :5173
```

Then open http://127.0.0.1:5173, click **Browse…** in the import box, pick
`sample-data/taxi.csv`, and follow the suggestions.

---

## Design

### One registry, many surfaces

Every plugin declares a single Pydantic `Params` model. That one model becomes the
request schema, the OpenAPI docs, the auto-rendered UI form, and the agent's tool
schema. There is no second place where plugin metadata is written down — adding a
backend plugin gives it a working UI with no frontend change.

### Kind ≠ mode

Plugins are **heterogeneous in contract, homogeneous in execution**. Each declares
two independent things:

- **`kind`** — what it consumes and produces, which fixes its Python interface.
- **`mode`** — how the runtime must execute it, which fixes its scheduling and the
  facilities it is handed.

| Mode | Runs as | Runtime provides |
|---|---|---|
| `pushdown` | one DuckDB statement | query construction, progress |
| `batch` | streamed Arrow batches | part-file checkpointing, resume, progress |
| `external` | async pool | result cache, retries, cost accounting, budget cap, row-level failure isolation |
| `inspect` | the request thread | read-only catalog access; never creates a job |

The stage a plugin belongs to does *not* determine how it runs. An extractor may be
a cheap regex (`pushdown`) or an LLM call (`external`); the runtime handles both and
nothing downstream can tell the difference.

### The six kinds

| Kind | Interface |
|---|---|
| `Reader` | URI → DuckDB relation |
| `Detector` | column stats → semantic-type guesses |
| `Transform` | DatasetVersion → DatasetVersion |
| `Aggregator` | dataset → query plan → new aggregate dataset |
| `Suggester` | catalog context → executable suggestions |
| `Visualizer` | dataset → `VizSpec` |

**Normalization, extraction and annotation are all `Transform`** — one kind, three
modes, selecting `sql()`, `process(batch)` or `async process_rows()`. The output
contract is identical in every case, so versioning, profiling, the UI and the agent
never learn which path ran.

`Aggregator` is separate only because it changes cardinality: it produces a new
dataset rather than a new version of an existing one.

### The semantic layer

A column's *meaning* (`net.ip`, `geo.lat`, `geo.country_iso2`) is tracked separately
from its storage type, in a hierarchy — a rule written for `categorical` applies to
`geo.country_iso2` automatically. This is what makes join and chart suggestion
automatic rather than hand-wired: two datasets are joinable when they share a
meaning, not merely a column name.

Detected types can be corrected in the UI. A human edit **pins** the column, freezing
it against future re-detection.

### Importing without typing a path

DuckDB reads data files **in place**, so the import box needs a path the *server*
can open — which a browser's file input cannot supply (it hands over contents, not a
location). **Browse…** therefore lists the server's filesystem, confined to
`DATAQ_BROWSE_ROOTS`; unset, that means your home and working directories, which is
right when the server is your own laptop. For a file that really is only on the
viewer's machine, the same dialog offers an upload, capped by `DATAQ_MAX_UPLOAD_MB`.
Browsing is the better path for multi-GB files: it avoids the copy entirely.

### Suggestions are executable

A `Suggestion` carries an `action` that is a literal API request body. The UI renders
it as a button; the agent invokes it directly. Nothing suggests in prose only.

### Storage: DuckDB as engine, not necessarily as format

DuckDB is the query engine everywhere. Two of its properties shape where bytes live:
it is **single-writer**, and a `.duckdb` file is **opaque**. So dataset versions go
through a `StorageBackend` protocol with two implementations:

| `DATAQ_STORAGE` | Layout | Trade-off |
|---|---|---|
| `parquet` *(default)* | immutable `part-*.parquet` per version | free checkpoints, resumable jobs, portable to S3 |
| `duckdb` | tables in one `warehouse.duckdb` | one file to host or copy; **no out-of-process workers** |

Both pass the same test suite, including resume-from-interruption equivalence.

The catalog stays in SQLite in *both* modes. It takes many small transactional writes
(job heartbeats, step status, column metadata) — exactly DuckDB's weak spot, and
co-locating it would make the write lock the bottleneck for the whole API.

### Jobs

Every data-producing call becomes a `Job` containing `Step`s, which together form a
replayable lineage DAG. `batch`/`external` jobs flush a part every N batches and
record a durable `rows_committed` watermark, so an interrupted run resumes to
byte-identical output. `JobRunner` is a protocol — the in-process thread pool can be
swapped for RQ/Celery/Temporal without touching a single plugin.

### LLM-backed plugins

A plugin backed by Claude is just `mode="external"` and implements only
`process_rows`. The runtime supplies an injected client, enforced concurrency and
batching, retries, structured output, per-step cost accounting with `max_cost_usd`,
prompt-cache-friendly layout, row-level failure isolation, and — most importantly —
a **persistent result cache** keyed on plugin version, params, model and the fields
the plugin declares. Re-running an extraction over a superset of rows only pays for
the new rows.

---

## API

```
GET  /api/plugins?kind=&mode=&applicable_to=   what can I do with this dataset?
POST /api/operations                           import | transform | aggregate | join -> 202 job
POST /api/inspect                              synchronous twin for viz/suggesters
GET  /api/jobs/{id}  /{id}/stream  /{id}/cancel
GET  /api/datasets/{id}/profile | versions | lineage | suggestions
POST /api/query      POST /api/query/sql
GET/POST /api/dashboards
```

`GET /api/plugins` tells you what may go in `plugin_id`; each descriptor's
`params_schema` tells you what goes in `params`. The raw-SQL path is gated by
DuckDB's own parser: exactly one statement, and it must be a `SELECT`.

---

## Layout

```
backend/src/dataq/
  core/        semantic type registry, profile + viz value objects
  plugins/     base (registry, descriptor), kinds, builtin/
  storage/     StorageBackend protocol + parquet and duckdb backends
  catalog/     SQLite models + repository
  query/       QuerySpec IR + compiler
  jobs/        runner, context, mode dispatcher, external facilities
  services/    profiler, operations, query, inspect, model client
  api/         FastAPI routes (thin: resolve a plugin, call a service)
frontend/src/
  api/         typed client + React Query hooks
  renderers/   registry keyed on VizSpec.renderer
  components/  SchemaForm (JSON Schema -> form), FileBrowser, JobProgress (SSE)
  pages/       Datasets, Dataset, Query, Explore, Dashboards
```

## Deployment

One container serves `/api` and the built SPA, with all state under one volume:

```bash
make docker && make docker-run     # http://localhost:8000
```

Config is entirely environment-driven — see `.env.example`. For a hosted deploy
(Railway, Fly), mount a volume at `/data`; set `DATAQ_STORAGE=duckdb` if you would
rather have a single file to back up than a directory of Parquet.

## Development

```bash
make test     # pytest, parameterised over both storage backends
make lint     # ruff + mypy, tsc + oxlint
make types    # regenerate frontend types from the live OpenAPI document
```

## The agent

The chat agent binds its tools to the **service layer**, not to HTTP, so it and the
API cannot drift apart — they are two front ends over the same functions. Tool calls
stream to the UI as they run, so the user watches the work rather than waiting on a
verdict.

Two permission scopes exist. `full` (the chat agent, driven by a human who can see
and cancel jobs) can create aggregates, joins and transforms. `read_only` — handed to
any agent-*backed plugin* — can query, profile and suggest but has no job-creating
tools at all, which is the recursion guard: a plugin cannot spawn unbounded work.

Set `ANTHROPIC_API_KEY` to enable it. Everything else runs without one.

## Status

Implemented and tested: import, profiling and semantic typing, transforms in all
three execution modes, the query layer, charts and maps, aggregates, joins, the
dashboard, and the agent.

The agent's tool surface and loop mechanics are covered by tests using a scripted
model client (tool dispatch, the `tool_result` round-trip, parallel calls, refusals,
the turn cap) — including the spec's cybersecurity workflow driven end to end through
agent tools. The loop has **not** been exercised against the live model; that needs an
API key.

Known gaps, in rough priority order:

- `dry_run` is defined on `OperationRequest` and documented as the way to preview an
  expensive extraction, but is not implemented yet. It matters most for `external`
  plugins, which is exactly where it is missing.
- The `external` transform ships one real plugin (`extract.entities`); it has been
  exercised against a fake client, not a live model.
- Job resume is implemented and tested at the storage layer, but nothing yet
  automatically restarts an interrupted job on startup — resume is a manual re-submit.
- The frontend has no test suite.
- Single-process only. Multi-worker deployment needs `DATAQ_STORAGE=parquet` and an
  out-of-process job runner.
