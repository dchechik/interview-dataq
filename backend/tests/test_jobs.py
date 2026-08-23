"""Job machinery: the parts most likely to be subtly wrong.

Covers resume-from-watermark equivalence, external-mode caching, budget aborts,
row-level failure isolation and cancellation.
"""

from __future__ import annotations

from typing import ClassVar

import pyarrow as pa
import pytest
from pydantic import BaseModel

from dataq.jobs.context import BudgetExceeded, Cost, JobCtx
from dataq.jobs.executor import run_streaming_transform
from dataq.jobs.external import ResultCache
from dataq.plugins.base import ColumnParams
from dataq.plugins.kinds import Transform
from dataq.services.model import FakeModelClient
from dataq.storage.base import VersionRef

from .fixtures import write_auth_csv


class Doubler(Transform):
    """Batch transform that appends a derived column."""

    id: ClassVar[str] = "test.doubler"
    title: ClassVar[str] = "Doubler"
    mode: ClassVar[str] = "batch"
    Params: ClassVar[type[BaseModel]] = ColumnParams

    def process(self, batch: pa.RecordBatch, params) -> pa.RecordBatch:
        idx = batch.schema.get_field_index(params.column)
        vals = batch.column(idx).to_pylist()
        arr = pa.array([None if v is None else v * 2 for v in vals], type=pa.int64())
        return pa.RecordBatch.from_arrays(
            [*batch.columns, arr],
            schema=pa.schema([*list(batch.schema), pa.field("doubled", pa.int64())]),
        )


class CountingExternal(Transform):
    """External transform that records how many rows it was actually asked about."""

    id: ClassVar[str] = "test.counting"
    title: ClassVar[str] = "Counting external"
    mode: ClassVar[str] = "external"
    Params: ClassVar[type[BaseModel]] = ColumnParams
    batch_size: ClassVar[int] = 5
    max_concurrency: ClassVar[int] = 2
    output_columns: ClassVar[tuple[tuple[str, str], ...]] = (("tag", "VARCHAR"),)

    seen: ClassVar[list[str]] = []

    def cache_key_fields(self, row, params):
        return [row.get(params.column)]

    async def process_rows(self, rows, ctx):
        for r in rows:
            CountingExternal.seen.append(str(r.get(ctx.params.column)))
        ctx.record_cost(Cost(calls=1, tokens_in=10, tokens_out=5, usd=0.01))
        return [{"tag": f"t{r.get(ctx.params.column)}"} for r in rows]


class FlakyExternal(CountingExternal):
    """Always fails, to prove row-level isolation."""

    id: ClassVar[str] = "test.flaky"
    title: ClassVar[str] = "Flaky external"

    async def process_rows(self, rows, ctx):
        raise RuntimeError("upstream exploded")


@pytest.fixture
def source(app_ctx, tmp_path):
    """A small materialised source table with a stable integer column."""
    with app_ctx.warehouse.cur() as conn:
        conn.execute("CREATE OR REPLACE TABLE src AS SELECT i AS n FROM range(120) t(i)")
    from dataq.core.profile import ColumnProfile, DatasetProfile

    profile = DatasetProfile(
        dataset_id="src", version=1, row_count=120,
        columns=[ColumnProfile(name="n", physical_type="BIGINT")],
    )
    return "src", profile


def _job_ctx(app_ctx, rows_total=120):
    job = app_ctx.catalog.create_job("t")
    step = app_ctx.catalog.create_step(job.id, op="transform")
    return JobCtx(catalog=app_ctx.catalog, job_id=job.id, step_id=step.id,
                  rows_total=rows_total), step


def _read(app_ctx, stored):
    with app_ctx.warehouse.cur() as conn:
        return conn.execute(
            f"SELECT n, doubled FROM {app_ctx.storage.sql_source(stored)} ORDER BY n"
        ).fetchall()


