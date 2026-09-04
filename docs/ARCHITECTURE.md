# DataQ Architecture

DataQ analyses medium-to-large datasets (1–10 GB) for non-technical users. A file is
imported, its columns are typed by *meaning* rather than storage, and everything after
that — query, chart, aggregate, join, enrich — is a plugin invocation reachable
identically from the web UI and from an LLM agent.

This document describes the shape of the system: where bytes live, what a dataset *is*,
what happens between "pick a file" and "queryable dataset", and how plugins sit on top
of all of it.

---

## 1. System shape

```
                    ┌──────────────────────────────────────────┐
   Browser  ───────▶│  FastAPI  (api/app.py)                   │
   (React SPA)      │  flat route table, no business logic     │
                    └────────────────┬─────────────────────────┘
                                     │
   Agent (Claude) ───────────────────┤   both front ends call the same functions
                                     ▼
                    ┌──────────────────────────────────────────┐
                    │  Services  (services/*.py)               │
                    │  operations · inspect · query · lineage  │
                    │  import_plan · join_plan · datasets      │
                    └───┬───────────────┬──────────────┬───────┘
                        │               │              │
              ┌─────────▼──────┐ ┌──────▼───────┐ ┌────▼──────────────┐
              │ Plugin registry│ │ Job runtime  │ │ Query compiler    │
              │ plugins/       │ │ jobs/        │ │ query/            │
              │ 6 kinds        │ │ 4 exec modes │ │ QuerySpec -> SQL  │
              └────────┬───────┘ └──────┬───────┘ └────┬──────────────┘
                       └────────────────┼──────────────┘
                                        ▼
              ┌───────────────┐  ┌──────────────┐  ┌──────────────────┐
              │ Catalog       │  │ Warehouse    │  │ Storage backend  │
              │ SQLite        │  │ DuckDB       │  │ parquet | duckdb │
              │ (metadata)    │  │ (engine)     │  │ (dataset bytes)  │
              └───────────────┘  └──────────────┘  └──────────────────┘
```

Three properties define the architecture:

1. **One registry, many surfaces.** A plugin declares a single Pydantic `Params` model.
   That model becomes the HTTP request schema, the OpenAPI docs, the auto-rendered UI
   form (`frontend/src/components/SchemaForm.tsx`) and the agent's tool schema. Adding a
   backend plugin gives it a working UI with no frontend change.
2. **HTTP is a thin skin.** `api/app.py` resolves a plugin and calls a service that takes
   an `AppContext`. The agent binds its tools to the same service functions
   (`services/agent.py`), so the two front ends cannot drift apart.
3. **Kind ≠ mode.** What a plugin *is* (its Python interface) is independent of how the
   runtime must *run* it (its scheduling and facilities). See §6.

### Module map

| Path | Responsibility |
|---|---|
| `backend/src/dataq/core/` | Value objects with no I/O: semantic-type registry, profiles, `ChartSpec`, `VizSpec`, `TimelineSpec`, feature IR, date-format inference |
| `backend/src/dataq/catalog/` | SQLModel tables + `Catalog` repository (the only code that touches SQLite) |
| `backend/src/dataq/storage/` | `StorageBackend` protocol and its two implementations |
| `backend/src/dataq/db.py` | DuckDB connection/cursor management, read-only SQL gate |
| `backend/src/dataq/query/` | `QuerySpec` IR and its compiler to parameterised DuckDB SQL |
| `backend/src/dataq/plugins/` | Plugin base classes, the six kind ABCs, the registry, and `builtin/` |
| `backend/src/dataq/jobs/` | Job context, mode dispatcher, external-mode facilities, runner protocol |
| `backend/src/dataq/services/` | The business logic every front end calls |
| `backend/src/dataq/api/` | FastAPI routes, auth middleware, user accounts |
| `frontend/src/` | React SPA: pages, a renderer registry mirroring the plugin registry, typed API client |

---

## 2. Core concepts

Eight nouns carry the whole system.

**Dataset** — a named thing in the catalog with a `kind` of `source`, `derived`,
`aggregate` or `join`. It owns a history of versions; it does not own bytes.

**Version** — an immutable materialisation. Owns a `StoredRef` (where its bytes are), a
row count, an ordered column schema, and the id of the step that produced it. Version
numbers are handed out by `next_version` and **never reused**.

**Column** — per-version metadata: physical type, semantic type, confidence, role,
`pinned`, sampled stats, rejected candidates, and an optional import warning. This is
the semantic layer that drives suggestion.

**Semantic type** — what a column *means* (`net.ip`, `geo.lat`, `time.timestamp`) as
opposed to how it is stored. Hierarchical, so a rule written for `categorical` applies
to `geo.country_iso2` automatically. Built-ins live in code; installation-specific ones
(`machine.name`, `cost_centre`) live in the catalog.

**Job / Step** — every data-producing call becomes a `Job` containing `Step`s. A step
records op, plugin id and version, params, input and output dataset versions, a durable
checkpoint watermark, and external-mode cost. Collectively the steps form a replayable
lineage DAG.

**QuerySpec** — the structured query IR (`query/spec.py`). Four producers: the UI filter
builder, `Aggregator` plugins, `VizSpec`s, and the agent. One compiler.

**VizSpec** — the backend/frontend visualisation contract (`core/viz.py`). Names a
`renderer` string; the frontend has its own registry keyed on that field. The backend
never renders.

