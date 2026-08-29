"""The operations service: one uniform path for every data-producing plugin call.

``POST /api/operations`` maps directly onto :func:`submit_operation`. The agent
calls the same functions. Nothing here knows about HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..core.profile import DatasetProfile
from ..jobs.context import JobCtx
from ..jobs.executor import run_transform
from ..plugins.base import REGISTRY
from ..plugins.builtin.readers import pick_reader
from ..plugins.kinds import AggregateCtx, AggregatePlan, Aggregator, Reader, Transform
from ..query.compiler import quote_ident
from ..query.spec import QuerySpec
from ..storage.base import VersionRef
from .browse import assert_readable_uri
from .context import AppContext
from .model import make_model_client
from .profiler import compute_stats, profile_columns, sniffer_ambiguities


class DatasetInput(BaseModel):
    dataset_id: str
    version: int | None = None


class OperationRequest(BaseModel):
    """The single request shape for import / transform / aggregate / join."""

    op: Literal["import", "transform", "aggregate", "join"]
    plugin_id: str = ""
    inputs: list[DatasetInput] = []
    params: dict[str, Any] = {}
    # For op="aggregate": materialise this query directly instead of asking a
    # plugin to build one. This is what turns a chart into a dataset -- the chart
    # already has a QuerySpec, it just was not persisted anywhere.
    from_query: QuerySpec | None = None
    # For op="import".
    uri: str = ""
    name: str = ""
    output_name: str = ""
    dry_run: bool = False
    max_cost_usd: float | None = Field(
        default=None, description="Abort an external-mode step once this is spent"
    )


class OperationAccepted(BaseModel):
    job_id: str
    step_id: str
    dataset_id: str = ""


class DryRunResult(BaseModel):
    """What ``dry_run=true`` returns: a sample plus an extrapolated estimate, so a
    non-technical user never launches an expensive job blind."""

    columns: list[str]
    rows: list[list[Any]]
    sampled_rows: int
    total_rows: int
    estimated_cost_usd: float | None = None
    estimated_seconds: float | None = None
    notes: list[str] = []


@dataclass
class _Prepared:
    profile: DatasetProfile
    source_sql: str


def _prepare(ctx: AppContext, inp: DatasetInput) -> _Prepared:
    profile = ctx.catalog.get_profile(inp.dataset_id, inp.version)
    if profile is None:
        raise KeyError(f"dataset {inp.dataset_id} has no version {inp.version}")
    source = ctx.resolve_source(inp.dataset_id, inp.version)
    return _Prepared(profile=profile, source_sql=source.sql)


def profile_and_store(
    ctx: AppContext, conn, dataset_id: str, version_id: str, source_sql: str,
    columns: list[tuple[str, str]], previous: list | None = None,
    warnings: dict[str, str] | None = None,
) -> None:
    stats = compute_stats(conn, source_sql, columns, ctx.settings.profile_sample_rows)
    profiles = profile_columns(stats, previous=previous)
    # A warning is a fact about how the column was read at import, so it belongs
    # to the column for as long as the column survives. Recomputing it later is
    # impossible -- the raw text is gone after the first version -- so a column
    # carried through a transform keeps the warning it arrived with. Without
    # this, the first transform on a dataset quietly clears every warning on it.
    carried = {c.name: c.warning for c in (previous or []) if c.warning}
    for prof in profiles:
        prof.warning = (warnings or {}).get(prof.name) or carried.get(prof.name)
    ctx.catalog.set_columns(version_id, profiles)


def _sniffer_warnings(conn, reader_cls, uri: str, req_params: dict,
                      columns: list[tuple[str, str]], job_ctx: JobCtx) -> dict[str, str]:
    """Check what the reader decided about ambiguous dates, while the text lasts.

    Only possible for readers that can hand back the unconverted text -- CSV,
    via all_varchar. For the rest there is nothing to compare against, and the
    empty dict says so honestly rather than implying the column was checked.
    """
    temporal = [n for n, t in columns if t.upper().startswith(("DATE", "TIMESTAMP"))]
    if not temporal or "all_varchar" not in reader_cls.Params.model_fields:
        return {}
    try:
        raw_params = reader_cls.parse_params({**req_params, "all_varchar": True})
        raw = reader_cls().to_relation(conn, uri, raw_params)
        typed = reader_cls().to_relation(
            conn, uri, reader_cls.parse_params(req_params))
        found = sniffer_ambiguities(conn, raw.sql_query(), typed.sql_query(), temporal)
    except Exception as exc:  # noqa: BLE001 -- a diagnostic must not fail the import
        job_ctx.log(f"date-format check skipped: {exc}")
        return {}
    for column, message in found.items():
        job_ctx.log(f"WARNING {column}: {message}")
    return found


# --------------------------------------------------------------------------- #
# import
# --------------------------------------------------------------------------- #
def run_import(ctx: AppContext, req: OperationRequest, job_ctx: JobCtx) -> str:
    # Same containment as the preview route: an import names a path the server
    # will open, so it has to be one the server was told it may open.
    assert_readable_uri(req.uri, ctx.settings)
    reader_cls = (
        REGISTRY.require(req.plugin_id) if req.plugin_id else pick_reader(req.uri)
    )
    if reader_cls is None:
        raise ValueError(f"no reader can handle {req.uri!r}; pass plugin_id explicitly")
    if not issubclass(reader_cls, Reader):
        raise TypeError(f"{reader_cls.id} is not a reader")

    reader: Reader = reader_cls()  # type: ignore[assignment]
    params = reader_cls.parse_params(req.params)
    name = req.name or req.uri.rsplit("/", 1)[-1].split(".")[0] or "dataset"

    with ctx.warehouse.cur() as conn:
        job_ctx.log(f"reading {req.uri} with {reader_cls.id}")
        rel = reader.to_relation(conn, req.uri, params)
        view = f"_dq_import_{job_ctx.step_id}"
        rel.create_view(view, replace=True)
        columns = list(zip(rel.columns, [str(t) for t in rel.types], strict=True))

        dataset = ctx.catalog.create_dataset(
            name=name, kind="source", source_uri=req.uri
        )
        ref = VersionRef(dataset_id=dataset.id, version=1)
        with ctx.warehouse.ddl_lock:
            stored = ctx.storage.write_relation(ref, f"SELECT * FROM {view}", conn)
        job_ctx.rows_total = stored.rows
        job_ctx.progress(stored.rows, force=True)

        version = ctx.catalog.add_version(
            dataset_id=dataset.id, version=1, stored=stored,
            columns_schema=[{"name": n, "physical_type": t} for n, t in columns],
            row_count=stored.rows, step_id=job_ctx.step_id,
        )
        job_ctx.log(f"imported {stored.rows:,} rows into {name}; profiling")
        warnings = _sniffer_warnings(conn, reader_cls, req.uri, req.params, columns, job_ctx)
        profile_and_store(
            ctx, conn, dataset.id, version.id, ctx.storage.sql_source(stored), columns,
            warnings=warnings,
        )
        conn.execute(f"DROP VIEW IF EXISTS {view}")

    ctx.catalog.update_step(
        job_ctx.step_id, status="succeeded", rows_committed=stored.rows,
        outputs=[{"dataset_id": dataset.id, "version": 1}],
    )
    return dataset.id


# --------------------------------------------------------------------------- #
# transform  (normalize / extract / annotate)
# --------------------------------------------------------------------------- #
def run_transform_op(ctx: AppContext, req: OperationRequest, job_ctx: JobCtx) -> str:
    plugin_cls = REGISTRY.require(req.plugin_id)
    if not issubclass(plugin_cls, Transform):
        raise TypeError(f"{req.plugin_id} is not a transform")
    plugin: Transform = plugin_cls()  # type: ignore[assignment]
    params = plugin_cls.parse_params(req.params)

    if not req.inputs:
        raise ValueError("transform requires an input dataset")
    inp = req.inputs[0]
    prepared = _prepare(ctx, inp)

    dataset = ctx.catalog.get_dataset(inp.dataset_id)
    assert dataset is not None
    new_version = ctx.catalog.next_version(inp.dataset_id)
    ref = VersionRef(dataset_id=inp.dataset_id, version=new_version)

    step = ctx.catalog.get_step(job_ctx.step_id)
    resume_parts = step.parts_committed if step else 0
    resume_rows = step.rows_committed if step else 0

    job_ctx.rows_total = prepared.profile.row_count
    model = make_model_client(ctx.settings) if plugin.mode == "external" else None

    with ctx.warehouse.cur() as conn:
        with ctx.warehouse.ddl_lock:
            result = run_transform(
                plugin=plugin, params=params, conn=conn,
                source_sql=prepared.source_sql, profile=prepared.profile,
                storage=ctx.storage, ref=ref, ctx=job_ctx, settings=ctx.settings,
                resolve=ctx.resolve_source,
                resume_parts=resume_parts, resume_rows=resume_rows,
                model=model, max_cost_usd=req.max_cost_usd,
            )
        source = ctx.storage.sql_source(result.stored)
        rel = conn.sql(f"SELECT * FROM {source} LIMIT 0")
        columns = list(zip(rel.columns, [str(t) for t in rel.types], strict=True))

        version = ctx.catalog.add_version(
            dataset_id=inp.dataset_id, version=new_version, stored=result.stored,
            columns_schema=[{"name": n, "physical_type": t} for n, t in columns],
            row_count=result.rows, step_id=job_ctx.step_id,
        )
        # Re-profile, carrying forward any human-pinned semantic types.
        profile_and_store(
            ctx, conn, inp.dataset_id, version.id, source, columns,
            previous=prepared.profile.columns,
        )

    ctx.catalog.update_step(
        job_ctx.step_id, status="succeeded", rows_committed=result.rows,
        cost=job_ctx.cost.as_dict(),
        outputs=[{"dataset_id": inp.dataset_id, "version": new_version}],
    )
    job_ctx.log(f"{plugin.id}: wrote version {new_version} ({result.rows:,} rows)")
    return inp.dataset_id


# --------------------------------------------------------------------------- #
# aggregate
# --------------------------------------------------------------------------- #
def run_aggregate_op(ctx: AppContext, req: OperationRequest, job_ctx: JobCtx) -> str:
    inp = req.inputs[0]
    prepared = _prepare(ctx, inp)

    if req.from_query is not None:
        # Materialising a query someone already has -- a chart's, typically. No
        # plugin is involved, so there is no trusted source for `derive`.
        if not req.from_query.is_aggregate:
            raise ValueError(
                "only an aggregating query can be saved as a dataset; this one "
                "returns raw rows, so it would just copy the source"
            )
        plan = AggregatePlan(spec=req.from_query.model_copy())
        title = "query"
        plugin_slug = "agg"
    else:
        plugin_cls = REGISTRY.require(req.plugin_id)
        if not issubclass(plugin_cls, Aggregator):
            raise TypeError(f"{req.plugin_id} is not an aggregator")
        plugin: Aggregator = plugin_cls()  # type: ignore[assignment]
        params = plugin_cls.parse_params(req.params)
        plan = plugin.plan(AggregateCtx(profile=prepared.profile, params=params))
        title = plugin.title
        plugin_slug = plugin.id.split(".")[-1]

    plan.spec.dataset = inp.dataset_id
    plan.spec.version = inp.version

    compiled = ctx.compiler().compile(plan.spec)
    agg_sql = compiled.sql
    if plan.derive:
        derived = ", ".join(f"({expr}) AS {quote_ident(alias)}"
                            for alias, expr in plan.derive.items())
        agg_sql = f"SELECT *, {derived} FROM ({agg_sql}) _agg"

    src_name = ctx.catalog.get_dataset(inp.dataset_id)
    base = src_name.name if src_name else "ds"
    out_name = req.output_name or f"{base}_{plugin_slug}"

    dataset = ctx.catalog.create_dataset(
        name=out_name, kind="aggregate",
        description=f"{title} over {src_name.name if src_name else inp.dataset_id}",
    )
    ref = VersionRef(dataset_id=dataset.id, version=1)
    with ctx.warehouse.cur() as conn:
        with ctx.warehouse.ddl_lock:
            stored = ctx.storage.write_relation(ref, agg_sql, conn, compiled.params)
        if plan.spec.limit is not None and stored.rows >= plan.spec.limit:
            # The result stopped exactly on the limit, so it is almost certainly
            # cut short. Left alone this is the worst kind of wrong: a complete
            # -looking table that is missing rows, which then annotates only
            # part of the data it is joined back to.
            raise ValueError(
                f"the aggregate produced {stored.rows:,} rows and hit its limit "
                f"of {plan.spec.limit:,}, so it is truncated and incomplete. "
                "Group by fewer columns, or use a coarser time grain."
            )
        source = ctx.storage.sql_source(stored)
        rel = conn.sql(f"SELECT * FROM {source} LIMIT 0")
        columns = list(zip(rel.columns, [str(t) for t in rel.types], strict=True))
        version = ctx.catalog.add_version(
            dataset_id=dataset.id, version=1, stored=stored,
            columns_schema=[{"name": n, "physical_type": t} for n, t in columns],
            row_count=stored.rows, step_id=job_ctx.step_id,
        )
        # Carry semantic types across from the source: a grouped country column is
        # still a country, and that is what makes the result joinable.
        inherited = [c for c in prepared.profile.columns if c.name in dict(columns)]
        profile_and_store(ctx, conn, dataset.id, version.id, source, columns,
                          previous=[c.model_copy(update={"pinned": True}) for c in inherited])

    job_ctx.rows_total = stored.rows
    job_ctx.progress(stored.rows, force=True)
    ctx.catalog.update_step(
        job_ctx.step_id, status="succeeded", rows_committed=stored.rows,
        outputs=[{"dataset_id": dataset.id, "version": 1}],
    )
    job_ctx.log(f"created aggregate '{out_name}' ({stored.rows:,} rows)")
    return dataset.id


# --------------------------------------------------------------------------- #
# join
# --------------------------------------------------------------------------- #
class JoinParams(BaseModel):
    left_column: str
    right_column: str
    how: Literal["inner", "left"] = "left"
    # Columns to bring across from the right side; empty means all non-key columns.
    right_select: list[str] = []
    prefix: str = ""


def run_join_op(ctx: AppContext, req: OperationRequest, job_ctx: JobCtx) -> str:
    if len(req.inputs) != 2:
        raise ValueError("join requires exactly two inputs")
    p = JoinParams.model_validate(req.params)
    left, right = req.inputs[0], req.inputs[1]
    lprep, rprep = _prepare(ctx, left), _prepare(ctx, right)
    lsrc = ctx.resolve_source(left.dataset_id, left.version)
    rsrc = ctx.resolve_source(right.dataset_id, right.version)

    if p.left_column not in lsrc.columns:
        raise ValueError(f"left column {p.left_column!r} not found")
    if p.right_column not in rsrc.columns:
        raise ValueError(f"right column {p.right_column!r} not found")

    right_cols = p.right_select or [c for c in rsrc.columns if c != p.right_column]
    missing = [c for c in right_cols if c not in rsrc.columns]
    if missing:
        raise ValueError(f"right columns not found: {missing}")

    prefix = p.prefix or ""
    projection = ["l.*"] + [
        f"r.{quote_ident(c)} AS {quote_ident(prefix + c)}" for c in right_cols
    ]
    sql = (
        f"SELECT {', '.join(projection)} FROM {lsrc.sql} l "
        f"{'INNER' if p.how == 'inner' else 'LEFT'} JOIN {rsrc.sql} r "
        f"ON l.{quote_ident(p.left_column)} = r.{quote_ident(p.right_column)}"
    )

    lname = ctx.catalog.get_dataset(left.dataset_id)
    rname = ctx.catalog.get_dataset(right.dataset_id)
    out_name = req.output_name or f"{lname.name if lname else 'l'}_x_{rname.name if rname else 'r'}"

    dataset = ctx.catalog.create_dataset(
        name=out_name, kind="join", view_sql=sql,
        description=f"{p.how} join on {p.left_column} = {p.right_column}",
    )
    ref = VersionRef(dataset_id=dataset.id, version=1)
    with ctx.warehouse.cur() as conn:
        with ctx.warehouse.ddl_lock:
            stored = ctx.storage.write_relation(ref, sql, conn)
        source = ctx.storage.sql_source(stored)
        rel = conn.sql(f"SELECT * FROM {source} LIMIT 0")
        columns = list(zip(rel.columns, [str(t) for t in rel.types], strict=True))
        version = ctx.catalog.add_version(
            dataset_id=dataset.id, version=1, stored=stored,
            columns_schema=[{"name": n, "physical_type": t} for n, t in columns],
            row_count=stored.rows, step_id=job_ctx.step_id,
        )
        carried = {c.name: c for c in lprep.profile.columns}
        for c in rprep.profile.columns:
            carried.setdefault(prefix + c.name, c.model_copy(update={"name": prefix + c.name}))
        previous = [c.model_copy(update={"pinned": True})
                    for n, c in carried.items() if n in dict(columns)]
        profile_and_store(ctx, conn, dataset.id, version.id, source, columns,
                          previous=previous)

    job_ctx.rows_total = stored.rows
    job_ctx.progress(stored.rows, force=True)
    ctx.catalog.update_step(
        job_ctx.step_id, status="succeeded", rows_committed=stored.rows,
        outputs=[{"dataset_id": dataset.id, "version": 1}],
    )
    job_ctx.log(f"joined into '{out_name}' ({stored.rows:,} rows)")
    return dataset.id


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
_HANDLERS = {
    "import": run_import,
    "transform": run_transform_op,
    "aggregate": run_aggregate_op,
    "join": run_join_op,
}


def submit_operation(ctx: AppContext, req: OperationRequest) -> OperationAccepted:
    """Create the job + step rows and hand the work to the runner."""
    title = f"{req.op}: {req.plugin_id or req.uri or ''}".strip()
    job = ctx.catalog.create_job(title=title)
    plugin_version = ""
    if req.plugin_id:
        plugin_version = REGISTRY.require(req.plugin_id).version
    step = ctx.catalog.create_step(
        job_id=job.id, op=req.op, plugin_id=req.plugin_id,
        plugin_version=plugin_version, params=req.params,
        inputs=[i.model_dump() for i in req.inputs],
    )

    def work() -> None:
        job_ctx = JobCtx(catalog=ctx.catalog, job_id=job.id, step_id=step.id)
        ctx.catalog.update_step(step.id, status="running")
        try:
            _HANDLERS[req.op](ctx, req, job_ctx)
        except Exception as exc:
            ctx.catalog.update_step(
                step.id, status="failed", error=f"{type(exc).__name__}: {exc}"
            )
            raise

    assert ctx.runner is not None, "AppContext.runner is not configured"
    ctx.runner.submit(job.id, work)
    return OperationAccepted(job_id=job.id, step_id=step.id)
