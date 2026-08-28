"""Turning a chart into a dataset, and back.

A chart and a rollup are the same QuerySpec with different fates: one renders and
disappears, the other materialises and joins the semantic graph. These are the
tests that they really are interchangeable.
"""

from __future__ import annotations

import pytest

from dataq.query.spec import QuerySpec, Sort
from dataq.services.chart import default_chart_for
from dataq.services.inspect import render_viz, suggest
from dataq.services.query import rows_as_dicts, run_query

from .fixtures import write_auth_csv, write_taxi_csv


@pytest.fixture
def auth(app_ctx, run_op, tmp_path):
    return run_op(op="import", uri=str(write_auth_csv(tmp_path / "a.csv", rows=600)),
                  name="auth")


@pytest.fixture
def taxi(app_ctx, run_op, tmp_path):
    ds = run_op(op="import", uri=str(write_taxi_csv(tmp_path / "t.csv", rows=800)),
                name="taxi")
    run_op(op="transform", plugin_id="normalize.timestamp",
           inputs=[{"dataset_id": ds}], params={"column": "tpep_pickup_datetime"})
    return ds


def test_a_charts_query_can_be_saved_as_a_dataset(app_ctx, run_op, auth):
    """The toggle: render a chart, then materialise exactly what it drew."""
    chart = render_viz(app_ctx, "viz.bar", auth, {"dimension": "country"})
    assert chart.row_count > 0

    agg = run_op(op="aggregate", inputs=[{"dataset_id": auth}],
                 from_query=chart.spec.query.model_dump(), output_name="from_chart")

    # Same numbers as the chart drew.
    stored = rows_as_dicts(run_query(app_ctx, QuerySpec(
        dataset=agg, order_by=[Sort(column="n", desc=True)])))
    assert [r["country"] for r in stored] == [r["country"] for r in chart.data]
    assert [r["n"] for r in stored] == [r["n"] for r in chart.data]


def test_the_materialised_chart_joins_the_semantic_graph(app_ctx, run_op, auth):
    """The point of materialising: it becomes joinable, which a chart never is."""
    chart = render_viz(app_ctx, "viz.bar", auth, {"dimension": "country"})
    agg = run_op(op="aggregate", inputs=[{"dataset_id": auth}],
                 from_query=chart.spec.query.model_dump(), output_name="country_counts")

    profile = app_ctx.catalog.get_profile(agg)
    country = profile.column("country")
    # Inherited from the source and pinned, which is what makes the join
    # suggester able to match it back.
    assert country.semantic_type == "geo.country_iso2"
    assert country.pinned is True

    joins = [s for s in suggest(app_ctx, auth, kinds=("join",))
             if s.action["inputs"][1]["dataset_id"] == agg]
    assert joins, "the materialised chart should be joinable back onto its source"

    # And it hangs off its parent in the derivation tree.
    from dataq.services.lineage import related

    assert agg in [c["id"] for c in related(app_ctx.catalog, auth)["children"]]


def test_a_raw_row_query_is_refused(app_ctx, run_op, taxi):
    """A non-aggregating chart query would just copy the source dataset."""
    from dataq.services.operations import OperationRequest, submit_operation

    chart = render_viz(app_ctx, "viz.histogram", taxi, {"column": "fare_amount"})
    assert not chart.spec.query.is_aggregate

    accepted = submit_operation(app_ctx, OperationRequest(
        op="aggregate", inputs=[{"dataset_id": taxi}],
        from_query=chart.spec.query))
    app_ctx.runner.wait(accepted.job_id, timeout=60)
    job = app_ctx.catalog.get_job(accepted.job_id)
    assert job.status == "failed"
    assert "aggregating query" in job.error


def test_a_cyclical_rollup_round_trips_through_a_chart(app_ctx, run_op, taxi):
    """The case the whole exercise was about: busiest hour of day, both ways."""
    chart = render_viz(app_ctx, "viz.timeseries", taxi,
                       {"time_column": "tpep_pickup_datetime", "part": "hour_of_day"})
    assert chart.spec.chart.encodings["x"].type == "ordinal"
    assert [r["bucket"] for r in chart.data][:2] == ["8am", "9am"]

    agg = run_op(op="aggregate", inputs=[{"dataset_id": taxi}],
                 from_query=chart.spec.query.model_dump(), output_name="by_hour")
    rows = rows_as_dicts(run_query(app_ctx, QuerySpec(
        dataset=agg, order_by=[Sort(column="bucket_ord")])))
    assert [r["bucket"] for r in rows][:2] == ["8am", "9am"]

    # And the materialised result charts itself back, still in clock order.
    profile = app_ctx.catalog.get_profile(agg)
    default = default_chart_for(profile)
    assert default.mark == "bar"
    assert default.encodings["x"].field == "bucket"


def test_default_chart_for_a_materialised_frequency_table(app_ctx, run_op, auth):
    """Dataset -> chart: an aggregate should know how to draw itself."""
    agg = run_op(op="aggregate", plugin_id="agg.frequency",
                 inputs=[{"dataset_id": auth}], params={"column": "country"},
                 output_name="freq")
    chart = default_chart_for(app_ctx.catalog.get_profile(agg))
    assert chart is not None
    assert chart.mark == "bar"
    assert chart.encodings["y"].field == "country"
    assert chart.encodings["x"].field in ("n", "share")