**Suggestion** — a proposed next step whose `action` is a literal API request body. The
UI renders it as a button; the agent invokes it directly. Nothing suggests in prose only.

---

## 3. Storage strategy

### 3.1 Three stores, on purpose

DuckDB is the query **engine** everywhere. It is not necessarily the storage **format**,
and it is deliberately not the metadata store.

| Store | Technology | Holds | Why separate |
|---|---|---|---|
| **Catalog** | SQLite (via SQLModel) | datasets, versions, columns, jobs, steps, dashboards, custom semantic types | Many small transactional writes — job heartbeats, step status, progress — which is exactly DuckDB's weak spot. Co-locating would make DuckDB's single write lock the bottleneck for the whole API. |
| **Warehouse** | one `warehouse.duckdb` | the query engine's own state; the external-plugin result cache; in `duckdb` storage mode, the dataset tables too | DuckDB streams a 10 GB file itself rather than round-tripping through Python |
| **Lake** | Parquet parts, or DuckDB tables | dataset version bytes | Immutable, checkpointable, portable |

The catalog stays in SQLite in **both** storage modes.

### 3.2 The `StorageBackend` protocol

Two DuckDB properties force dataset bytes behind an abstraction (`storage/base.py`):

- it is **single-writer** — one process may hold a `.duckdb` file for writing, so any
  out-of-process worker deadlocks;
- a `.duckdb` file is **opaque** — you cannot hand one version to another tool, and
  object storage is not a natural fit.

```python
class StorageBackend(abc.ABC):
    def write_relation(ref, rel_sql, conn, params) -> StoredRef   # one-shot (pushdown)
    def open_writer(ref, schema, conn) -> PartWriter              # part-wise (batch/external)
    def sql_source(stored) -> str                                 # a FROM-clause fragment
    def drop(stored, conn)                                        # free one version
    def drop_dataset(dataset_id, conn)                            # free everything, listed or not
```

| `DATAQ_STORAGE` | Layout | Trade-off |
|---|---|---|
| `parquet` *(default)* | `data/lake/<dataset_id>/v<n>/part-NNNNN.parquet` | free checkpoints, resumable jobs, `base_uri` can become `s3://` with httpfs |
| `duckdb` | tables `ds_<dataset_id>__v<n>` in `warehouse.duckdb` | one file to host or `scp`; **no out-of-process workers** |

Both pass the same test suite, including resume-from-interruption equivalence.

`drop_dataset` exists separately from looping over `drop` because deleting version by
version trusts the catalog to know what exists, and it does not always: a run that failed
between writing files and recording the version leaves data nothing points at.

### 3.3 Immutability and the part writer

A version's bytes are written once and never mutated. That is what makes checkpointing
free: a `PartWriter` writes numbered parts, and the step row records a durable
`(parts_committed, rows_committed)` watermark after each flush.

```
batch loop ──▶ write_part(n, buffer) ──▶ ctx.checkpoint(n+1, rows)   [SQLite commit]
                                              │
  crash / restart ────────────────────────────┘
       resume:  scan source with OFFSET rows_committed
                writer.discard_from(parts_committed)
```

Both backends make the discard safe:

- **Parquet** writes to `part-NNNNN.parquet.tmp` and renames, so a crash mid-write never
  leaves a torn part that resume would mistake for committed. `abort()` removes only
  `.tmp` files.
- **DuckDB** appends into a staging table tagged with `_dq_part`, so `discard_from` is a
  `DELETE` and `finalize` promotes the staging table (ordered by part, to match Parquet's
  row order). `abort()` deliberately does **nothing** — each `write_part` is a single
  `INSERT`, and dropping the staging table would destroy the checkpoints resume needs.

Resume relies on `SET preserve_insertion_order=true` plus the row watermark, so the
resumed run sees exactly the rows the interrupted one had not committed, and produces
byte-identical output.

### 3.4 Connection management

`db.py` opens **one DuckDB instance per process**. Each unit of work takes a `cursor()` —
an independent, thread-safe execution context sharing the catalog and buffer pool.

Two rules the code depends on:

- A DuckDB connection holds **one active result at a time**, so the streaming executor
  gives its Arrow scan its own cursor. Writes issued on the same connection while
  streaming would silently truncate the reader (`jobs/executor.py`).
- `Warehouse.ddl_lock` serialises `CREATE`/`DROP TABLE` so two jobs cannot race.

The raw-SQL escape hatch (`POST /api/query/sql`) is gated by `assert_read_only`, which
parses with DuckDB's own parser and requires exactly one statement of type `SELECT` —
so comment tricks and stacked statements cannot slip through. It prevents writes, not
filesystem reads; a multi-tenant deployment should additionally run that path with
`enable_external_access=false`.

### 3.5 On-disk layout

```
data/                       # DATAQ_DATA_DIR — mount one volume
├── catalog.sqlite          # all metadata
├── warehouse.duckdb        # engine state + external result cache (+ tables in duckdb mode)
├── session_secret          # signs session tokens, so restarts don't sign everyone out
├── uploads/                # files sent from a browser
└── lake/
    └── <dataset_id>/
        ├── v1/part-00000.parquet
        └── v2/part-00000.parquet
```

### 3.6 Version lifecycle rules

