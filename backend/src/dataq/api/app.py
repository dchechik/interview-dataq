"""FastAPI application.

Handlers are deliberately thin: they resolve a plugin and call into
``dataq.services``. The agent binds to those same services, so the two surfaces
cannot drift apart.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

import dataq.plugins.builtin  # noqa: F401  (registers built-in plugins)

from ..config import get_settings
from ..core.profile import DatasetProfile
from ..core.types import TERMINAL_JOB_STATUSES
from ..db import UnsafeSQLError
from ..jobs.runner import ThreadPoolJobRunner
from ..plugins.base import REGISTRY, PluginDescriptor
from ..plugins.builtin.readers import pick_reader
from ..query.compiler import QueryError
from ..query.spec import QueryResult, QuerySpec
from ..services import browse as browse_service
from ..services import datasets as dataset_service
from ..services import import_plan
from ..services import inspect as inspect_service
from ..services import lineage as lineage_service
from ..services.context import AppContext, build_context
from ..services.operations import OperationAccepted, OperationRequest, submit_operation
from ..services.query import run_query, run_sql
from . import users
from .auth import TokenAuthMiddleware, check_settings

CTX: AppContext | None = None


def context() -> AppContext:
    if CTX is None:  # pragma: no cover - set during lifespan
        raise RuntimeError("application context not initialised")
    return CTX


@asynccontextmanager
async def lifespan(app: FastAPI):
    global CTX
    settings = get_settings()
    CTX = build_context(settings)
    CTX.runner = ThreadPoolJobRunner(CTX.catalog, workers=settings.job_workers)
    yield
    CTX.runner.shutdown()
    CTX.warehouse.close()


def create_app(ctx: AppContext | None = None) -> FastAPI:
    """``ctx`` is injected by tests; production builds it in the lifespan hook."""
    global CTX
    app = FastAPI(title="DataQ", version="0.1.0", lifespan=lifespan if ctx is None else None)
    if ctx is not None:
        CTX = ctx

    settings = ctx.settings if ctx is not None else get_settings()
    # Refuse to start rather than serve an unprotected API on a public URL.
    check_settings(settings)
    # Auth engages when it is asked for -- a token, a user list, or the
    # require_auth catch -- and not merely because a built-in account exists.
    # A laptop instance nobody else can reach should not have a login screen,
    # which is the property the shared-token design started with.
    if settings.auth_token or settings.users or settings.require_auth:
        app.add_middleware(
            TokenAuthMiddleware, token=settings.auth_token,
            secret=users.session_secret(settings.data_dir, settings.session_secret),
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_credentials=True,
        allow_headers=["*"],
    )
    _register_routes(app)
    _mount_frontend(app, settings)
    return app


# --------------------------------------------------------------------------- #
# response models
# --------------------------------------------------------------------------- #
class DatasetSummary(BaseModel):
    id: str
    name: str
    kind: str
    description: str
    source_uri: str
    latest_version: int
    row_count: int = 0
    created_at: str


class LoginRequest(BaseModel):
    username: str
    password: str


class SqlRequest(BaseModel):
    sql: str
    limit: int = 1000


class InspectRequest(BaseModel):
    plugin_id: str
    dataset_id: str
    version: int | None = None
    params: dict[str, Any] = {}
    limit: int | None = None


class PreviewRequest(BaseModel):
    uri: str
    plugin_id: str = ""
    params: dict[str, Any] = {}
    limit: int = 20


class AgentChatRequest(BaseModel):
    message: str
    history: list[dict[str, Any]] = []


class DashboardRequest(BaseModel):
    id: str | None = None
    name: str
    description: str = ""
    panels: list[dict[str, Any]] = []


def _summary(ctx: AppContext, ds) -> DatasetSummary:
    version = ctx.catalog.get_version(ds.id)
    return DatasetSummary(
        id=ds.id, name=ds.name, kind=ds.kind, description=ds.description,
        source_uri=ds.source_uri, latest_version=ds.latest_version,
        row_count=version.row_count if version else 0,
        created_at=ds.created_at.isoformat(),
    )


def _register_routes(app: FastAPI) -> None:  # noqa: C901 - a flat route table
    @app.get("/api/health")
    def health() -> dict:
        ctx = context()
        return {"status": "ok", "storage": ctx.settings.storage,
                "plugins": len(REGISTRY.list())}

    # --- plugins ---------------------------------------------------------
    @app.get("/api/plugins", response_model=list[PluginDescriptor])
    def list_plugins(
        kind: str | None = None,
        mode: str | None = None,
        applicable_to: str | None = Query(default=None, description="Dataset id"),
    ) -> list[PluginDescriptor]:
        """The single source of plugin metadata.

        The UI's dynamic form renderer and the agent's tool schemas are both
        generated from this response.
        """
        ctx = context()
        if applicable_to:
            try:
                found = inspect_service.applicable_plugins(ctx, applicable_to)
            except KeyError as exc:
                raise HTTPException(404, str(exc)) from exc
            return [d for d in found
                    if (kind is None or d.kind == kind) and (mode is None or d.mode == mode)]
        return [p.descriptor() for p in REGISTRY.list(kind=kind, mode=mode)]

    @app.get("/api/semantic-types")
    def semantic_types() -> list[dict]:
        from ..core.semantic import SEMANTIC_TYPES

        return [
            {"id": t.id, "title": t.title, "parent": t.parent, "role": t.role,
             "joinable": t.joinable, "description": t.description}
            for t in SEMANTIC_TYPES.all()
        ]

    # --- sources & datasets ----------------------------------------------
    @app.post("/api/sources/preview")
    def preview(req: PreviewRequest) -> dict:
        """Peek at a file before importing it."""
        ctx = context()
        # Without this the uri goes straight into read_csv(), which is an
        # arbitrary server-side file read for anyone who can call the endpoint.
        try:
            browse_service.assert_readable_uri(req.uri, ctx.settings)
        except browse_service.BrowseError as exc:
            raise HTTPException(403, str(exc)) from exc
        reader_cls = REGISTRY.require(req.plugin_id) if req.plugin_id else pick_reader(req.uri)
        if reader_cls is None:
            raise HTTPException(400, f"no reader can handle {req.uri!r}")
        try:
            with ctx.warehouse.cur() as conn:
                rel = reader_cls().to_relation(conn, req.uri, reader_cls.parse_params(req.params))
                rows = rel.limit(req.limit).fetchall()
                columns = list(rel.columns)
                types = [str(t) for t in rel.types]
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            raise HTTPException(400, f"{type(exc).__name__}: {exc}") from exc
        return {"reader": reader_cls.id, "columns": columns, "types": types,
                "rows": [list(r) for r in rows]}

    @app.post("/api/sources/plan")
    def plan_import(req: PreviewRequest) -> dict:
        """Propose how each column should be imported, with the evidence.

        The preview a person confirms. It runs the same profiler the import
        runs, so what it shows is what will happen rather than a second opinion.
        """
        ctx = context()
        try:
            browse_service.assert_readable_uri(req.uri, ctx.settings)
        except browse_service.BrowseError as exc:
            raise HTTPException(403, str(exc)) from exc
        reader_cls = (REGISTRY.require(req.plugin_id) if req.plugin_id
                      else pick_reader(req.uri))
        if reader_cls is None:
            raise HTTPException(400, f"no reader can handle {req.uri!r}")
        try:
            with ctx.warehouse.cur() as conn:
                plan = import_plan.build_plan(conn, reader_cls, req.uri, req.params)
        except import_plan.PlanError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            raise HTTPException(400, f"{type(exc).__name__}: {exc}") from exc
        return plan.model_dump()

    @app.get("/api/sources/browse")
    def browse(path: str | None = None, show_hidden: bool = False) -> dict:
        """List a server-side directory so the UI can offer a file picker.

        DuckDB reads data files in place, so the picker has to return a path the
        *server* can open. Confined to the configured browse roots.
        """
        try:
            return browse_service.list_directory(path, context().settings, show_hidden)
        except browse_service.BrowseError as exc:
            raise HTTPException(403, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/sources/upload")
    async def upload(file: UploadFile = File(...)) -> dict:
        """Accept a file from the browser when it is not on the same machine.

        Streamed to disk in chunks so a large upload does not have to fit in
        memory. Prefer browsing for multi-GB files, which avoids the copy entirely.
        """
        settings = context().settings
        target = browse_service.safe_upload_path(file.filename or "upload", settings)
        limit = settings.max_upload_mb * 1024 * 1024
        written = 0
        try:
            with target.open("wb") as out:
                while chunk := await file.read(1024 * 1024):
                    written += len(chunk)
                    if written > limit:
                        raise HTTPException(
                            413,
                            f"file exceeds the {settings.max_upload_mb} MB upload limit; "
                            "put it somewhere the server can read and use Browse instead",
                        )
                    out.write(chunk)
        except HTTPException:
            target.unlink(missing_ok=True)
            raise
        except OSError as exc:
            target.unlink(missing_ok=True)
            raise HTTPException(500, f"could not save upload: {exc}") from exc
        return {"uri": str(target), "name": target.name, "bytes": written}

    @app.get("/api/datasets", response_model=list[DatasetSummary])
    def list_datasets() -> list[DatasetSummary]:
        ctx = context()
        return [_summary(ctx, d) for d in ctx.catalog.list_datasets()]

    @app.get("/api/datasets/tree")
    def dataset_tree() -> list[dict]:
        """Datasets nested under the dataset they were derived from.

        Declared before /api/datasets/{dataset_id} so "tree" is not swallowed as
        a dataset id.
        """
        ctx = context()

        def to_dict(node) -> dict:
            out = _summary(ctx, node.dataset).model_dump()
            out["derived_via"] = (
                {"op": node.edge.op, "plugin_id": node.edge.plugin_id}
                if node.edge else None
            )
            out["joined_with"] = [
                {"id": e.parent_id,
                 "name": getattr(ctx.catalog.get_dataset(e.parent_id), "name", e.parent_id)}
                for e in node.others
            ]
            out["descendants"] = node.descendants()
            out["children"] = [to_dict(c) for c in node.children]
            return out

        return [to_dict(n) for n in lineage_service.build_forest(ctx.catalog)]

    @app.get("/api/datasets/{dataset_id}", response_model=DatasetSummary)
    def get_dataset(dataset_id: str) -> DatasetSummary:
        ctx = context()
        ds = ctx.catalog.get_dataset(dataset_id)
        if ds is None:
            raise HTTPException(404, "dataset not found")
        return _summary(ctx, ds)

    @app.delete("/api/datasets/{dataset_id}")
    def delete_dataset(dataset_id: str, cascade: bool = False) -> dict:
        """Delete a dataset, its versions, and the stored bytes behind them.

        Refuses rather than stranding anything: 409 when other datasets are
        derived from this one (unless ``cascade``), or when a job is still
        writing to it.
        """
        try:
            result = dataset_service.delete_dataset(context(), dataset_id, cascade)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except dataset_service.DeleteRefused as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "deleted": result.ids,
            "datasets": result.datasets,
            "versions": result.versions,
            "bytes_freed": result.bytes_freed,
        }

    @app.get("/api/datasets/{dataset_id}/dependents")
    def dataset_dependents(dataset_id: str) -> list[dict]:
        """What a delete would take with it. The UI asks before confirming."""
        ctx = context()
        if ctx.catalog.get_dataset(dataset_id) is None:
            raise HTTPException(404, "dataset not found")
        out = []
        for child in dataset_service.descendants(ctx, dataset_id):
            row = ctx.catalog.get_dataset(child)
            if row is not None:
                out.append({"id": row.id, "name": row.name, "kind": row.kind})
        return out

    @app.get("/api/datasets/{dataset_id}/versions")
    def list_versions(dataset_id: str) -> list[dict]:
        ctx = context()
        return [
            {"version": v.version, "row_count": v.row_count,
             "created_at": v.created_at.isoformat(), "produced_by_step": v.produced_by_step,
             "columns": len(v.columns_schema)}
            for v in ctx.catalog.list_versions(dataset_id)
        ]

    @app.get("/api/datasets/{dataset_id}/profile", response_model=DatasetProfile)
    def get_profile(dataset_id: str, version: int | None = None) -> DatasetProfile:
        profile = context().catalog.get_profile(dataset_id, version)
        if profile is None:
            raise HTTPException(404, "dataset or version not found")
        return profile

    @app.post("/api/datasets/{dataset_id}/columns/{column}/type")
    def pin_column_type(
        dataset_id: str, column: str, semantic_type: str | None = None,
        role: str | None = None, version: int | None = None,
    ) -> dict:
        """A human correction. Pinning freezes the type against re-detection."""
        ctx = context()
        row = ctx.catalog.get_version(dataset_id, version)
        if row is None:
            raise HTTPException(404, "dataset or version not found")
        try:
            ctx.catalog.pin_column_type(row.id, column, semantic_type, role)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"dataset_id": dataset_id, "column": column, "semantic_type": semantic_type}

    @app.get("/api/datasets/{dataset_id}/lineage")
    def lineage(dataset_id: str) -> list[dict]:
        return [
            {"id": s.id, "op": s.op, "plugin_id": s.plugin_id, "params": s.params,
             "inputs": s.inputs, "outputs": s.outputs, "status": s.status,
             "rows": s.rows_committed, "cost": s.cost,
             "created_at": s.created_at.isoformat()}
            for s in context().catalog.lineage(dataset_id)
        ]

    @app.get("/api/datasets/{dataset_id}/related")
    def related(dataset_id: str) -> dict:
        """Immediate parents and derived children of a dataset."""
        try:
            return lineage_service.related(context().catalog, dataset_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/datasets/{dataset_id}/suggestions")
    def suggestions(dataset_id: str, kind: str | None = None) -> list[dict]:
        ctx = context()
        try:
            found = inspect_service.suggest(ctx, dataset_id, kinds=(kind,) if kind else ())
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return [s.model_dump() for s in found]

    # --- auth ------------------------------------------------------------
    @app.post("/api/auth/login")
    def login(req: LoginRequest) -> dict:
        """Exchange a password for a session token.

        One message for every failure, and the same work done whether or not
        the username exists, so this cannot be used to enumerate accounts.
        """
        settings = context().settings
        accounts = users.resolve_users(settings.users)
        if not users.authenticate(req.username, req.password, accounts):
            raise HTTPException(401, "wrong username or password")
        secret = users.session_secret(settings.data_dir, settings.session_secret)
        token = users.issue_session(req.username, secret, settings.session_hours)
        return {"token": token, "username": req.username,
                "expires_in_hours": settings.session_hours}

    @app.get("/api/auth/me")
    def whoami(request: Request) -> dict:
        """Who the current credential belongs to. Also the "am I still logged
        in" check the UI makes on load."""
        return {"username": getattr(request.state, "user", None)}

    # --- operations ------------------------------------------------------
    @app.post("/api/operations", response_model=OperationAccepted, status_code=202)
    def create_operation(req: OperationRequest) -> OperationAccepted:
        """The single entry point for every data-producing plugin invocation."""
        ctx = context()
        try:
            return submit_operation(ctx, req)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/inspect")
    def run_inspect(req: InspectRequest) -> dict:
        """The synchronous twin of /api/operations, for inspect-mode plugins."""
        ctx = context()
        plugin = REGISTRY.get(req.plugin_id)
        if plugin is None:
            raise HTTPException(404, f"unknown plugin: {req.plugin_id}")
        try:
            if plugin.kind == "visualizer":
                return inspect_service.render_viz(
                    ctx, req.plugin_id, req.dataset_id, req.params, req.version, req.limit
                ).model_dump()
            raise HTTPException(400, f"{req.plugin_id} is not invocable via /api/inspect")
        except (KeyError, QueryError) as exc:
            raise HTTPException(400, str(exc)) from exc

    # --- query -----------------------------------------------------------
    @app.post("/api/query", response_model=QueryResult)
    def query(spec: QuerySpec) -> QueryResult:
        try:
            return run_query(context(), spec)
        except QueryError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/query/sql", response_model=QueryResult)
    def query_sql(req: SqlRequest) -> QueryResult:
        try:
            return run_sql(context(), req.sql, req.limit)
        except UnsafeSQLError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - SQL errors belong to the user
            raise HTTPException(400, f"{type(exc).__name__}: {exc}") from exc

    # --- jobs ------------------------------------------------------------
    def _job_dict(job) -> dict:
        return {
            "id": job.id, "title": job.title, "status": job.status,
            "progress": job.progress, "logs": job.logs, "error": job.error,
            "created_at": job.created_at.isoformat(),
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        }

    @app.get("/api/jobs")
    def list_jobs(limit: int = 50) -> list[dict]:
        return [_job_dict(j) for j in context().catalog.list_jobs(limit)]

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        job = context().catalog.get_job(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        out = _job_dict(job)
        out["steps"] = [
            {"id": s.id, "op": s.op, "plugin_id": s.plugin_id, "status": s.status,
             "rows_committed": s.rows_committed, "parts_committed": s.parts_committed,
             "cost": s.cost, "outputs": s.outputs, "error": s.error}
            for s in context().catalog.list_steps(job_id)
        ]
        return out

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict:
        ctx = context()
        if ctx.catalog.get_job(job_id) is None:
            raise HTTPException(404, "job not found")
        ctx.catalog.update_job(job_id, cancel_requested=True)
        return {"cancel_requested": True}

    @app.get("/api/jobs/{job_id}/stream")
    async def stream_job(job_id: str) -> StreamingResponse:
        ctx = context()

        async def events():
            last = None
            for _ in range(3600):  # ~30 min ceiling
                job = ctx.catalog.get_job(job_id)
                if job is None:
                    yield f"data: {json.dumps({'error': 'not found'})}\n\n"
                    return
                payload = _job_dict(job)
                serialised = json.dumps(payload)
                if serialised != last:
                    last = serialised
                    yield f"data: {serialised}\n\n"
                if job.status in TERMINAL_JOB_STATUSES:
                    return
                await asyncio.sleep(0.5)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # --- agent -----------------------------------------------------------
    @app.get("/api/agent/tools")
    def agent_tools() -> list[dict]:
        """The agent's tool surface, for transparency in the UI."""
        from ..services.agent import build_tools

        return [
            {"name": t.name, "description": t.description, "scope": t.scope}
            for t in build_tools(context(), scope="full")
        ]

    @app.post("/api/agent/estimate")
    def agent_estimate(req: AgentChatRequest) -> dict:
        """What a run will cost, so the user can decline before spending anything."""
        from ..services.agent import AnalysisAgent

        return AnalysisAgent(context(), scope="full").estimate(req.message, req.history)

    @app.post("/api/agent/chat")
    async def agent_chat(req: AgentChatRequest) -> StreamingResponse:
        """Stream the agent's work as SSE so the user watches tools run."""
        from ..services.agent import AnalysisAgent

        ctx = context()

        async def events():
            try:
                agent = AnalysisAgent(ctx, scope="full")
            except Exception as exc:  # noqa: BLE001 - e.g. no API key configured
                yield f"data: {json.dumps({'type': 'error', 'text': str(exc)})}\n\n"
                return
            try:
                # The SDK call is synchronous; run the generator in a worker thread
                # so the event loop keeps serving other requests.
                for turn in await asyncio.to_thread(
                    lambda: list(agent.run(req.message, req.history))
                ):
                    payload = {
                        "type": turn.type, "text": turn.text,
                        "tool_name": turn.tool_name, "tool_input": turn.tool_input,
                        "tool_result": turn.tool_result,
                    }
                    yield f"data: {json.dumps(payload, default=str)}\n\n"
            except Exception as exc:  # noqa: BLE001
                err = {"type": "error", "text": f"{type(exc).__name__}: {exc}"}
                yield f"data: {json.dumps(err)}\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # --- dashboards ------------------------------------------------------
    @app.get("/api/dashboards")
    def list_dashboards() -> list[dict]:
        return [
            {"id": d.id, "name": d.name, "description": d.description,
             "panels": d.panels, "updated_at": d.updated_at.isoformat()}
            for d in context().catalog.list_dashboards()
        ]

    @app.get("/api/dashboards/{dashboard_id}")
    def get_dashboard(dashboard_id: str) -> dict:
        d = context().catalog.get_dashboard(dashboard_id)
        if d is None:
            raise HTTPException(404, "dashboard not found")
        return {"id": d.id, "name": d.name, "description": d.description,
                "panels": d.panels, "updated_at": d.updated_at.isoformat()}

    @app.post("/api/dashboards")
    def save_dashboard(req: DashboardRequest) -> dict:
        d = context().catalog.save_dashboard(
            name=req.name, panels=req.panels, description=req.description,
            dashboard_id=req.id,
        )
        return {"id": d.id, "name": d.name}


class SpaStaticFiles(StaticFiles):
    """Static files with a single-page-app fallback.

    ``html=True`` alone only serves index.html for directory paths, so a client
    -side route like /datasets/abc/explore 404s -- in-app navigation works but a
    refresh or a shared link does not. Unknown paths therefore fall back to
    index.html and let the router resolve them.

    Two kinds of path are excluded, both because answering them with HTML turns
    a missing thing into a baffling one:

    * ``/api/`` -- a typo'd endpoint would reach the caller as a JSON parse
      error rather than a 404.
    * anything that names a file -- a missing asset would be served as an HTML
      page with a 200, and whatever tried to load it fails somewhere far away.
      This is not hypothetical: a missing worker chunk was served as index.html,
      the module worker died parsing HTML as JavaScript, and the only visible
      symptom was a map that drew no points.

    A client-side route has no file extension, which is what separates the two.
    """

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            if path.startswith("api/") or "." in path.rsplit("/", 1)[-1]:
                raise
            return await super().get_response("index.html", scope)


def _mount_frontend(app: FastAPI, settings) -> None:
    """Serve the built SPA in production, so the whole app is one container.

    Mounted last so it never shadows /api.
    """
    configured = settings.static_dir
    candidates = [configured] if configured else [
        Path("/app/static"),
        Path(__file__).resolve().parents[4] / "frontend" / "dist",
    ]
    for dist in candidates:
        if dist and dist.is_dir():
            app.mount("/", SpaStaticFiles(directory=dist, html=True), name="frontend")
            return


app = create_app()
