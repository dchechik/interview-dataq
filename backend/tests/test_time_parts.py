"""Cyclical time rollups.

Truncation answers "what happened on 3 March?"; a part answers "when in the day is
this busiest?". The part must collapse every occurrence of a slice together, and it
must stay chart-orderable even though "Thu" does not sort as text.
"""

from __future__ import annotations

import pytest

from dataq.query.compiler import TIME_PARTS, QueryCompiler, QueryError, ResolvedSource
from dataq.query.spec import QuerySpec, Select, Sort, TimeBucket
from dataq.services.query import rows_as_dicts, run_query

from .fixtures import write_taxi_csv

SRC = ResolvedSource(sql="t", columns={"ts": "TIMESTAMP", "amount": "DOUBLE"})


@pytest.fixture
def compiler() -> QueryCompiler:
    return QueryCompiler(lambda dataset_id, version: SRC)


def test_truncate_is_still_the_default(compiler):
    c = compiler.compile(
        QuerySpec(dataset="d", time_bucket=TimeBucket(column="ts", interval="hour"))
    )
    assert "date_trunc('hour'" in c.sql
    assert c.output_columns == ["bucket"]


def test_part_emits_a_label_and_an_ordinal(compiler):
    c = compiler.compile(
        QuerySpec(dataset="d", time_bucket=TimeBucket(column="ts", part="hour_of_day"))
    )
    assert "date_trunc" not in c.sql
    assert 'AS "bucket"' in c.sql
    assert 'AS "bucket_ord"' in c.sql
    assert c.output_columns == ["bucket", "bucket_ord"]
    # Both are grouped, so the ordinal is orderable without an aggregate over it.
    assert c.sql.count("GROUP BY") == 1


def test_part_can_be_ordered_by_its_ordinal(compiler):
    c = compiler.compile(
        QuerySpec(
            dataset="d",
            time_bucket=TimeBucket(column="ts", part="day_of_week"),
            select=[Select(column="*", agg="count", alias="n")],
            order_by=[Sort(column="bucket_ord")],
        )
    )
    assert 'ORDER BY "bucket_ord"' in c.sql


def test_bad_part_is_rejected(compiler):
    spec = QuerySpec(dataset="d", time_bucket=TimeBucket(column="ts", interval="day"))
    spec.time_bucket.part = "fortnight_of_epoch"  # bypasses pydantic on purpose
    with pytest.raises(QueryError, match="bad time part"):
        compiler.compile(spec)


def test_part_column_is_still_validated(compiler):
    with pytest.raises(QueryError, match="unknown column"):
        compiler.compile(
            QuerySpec(dataset="d", time_bucket=TimeBucket(column="nope", part="hour_of_day"))
        )


def test_every_declared_part_compiles(compiler):
    for part in TIME_PARTS:
        c = compiler.compile(
            QuerySpec(dataset="d", time_bucket=TimeBucket(column="ts", part=part))
        )
        assert 'AS "bucket"' in c.sql, part


# --------------------------------------------------------------------------- #
# against real data
# --------------------------------------------------------------------------- #
@pytest.fixture
def taxi(app_ctx, run_op, tmp_path):
    ds = run_op(op="import", uri=str(write_taxi_csv(tmp_path / "t.csv", rows=2000)),
                name="taxi")
    run_op(op="transform", plugin_id="normalize.timestamp",
           inputs=[{"dataset_id": ds}], params={"column": "tpep_pickup_datetime"})
    return ds


def test_hour_of_day_labels_read_as_clock_times(app_ctx, taxi):
    result = run_query(app_ctx, QuerySpec(
        dataset=taxi,
        time_bucket=TimeBucket(column="tpep_pickup_datetime", part="hour_of_day"),
        select=[Select(column="*", agg="count", alias="n")],
        order_by=[Sort(column="bucket_ord")],
    ))
    rows = rows_as_dicts(result)
    labels = [r["bucket"] for r in rows]

    # The fixture only generates these hours, and they must read as clock times.
    assert labels == ["8am", "9am", "5pm", "6pm", "7pm", "8pm", "9pm", "10pm", "11pm"]
    # Ordered by the ordinal, so the day runs forwards rather than alphabetically.
    assert [r["bucket_ord"] for r in rows] == [8, 9, 17, 18, 19, 20, 21, 22, 23]
    assert sum(r["n"] for r in rows) == 2000


