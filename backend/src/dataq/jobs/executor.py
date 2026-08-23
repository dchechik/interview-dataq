"""The mode dispatcher.

This is the module that makes "normalize" and "extract" the same thing. A
``Transform`` produces a new ``DatasetVersion`` regardless of whether it emitted
SQL, processed Arrow batches, or called an LLM; the differences are confined here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from ..config import Settings
from ..core.profile import DatasetProfile
from ..plugins.kinds import SqlPlan, Transform, TransformCtx
from ..query.compiler import quote_ident
from ..storage.base import StorageBackend, StoredRef, VersionRef
from .context import JobCtx
from .external import ExternalRunner, ResultCache


@dataclass
class ExecResult:
    stored: StoredRef
    rows: int


def build_projection(plan: SqlPlan, source_columns: list[str]) -> str:
    """Assemble the SELECT list for a pushdown transform.

    The runtime owns the projection, not the plugin, so a transform cannot
    accidentally drop columns, reorder rows or change cardinality.
    """
    dropped = set(plan.drop)
    parts: list[str] = []
    for name in source_columns:
        if name in dropped:
            continue
        if name in plan.replace:
            parts.append(f"({plan.replace[name]}) AS {quote_ident(name)}")
        else:
            parts.append(quote_ident(name))
    for name, expr in plan.add.items():
        if name in dropped:
            continue
        parts.append(f"({expr}) AS {quote_ident(name)}")
    return ", ".join(parts) if parts else "*"


def run_pushdown_transform(
    plugin: Transform, params: Any, conn, source_sql: str, profile: DatasetProfile,
    storage: StorageBackend, ref: VersionRef, ctx: JobCtx,
) -> ExecResult:
    plan = plugin.sql(TransformCtx(conn=conn, source_sql=source_sql, profile=profile,
                                   params=params))
    projection = build_projection(plan, [c.name for c in profile.columns])
    sql = f"SELECT {projection} FROM {source_sql}"
    if plan.where:
        sql += f" WHERE {plan.where}"
    ctx.log(f"{plugin.id}: pushdown -> {sql[:200]}")
    stored = storage.write_relation(ref, sql, conn)
    ctx.progress(stored.rows, force=True)
    return ExecResult(stored=stored, rows=stored.rows)


def _augment_batch(batch: pa.RecordBatch, results: list[dict[str, Any]]) -> pa.RecordBatch:
    """Append per-row result columns to a batch, preserving the original columns."""
    if not results:
        return batch
    names = list(results[0].keys())
    arrays = list(batch.columns)
    schema_fields = list(batch.schema)
    for n in names:
        col = [r.get(n) for r in results]
        arr = pa.array(col)
        arrays.append(arr)
        schema_fields.append(pa.field(n, arr.type))
    return pa.RecordBatch.from_arrays(arrays, schema=pa.schema(schema_fields))


def run_streaming_transform(
    plugin: Transform, params: Any, conn, source_sql: str, profile: DatasetProfile,
    storage: StorageBackend, ref: VersionRef, ctx: JobCtx, settings: Settings,
    resume_parts: int = 0, resume_rows: int = 0,
    model=None, max_cost_usd: float | None = None,
) -> ExecResult:
    """Shared path for ``batch`` and ``external`` modes.

    Resume works by replaying the source with an OFFSET equal to the durable row
    watermark. DuckDB preserves insertion order for these scans, so the resumed run
    sees exactly the rows the interrupted one had not yet committed.
    """
    conn.execute("SET preserve_insertion_order=true")

    scan_sql = f"SELECT * FROM {source_sql}"
    if resume_rows:
        # LIMIT is required before OFFSET in DuckDB; -1 means "all remaining".
        scan_sql += f" LIMIT -1 OFFSET {int(resume_rows)}"
        ctx.log(f"{plugin.id}: resuming from row {resume_rows} (part {resume_parts})")

    reader = conn.sql(scan_sql).to_arrow_reader(settings.batch_rows)

    external_runner = None
    if plugin.mode == "external":
        external_runner = ExternalRunner(
            plugin=plugin, params=params, ctx=ctx, cache=ResultCache(conn),
            settings=settings, model=model, max_cost_usd=max_cost_usd,
        )
        loop = asyncio.new_event_loop()

    def transform(batch: pa.RecordBatch) -> pa.RecordBatch:
        if external_runner is None:
            return plugin.process(batch, params)
        rows = batch.to_pylist()
        results = loop.run_until_complete(external_runner.process(rows))
        return _augment_batch(batch, results)

    writer = None
    part = resume_parts
    rows_done = resume_rows
    buffer: list[pa.RecordBatch] = []
    try:
        for batch in reader:
            ctx.check_cancelled()
            out = transform(batch)
            if writer is None:
                writer = storage.open_writer(ref, out.schema, conn)
                # Drop anything at or beyond the watermark: a part may have been
                # half-written when the process died.
                writer.discard_from(part)
            buffer.append(out)
            rows_done += out.num_rows
            ctx.progress(rows_done)
            if len(buffer) >= settings.checkpoint_every_batches:
                writer.write_part(part, buffer)
                part += 1
                buffer = []
                ctx.checkpoint(part, rows_done)
        if writer is None:
            # Empty source: still produce a valid, empty version.
            writer = storage.open_writer(ref, pa.schema([]), conn)
            writer.discard_from(part)
        if buffer:
            writer.write_part(part, buffer)
            part += 1
            ctx.checkpoint(part, rows_done)
        stored = writer.finalize()
    except BaseException:
        if writer is not None:
            writer.abort()
        raise
    finally:
        if external_runner is not None:
            loop.close()

    ctx.progress(rows_done, force=True)
    return ExecResult(stored=stored, rows=stored.rows)


def run_transform(
    plugin: Transform, params: Any, conn, source_sql: str, profile: DatasetProfile,
    storage: StorageBackend, ref: VersionRef, ctx: JobCtx, settings: Settings,
    resume_parts: int = 0, resume_rows: int = 0,
    model=None, max_cost_usd: float | None = None,
) -> ExecResult:
    """Dispatch on execution mode. This is the only place that branches on it."""
    if plugin.mode == "pushdown":
        return run_pushdown_transform(
            plugin, params, conn, source_sql, profile, storage, ref, ctx
        )
    if plugin.mode in ("batch", "external"):
        return run_streaming_transform(
            plugin, params, conn, source_sql, profile, storage, ref, ctx, settings,
            resume_parts=resume_parts, resume_rows=resume_rows,
            model=model, max_cost_usd=max_cost_usd,
        )
    raise ValueError(f"{plugin.id}: transforms cannot run in mode {plugin.mode!r}")