Three storage properties dictate the semantics of revert and delete:

- version numbers are never reused, and every step records the number it wrote;
- a version **owns** its bytes — deleting one drops them, so two version rows cannot
  share a `StoredRef`;
- history is append-only.

Therefore:

| Operation | Behaviour | Reason |
|---|---|---|
| **Revert** | copies the old version forward as a *new* version, as a job | "Revert as a new commit." Moving `latest_version` backwards would point the next write at a number some step's provenance already claims. Column metadata is *copied*, not recomputed — the bytes are identical, and re-profiling would lose pins and import warnings, which cannot be recomputed at all. |
| **Delete version** | frees the bytes for one version; refuses the only version and the current one | A dataset with no data is a ghost; the current version is what every query resolves to and is the high-water mark. Restore-then-delete reaches every state. |
| **Delete dataset** | frees disk via `drop_dataset`, refuses to strand children (409 + `?cascade=true`), refuses to race a live job | The catalog only knows about rows; deleting metadata and leaving bytes is how a lake grows forever while the dataset list says nothing is there. |

A *failed* operation leaves the catalog as it found it: `_new_dataset` in
`services/operations.py` is a context manager that deletes the dataset row if anything
between creating it and filling it raises.

---

## 4. How datasets are defined

### 4.1 The catalog tables

`catalog/models.py`:

```
DatasetRow      id, name, kind, description, source_uri, view_sql, latest_version
   └── VersionRow      dataset_id, version, row_count, stored_ref, columns_schema,
        │              produced_by_step
        └── ColumnRow  position, name, physical_type, semantic_type, confidence,
                       role, pinned, stats, candidates, warning

JobRow          status, progress, logs, error, cancel_requested
   └── StepRow  op, plugin_id, plugin_version, params, inputs, outputs,
                parts_committed, rows_committed, cost, status, error

SemanticTypeRow id, title, parent, role, joinable, description
DashboardRow    name, panels (a list of VizSpecs — the recipe, not a snapshot)
```

`view_sql` on `DatasetRow` backs an *unmaterialised* join or aggregate. In practice joins
and aggregates are materialised at creation, but the field keeps a view-backed dataset
expressible.

### 4.2 Resolution: dataset id → something SQL can read

Nothing above the storage layer knows about paths or table names. `AppContext.resolve_source`
(`services/context.py`) is the single choke point:

```python
resolve_source(dataset_id, version) -> ResolvedSource(sql=..., columns={name: physical_type})
```

It prefers the materialised version's `StoredRef` via `storage.sql_source(...)`, falls
back to the dataset's `view_sql`, and raises otherwise. The query compiler, the transform
runtime's `JoinPlan` resolver, and the join op all go through it — which is why a plugin
can name a dataset id and get back something joinable without ever seeing a filesystem.

### 4.3 The semantic layer

`core/semantic.py` holds a registry of `SemanticType(id, title, parent, role, joinable,
physical)`. Roots are `numeric`, `text`, `temporal`, `boolean`; `categorical` descends
from `text`; then families like `geo.*`, `time.*`, `net.*`, `identity.*`, `money.*`, and
`numeric.share`/`numeric.rarity`.

Dotted ids are a *naming convention*, not a structure — `geo.lat`'s parent is `numeric`,
not `geo`. Matching is by ancestry:

```python
SEMANTIC_TYPES.matches_any("geo.country_iso2", ("categorical",))  # True
```

This is what makes join and chart suggestion automatic rather than hand-wired: two
datasets are joinable when they share a *meaning*, not merely a column name.

Three facts about a column are tracked separately and deliberately:

- **physical type** — settled by the reader at import; immutable for the life of the
  version;
- **semantic type** — what it means; derived later, editable forever;
- **role** (`dimension | measure | time | key | geo | ignore`) — what it can *do as
  stored*. A `VARCHAR` holding `03/07/2011 08:07:29` *means* `time.timestamp` but is a
  `dimension` until parsed, because subtracting an interval from text is a type error.
  Without that split, every time-based plugin picked such a column up and failed inside
  DuckDB with `No function matches -(VARCHAR, INTERVAL)`.

**Pinning.** A human edit to a column's type sets `pinned=True`, which freezes it against
re-detection on every subsequent version. Accepting a proposed value is *not* an
override; only actual changes pin (`_planned_profiles`), or the marker would mean nothing.

**Custom types.** No detector will ever recognise a fleet's machine names, and a column
with no meaning satisfies no plugin's `Accepts` gate and joins to nothing. So people can
define types, persisted in `SemanticTypeRow` and loaded into the process-wide registry at
context build (`services/semantic_types.py`). They are ordinary members of the hierarchy —
they name a parent, inherit matching, and are indistinguishable to every consumer. Only
two things separate them: they can be edited and deleted, and no detector produces them.
`seal_builtins()` draws the line.

### 4.4 The derivation tree

`services/lineage.py` turns the DAG in `Step.inputs`/`Step.outputs` into the tree the UI
nests, using two rules:

- **A transform is not an edge.** It produces a new *version* of the dataset it was given,
  so it belongs to that dataset's history, not to its offspring.
- **A join has two parents but a node has one.** It nests under its left input and names
  the other parent inline, so the second edge stays visible.

A dataset whose parent was deleted surfaces as a root rather than disappearing.

---

