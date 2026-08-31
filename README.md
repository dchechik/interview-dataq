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

### Dates, and what the importer had to guess

Date columns get their own treatment, because they are where automatic typing
quietly goes wrong. DuckDB's CSV sniffer types anything non-ISO as `VARCHAR`, so
`Mar 04, 2016` and `03/04/2016 02:05 PM` arrive as text and stay unusable for
bucketing, charting or timelines. Profiling now tries ~35 formats against the
sampled values and records which fit, so `Parse timestamp` needs no format
argument — it uses what was detected.

Where it refuses to help is ambiguity. `03/04/2016` is the 4th of March in most
of the world and the 3rd of April in the US, and no amount of statistics settles
which. Three things follow from that:

- A **text** column that reads both ways is reported as ambiguous, and parsing it
  fails with both options rather than silently picking one. A sample containing
  any day past the 12th resolves itself, because then only one reading parses.
- A column DuckDB already converted is worse, because the sniffer picks a reading
  **and does not record which**, and by profiling time the text is gone. So the
  raw file is re-read at import, and a column whose text could not settle the
  question gets a warning naming both readings, the one the importer took, and
  the `timestampformat` that pins the other. It shows as a `check format` badge
  in the schema table.
- Numeric columns named like times are checked against the epoch ranges, so
  `event_time` as a BIGINT is recognised rather than averaged.

### Import decides the types with you

A column's physical type is settled by the reader the moment the file is read,
copied into every record of the dataset, and can never be changed afterwards —
storage is immutable per version, and the only escape is a transform that adds a
second column beside it. Meaning and role, by contrast, are worked out later and
stay editable forever. So the one decision that could not be revisited was the
one nobody was shown, which is how a column of dates ends up stored as text.

Importing now proposes a plan first: for each column, what the reader found,
what it will be stored as, how a date will be read, its meaning and its role.
The proposal comes from running the *real* profiler over a sample, so it shows
what the import will do rather than a second opinion. The normal path is to look
and press Import.

Casting happens in the projection that materialises the version, not at read
time, because at read time it cannot be survived: `types={'date':'TIMESTAMP'}`
aborts the whole import on the first unparseable row — one `n/a` in 850,000 —
and `ignore_errors` answers that by silently dropping rows, measured at 18 lost
from 43. A projection cannot drop anything, so a value that will not convert
becomes NULL, and the count is reported against the column.

**Ambiguity is settled where it can be and asked where it cannot.** A prefix
sample is a poor witness for day-first versus month-first: the first 2,000 rows
of a time-sorted export can span a single day, in which every day is ≤ 12 and
both readings fit. So when the sample cannot choose, the file is asked how often
each reading actually fails — 109ms over 200,000 rows. Only a column where both
readings survive that is put to the user, and then with a worked example rather
than the word "ambiguous".

A column whose reading is yours to choose is held back as text
(`types={col: 'VARCHAR'}`), because once the sniffer has turned `03/04/2016` into
a DATE, which of March or April it picked is unrecoverable.

**Meaning is not storage.** A `VARCHAR` holding `03/07/2011 08:07:29` *means* a
timestamp, and detection says so. It still cannot be a time axis: subtracting an
interval from text is a type error. So a column's **role** reflects what it can
do as stored — a text date is a dimension until it is parsed — while its
semantic type records what it means, which is what makes the fix suggestable.
Parsing it is then the top suggestion, carrying the format already detected.

Without that split, every time-based plugin picked the column up and failed
inside DuckDB with `No function matches -(VARCHAR, INTERVAL)` and forty lines of
candidate operators, naming neither the column nor the remedy.

### Parsing that fails says so

Every parsing expression DuckDB offers — `try_cast`, `try_strptime` — reports
failure by returning NULL. A transform with the wrong format therefore *succeeds*,
writes a column of NULLs, and reports the full row count; the dataset looks fine
until something downstream has nothing to work with.

So a `SqlPlan` can declare a `ParseCheck` on a column it produces, and the runtime
measures how many non-null inputs yielded non-null outputs — on a 20k-row sample,
before writing anything, so a wrong format costs milliseconds rather than a full
pass over a multi-GB dataset. The error names the rate, an example value that
failed, and the formats that would have worked.

The check is for wrong formats, not imperfect data: the default threshold tolerates
the handful of `n/a` rows every real column has, and an all-null input is not
blamed on the transform that could not parse it.

### Behavioural features

Given `(user, timestamp, activity_type, location)`, every row can carry how often
this user did this recently, how common it is across everyone, and how long since
they last did it. Written as expressions, one per line:

```
share()            by activity_type
count()            by user, activity_type over 30d
count()            by user, activity_type in day
days_since_last()  by user, activity_type
avg(amount)        by user over 7d as spend_7d
```

The editor opens with a draft rather than an empty box. Knowing the language
exists is not the same as knowing what to write, and the useful expressions
follow from the table: pick whoever acts, pick the clock, then every categorical
column gets the same three questions — how often has this actor seen this value
lately, how long since they last did, and how common is it across everyone — and
every numeric column gets a percentile, overall and within the actor.

