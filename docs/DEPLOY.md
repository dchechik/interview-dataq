# Deploying DataQ to Railway

Code and data are deployed separately. Code lives in the container image; data
lives on a **volume** that survives every deploy. You can redeploy the app a
dozen times and the imported datasets stay exactly where they were.

## How Railway fits together

Three facts explain the whole setup:

1. A **service** builds from the `Dockerfile` in this repo. `railway up` uploads
   the build context and builds it remotely, so you do not need Docker installed.
2. A **volume** is a persistent disk mounted at a path you pick. It survives
   deploys and restarts, and a service can have exactly one. It is **not**
   mounted during the build, so data cannot be baked into the image — it has to
   arrive at runtime.
3. Railway injects a **`PORT`** and probes a healthcheck before sending traffic.
   `railway.json` points that probe at `/api/health`.

## One-time setup

```bash
brew install railway
railway login         # opens a browser to authenticate
railway init          # creates the project, links this directory to it
```

The link is stored in your global `~/.railway/config.json`, keyed by directory —
nothing to commit, and a fresh clone needs linking again. `railway status` shows
what you are linked to; `railway link` connects to an existing project instead of
creating one.

### 1. Deploy the code

```bash
make railway-deploy   # == railway up
```

The first deploy will fail its healthcheck until you finish step 3 — the app
refuses to start without an auth token. That is deliberate.

**This is a code-only deploy, and it is the only kind there is.** Two separate
things guarantee it: `.dockerignore` excludes `data/`, so the directory is never
uploaded as build context; and the volume is not mounted during the build, so
nothing could write datasets into the image even if it wanted to. A deploy
swaps the container and leaves the volume untouched.

Data moves only when you run a `data-*` target. So:

| I want to… | Run |
|---|---|
| Ship a code change, leave data alone | `make railway-deploy` |
| Ship code *and* data | `make railway-deploy && make data-push` |
| Ship data only, no code change | `make data-push` |
| Ship data, wiping what is there | `make data-replace` |

The image declares no `VOLUME` for `/data`, deliberately: Railway rejects a
Dockerfile that has one, because persistence there is a volume you attach to
the service rather than something the image can request. The entrypoint creates
the directory itself, so the path exists whether or not anything is mounted
over it — which is also why the app runs fine locally with no volume at all.

### 2. Add the volume

In the Railway dashboard, on the service: `⌘K` → **New Volume** (or right-click
the service → Add Volume). Set the **mount path** to:

```
/data
```

That single choice is what makes data survive deploys — `DATAQ_DATA_DIR=/data`
is already baked into the image, so the catalog, the parquet lake and uploads
all land on the volume.

Volume sizes are 0.5 GB on Trial, 5 GB on Hobby, 50 GB on Pro. Check what you
are about to ship first:

```bash
make data-size
```

### 3. Set the variables

Service → **Variables**:

| Variable | Value | Why |
|---|---|---|
| `DATAQ_AUTH_TOKEN` | a long random string | The shared secret the UI and API require |
| `DATAQ_REQUIRE_AUTH` | `true` | Makes the app refuse to start without a token, so it can never be public and open |
| `DATAQ_BROWSE_ROOTS` | `/data/uploads` | Without this the file browser defaults to `$HOME` and the working directory — inside the container, your whole app tree |
| `DATAQ_STORAGE` | `parquet` | Immutable parquet parts; resumable imports |
| `DATAQ_ANTHROPIC_API_KEY` | `sk-ant-…` | Only needed for the agent and LLM plugins |

`DATAQ_DATA_DIR` is already set in the image. Generate a token with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 4. Give it a URL

Service → Settings → Networking → **Generate Domain**. Open it, and the UI will
prompt once for the token and remember it in that browser.

## The data workflow

```bash
make data-size      # what am I about to ship, and does it fit?
make data-push      # ./data  ->  Railway, merged into what is there
make data-replace   # ./data  ->  Railway, wiping the volume first
make data-pull      # Railway ->  ./data-from-railway
```

`data-push` tars `./data`, uploads it to `/_inbox` on the volume, and restarts
the service. The container entrypoint unpacks it **before uvicorn starts** —
which matters, because the running app holds `catalog.sqlite` open in WAL mode
and `warehouse.duckdb` open for its whole lifetime. Replacing those files under
a live process risks corrupting them; before the app opens anything, they are
just files.

The bundle is renamed to `*.applied-<timestamp>` once unpacked, so a later
restart does not redo the restore.

**Reset** is `make data-replace`. The `.replace.` in the bundle filename is what
tells the entrypoint to clear `/data` first, so the intent travels with the file
rather than living in an environment variable someone set weeks ago.

`make data-reset` prints the direct-deletion commands rather than running them:
`railway volume files delete` refuses to run when invoked by an AI agent, so a
target that silently fails for an assistant but works for you would be worse
than no target at all.

### Pulling data back

`make data-pull` downloads the volume to `./data-from-railway`. One caveat: the
SQLite catalog is in WAL mode, so a copy taken while the service is running can
catch a torn write. Pause the service first if you need a guaranteed-clean
backup. The parquet lake itself is immutable and safe to copy hot.

## Deploying code afterwards

```bash
make railway-deploy
```

New image, same volume. Your datasets are untouched — that is the whole point of
the split.

## Security notes

The app has no user accounts; `DATAQ_AUTH_TOKEN` is a single shared secret
checked in constant time on every `/api/*` route except `/api/health`, which
stays open because Railway probes it. That is proportionate for a single-user
tool, and it is genuinely necessary: without it, anyone who finds the URL can
read server-side files, upload, delete datasets, and spend your Anthropic
budget through the agent.

Two things to be aware of even with the token set:

- `POST /api/query/sql` is `SELECT`-only, but DuckDB can still read files from
  a `SELECT` (`read_csv('/etc/passwd')`). Disabling that would also block
  reading the parquet lake, so it is not available in this storage mode. Treat
  the token as granting real access to the container, not just to the data.
- `numReplicas` is pinned to 1 in `railway.json`. A volume attaches to one
  replica, and both DuckDB and SQLite are single-writer, so scaling out would
  corrupt data rather than distribute load.

Imports and previews are confined to `DATAQ_BROWSE_ROOTS`; remote (`https://`,
`s3://`) sources are refused unless you set `DATAQ_ALLOW_REMOTE_URIS=true`,
because fetching them makes the server issue outbound requests on a caller's
behalf.

## Troubleshooting

**Healthcheck never passes.** Check the logs (`make railway-logs`). If the app
exited with `MisconfiguredAuth`, you set `DATAQ_REQUIRE_AUTH` without
`DATAQ_AUTH_TOKEN`.

**The UI loads but every request 401s.** The stored token is stale. The prompt
reappears automatically after a failed request; clear it manually with
`localStorage.removeItem('dataq.token')` in the browser console.

**Data vanished after a deploy.** The volume is not mounted, or is mounted
somewhere other than `/data`. Check Service → Settings → Volumes.

**Import fails with "path is outside the browsable directories".** The file is
not under `DATAQ_BROWSE_ROOTS`. Upload it through the UI (uploads land in
`/data/uploads`, which is always browsable) or widen the roots.