## 5. The import and processing pipeline

### 5.1 Why import is a two-phase operation

A column's physical type is settled by the reader the moment the file is read, is copied
into every record, and can never be changed afterwards — storage is immutable per version,
and the only escape is a transform that adds a second column beside it. Meaning and role
are worked out later and stay editable forever.

So the one decision that cannot be revisited was the one nobody was shown. Import
therefore proposes a plan first.

```
 POST /api/sources/preview     first N rows, as the reader would read them
 POST /api/sources/plan        ColumnPlan[] — for each column: what the reader found,
        │                      what it will be stored as, how a date will be read,
        │                      its meaning, its role, and any ambiguity to resolve
        ▼
 POST /api/operations {op: "import", uri, params: {..., columns: ColumnPlan[]}}
```

The proposal is built by running the **real profiler** over a 2,000-row sample
(`services/import_plan.py`), not by reimplementing it — which is the property that makes
the preview worth trusting: what it shows is what the import will do, because it is the
same code.

### 5.2 What `run_import` does

`services/operations.py::run_import`:

1. **Confine the URI.** `assert_readable_uri` — an import names a path the *server* will
   open, so it must be one the server was told it may open (`DATAQ_BROWSE_ROOTS`).
2. **Pick a reader** — explicit `plugin_id`, or `pick_reader(uri)` by extension.
3. **Read to a relation.** `Reader.to_relation` returns a DuckDB relation, not Arrow, so
   DuckDB streams the file itself.
4. **Hold ambiguous columns as text.** A column whose date reading is the user's to choose
   is re-read with `column_types={col: 'VARCHAR'}`, because once the sniffer has turned
   `03/04/2016` into a DATE, which of March or April it picked is unrecoverable.
5. **Measure cast losses** before writing (`_cast_losses`) — counting the values a planned
   cast turns into NULL, per column.
6. **Materialise through a projection**, not through reader types. `types={'date':'TIMESTAMP'}`
   aborts the whole import on the first unparseable row; `ignore_errors` answers that by
   silently dropping rows. A projection cannot drop anything, so a value that will not
   convert becomes NULL and the count is reported against the column.
7. **Re-derive the schema from what was written**, not from what was read — after a cast
   the physical type is the whole point.
8. **Record the version**, then profile.
9. **Attach warnings**: sniffer date ambiguities (`_sniffer_warnings`, only possible for
   readers that can hand back unconverted text via `all_varchar`), cast losses, and
   rejected rows.

### 5.3 Profiling and detection

`services/profiler.py`:

```
compute_stats(conn, source_sql, columns, sample_rows)
    ├── one aggregate pass over the WHOLE table: count, approx_count_distinct, min, max
    └── sampled passes for sample_values and top_values
                  │
                  ▼
run_detectors(stats)  →  every Detector plugin votes, sorted by confidence
                  │
                  ▼
profile_columns(stats, previous)  →  ColumnProfile per column
    · highest-confidence guess above CONFIDENCE_THRESHOLD (0.5) wins
    · lower-confidence guesses are kept as `candidates`
    · a `pinned` column from `previous` keeps its type, unchallenged
```

Counts come from the whole table rather than the sample because sampled distinct counts
are capped at the sample size, so any column with more distinct values than that looks
like a per-row identifier — and "roughly as many distinct values as rows" is exactly the
test used to tell a user id from an event id. `approx_count_distinct` is HyperLogLog, so
one pass over 11.4M rows × 19 columns costs ~0.3s.

Profiling is bounded, so it is roughly constant-time whether the dataset is 10 MB or 10 GB.

### 5.4 Date formats

`core/datefmt.py` tries ~35 formats against sampled values and ranks them by how many
parse, so `normalize.timestamp` needs no format argument. What it refuses to do is guess
when guessing is unsafe:

- A **text** column that reads both day-first and month-first is reported ambiguous, and
  parsing fails naming both options. Any day past the 12th in the sample resolves it.
- When a 2,000-row prefix cannot choose — a time-sorted export's first rows can span one
  day, where every day is ≤ 12 — the file is asked how often each reading actually fails
  over `SETTLE_ROWS` (200,000), which costs ~109ms.
- A column DuckDB **already converted** is worse: the sniffer picked a reading and did not
  record which. So the raw file is re-read at import and a warning names both readings,
  the one taken, and the `timestampformat` that pins the other.
- Numeric columns named like times are checked against epoch ranges, so `event_time` as a
  BIGINT is recognised rather than averaged.

### 5.5 Malformed rows

DuckDB stops on the first row that does not match the settled columns and answers with a
list of flags. The import panel exposes the three outcomes those flags actually produce:

| When a row does not match | What happens | Flags |
|---|---|---|
| **Stop the import** (default) | nothing is imported | — |
| **Keep the row** | extra values dropped, missing ones empty; no row lost | `strict_mode=false`, `null_padding=true` |
| **Skip the row** | the row is not imported, **and is counted** | `ignore_errors=true` + `store_rejects=true` |

"Keep" sets both flags because they cover different halves: `strict_mode` admits a row
with too many values and still stops on one with too few; `null_padding` does the reverse.

Skipping is never silent — `readers.rejected_rows` counts by distinct file and line
(DuckDB records one reject per bad *column*) and the record is capped, so a wholly
malformed file cannot fill memory with a description of itself. The failure itself is
re-raised as a sentence by `explain_read_error`, not as DuckDB's settings dump.

