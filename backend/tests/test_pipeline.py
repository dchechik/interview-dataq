"""End-to-end pipeline: import -> profile -> transform -> query, on both backends."""

from __future__ import annotations

from dataq.query.spec import Filter, QuerySpec, Select, Sort
from dataq.services.query import run_query, run_sql

from .fixtures import write_auth_csv, write_taxi_csv


def test_import_profiles_and_queries(app_ctx, run_op, tmp_path):
    path = write_auth_csv(tmp_path / "auth.csv", rows=800)
    ds = run_op(op="import", uri=str(path), name="auth")

    profile = app_ctx.catalog.get_profile(ds)
    assert profile.row_count == 800
    assert profile.column("src_ip").semantic_type == "net.ip"
    assert profile.column("country").semantic_type == "geo.country_iso2"

    res = run_query(
        app_ctx,
        QuerySpec(
            dataset=ds,
            group_by=["country"],
            select=[Select(column="*", agg="count", alias="n")],
            order_by=[Sort(column="n", desc=True)],
            limit=3,
        ),
    )
    assert res.columns == ["country", "n"]
    assert res.rows[0][0] == "US"  # the fixture is deliberately US-skewed
    assert sum(r[1] for r in res.rows) <= 800


def test_pushdown_transform_creates_new_version(app_ctx, run_op, tmp_path):
    ds = run_op(op="import", uri=str(write_auth_csv(tmp_path / "auth.csv", rows=300)), name="auth")
    run_op(op="transform", plugin_id="normalize.ip", inputs=[{"dataset_id": ds}],
           params={"column": "src_ip"})

    versions = app_ctx.catalog.list_versions(ds)
    assert [v.version for v in versions] == [2, 1]

    profile = app_ctx.catalog.get_profile(ds)
    assert profile.column("src_ip_canon") is not None
    assert profile.column("src_ip_int") is not None
    assert profile.row_count == 300

    res = run_query(
        app_ctx,
        QuerySpec(dataset=ds, select=[Select(column="src_ip"), Select(column="src_ip_int")],
                  filters=[Filter(column="src_ip_int", op="is_not_null")], limit=5),
    )
    assert res.row_count == 5
    # The integer form must round-trip: a.b.c.d -> a<<24 | b<<16 | c<<8 | d
    for ip, as_int in res.rows:
        a, b, c, d = (int(x) for x in ip.split("."))
        assert as_int == a * 16777216 + b * 65536 + c * 256 + d


def test_batch_transform_creates_new_version(app_ctx, run_op, tmp_path):
    """The batch path must produce the same shape of result as the pushdown path."""
    ds = run_op(op="import", uri=str(write_auth_csv(tmp_path / "auth.csv", rows=500)), name="auth")
    run_op(op="transform", plugin_id="transform.ip_class", inputs=[{"dataset_id": ds}],
           params={"column": "src_ip"})

    profile = app_ctx.catalog.get_profile(ds)
    assert profile.row_count == 500
    assert profile.column("src_ip_class") is not None

    res = run_query(
        app_ctx,
        QuerySpec(dataset=ds, group_by=["src_ip_class"],
                  select=[Select(column="*", agg="count", alias="n")], limit=20),
    )
    classes = {r[0] for r in res.rows}
    assert classes <= {"public", "private", "loopback", "reserved", "multicast", "invalid"}
    assert sum(r[1] for r in res.rows) == 500


def test_transform_chain_preserves_row_count(app_ctx, run_op, tmp_path):
    ds = run_op(op="import", uri=str(write_auth_csv(tmp_path / "auth.csv", rows=400)), name="auth")
    run_op(op="transform", plugin_id="normalize.ip", inputs=[{"dataset_id": ds}],
           params={"column": "src_ip"})
    run_op(op="transform", plugin_id="transform.ip_class", inputs=[{"dataset_id": ds}],
           params={"column": "src_ip"})
    run_op(op="transform", plugin_id="normalize.country", inputs=[{"dataset_id": ds}],
           params={"column": "country"})

    profile = app_ctx.catalog.get_profile(ds)
    assert profile.row_count == 400
    for col in ("src_ip_canon", "src_ip_int", "src_ip_class", "country_iso2"):
        assert profile.column(col) is not None, col
    assert app_ctx.catalog.get_dataset(ds).latest_version == 4


def test_taxi_import_and_timeseries(app_ctx, run_op, tmp_path):
    ds = run_op(op="import", uri=str(write_taxi_csv(tmp_path / "taxi.csv", rows=600)), name="taxi")
    profile = app_ctx.catalog.get_profile(ds)
    assert profile.column("pickup_latitude").semantic_type == "geo.lat"

    from dataq.query.spec import TimeBucket

    res = run_query(
        app_ctx,
        QuerySpec(
            dataset=ds,
            time_bucket=TimeBucket(column="tpep_pickup_datetime", interval="day"),
            select=[Select(column="fare_amount", agg="avg", alias="avg_fare")],
            order_by=[Sort(column="bucket")],
            limit=40,
        ),
    )
    assert res.columns == ["bucket", "avg_fare"]
    assert 1 <= res.row_count <= 28


def test_raw_sql_is_read_only(app_ctx, run_op, tmp_path):
    import pytest

    from dataq.db import UnsafeSQLError

    run_op(op="import", uri=str(write_auth_csv(tmp_path / "auth.csv", rows=50)), name="auth")
    ok = run_sql(app_ctx, "SELECT 1 AS x")
    assert ok.rows == [[1]]
    for bad in ["DROP TABLE auth", "SELECT 1; DROP TABLE auth", "CREATE TABLE z AS SELECT 1"]:
        with pytest.raises(UnsafeSQLError):
            run_sql(app_ctx, bad)


def test_lineage_records_every_step(app_ctx, run_op, tmp_path):
    ds = run_op(op="import", uri=str(write_auth_csv(tmp_path / "auth.csv", rows=100)), name="auth")
    run_op(op="transform", plugin_id="normalize.ip", inputs=[{"dataset_id": ds}],
           params={"column": "src_ip"})
    steps = app_ctx.catalog.lineage(ds)
    assert [s.op for s in steps] == ["import", "transform"]
    assert steps[1].plugin_id == "normalize.ip"
    assert steps[1].params == {"column": "src_ip"}
