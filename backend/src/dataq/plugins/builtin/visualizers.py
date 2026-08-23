"""Built-in visualizers.

Each returns a ``VizSpec`` naming a renderer; the backend never renders. Adding a
chart type that reuses an existing renderer is therefore a backend-only change.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from ...core.viz import Animate, VizSpec
from ...query.spec import Interval, QuerySpec, Select, Sort, TimeBucket
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
            spec={
                "mark": {"type": "bar", "color": PALETTE[0]},
                "encoding": {
                    "x": {"field": p.column, "bin": {"maxbins": p.bins},
                          "type": "quantitative", "title": p.column},
                    "y": {"aggregate": "count", "type": "quantitative", "title": "rows"},
                },
            },
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
            spec={
                "mark": {"type": "bar", "color": PALETTE[0]},
                "encoding": {
                    "y": {"field": p.dimension, "type": "nominal", "sort": "-x",
                          "title": p.dimension},
                    "x": {"field": value_field, "type": "quantitative", "title": value_field},
                },
            },
        )


class TimeSeriesParams(BaseModel):
    time_column: str
    interval: Interval = "day"
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
        encoding = {
            "x": {"field": "bucket", "type": "temporal", "title": p.interval},
            "y": {"field": value_field, "type": "quantitative", "title": value_field},
        }
        if p.series:
            encoding["color"] = {"field": p.series, "type": "nominal",
                                 "scale": {"range": PALETTE}}
        return VizSpec(
            renderer="vega-lite",
            title=f"{value_field} by {p.interval}",
            query=QuerySpec(
                dataset="",
                time_bucket=TimeBucket(column=p.time_column, interval=p.interval),
                group_by=[p.series] if p.series else [],
                select=select, order_by=[Sort(column="bucket")], limit=10_000,
            ),
            spec={"mark": {"type": "line", "point": True, "color": PALETTE[0]},
                  "encoding": encoding},
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


@register
class MapPoints(Visualizer):
    """Geographic scatter of points."""

    id: ClassVar[str] = "viz.map_points"
    title: ClassVar[str] = "Map (points)"
    Params: ClassVar[type[BaseModel]] = MapParams
    accepts: ClassVar[Accepts] = Accepts(semantic_types=("geo.lat",))

    def spec(self, ctx: VizCtx) -> VizSpec:
        p: MapParams = ctx.params
        select = [Select(column=p.lat_column, alias="lat"),
                  Select(column=p.lng_column, alias="lng")]
        if p.measure:
            select.append(Select(column=p.measure, alias="value"))
        animate = None
        query = QuerySpec(dataset="", select=select, limit=p.limit)
        if p.animate_by:
            # Aggregate into frames so the scrubber costs no extra queries.
            query = QuerySpec(
                dataset="",
                time_bucket=TimeBucket(column=p.animate_by, interval=p.interval,
                                       alias="frame"),
                group_by=[p.lat_column, p.lng_column],
                select=[Select(column="*", agg="count", alias="value")],
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