### 5.6 The five operations

`POST /api/operations` is the single request shape for every data-producing call, and
returns `202` with a job id. `submit_operation` creates the `Job` and `Step` rows and
hands the work to the runner.

| `op` | Input → output | Notes |
|---|---|---|
| `import` | URI → new source dataset @ v1 | §5.2 |
| `transform` | dataset v*n* → same dataset v*n+1* | normalize / extract / annotate; mode decides how |
| `aggregate` | dataset → **new** aggregate dataset | changes cardinality, so it cannot be a new version. Also accepts `from_query` — materialising a chart's own `QuerySpec` |
| `join` | two datasets → **new** join dataset | composite key, fan-out refused unless `allow_fanout` |
| `revert` | dataset v*k* → same dataset v*n+1* | copies bytes forward; metadata copied, not recomputed |

### 5.7 Job lifecycle

```
submit_operation ──▶ JobRow(queued) + StepRow(queued)
                          │
     ThreadPoolJobRunner ─┴─▶ JobRow(running), StepRow(running)
                                    │
                          JobCtx: log() · progress() · check_cancelled() · checkpoint()
                                    │
              ┌─────────────────────┼──────────────────────┐
        succeeded              cancelled                failed
                          (JobCancelled)          (message on the job row)
                                    │
                          BudgetExceeded → succeeded + "stopped early"
                          (partial results are committed on purpose)
```

`JobRunner` is a `Protocol`; the v0 `ThreadPoolJobRunner` can be swapped for
RQ/Celery/Temporal without touching a plugin. A thread pool is the right shape because
the work is either inside DuckDB (which releases the GIL) or awaiting network I/O.

`JobCtx.progress` is throttled to 4 Hz so a fast batch loop does not hammer SQLite; it
records rows done, rows/s, ETA and accumulated cost. `GET /api/jobs/{id}/stream` is the
SSE feed the UI's `JobProgress` component reads.

### 5.8 Guardrails

The recurring failure mode this codebase designs against is **a job that succeeds and is
wrong**. Every guard below exists because some silent failure was cheaper to detect than
to discover downstream.

| Guard | Where | Catches |
|---|---|---|
| `ParseCheck` on a `SqlPlan` | `jobs/executor.py::run_checks` | `try_cast`/`try_strptime` return NULL on failure, so a wrong format *succeeds* and writes a column of NULLs. Measured over a 20k-row sample **before writing**; the error names the rate, an example value, and formats that would have worked. An all-null input is not blamed on the transform. |
| Cast-loss counting | `operations::_cast_losses` | Values a planned import cast turns into NULL — reported per column, never silent |
| Right-side uniqueness | `executor::duplicate_key_rows` | A left join onto duplicated keys multiplies rows instead of annotating them |
| Match rate probe | `executor::match_rate` | A key that matches nothing still reports success; a left join answers "no match" with NULL |
| Name collision | `executor::name_collisions` | DuckDB returns two result columns of the same name without complaint; the catalog stores one column per name per version |
| Row-count belt-and-braces | `run_pushdown_transform` | An annotation whose row count changed |
| Fan-out refusal | `run_join_op` | Result rows > left rows without `allow_fanout` — the written result is dropped |
| Limit-truncation refusal | `run_aggregate_op` | An aggregate that stopped exactly on its limit is truncated and incomplete; `limited_on_purpose` distinguishes a deliberate top-K |
| Ghost-dataset cleanup | `_new_dataset` | A dataset row created before the work that fills it, when that work fails |

Everything the join op's post-hoc guard checks is also checked *before* the write by
`services/join_plan.py`, which powers `POST /api/datasets/{id}/join-preview` — so the
join form reports uniqueness, match rate and collisions as the key is edited, for the
price of a `GROUP BY` and a bounded probe.

---

## 6. Plugin architecture

### 6.1 Kind × mode

Plugins are **heterogeneous in contract, homogeneous in execution**. Each declares two
independent things.

**`kind`** — what it consumes and produces, which fixes its Python interface:

| Kind | Interface | Default mode |
|---|---|---|
| `Reader` | `to_relation(conn, uri, params) -> DuckDBPyRelation` | `pushdown` |
| `Detector` | `detect(stats) -> list[SemanticGuess]` | `inspect` |
| `Transform` | `DatasetVersion -> DatasetVersion` | any of three |
| `Aggregator` | `plan(ctx) -> AggregatePlan` (a `QuerySpec` + derived exprs) | `pushdown` |
| `Suggester` | `suggest(ctx) -> list[Suggestion]` | `inspect` |
| `Visualizer` | `spec(ctx) -> VizSpec` | `inspect` |

**`mode`** — how the runtime must execute it, which fixes scheduling and the facilities
it is handed:

| Mode | Runs as | Runtime provides |
|---|---|---|
| `pushdown` | one DuckDB statement | projection assembly, parse checks, optional join widening, progress |
| `batch` | streamed Arrow record batches | part-file checkpointing, resume, progress, cancellation |
| `external` | bounded async pool | result cache, retries, cost accounting, budget cap, row-level failure isolation, injected model client |
| `inspect` | the request thread | read-only catalog access; **never creates a job** |