The one thing the table cannot settle is **who acts**, so that is shown and not
assumed. Two email columns can be identical in type and semantics; whether
behaviour is per-recipient or per-sender is a question about intent. Candidates
are ranked on the *weaker* of two things — how many of them there are, and how
many events each has — because ranking on either alone goes wrong in opposite
directions: cardinality picks a near-unique sender address over the recipient it
was sent to, and events-per-value picks the seven-value country column over
both. Changing the actor rewrites the draft; typing in the box stops it.

Each expression parses to a typed `Feature` — which is what the API and the agent exchange,
the shorthand being for people — and compiles to one SQL window expression. There
is a `raw` escape hatch for the rest, the same structure-plus-a-way-out pairing
as `ChartSpec` and its `raw_vega_lite`.

**Two steps, because the intermediate is worth having.** `agg.features` builds a
feature table — one row per `(user, activity_type[, day])` with counts, shares,
rolling totals and first/last seen — which is a dataset you can chart and query
on its own. `enrich.features` then attaches it back, on as many key columns as it
takes, and computes the features no grouped table can hold. The split is not
merely organisational: whole-partition and calendar-bucket statistics are exactly
what a `GROUP BY` computes, while "the 30 days before *this* event" and "days
since the previous one" have a different answer on every row.

Two things the design turns on:

- **Cost tracks the number of distinct sorts, not the number of features.**
  Features sharing a window emit identical `OVER` text and DuckDB reuses one
  sort. On 5M rows, five features over one window cost 0.7s where six across
  three windows cost 6.3s.
- **A feature that sees the future says so.** A trailing window only looks back,
  but a whole-dataset `share()` counts events that come after the row it
  describes, and a calendar bucket includes the rest of today. That is often
  the question you want — it is recorded on the column rather than left to be
  rediscovered.

Rolling totals in the feature table are read off buckets, so they stop one bucket
short: `n_30d` means the 30 days *before* today, never the current day, whose
later events would otherwise leak backwards.

Feature stores were the reference for the interface — Tecton's
`Aggregation(column, function, time_window)` is nearly the same object, its tiling
is the feature table, and Chalk's `Windowed` is the `over 1d,7d,30d` fan-out. The
serving half of those systems is deliberately absent: features here are columns
on a dataset, not a serving surface.

### Signing in

A hosted instance requires a username and password. The built-in account is
`dmitry`; its password was generated at setup and is not in the repository —
only its scrypt hash is, in `backend/src/dataq/api/users.py`.

```bash
# Add or replace accounts without touching the code:
cd backend && uv run python -m dataq.api.newuser alice
# prints  alice:scrypt$...  — put it in DATAQ_USERS
```

`DATAQ_USERS` takes `name:hash` entries separated by commas or newlines, and
*replaces* the built-in account rather than adding to it. Everyone signed in
sees the same datasets; the point is to keep the instance off the public
internet, not to model permissions.

Signing in exchanges the password for a session token, signed with a key kept in
the data directory so a restart or a redeploy does not sign everybody out.
Sessions last two weeks by default (`DATAQ_SESSION_HOURS`).

The older shared `DATAQ_AUTH_TOKEN` still works alongside accounts — that is
what the deploy scripts and `curl` examples use. Either credential is accepted.

**The built-in account does not satisfy `DATAQ_REQUIRE_AUTH`.** Its hash is
committed, so every clone knows the account and shares its password; a
deployment must set `DATAQ_USERS` or `DATAQ_AUTH_TOKEN` of its own, and the app
refuses to start otherwise rather than going live on a credential that is not a
secret.

### Deleting a dataset

`DELETE /api/datasets/{id}`, or the button on the dataset page. Three things it
does that the obvious implementation does not:

- **Frees the disk.** The catalog only knows about rows; the parquet parts live
  behind the storage backend. Deleting the metadata and leaving the bytes is how
  a lake grows forever while the dataset list says nothing is there.
- **Refuses to strand a derivation tree.** An aggregate built from a dataset
  stays valid after its parent goes, but its provenance does not — so a parent
  with children returns 409 naming them, and `?cascade=true` removes the subtree.
  The UI asks first, listing what would go.
- **Refuses to race a job.** Deleting under a live writer leaves a version row
  pointing at files that no longer exist.

Relatedly, a *failed* operation now leaves the catalog as it found it. The
dataset row is created before the work that fills it, so anything failing in
between used to leave a ghost: listed in the UI, zero rows, unqueryable,
unexplainable.

### Importing without typing a path

DuckDB reads data files **in place**, so the import box needs a path the *server*
can open — which a browser's file input cannot supply (it hands over contents, not a
location). **Browse…** therefore lists the server's filesystem, confined to
`DATAQ_BROWSE_ROOTS`; unset, that means your home and working directories, which is
right when the server is your own laptop. For a file that really is only on the
viewer's machine, the same dialog offers an upload, capped by `DATAQ_MAX_UPLOAD_MB`.
Browsing is the better path for multi-GB files: it avoids the copy entirely.

### Derivation tree