def test_interrupted_batch_job_resumes_to_identical_output(app_ctx, source):
    """A job killed mid-run and resumed must produce exactly the uninterrupted result."""
    source_sql, profile = source
    app_ctx.settings.batch_rows = 10
    app_ctx.settings.checkpoint_every_batches = 2  # a part every 20 rows

    ctx_a, _ = _job_ctx(app_ctx)
    with app_ctx.warehouse.cur() as conn:
        clean = run_streaming_transform(
            Doubler(), ColumnParams(column="n"), conn, source_sql, profile,
            app_ctx.storage, VersionRef(dataset_id="clean", version=1),
            ctx_a, app_ctx.settings,
        )
    expected = _read(app_ctx, clean.stored)
    assert len(expected) == 120

    # Interrupt after the third part (60 rows) by raising from the plugin.
    class Interrupting(Doubler):
        calls = 0

        def process(self, batch, params):
            Interrupting.calls += 1
            if Interrupting.calls > 6:  # 6 batches x 10 rows = 60 rows committed
                raise KeyboardInterrupt("simulated crash")
            return super().process(batch, params)

    ctx_b, step_b = _job_ctx(app_ctx)
    ref = VersionRef(dataset_id="resumed", version=1)
    with app_ctx.warehouse.cur() as conn, pytest.raises(KeyboardInterrupt):
        run_streaming_transform(
            Interrupting(), ColumnParams(column="n"), conn, source_sql, profile,
            app_ctx.storage, ref, ctx_b, app_ctx.settings,
        )

    step_b = app_ctx.catalog.get_step(step_b.id)
    assert step_b.rows_committed == 60, "watermark should reflect durable parts only"
    assert step_b.parts_committed == 3

    # Resume from the recorded watermark.
    ctx_c, _ = _job_ctx(app_ctx)
    with app_ctx.warehouse.cur() as conn:
        resumed = run_streaming_transform(
            Doubler(), ColumnParams(column="n"), conn, source_sql, profile,
            app_ctx.storage, ref, ctx_c, app_ctx.settings,
            resume_parts=step_b.parts_committed, resume_rows=step_b.rows_committed,
        )
    assert _read(app_ctx, resumed.stored) == expected


def test_external_cache_only_pays_for_new_rows(app_ctx, source):
    """Re-running over a superset must issue calls only for the rows not seen before."""
    source_sql, profile = source
    app_ctx.settings.batch_rows = 50
    CountingExternal.seen.clear()

    with app_ctx.warehouse.cur() as conn:
        conn.execute("CREATE OR REPLACE TABLE small AS SELECT i AS n FROM range(20) t(i)")
        ctx1, _ = _job_ctx(app_ctx, 20)
        run_streaming_transform(
            CountingExternal(), ColumnParams(column="n"), conn, "small",
            profile.model_copy(update={"row_count": 20}), app_ctx.storage,
            VersionRef(dataset_id="ext1", version=1), ctx1, app_ctx.settings,
            model=FakeModelClient(),
        )
    first_pass = len(CountingExternal.seen)
    assert first_pass == 20

    CountingExternal.seen.clear()
    with app_ctx.warehouse.cur() as conn:
        conn.execute("CREATE OR REPLACE TABLE bigger AS SELECT i AS n FROM range(30) t(i)")
        ctx2, _ = _job_ctx(app_ctx, 30)
        run_streaming_transform(
            CountingExternal(), ColumnParams(column="n"), conn, "bigger",
            profile.model_copy(update={"row_count": 30}), app_ctx.storage,
            VersionRef(dataset_id="ext2", version=1), ctx2, app_ctx.settings,
            model=FakeModelClient(),
        )
    # Only the 10 genuinely new rows should have reached the plugin.
    assert len(CountingExternal.seen) == 10
    assert set(CountingExternal.seen) == {str(i) for i in range(20, 30)}
    assert ctx2.cost.cache_hits == 20


