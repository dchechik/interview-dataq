"""The operations service: one uniform path for every data-producing plugin call.

``POST /api/operations`` maps directly onto :func:`submit_operation`. The agent
calls the same functions. Nothing here knows about HTTP.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ..core.profile import ColumnProfile, DatasetProfile
from ..jobs.context import JobCtx
from ..jobs.executor import name_collisions, run_transform
from ..plugins.base import REGISTRY
from ..plugins.builtin import readers
from ..plugins.builtin.readers import pick_reader
from ..plugins.kinds import AggregateCtx, AggregatePlan, Aggregator, Reader, Transform
from ..query.compiler import quote_ident
from ..query.spec import QuerySpec
from ..storage.base import StoredRef, VersionRef
from .browse import assert_readable_uri
from .context import AppContext
from .import_plan import (
    ColumnPlan,
    cast_expr,
    cast_projection,
    explain_read_error,
    text_columns,
    validate_plan,
)
from .model import make_model_client
from .profiler import compute_stats, profile_columns, sniffer_ambiguities


class DatasetInput(BaseModel):
    dataset_id: str
    version: int | None = None


class OperationRequest(BaseModel):
    """The single request shape for import / transform / aggregate / join."""

    op: Literal["import", "transform", "aggregate", "join", "revert"]
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


@contextmanager
def _readable_read_errors():
    """Re-raise a malformed-row failure as the sentence a person can act on.

    Wrapped around the whole read because DuckDB's CSV scan is lazy: the error
    does not come from ``to_relation``, it comes from whichever query first
    pulls rows through it -- the cast measurement, or the write. Anything that
    is not a row-parsing failure passes through untouched, so a real bug still
    arrives with its own type and message.
    """
    try:
        yield
    except Exception as exc:
        hint = explain_read_error(exc)
        if hint is None:
            raise
        raise ValueError(hint) from exc


@contextmanager
def _new_dataset(ctx: AppContext, **fields):
    """Create a dataset, and take it away again if filling it fails.

    Everything between creating the row and adding its first version can fail --
    the read, the write, a post-condition. A dataset row with no version is a
    ghost: it lists in the UI, reports zero rows, cannot be queried, and cannot
    be explained. Removing it means a failed operation leaves the catalog as it
    found it.
    """
    dataset = ctx.catalog.create_dataset(**fields)
    try:
        yield dataset
    except BaseException:
        ctx.catalog.delete_dataset(dataset.id)
        raise


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
                      columns: list[tuple[str, str]], job_ctx: JobCtx,
                      skip: set[str] | None = None) -> dict[str, str]:
    """Check what the reader decided about ambiguous dates, while the text lasts.

    Only possible for readers that can hand back the unconverted text -- CSV,
    via all_varchar. For the rest there is nothing to compare against, and the
    empty dict says so honestly rather than implying the column was checked.
    """
    # A column whose format the import was told is not one the reader guessed
    # at, so the warning -- which exists to report an unrecorded guess -- would
    # be describing something that did not happen.
    temporal = [n for n, t in columns
                if t.upper().startswith(("DATE", "TIMESTAMP"))
                and n not in (skip or set())]
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


def _cast_losses(conn, view: str, plans: dict[str, ColumnPlan],
                 columns: list[tuple[str, str]], job_ctx: JobCtx) -> dict[str, str]:
    """Count the values a planned cast turns into NULL, before writing.

    The cast is deliberately survivable -- try_cast and try_strptime answer a
    value they cannot convert with NULL rather than ending the import, because
    one 'n/a' in 850,000 rows should not cost the file. That makes the count the
    only evidence anything was lost, so it is measured and attached to the
    column rather than left for someone to notice.
    """
    measured: list[tuple[str, str]] = []
    for name, source_type in columns:
        plan = plans.get(name)
        expr = cast_expr(name, plan, source_type) if plan else None
        if expr is not None:
            measured.append((name, expr))
    if not measured:
        return {}

    parts = []
    for i, (name, expr) in enumerate(measured):
        col = quote_ident(name)
        parts.append(f"count({col}) AS n{i}")
        parts.append(f"count({expr}) AS ok{i}")
    row = conn.execute(f"SELECT {', '.join(parts)} FROM {view}").fetchone()

    out: dict[str, str] = {}
    for i, (name, _) in enumerate(measured):
        had, kept = int(row[i * 2] or 0), int(row[i * 2 + 1] or 0)
        lost = had - kept
        if not had:
            continue
        job_ctx.log(f"{name}: converted {kept:,} of {had:,} values")
        if lost:
            out[name] = (
                f"{lost:,} of {had:,} values ({lost / had:.1%}) could not be read "
                f"as {plans[name].target_type} and are empty. Everything else "
                "converted; no rows were dropped."
            )
            job_ctx.log(f"WARNING {name}: {out[name]}")
    return out


def _planned_profiles(plans: list[ColumnPlan], columns: list[tuple[str, str]]) -> list:
    """The columns a person actually overrode, as pinned profiles.

    Only those: `profile_columns` treats a pin as final, and accepting a
    proposal is not an override. Freezing every column would stop re-detection
    on a dataset nobody had corrected, and would make the "pinned" marker in the
    UI mean nothing.
    """
    typed = dict(columns)
    out = []
    for plan in plans:
        if not plan.pinned or plan.name not in typed:
            continue
        out.append(ColumnProfile(
            name=plan.name, physical_type=typed[plan.name],
            semantic_type=plan.semantic_type, confidence=1.0,
            role=plan.role or "dimension", pinned=True,
        ))
    return out


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
    # The column plan rides in params alongside the reader's own settings.
    raw_params = {k: v for k, v in req.params.items() if k != "columns"}
    plans = [ColumnPlan.model_validate(c) for c in req.params.get("columns", [])]
    params = reader_cls.parse_params(raw_params)
    name = req.name or req.uri.rsplit("/", 1)[-1].split(".")[0] or "dataset"

    with _readable_read_errors(), ctx.warehouse.cur() as conn:
        job_ctx.log(f"reading {req.uri} with {reader_cls.id}")
        rel = reader.to_relation(conn, req.uri, params)
        columns = list(zip(rel.columns, [str(t) for t in rel.types], strict=True))

        by_name = {p.name: p for p in plans}
        if plans:
            validate_plan(plans, {n for n, _ in columns})
            # A column whose reading is the user's to choose has to arrive
            # unconverted: once the sniffer has turned 03/04/2016 into a DATE,
            # which of March or April it picked cannot be recovered.
            held = text_columns(by_name, columns)
            if held:
                job_ctx.log(f"reading {', '.join(held)} as text to apply the "
                            "chosen format")
                params = reader_cls.parse_params(
                    {**raw_params, "column_types": {c: "VARCHAR" for c in held}})
                rel = reader.to_relation(conn, req.uri, params)
                columns = list(
                    zip(rel.columns, [str(t) for t in rel.types], strict=True))

        view = f"_dq_import_{job_ctx.step_id}"
        rel.create_view(view, replace=True)
        projection = cast_projection(by_name, columns) if plans else "*"
        cast_losses = _cast_losses(conn, view, by_name, columns, job_ctx)

        with _new_dataset(ctx, name=name, kind="source",
                          source_uri=req.uri) as dataset:
            ref = VersionRef(dataset_id=dataset.id, version=1)
            with ctx.warehouse.ddl_lock:
                stored = ctx.storage.write_relation(
                    ref, f"SELECT {projection} FROM {view}", conn)
            # Re-derived from what was written, not from what was read: after a
            # cast the physical type is the whole point, and the pre-cast list
            # would record the type the plan set out to change.
            written = conn.sql(
                f"SELECT * FROM {ctx.storage.sql_source(stored)} LIMIT 0")
            columns = list(
                zip(written.columns, [str(t) for t in written.types], strict=True))
            job_ctx.rows_total = stored.rows
            job_ctx.progress(stored.rows, force=True)

            version = ctx.catalog.add_version(
                dataset_id=dataset.id, version=1, stored=stored,
                columns_schema=[{"name": n, "physical_type": t} for n, t in columns],
                row_count=stored.rows, step_id=job_ctx.step_id,
            )
            job_ctx.log(f"imported {stored.rows:,} rows into {name}; profiling")
            warnings = _sniffer_warnings(
                conn, reader_cls, req.uri, raw_params, columns, job_ctx,
                skip={p.name for p in plans if p.format})
            warnings.update(cast_losses)
            # Rows the reader was told to skip. Counted rather than assumed:
            # "skip the bad rows" is a reasonable thing to ask for and a
            # terrible thing to be given silently, since the resulting dataset
            # looks complete and is not.
            skipped = readers.rejected_rows(conn)
            if skipped:
                job_ctx.log(f"WARNING {skipped.describe()}")
            profile_and_store(
                ctx, conn, dataset.id, version.id, ctx.storage.sql_source(stored),
                columns, previous=_planned_profiles(plans, columns),
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

    with _new_dataset(
        ctx, name=out_name, kind="aggregate",
        description=f"{title} over {src_name.name if src_name else inp.dataset_id}",
    ) as dataset:
        ref = VersionRef(dataset_id=dataset.id, version=1)
        with ctx.warehouse.cur() as conn:
            with ctx.warehouse.ddl_lock:
                stored = ctx.storage.write_relation(ref, agg_sql, conn, compiled.params)
            if (plan.spec.limit is not None and not plan.limited_on_purpose
                    and stored.rows >= plan.spec.limit):
                # The result stopped exactly on the limit, so it is almost certainly
                # cut short. Left alone this is the worst kind of wrong: a complete
                # -looking table that is missing rows, which then annotates only
                # part of the data it is joined back to.
                with ctx.warehouse.ddl_lock:
                    ctx.storage.drop(stored, conn)
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
class JoinKey(BaseModel):
    """One column pair the two sides are matched on."""

    left: str
    right: str


class JoinParams(BaseModel):
    """What to join on, and what to bring across.

    The key is a *list* of pairs because one pair is often not enough to identify
    a row. A table of per-``(user, activity_type)`` counts has neither column
    unique on its own -- each user appears once per activity, each activity once
    per user -- so matching on either alone multiplies rows, and only the pair
    annotates them. That case is what ``allow_fanout`` was added for; a composite
    key answers it properly instead.
    """

    on: list[JoinKey] = []
    # The one-pair spelling, which is what a suggestion's action and the agent's
    # create_join emit. Kept rather than migrated: a Suggestion carries a literal
    # request body, and there is no reason a single-column key should have to be
    # written as a list.
    left_column: str = ""
    right_column: str = ""
    how: Literal["inner", "left"] = "left"
    # Columns to bring across from the right side; empty means all non-key columns.
    right_select: list[str] = []
    prefix: str = ""
    # A join whose right side has repeated keys multiplies rows rather than
    # annotating them. Almost always that is a mistake -- the wrong key, or a
    # key that needs a second column -- so it is refused unless asked for.
    allow_fanout: bool = False

    @model_validator(mode="after")
    def _normalise_key(self) -> JoinParams:
        """Fold the one-pair spelling into ``on``, so nothing downstream reads both."""
        if not self.on:
            if not (self.left_column and self.right_column):
                raise ValueError(
                    "a join needs a key: either on=[{left, right}, ...] or "
                    "left_column and right_column"
                )
            self.on = [JoinKey(left=self.left_column, right=self.right_column)]
        return self

    @property
    def key_description(self) -> str:
        return ", ".join(f"{k.left} = {k.right}" for k in self.on)


def run_join_op(ctx: AppContext, req: OperationRequest, job_ctx: JobCtx) -> str:
    if len(req.inputs) != 2:
        raise ValueError("join requires exactly two inputs")
    p = JoinParams.model_validate(req.params)
    left, right = req.inputs[0], req.inputs[1]
    lprep, rprep = _prepare(ctx, left), _prepare(ctx, right)
    lsrc = ctx.resolve_source(left.dataset_id, left.version)
    rsrc = ctx.resolve_source(right.dataset_id, right.version)

    for k in p.on:
        if k.left not in lsrc.columns:
            raise ValueError(f"left column {k.left!r} not found")
        if k.right not in rsrc.columns:
            raise ValueError(f"right column {k.right!r} not found")

    key_columns = {k.right for k in p.on}
    right_cols = p.right_select or [c for c in rsrc.columns if c not in key_columns]
    missing = [c for c in right_cols if c not in rsrc.columns]
    if missing:
        raise ValueError(f"right columns not found: {missing}")

    prefix = p.prefix or ""
    added = [prefix + c for c in right_cols]
    # Two result columns of the same name is something DuckDB will happily
    # return and the catalog cannot store -- one column per name per version.
    # Caught here, where there is still a remedy to name, rather than as an
    # integrity error after the whole join has been written.
    collisions = name_collisions(lsrc.columns, added)
    if collisions:
        raise ValueError(
            f"joining would produce duplicate columns: {', '.join(collisions)}. "
            "Set a prefix, or name the columns to bring across in right_select."
        )

    projection = ["l.*"] + [
        f"r.{quote_ident(c)} AS {quote_ident(prefix + c)}" for c in right_cols
    ]
    on = " AND ".join(
        f"l.{quote_ident(k.left)} = r.{quote_ident(k.right)}" for k in p.on
    )
    sql = (
        f"SELECT {', '.join(projection)} FROM {lsrc.sql} l "
        f"{'INNER' if p.how == 'inner' else 'LEFT'} JOIN {rsrc.sql} r "
        f"ON {on}"
    )

    lname = ctx.catalog.get_dataset(left.dataset_id)
    rname = ctx.catalog.get_dataset(right.dataset_id)
    out_name = req.output_name or f"{lname.name if lname else 'l'}_x_{rname.name if rname else 'r'}"

    with _new_dataset(
        ctx, name=out_name, kind="join", view_sql=sql,
        description=f"{p.how} join on {p.key_description}",
    ) as dataset:
        ref = VersionRef(dataset_id=dataset.id, version=1)
        with ctx.warehouse.cur() as conn:
            left_rows = conn.execute(f"SELECT count(*) FROM {lsrc.sql}").fetchone()[0]
            with ctx.warehouse.ddl_lock:
                stored = ctx.storage.write_relation(ref, sql, conn)

            if not p.allow_fanout and stored.rows > left_rows:
                # The right side repeats the join key, so each left row matched
                # several and this is a multiplication rather than an
                # annotation. Left quiet it is expensive and invisible: every
                # count downstream is inflated and nothing says why.
                with ctx.warehouse.ddl_lock:
                    ctx.storage.drop(stored, conn)
                keys = ", ".join(k.right for k in p.on)
                raise ValueError(
                    f"joining on {keys} turned {left_rows:,} rows into "
                    f"{stored.rows:,}: the right side has more than one row per "
                    f"({keys}), so each row matched several. Add the columns "
                    "that make the key unique there, use enrich.features to "
                    "attach it as a new version of this dataset instead, or "
                    "pass allow_fanout to keep this."
                )
            job_ctx.log(f"joined {left_rows:,} rows -> {stored.rows:,}")

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
# revert
# --------------------------------------------------------------------------- #
def run_revert_op(ctx: AppContext, req: OperationRequest, job_ctx: JobCtx) -> str:
    """Bring an earlier version back, by writing its data as a new version.

    Reverting forwards rather than moving a pointer, for three reasons that are
    all properties of how versions are stored here:

    * Version numbers are handed out by ``next_version`` and never reused, and
      a step's provenance records the number it wrote. Moving ``latest_version``
      backwards would point a later write at a number some step already claims.
    * A version owns its bytes -- deleting one drops them -- so two version rows
      cannot share a ``StoredRef`` without one delete destroying the other.
    * History stays append-only, which means the revert is itself an ordinary
      version, and reverting the revert is the same operation again.

    So this is the "revert as a new commit" model: nothing is rewritten, and the
    version you came from is still there afterwards.
    """
    if not req.inputs:
        raise ValueError("revert requires an input dataset")
    inp = req.inputs[0]
    if inp.version is None:
        raise ValueError("revert requires the version to go back to")

    dataset = ctx.catalog.get_dataset(inp.dataset_id)
    if dataset is None:
        raise KeyError(f"unknown dataset: {inp.dataset_id}")
    source_row = ctx.catalog.get_version(inp.dataset_id, inp.version)
    if source_row is None:
        raise ValueError(f"{dataset.name} has no version {inp.version}")
    current = ctx.catalog.get_version(inp.dataset_id)
    if current is not None and current.version == inp.version:
        raise ValueError(
            f"v{inp.version} is already the current version of {dataset.name}; "
            "reverting to it would copy the data for no change"
        )

    new_version = ctx.catalog.next_version(inp.dataset_id)
    ref = VersionRef(dataset_id=inp.dataset_id, version=new_version)
    job_ctx.rows_total = source_row.row_count

    with ctx.warehouse.cur() as conn:
        source_sql = ctx.storage.sql_source(StoredRef(**source_row.stored_ref))
        job_ctx.log(
            f"copying v{inp.version} of {dataset.name} "
            f"({source_row.row_count:,} rows) into v{new_version}"
        )
        with ctx.warehouse.ddl_lock:
            stored = ctx.storage.write_relation(ref, f"SELECT * FROM {source_sql}", conn)

        written = conn.sql(f"SELECT * FROM {ctx.storage.sql_source(stored)} LIMIT 0")
        columns = list(zip(written.columns, [str(t) for t in written.types], strict=True))
        version = ctx.catalog.add_version(
            dataset_id=inp.dataset_id, version=new_version, stored=stored,
            columns_schema=[{"name": n, "physical_type": t} for n, t in columns],
            row_count=stored.rows, step_id=job_ctx.step_id,
        )

        # The column metadata is copied, not recomputed. It describes data that
        # is byte-identical, so a re-profile would spend a full scan to
        # rediscover what the catalog already holds -- and, being sampled, would
        # answer with subtly different stats for data that did not change. It
        # would also lose the parts that cannot be recomputed at all: a pin is a
        # decision somebody made, and an import warning is a fact about text
        # that no longer exists.
        restored = ctx.catalog.get_profile(inp.dataset_id, inp.version)
        if restored is not None:
            ctx.catalog.set_columns(version.id, restored.columns)

    job_ctx.progress(stored.rows, force=True)
    ctx.catalog.update_step(
        job_ctx.step_id, status="succeeded", rows_committed=stored.rows,
        outputs=[{"dataset_id": inp.dataset_id, "version": new_version}],
    )
    job_ctx.log(
        f"reverted {dataset.name} to v{inp.version}, written as v{new_version} "
        f"({stored.rows:,} rows)"
    )
    return inp.dataset_id


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
_HANDLERS = {
    "import": run_import,
    "transform": run_transform_op,
    "aggregate": run_aggregate_op,
    "join": run_join_op,
    "revert": run_revert_op,
}


def submit_operation(ctx: AppContext, req: OperationRequest) -> OperationAccepted:
    """Create the job + step rows and hand the work to the runner."""
    title = f"{req.op}: {req.plugin_id or req.uri or ''}".strip()
    if req.op == "revert" and req.inputs:
        # The generic title would be a bare "revert:" -- no plugin, no uri. What
        # a reader wants from the job list is which dataset went back to when.
        src = ctx.catalog.get_dataset(req.inputs[0].dataset_id)
        title = (f"revert: {src.name if src else req.inputs[0].dataset_id} "
                 f"to v{req.inputs[0].version}")
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