The stage a plugin belongs to does not determine how it runs. An extractor may be a cheap
regex (`pushdown`) or an LLM call (`external`); nothing downstream can tell the difference.

**Normalization, extraction and annotation are all `Transform`** — one kind, three modes,
selecting `sql()`, `process(batch)` or `async process_rows()`. `Aggregator` is separate
only because it changes cardinality.

### 6.2 The descriptor is the single source of truth

```python
@register
class NormalizeTimestamp(Transform):
    id      = "normalize.timestamp"
    title   = "Parse timestamp"
    mode    = "pushdown"
    version = "1"
    Params  = TimestampParams          # one Pydantic model
    accepts = Accepts(semantic_types=("time.timestamp",))
    produces = Produces(semantic_types=("time.timestamp",))

    def sql(self, ctx: TransformCtx) -> SqlPlan: ...
```

`Plugin.descriptor()` serialises exactly that, including
`Params.model_json_schema()`. `GET /api/plugins` returns it verbatim, and it feeds:

- the FastAPI request validation and OpenAPI docs,
- `SchemaForm.tsx`, which renders the JSON Schema as a form (column-typed fields become
  pickers),
- the agent's tool schema generator.

`Accepts` answers *"what can I do with this dataset?"* —
`GET /api/plugins?applicable_to=<id>` filters on semantic types (descendants count),
dataset kinds and minimum rows. That one query drives both the UI action list and the
agent's choices.

`register` validates at import time: non-empty `id`/`kind`/`mode`/`title`, no duplicate
ids, and every semantic type named in `accepts`/`produces` must exist.

### 6.3 Extension without forking

`PluginRegistry._ensure_entry_points()` loads the `dataq.plugins` entry-point group on
first lookup, so `pip install dataq-plugin-geoip` adds capability with no core change.
An entry point may be a `Plugin` subclass or a `register(registry)` hook.

### 6.4 What each mode's runtime does

**`pushdown`** — the plugin returns a `SqlPlan`, which is *column expressions, not a
query*:

```python
SqlPlan(add={...}, replace={...}, drop=(...), where=None,
        checks=(ParseCheck(...),), join=JoinPlan(...) | None)
```

The runtime assembles the `SELECT` (`build_projection`), so a transform cannot
accidentally drop the projection, reorder rows, or change cardinality. If a `JoinPlan` is
declared, the runtime widens the source first — wrapped in a subquery so the outer
projection stays unqualified, which means `add` expressions can reference brought-across
columns without knowing a join happened. The plugin never sees a storage path: it names a
dataset id and `TransformCtx.resolve_dataset` hands back a FROM-clause fragment.

**`batch`** — `process(batch, params) -> RecordBatch`. The runtime streams Arrow batches
from a dedicated read cursor, calls the plugin, buffers, flushes a part every
`checkpoint_every_batches`, and records the watermark.

**`external`** — the plugin implements only `process_rows(rows, ctx)` and declares
`batch_size`, `max_concurrency`, `output_columns` and `cache_key_fields`. `ExternalRunner`
(`jobs/external.py`) supplies everything else:

- a **persistent result cache** in the warehouse, keyed on
  `sha256(plugin_id, plugin_version, params, model, declared fields)` — so re-running an
  extraction over a superset of rows only pays for the new rows, and unrelated schema
  churn does not cause a miss. Bumping `Plugin.version` invalidates it.
- a bounded `asyncio.Semaphore` pool, so the plugin never manages concurrency;
- retry with exponential backoff (3 attempts);
- **row-level failure isolation** — a chunk that fails after retries lands as NULLs plus
  a `<plugin>_error` string rather than killing an hour-long job;
- cost accounting via `ExternalCtx.record_cost`, and a hard `max_cost_usd` cap that raises
  `BudgetExceeded` — which the runner treats as *success with partial results*, not failure.

The model client is injected (`services/model.py`), never constructed by the plugin, which
keeps auth and model choice central and lets tests pass a fake.

**`inspect`** — `services/inspect.py` runs the plugin in the request thread. A visualizer
returns a `VizSpec` whose `query.dataset` the *service* binds, so a visualizer can never
read from somewhere it was not asked to. The chart is then resolved against the query's
actual output columns and the source's semantic types, so a field the query does not
return is an error rather than an unexplained empty chart.

### 6.5 Built-in plugins

| Kind | Ids |
|---|---|
| Reader | `read.csv`, `read.parquet`, `read.json` |
| Detector | `detect.ip`, `detect.country_iso2`, `detect.latlng`, `detect.timestamp`, `detect.email`, `detect.url`, `detect.money`, `detect.boolean`, `detect.cardinality`, `detect.share` |
| Transform | `normalize.ip`, `normalize.timestamp`, `normalize.country`, `normalize.numeric` (pushdown); `transform.ip_class` (batch); `extract.entities` (external, LLM); `enrich.features` (pushdown) |
| Aggregator | `agg.frequency`, `agg.time_rollup`, `agg.topk`, `agg.features` |
| Suggester | `suggest.viz`, `suggest.aggregate`, `suggest.normalize`, `suggest.features`, `suggest.join` |
| Visualizer | `viz.histogram`, `viz.bar`, `viz.timeseries`, `viz.map_points`, `viz.table`, `viz.timeline` |

### 6.6 Behavioural features — a plugin pair worth reading