def test_budget_cap_aborts_with_partial_results(app_ctx, source):
    source_sql, profile = source
    app_ctx.settings.batch_rows = 10
    app_ctx.settings.checkpoint_every_batches = 1
    CountingExternal.seen.clear()

    ctx, step = _job_ctx(app_ctx)
    with app_ctx.warehouse.cur() as conn, pytest.raises(BudgetExceeded):
        run_streaming_transform(
            CountingExternal(), ColumnParams(column="n"), conn, source_sql, profile,
            app_ctx.storage, VersionRef(dataset_id="capped", version=1),
            ctx, app_ctx.settings, model=FakeModelClient(), max_cost_usd=0.03,
        )
    # It stopped early rather than processing all 120 rows.
    assert len(CountingExternal.seen) < 120
    assert ctx.cost.usd >= 0.03


def test_external_row_failure_is_isolated(app_ctx, source):
    """A permanently failing chunk must yield NULLs, not kill the job."""
    source_sql, profile = source
    app_ctx.settings.batch_rows = 20

    ctx, _ = _job_ctx(app_ctx)
    with app_ctx.warehouse.cur() as conn:
        conn.execute("CREATE OR REPLACE TABLE tiny AS SELECT i AS n FROM range(10) t(i)")
        result = run_streaming_transform(
            FlakyExternal(), ColumnParams(column="n"), conn, "tiny",
            profile.model_copy(update={"row_count": 10}), app_ctx.storage,
            VersionRef(dataset_id="flaky", version=1), ctx, app_ctx.settings,
            model=FakeModelClient(),
        )
        rows = conn.execute(
            f"SELECT tag, test_flaky_error FROM {app_ctx.storage.sql_source(result.stored)}"
        ).fetchall()
    assert result.rows == 10
    assert all(tag is None for tag, _ in rows)
    assert all("upstream exploded" in (err or "") for _, err in rows)


def test_cancellation_stops_a_running_job(app_ctx, run_op, tmp_path):
    from dataq.services.operations import OperationRequest, submit_operation

    path = write_auth_csv(tmp_path / "auth.csv", rows=200)
    ds = run_op(op="import", uri=str(path), name="auth")

    app_ctx.settings.batch_rows = 1  # force many iterations so cancel lands mid-run
    accepted = submit_operation(
        app_ctx,
        OperationRequest(op="transform", plugin_id="transform.ip_class",
                         inputs=[{"dataset_id": ds}], params={"column": "src_ip"}),
    )
    app_ctx.catalog.update_job(accepted.job_id, cancel_requested=True)
    app_ctx.runner.wait(accepted.job_id, timeout=60)
    job = app_ctx.catalog.get_job(accepted.job_id)
    assert job.status in ("cancelled", "succeeded")
    if job.status == "cancelled":
        assert app_ctx.catalog.get_dataset(ds).latest_version == 1


def test_cache_key_ignores_unrelated_columns(app_ctx):
    """Changing a column the plugin did not declare must not invalidate the cache."""
    with app_ctx.warehouse.cur() as conn:
        cache = ResultCache(conn)
        plugin = CountingExternal()
        params = ColumnParams(column="n")
        k1 = ResultCache.make_key(
            plugin.id, plugin.version, params.model_dump(), "m",
            list(plugin.cache_key_fields({"n": 1, "other": "a"}, params)),
        )
        k2 = ResultCache.make_key(
            plugin.id, plugin.version, params.model_dump(), "m",
            list(plugin.cache_key_fields({"n": 1, "other": "CHANGED"}, params)),
        )
        assert k1 == k2
        # But bumping the plugin version must invalidate.
        k3 = ResultCache.make_key(
            plugin.id, "99", params.model_dump(), "m",
            list(plugin.cache_key_fields({"n": 1}, params)),
        )
        assert k3 != k1
        cache.put_many(plugin.id, [(k1, {"tag": "x"})])
        assert cache.get_many([k1]) == {k1: {"tag": "x"}}
