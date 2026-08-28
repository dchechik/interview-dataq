"""Built-in visualizers.

Each returns a ``VizSpec`` naming a renderer; the backend never renders. Adding a
chart type that reuses an existing renderer is therefore a backend-only change.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from ...core.chart import ChartSpec, Encoding
from ...core.viz import Animate, VizSpec
from ...query.spec import Filter, Interval, QuerySpec, Select, Sort, TimeBucket, TimePart
from ..base import Accepts, Produces, register
from ..kinds import Visualizer, VizCtx

# A colourblind-safe categorical ramp, applied consistently across chart types.
PALETTE = ["#4269d0", "#efb118", "#ff725c", "#6cc5b0", "#3ca951",
           "#ff8ab7", "#a463f2", "#97bbf5", "#9c6b4e", "#9498a0"]


class HistogramParams(BaseModel):
    column: str
    bins: int = Field(default=30, ge=2, le=200)


@register
class Histogram(Visualizer):
    """Distribution of a numeric column."""

    id: ClassVar[str] = "viz.histogram"
    title: ClassVar[str] = "Histogram"
    Params: ClassVar[type[BaseModel]] = HistogramParams
    accepts: ClassVar[Accepts] = Accepts(semantic_types=("numeric",))
    produces: ClassVar[Produces] = Produces(description="Distribution chart")

    def spec(self, ctx: VizCtx) -> VizSpec:
        p: HistogramParams = ctx.params
        return VizSpec(
            renderer="vega-lite",
            title=f"Distribution of {p.column}",
            # Vega-Lite bins client-side; we just need the raw values, capped.
            query=QuerySpec(dataset="", select=[Select(column=p.column)], limit=50_000),
            # Binning and counting are client-side, which is why they are
            # encodings rather than part of the query.
            chart=ChartSpec(
                mark="bar",
                encodings={
                    "x": Encoding(field=p.column, type="quantitative", bin=p.bins),
                    "y": Encoding(field=p.column, aggregate="count", title="rows"),
                },
                raw_vega_lite={"mark": {"type": "bar", "color": PALETTE[0]}},
            ),
        )


class BarParams(BaseModel):
    dimension: str
    measure: str | None = None
    k: int = Field(default=20, ge=1, le=200)


@register
class BarChart(Visualizer):
    """Top values of a categorical column."""

    id: ClassVar[str] = "viz.bar"
    title: ClassVar[str] = "Bar chart"
    Params: ClassVar[type[BaseModel]] = BarParams
    accepts: ClassVar[Accepts] = Accepts(semantic_types=("categorical", "text", "boolean"))

    def spec(self, ctx: VizCtx) -> VizSpec:
        p: BarParams = ctx.params
        select = [Select(column="*", agg="count", alias="n")]
        value_field = "n"
        if p.measure:
            select.append(Select(column=p.measure, agg="sum", alias=f"sum_{p.measure}"))
            value_field = f"sum_{p.measure}"
        return VizSpec(
            renderer="vega-lite",
            title=f"Top {p.k} by {p.dimension}",
            query=QuerySpec(
                dataset="", group_by=[p.dimension], select=select,
                order_by=[Sort(column=value_field, desc=True)], limit=p.k,
            ),
            chart=ChartSpec(
                mark="bar",
                encodings={
                    # Horizontal: long category labels read better on the y axis.
                    "y": Encoding(field=p.dimension, sort="-x"),
                    "x": Encoding(field=value_field),
                },
                raw_vega_lite={"mark": {"type": "bar", "color": PALETTE[0]}},
            ),
        )


class TimeSeriesParams(BaseModel):
    time_column: str
    interval: Interval = "day"
    part: TimePart | None = Field(
        default=None,
        description=(
            "Optional: chart a repeating slice of time instead of the timeline — "
            "e.g. hour_of_day puts every 1pm in one bar, whatever the date"
        ),
    )
    measure: str | None = None
    series: str | None = Field(default=None, description="Optional column to split series by")


@register
class TimeSeries(Visualizer):
    """A metric over time, optionally split into series."""

    id: ClassVar[str] = "viz.timeseries"
    title: ClassVar[str] = "Time series"
    Params: ClassVar[type[BaseModel]] = TimeSeriesParams
    accepts: ClassVar[Accepts] = Accepts(semantic_types=("temporal",))

    def spec(self, ctx: VizCtx) -> VizSpec:
        p: TimeSeriesParams = ctx.params
        select = [Select(column="*", agg="count", alias="n")]
        value_field = "n"
        if p.measure:
            select.append(Select(column=p.measure, agg="avg", alias=f"avg_{p.measure}"))
            value_field = f"avg_{p.measure}"

        bucket = TimeBucket(column=p.time_column, interval=p.interval, part=p.part)
        encodings = {
            # The resolver types the x axis: temporal for a truncated timeline,
            # ordinal sorted by bucket_ord for a cyclical part.
            "x": Encoding(field=bucket.alias),
            "y": Encoding(field=value_field),
        }
        if p.series:
            encodings["color"] = Encoding(field=p.series)

        # A cyclical part has no continuous axis to draw a line along, so bars.
        mark = "bar" if p.part else "line"
        raw = {"mark": {"type": "bar", "color": PALETTE[0]}} if p.part else {
            "mark": {"type": "line", "point": True, "color": PALETTE[0]}
        }
        if p.series:
            raw["encoding"] = {"color": {"scale": {"range": PALETTE}}}

        label = p.part.replace("_", " ") if p.part else p.interval
        return VizSpec(
            renderer="vega-lite",
            title=f"{value_field} by {label}",
            query=QuerySpec(
                dataset="",
                time_bucket=bucket,
                group_by=[p.series] if p.series else [],
                select=select,
                order_by=[Sort(column=bucket.ordinal_alias if p.part else bucket.alias)],
                limit=10_000,
            ),
            chart=ChartSpec(mark=mark, encodings=encodings, raw_vega_lite=raw),
        )


class MapParams(BaseModel):
    lat_column: str
    lng_column: str
    measure: str | None = None
    limit: int = Field(default=20_000, ge=100, le=200_000)
    animate_by: str | None = Field(
        default=None, description="Temporal column to animate the map over"
    )
    interval: Interval = "day"
    drop_invalid_coords: bool = Field(
        default=True,
        description=(
            "Skip rows with missing coordinates, out-of-range values, or the "
            "(0, 0) 'null island' that GPS dropouts record"
        ),
    )


# GPS dropouts are commonly written as exactly 0 -- real NYC taxi data does this.
# A single such row sits ~4,000 miles from the rest and stretches the map's
# viewport across an ocean, shrinking the actual data to a sub-pixel dot.
NULL_ISLAND_EPSILON = 0.0001


@register
class MapPoints(Visualizer):
    """Geographic scatter of points.

    ``drop_invalid_coords`` (on by default) filters nulls, out-of-range values and
    the (0, 0) sentinel. Note this also drops the rare genuine reading that sits
    exactly on the equator or the prime meridian; turn it off for data where that
    matters.
    """

    id: ClassVar[str] = "viz.map_points"
    title: ClassVar[str] = "Map (points)"
    Params: ClassVar[type[BaseModel]] = MapParams
    accepts: ClassVar[Accepts] = Accepts(semantic_types=("geo.lat",))

    @staticmethod
    def _coord_filters(p: MapParams) -> list[Filter]:
        if not p.drop_invalid_coords:
            return []
        eps = [-NULL_ISLAND_EPSILON, NULL_ISLAND_EPSILON]
        return [
            Filter(column=p.lat_column, op="is_not_null"),
            Filter(column=p.lng_column, op="is_not_null"),
            Filter(column=p.lat_column, op="between", value=[-90, 90]),
            Filter(column=p.lng_column, op="between", value=[-180, 180]),
            Filter(column=p.lat_column, op="not_between", value=eps),
            Filter(column=p.lng_column, op="not_between", value=eps),
        ]

    def spec(self, ctx: VizCtx) -> VizSpec:
        p: MapParams = ctx.params
        if p.animate_by:
            # Caught here so the user gets a sentence rather than a DuckDB binder
            # error about date_trunc's argument types.
            column = ctx.profile.column(p.animate_by)
            if column is None:
                raise ValueError(f"cannot animate by {p.animate_by!r}: no such column")
            if not column.physical_type.upper().startswith(("TIMESTAMP", "DATE")):
                raise ValueError(
                    f"cannot animate by {p.animate_by!r}: it is "
                    f"{column.physical_type}, not a date or timestamp"
                )
        filters = self._coord_filters(p)
        select = [Select(column=p.lat_column, alias="lat"),
                  Select(column=p.lng_column, alias="lng")]
        if p.measure:
            select.append(Select(column=p.measure, alias="value"))
        animate = None
        query = QuerySpec(dataset="", select=select, filters=filters, limit=p.limit)
        if p.animate_by:
            # Aggregate into frames so the scrubber costs no extra queries.
            query = QuerySpec(
                dataset="",
                time_bucket=TimeBucket(column=p.animate_by, interval=p.interval,
                                       alias="frame"),
                group_by=[p.lat_column, p.lng_column],
                select=[Select(column="*", agg="count", alias="value")],
                filters=filters,
                limit=p.limit,
            )
            animate = Animate(field="frame", label=p.animate_by)
        return VizSpec(
            renderer="maplibre",
            title="Geographic distribution",
            query=query,
            spec={
                "layer": "scatter",
                "lat_field": p.lat_column if p.animate_by else "lat",
                "lng_field": p.lng_column if p.animate_by else "lng",
                "value_field": "value" if (p.measure or p.animate_by) else None,
                "color": PALETTE[0],
            },
            animate=animate,
        )


class TableParams(BaseModel):
    limit: int = Field(default=200, ge=1, le=10_000)


@register
class TableViz(Visualizer):
    """Plain rows. The honest default when nothing smarter applies."""

    id: ClassVar[str] = "viz.table"
    title: ClassVar[str] = "Table"
    Params: ClassVar[type[BaseModel]] = TableParams

    def spec(self, ctx: VizCtx) -> VizSpec:
        p: TableParams = ctx.params
        return VizSpec(
            renderer="table", title="Rows",
            query=QuerySpec(dataset="", limit=p.limit), spec={},
        )