`core/features.py` + `plugins/builtin/features.py` implement per-entity, time-windowed
statistics: "how unusual is this event, for this actor, right now". Every such question
is the same shape — *an aggregate, over a partition, within a window, evaluated per row* —
written as a shorthand that parses to a typed `Feature` (which is what the API and agent
exchange) and compiles to one SQL window expression:

```
share()            by activity_type
count()            by user, activity_type over 30d
days_since_last()  by user, activity_type
avg(amount)        by user over 7d as spend_7d
```

It is split into two plugins because the intermediate is worth having. `agg.features`
builds a feature *table* — one row per `(user, activity_type[, day])` — which is a
dataset you can chart on its own. `enrich.features` attaches it back on a composite key
and computes what no grouped table can hold: "the 30 days before *this* event", "days
since the previous one".

Two design facts:

- **Cost tracks distinct sorts, not feature count.** Features sharing a window emit
  identical `OVER` text and DuckDB reuses one sort. On 5M rows: five features over one
  window, 0.7s; six across three windows, 6.3s.
- **A feature that sees the future says so.** A trailing window only looks back, but a
  whole-dataset `share()` counts events after the row it describes. That is recorded on
  the column rather than left to be rediscovered.

Feature stores were the reference (Tecton's `Aggregation`, Chalk's `Windowed`); the
serving half is deliberately absent — features here are columns on a dataset.

---

## 7. Query and visualisation layers

### 7.1 `QuerySpec` → SQL

`query/compiler.py` enforces two rules, because specs arrive from the UI *and* from an
LLM agent:

1. Every identifier is validated against the resolved source schema, then quoted. An
   unknown column is an error, never interpolated text.
2. Every literal is bound as a `?` parameter, never formatted into the string.

Together, a malicious or confused spec cannot inject SQL. `inline_params` exists purely
for *display* — folding parameters back as quoted literals so the SQL editor can be
seeded with a runnable query.

`TimeBucket` has two modes: truncation (each calendar day its own bucket) and `part` — a
*cyclical* slice where every 1pm across the dataset collapses into one bucket. In part
mode the compiler emits a readable label plus an `{alias}_ord` ordinal, because "Thu"
sorts alphabetically and would otherwise scramble the chart.

`AggregatePlan.derive` is the escape hatch for window functions (a rarity share is
`n / sum(n) over ()`), which `QuerySpec` deliberately cannot express. It is authored by
plugin code — trusted — never by a user or an agent, so it does not widen the injection
surface.

### 7.2 The rendering contract

```
Visualizer plugin ──▶ VizSpec { renderer, title, query, chart?, timeline?, spec?, animate? }
                              │
   services/inspect.py ───────┤  binds query.dataset, runs it, resolves the chart
                              ▼
   RenderedViz { spec, data, row_count, sql, elapsed_ms, truncated }
                              │
   frontend RENDERERS[spec.renderer] ──▶ VegaLite | MapLibre | Table | Timeline
```

The backend never renders. Consequence: **a new chart type that reuses an existing
renderer is a backend-only change.** Only a genuinely new rendering technology touches
`frontend/src/renderers/index.tsx`.

`ChartSpec` (`core/chart.py`) is a deliberate typed subset of Vega-Lite's vocabulary, so
it compiles almost 1:1 — the point of owning it is that it can be *resolved* against
things DataQ already knows: the query's real output columns, and each column's semantic
type (a `time.timestamp` is temporal, a `money.amount` quantitative, a
`geo.country_iso2` nominal). `raw_vega_lite` is the escape hatch — the same
structure-plus-a-way-out pairing as `QuerySpec`/raw SQL and `AggregatePlan.derive`.

Every chart carries the SQL that produced it, collapsed underneath. A chart chosen by a
suggester or an agent rather than by the person reading it is hard to trust otherwise.

Dashboards persist the **recipe** (a list of `VizSpec`s), not a snapshot.

---

## 8. The agent

`services/agent.py` binds Claude tools to the *service layer*, not to HTTP. Two scopes:

| Scope | Tools | Used by |
|---|---|---|
| `READ_ONLY` | `list_datasets`, `profile_dataset`, `run_query`, `get_suggestions`, `list_plugins`, `render_viz`, `get_job` | agent-backed *plugins* — so a plugin can explore data but can never spawn jobs (no recursion) |
| `FULL` | adds `create_aggregate`, `create_join`, `apply_transform`, `save_dashboard` | the chat agent, driven by a human who can see and cancel what it starts |

`AnalysisAgent` writes the tool-use loop out rather than using the SDK's tool runner,
because each step is streamed to the UI as a structured `AgentTurn` and dispatch is gated
on the caller's scope. The agent's `create_join` exposes a single column pair; the
composite-key form is reachable through `enrich.features` and the join form.

The system prompt teaches the agent the same workflow the UI teaches a person: profile
for *meaning*, read the suggestions, query, chart, save. `POST /api/agent/estimate`
prices a run before it starts; `POST /api/agent/chat` streams turns.

Because suggestions carry executable `action` payloads, the agent and the UI consume them
identically — one renders a button, the other calls the endpoint.

---

## 9. HTTP surface

