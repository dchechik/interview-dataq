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

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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
from ..services import inspect as inspect_service
from ..services.context import AppContext, build_context
from ..services.operations import OperationAccepted, OperationRequest, submit_operation
from ..services.query import run_query, run_sql

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

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_credentials=True,
        allow_headers=["*"],
    )
    _register_routes(app)
    _mount_frontend(app)
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

    @app.get("/api/datasets", response_model=list[DatasetSummary])
    def list_datasets() -> list[DatasetSummary]:
        ctx = context()
        return [_summary(ctx, d) for d in ctx.catalog.list_datasets()]

    @app.get("/api/datasets/{dataset_id}", response_model=DatasetSummary)
    def get_dataset(dataset_id: str) -> DatasetSummary:
        ctx = context()
        ds = ctx.catalog.get_dataset(dataset_id)
        if ds is None:
            raise HTTPException(404, "dataset not found")
        return _summary(ctx, ds)

    @app.delete("/api/datasets/{dataset_id}")
    def delete_dataset(dataset_id: str) -> dict:
        context().catalog.delete_dataset(dataset_id)
        return {"deleted": dataset_id}

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

    @app.get("/api/datasets/{dataset_id}/suggestions")
    def suggestions(dataset_id: str, kind: str | None = None) -> list[dict]:
        ctx = context()
        try:
            found = inspect_service.suggest(ctx, dataset_id, kinds=(kind,) if kind else ())
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return [s.model_dump() for s in found]

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


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built SPA in production, so the whole app is one container."""
    dist = Path(__file__).resolve().parents[3] / "static"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")


app = create_app()