Aggregates and joins nest under the dataset they came from, on the datasets page
and again as "Related datasets" on a dataset's own page. Two rules make the DAG in
`Step.inputs`/`outputs` render as a tree:

- A **transform is not an edge.** It produces a new *version* of the dataset it was
  given, so it belongs to that dataset's history, not to its offspring.
- A **join has two parents but a node has one.** It nests under its left input and
  names the other parent inline, so the second edge stays visible.

A dataset whose parent was deleted surfaces as a root rather than disappearing.

### Charts show their query

Every chart carries the SQL that produced it, collapsed under the chart. A chart
chosen by a suggester or an agent rather than by the person reading it is hard to
trust otherwise. The Query page's default **Rows** view is a plain paginated
`SELECT *`, which is what you want first when meeting an unfamiliar dataset.

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
GET  /api/datasets/tree              datasets nested by derivation
GET  /api/datasets/{id}/related      immediate parents and derived children
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
  services/    profiler, operations, query, inspect, lineage, browse, agent, model
  api/         FastAPI routes (thin: resolve a plugin, call a service)
frontend/src/
  api/         typed client + React Query hooks
  renderers/   registry keyed on VizSpec.renderer
  components/  SchemaForm (JSON Schema -> form), DatasetTree, FileBrowser,
               ChartEditor, ChartInspector, JobProgress
  pages/       Datasets, Dataset, Query, Explore, Timeline, Dashboards, Ask
```

## Deployment

One container serves `/api` and the built SPA, with all state under one volume, so
locally that is:

```bash
make docker && make docker-run     # http://localhost:8000
```

### Railway

Code and data deploy **separately**. The image holds the code; a Railway volume
mounted at `/data` holds the datasets and survives every deploy. `docs/DEPLOY.md`
is the full walkthrough — this is the short version.

**Link an account** (Railway builds remotely, so you do not need Docker locally):

```bash
brew install railway
railway login          # opens a browser to authenticate
railway init           # creates a project and links this directory to it
```

`railway init` creates a project and links this directory to it, so every later
command run here targets that project. The link lives in your global
`~/.railway/config.json` keyed by directory — there is nothing to commit, and a
fresh clone needs linking again. `railway status` shows what you are linked to;
`railway link` connects to an existing project instead of creating a new one.

**Deploy the code — and only the code:**

```bash
make railway-deploy    # == railway up
make railway-logs      # tail the running service
```

This never ships data, and there is no flag to make it. `.dockerignore` excludes
`data/`, so the directory is not even uploaded as build context; and a Railway
volume is not mounted during the build, so nothing could be written into the
image anyway. A code deploy replaces the container and leaves the volume exactly
as it was. Deploy as often as you like — your datasets do not move.

Data travels only when you ask for it, through the `data-*` targets below. So
the two cases you might want are:

| I want to… | Run |
|---|---|
| Ship a code change, leave data alone | `make railway-deploy` |
| Ship code *and* data | `make railway-deploy && make data-push` |

The first deploy fails its healthcheck until you finish the setup below — the app
refuses to start without an auth token, which is deliberate. Then, once, in the
dashboard:

1. **Add a volume** to the service with mount path `/data`. This is what makes
   data outlive deploys; `DATAQ_DATA_DIR=/data` is already baked into the image.
2. **Set variables** — `DATAQ_AUTH_TOKEN` (a long random string),
   `DATAQ_REQUIRE_AUTH=true`, `DATAQ_BROWSE_ROOTS=/data/uploads`,
   `DATAQ_STORAGE=parquet`, and `DATAQ_ANTHROPIC_API_KEY` if you want the agent.
3. **Generate a domain** under Settings → Networking.

Build settings and the `/api/health` healthcheck come from `railway.json`, which
overrides the dashboard, so there is nothing to click for those.

**Move data separately from code:**

```bash
make data-size      # what am I about to ship, and does it fit the volume?
make data-push      # ./data  ->  Railway, merged into what is there
make data-replace   # ./data  ->  Railway, wiping the volume first
make data-pull      # Railway ->  ./data-from-railway
```

`data-push` uploads a tarball to the volume and restarts the service; the
container entrypoint unpacks it **before** uvicorn starts, because the running
app holds `catalog.sqlite` open in WAL mode and replacing it underneath a live
process risks corruption.

A hosted instance needs `DATAQ_AUTH_TOKEN` set. Without it anyone who finds the
URL can read server-side files and spend your Anthropic budget, which is why
`DATAQ_REQUIRE_AUTH` makes the app refuse to start rather than come up open.

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

Before any request is sent the UI prices the run — tokens in the first request
(counted by the API when a key is present, approximated otherwise) and a ceiling
for the whole loop — and asks. The user is paying, so the decision is theirs.

Set `ANTHROPIC_API_KEY` (or `DATAQ_ANTHROPIC_API_KEY` — both are read) to enable
it. Everything else runs without one.

## Status

Implemented and tested: import, profiling and semantic typing, transforms in all
three execution modes, the query layer, charts and maps, aggregates, joins, the
typed chart grammar, the timeline view, the dashboard, the agent, and a
Railway deployment with data decoupled from code.

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