```
GET  /api/plugins?kind=&mode=&applicable_to=    what can I do with this dataset?
GET  /api/semantic-types   POST   DELETE /{id}  the meaning vocabulary
POST /api/sources/preview | /plan | /upload     import phase 1
GET  /api/sources/browse                        server-side file picker (confined)
POST /api/operations                            import|transform|aggregate|join|revert -> 202
POST /api/inspect                               synchronous twin for viz/suggesters
GET  /api/jobs  /{id}  /{id}/stream  /{id}/cancel
GET  /api/datasets  /tree  /{id}  /{id}/related  /{id}/dependents
GET  /api/datasets/{id}/profile | versions | lineage | suggestions | feature-plan
GET  /api/datasets/{id}/join-candidates
POST /api/datasets/{id}/join-preview            what would this join do? (no job)
POST /api/datasets/{id}/columns/{col}/type      pin a meaning
POST /api/datasets/{id}/revert                  -> 202
DEL  /api/datasets/{id}  /{id}/versions/{n}
POST /api/query  /api/query/compile  /api/query/sql
GET/POST /api/dashboards
POST /api/auth/login    GET /api/auth/me
GET  /api/agent/tools   POST /api/agent/estimate  /api/agent/chat
```

**Auth** (`api/auth.py`, `api/users.py`) engages only when asked for — a shared
`DATAQ_AUTH_TOKEN`, a `DATAQ_USERS` list, or `DATAQ_REQUIRE_AUTH`. A laptop instance
nobody else can reach gets no login screen. Passwords are scrypt hashes; login exchanges
one for a session token signed with a key kept in the data directory, so a restart does
not sign everybody out. The committed built-in account deliberately does **not** satisfy
`DATAQ_REQUIRE_AUTH` — its hash is public, so a deployment must bring its own credential
or the app refuses to start.

---

## 10. Frontend

`frontend/src/`:

| Area | Notes |
|---|---|
| `api/client.ts`, `api/types.ts`, `api/hooks.ts` | typed client mirroring the backend models |
| `pages/` | Datasets, Dataset, Explore, Query, Dashboards, Timeline, Agent |
| `renderers/` | the registry keyed on `VizSpec.renderer` — the mirror image of the plugin registry |
| `components/SchemaForm.tsx` | renders any plugin's JSON Schema as a form; unwraps Pydantic's `anyOf: [T, null]`, humanises enum values, turns column-typed fields into pickers |
| `components/ImportPanel.tsx`, `FileBrowser.tsx` | the two-phase import, including the malformed-row choice |
| `components/JoinPanel.tsx` | the live join preview |
| `components/MeaningSelect.tsx` | semantic-type editing (which pins) |
| `components/JobProgress.tsx` | SSE job feed |
| `components/FeatureDraft.tsx` | the pre-filled feature expression editor |

Files are read **in place** by DuckDB, so the import box needs a path the *server* can
open — which a browser's file input cannot supply. Hence the server-side browser,
confined to `DATAQ_BROWSE_ROOTS`, with upload as the fallback for a file that really is
only on the viewer's machine.

---

## 11. Configuration

All settings are env-overridable with the `DATAQ_` prefix (`config.py`). The ones that
change architecture rather than tuning:

| Setting | Default | Effect |
|---|---|---|
| `DATAQ_DATA_DIR` | `./data` | everything lives here; deployment is "mount one volume" |
| `DATAQ_STORAGE` | `parquet` | `parquet` or `duckdb` — see §3.2 |
| `DATAQ_JOB_WORKERS` | 2 | thread-pool size |
| `DATAQ_BATCH_ROWS` | 100,000 | Arrow batch size for streaming modes |
| `DATAQ_CHECKPOINT_EVERY_BATCHES` | 5 | how often a part is flushed and the watermark recorded |
| `DATAQ_PROFILE_SAMPLE_ROWS` | 10,000 | profiling sample |
| `DATAQ_BROWSE_ROOTS` | home + cwd | what the file browser may list |
| `DATAQ_ALLOW_REMOTE_URIS` | false | whether an import may name `s3://` / `https://` |
| `DATAQ_STATIC_DIR` | unset | serve a built SPA at `/`, so production is a single container |
| `DATAQ_MODEL` | `claude-opus-5` | model for the agent and external plugins |

---

## 12. Design rules, as an index

Rules that recur and explain most of the code:

1. **A plugin never sees storage.** It names a dataset id; the service resolves it.
2. **The runtime owns the projection**, so a transform cannot change cardinality by accident.
3. **Structure, plus a documented way out.** `QuerySpec` + raw SQL; `ChartSpec` +
   `raw_vega_lite`; `QuerySpec` + `AggregatePlan.derive`. The escape hatch is always
   narrower or more trusted than the structured path.
4. **Silence is the enemy.** Every operation that can succeed while being wrong has a
   pre-write check that names the problem in a sentence, with an example.
5. **Check before writing, not after.** A guard that costs a full pass is kept as the last
   line of defence, but the same question is asked first for the price of a `GROUP BY`.
6. **What cannot be recomputed is carried, not re-derived.** Pins and import warnings
   survive transforms and reverts; stats do not need to.
7. **The immutable decision gets shown first.** Physical type is proposed with evidence
   before import, because it can never be revisited.
8. **One definition, many consumers.** A plugin's `Params`; a column's semantic type; a
   `VizSpec`'s renderer name.
9. **Metadata and data have different write patterns**, so they live in different stores.
10. **A failed operation leaves the catalog as it found it.**