def test_a_part_collapses_dates_together(app_ctx, taxi):
    """The point of the feature: one bucket per hour, not one per date-hour."""
    by_part = run_query(app_ctx, QuerySpec(
        dataset=taxi,
        time_bucket=TimeBucket(column="tpep_pickup_datetime", part="hour_of_day"),
        select=[Select(column="*", agg="count", alias="n")],
    ))
    by_hour = run_query(app_ctx, QuerySpec(
        dataset=taxi,
        time_bucket=TimeBucket(column="tpep_pickup_datetime", interval="hour"),
        select=[Select(column="*", agg="count", alias="n")],
    ))
    assert by_part.row_count == 9          # 9 distinct hours of the day
    assert by_hour.row_count > by_part.row_count  # every date's hour, separately


def test_day_of_week_orders_monday_first(app_ctx, taxi):
    result = run_query(app_ctx, QuerySpec(
        dataset=taxi,
        time_bucket=TimeBucket(column="tpep_pickup_datetime", part="day_of_week"),
        select=[Select(column="*", agg="count", alias="n")],
        order_by=[Sort(column="bucket_ord")],
    ))
    labels = [r["bucket"] for r in rows_as_dicts(result)]
    assert labels == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def test_thursday_night_question(app_ctx, taxi):
    """The spec's own example: busiest pickups on a Thursday night."""
    from dataq.query.spec import Filter

    result = run_query(app_ctx, QuerySpec(
        dataset=taxi,
        filters=[Filter(column="tpep_pickup_datetime", op="is_not_null")],
        time_bucket=TimeBucket(column="tpep_pickup_datetime", part="day_of_week"),
        select=[Select(column="*", agg="count", alias="trips"),
                Select(column="fare_amount", agg="avg", alias="avg_fare")],
        order_by=[Sort(column="trips", desc=True)],
    ))
    rows = rows_as_dicts(result)
    assert {"bucket", "bucket_ord", "trips", "avg_fare"} <= set(rows[0])
    assert len(rows) == 7


# --------------------------------------------------------------------------- #
# through the plugin
# --------------------------------------------------------------------------- #
def test_rollup_plugin_defaults_to_the_timeline(app_ctx, run_op, taxi):
    agg = run_op(op="aggregate", plugin_id="agg.time_rollup",
                 inputs=[{"dataset_id": taxi}],
                 params={"time_column": "tpep_pickup_datetime", "interval": "day"})
    profile = app_ctx.catalog.get_profile(agg)
    names = {c.name for c in profile.columns}
    assert names == {"bucket", "n"}
    assert profile.column("bucket").physical_type.startswith("TIMESTAMP")


def test_rollup_plugin_by_hour_of_day(app_ctx, run_op, taxi):
    agg = run_op(op="aggregate", plugin_id="agg.time_rollup",
                 inputs=[{"dataset_id": taxi}],
                 params={"time_column": "tpep_pickup_datetime", "part": "hour_of_day",
                         "measure": "fare_amount"},
                 output_name="by_hour")
    result = run_query(app_ctx, QuerySpec(dataset=agg, order_by=[Sort(column="bucket_ord")]))
    rows = rows_as_dicts(result)
    assert [r["bucket"] for r in rows][:2] == ["8am", "9am"]
    assert {"n", "sum_fare_amount", "avg_fare_amount"} <= set(rows[0])
    # The stored aggregate is already in reading order, since the plugin sorts it.
    stored = run_query(app_ctx, QuerySpec(dataset=agg))
    assert [r["bucket"] for r in rows_as_dicts(stored)][0] == "8am"


def test_rollup_plugin_by_month(app_ctx, run_op, taxi):
    agg = run_op(op="aggregate", plugin_id="agg.time_rollup",
                 inputs=[{"dataset_id": taxi}],
                 params={"time_column": "tpep_pickup_datetime", "part": "month_of_year"})
    rows = rows_as_dicts(run_query(app_ctx, QuerySpec(dataset=agg)))
    # The fixture is all March.
    assert [r["bucket"] for r in rows] == ["Mar"]
    assert rows[0]["n"] == 2000


def test_rollup_plugin_part_with_dimensions(app_ctx, run_op, taxi):
    agg = run_op(op="aggregate", plugin_id="agg.time_rollup",
                 inputs=[{"dataset_id": taxi}],
                 params={"time_column": "tpep_pickup_datetime", "part": "hour_of_day",
                         "dimensions": ["payment_type"]})
    rows = rows_as_dicts(run_query(app_ctx, QuerySpec(dataset=agg)))
    assert {"bucket", "bucket_ord", "payment_type", "n"} <= set(rows[0])
    assert sum(r["n"] for r in rows) == 2000
