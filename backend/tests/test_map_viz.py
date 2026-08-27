"""Map rendering against dirty coordinates.

Real GPS data records dropouts as exactly (0, 0). One such row sits ~4,000 miles
from New York, and a viewport fitted to it shrinks the actual data to a sub-pixel
dot -- which looks exactly like a map that failed to plot anything.
"""

from __future__ import annotations

import csv

import pytest

from dataq.services.inspect import render_viz


@pytest.fixture
def dirty_csv(tmp_path):
    """NYC pickups plus the kinds of bad coordinate real data contains."""
    path = tmp_path / "dirty.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["trip_id", "pickup_at", "pickup_latitude", "pickup_longitude",
                    "fare_amount"])
        for i in range(40):
            w.writerow([f"t{i}", f"2024-03-{1 + i % 5:02d} 10:00:00",
                        40.70 + i * 0.001, -74.00 + i * 0.001, 10 + i])
        # GPS dropouts, recorded as exactly zero.
        w.writerow(["null_island_1", "2024-03-01 10:00:00", 0.0, 0.0, 5])
        w.writerow(["null_island_2", "2024-03-02 10:00:00", 0.0, 0.0, 6])
        w.writerow(["missing", "2024-03-03 10:00:00", "", "", 7])
        w.writerow(["out_of_range", "2024-03-04 10:00:00", 91.5, -181.2, 8])
    return path


@pytest.fixture
def dataset(app_ctx, run_op, dirty_csv):
    return run_op(op="import", uri=str(dirty_csv), name="dirty")


PARAMS = {"lat_column": "pickup_latitude", "lng_column": "pickup_longitude"}


def test_invalid_coordinates_are_dropped_by_default(app_ctx, dataset):
    out = render_viz(app_ctx, "viz.map_points", dataset, PARAMS)
    lats = [r["lat"] for r in out.data]
    lngs = [r["lng"] for r in out.data]

    assert out.row_count == 40, "only the real NYC points should be plotted"
    assert all(v is not None for v in lats)
    # The whole point: the viewport-wrecking outliers are gone.
    assert not any(abs(v) < 1e-4 for v in lats)
    assert min(lats) > 40 and max(lats) < 41
    assert min(lngs) > -75 and max(lngs) < -73


def test_opting_out_keeps_every_row(app_ctx, dataset):
    out = render_viz(app_ctx, "viz.map_points", dataset,
                     {**PARAMS, "drop_invalid_coords": False})
    assert out.spec.query.filters == []
    assert out.row_count == 44  # every row in the file, dirty ones included


def test_animated_map_filters_too(app_ctx, run_op, dirty_csv):
    """The animated path builds a different query and must not lose the filters."""
    ds = run_op(op="import", uri=str(dirty_csv), name="dirty2")
    out = render_viz(app_ctx, "viz.map_points", ds,
                     {**PARAMS, "animate_by": "pickup_at", "interval": "day"})
    assert out.spec.query.filters, "filters must be applied to the animated query too"
    assert out.spec.animate is not None
    lats = [r["pickup_latitude"] for r in out.data]
    assert not any(abs(v) < 1e-4 for v in lats)
    # Five distinct days in the fixture, and every frame has points to draw.
    frames = {str(r["frame"]) for r in out.data}
    assert len(frames) == 5


def test_animating_by_a_non_temporal_column_explains_itself(app_ctx, dataset):
    """A cryptic binder error from DuckDB is not a usable message."""
    with pytest.raises(ValueError, match="not a date or timestamp"):
        render_viz(app_ctx, "viz.map_points", dataset, {**PARAMS, "animate_by": "trip_id"})
    with pytest.raises(ValueError, match="no such column"):
        render_viz(app_ctx, "viz.map_points", dataset, {**PARAMS, "animate_by": "nope"})


def test_debug_fields_are_populated(app_ctx, dataset):
    """The chart's debug view needs the executed SQL, not just the spec."""
    out = render_viz(app_ctx, "viz.map_points", dataset, PARAMS)
    assert out.sql.startswith("SELECT")
    assert "pickup_latitude" in out.sql
    assert out.elapsed_ms >= 0
    assert out.truncated is False


def test_other_visualizers_also_report_sql(app_ctx, dataset):
    out = render_viz(app_ctx, "viz.histogram", dataset, {"column": "fare_amount"})
    assert out.sql.startswith("SELECT")
    assert out.spec.renderer == "vega-lite"


def test_null_island_epsilon_is_narrow_enough_to_keep_real_places(app_ctx, run_op, tmp_path):
    """The filter must not swallow genuine coordinates merely near zero."""
    path = tmp_path / "near_zero.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "lat", "lng"])
        w.writerow(["greenwich", 51.4779, -0.0015])   # real: London, just off the meridian
        w.writerow(["quito", -0.1807, -78.4678])      # real: near the equator
        w.writerow(["dropout", 0.0, 0.0])
    ds = run_op(op="import", uri=str(path), name="near_zero")
    out = render_viz(app_ctx, "viz.map_points", ds,
                     {"lat_column": "lat", "lng_column": "lng"})
    ids = {(round(r["lat"], 3), round(r["lng"], 3)) for r in out.data}
    assert (51.478, -0.002) in ids or len(out.data) == 2
    assert out.row_count == 2, "only the dropout should be removed"
