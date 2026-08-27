"""Built-in aggregators.

Each produces a new ``aggregate`` dataset. Because they emit a query plan rather
than SQL text, the result stays composable with the rest of the query layer and
inherits the source's semantic types -- which is what makes an aggregate joinable
back onto the data it came from.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from ...query.spec import Interval, QuerySpec, Select, Sort, TimeBucket, TimePart
from ..base import Accepts, Produces, register
from ..kinds import AggregateCtx, AggregatePlan, Aggregator


class FrequencyParams(BaseModel):
    column: str = Field(description="Column whose value frequencies to compute")
    min_count: int = Field(default=1, description="Drop values rarer than this")


@register
class FrequencyAggregate(Aggregator):
    """Value frequency table -- how common is each value of a column?

    This is the building block for rarity annotation: aggregate the frequency of
    (say) country, then join it back onto the events so every row carries how
    common its country is.
    """

    id: ClassVar[str] = "agg.frequency"
    title: ClassVar[str] = "Value frequency (rarity)"
    Params: ClassVar[type[BaseModel]] = FrequencyParams
    accepts: ClassVar[Accepts] = Accepts(semantic_types=("categorical", "text", "boolean"))
    produces: ClassVar[Produces] = Produces(
        dataset_kind="aggregate", description="One row per distinct value with n and share"
    )

    def plan(self, ctx: AggregateCtx) -> AggregatePlan:
        p: FrequencyParams = ctx.params
        spec = QuerySpec(
            dataset="",
            group_by=[p.column],
            select=[Select(column="*", agg="count", alias="n")],
            order_by=[Sort(column="n", desc=True)],
            limit=1_000_000,
        )
        # Window functions are not expressible in QuerySpec by design; a plugin may
        # layer them on because plugin code is trusted.
        return AggregatePlan(
            spec=spec,
            derive={
                "share": "n::DOUBLE / SUM(n) OVER ()",
                "rarity": "1.0 - (n::DOUBLE / SUM(n) OVER ())",
            },
        )


class TimeRollupParams(BaseModel):
    time_column: str
    interval: Interval = Field(
        default="day",
        description="Bucket size when rolling up the timeline (ignored if 'part' is set)",
    )
    part: TimePart | None = Field(
        default=None,
        description=(
            "Optional: roll up by a repeating slice of time instead of the timeline — "
            "e.g. hour_of_day groups every 1pm together, whatever the date"
        ),
    )
    dimensions: list[str] = []
    measure: str | None = Field(default=None, description="Column to sum; counts when omitted")


@register
class TimeRollupAggregate(Aggregator):
    """Roll events up into time buckets, optionally split by dimensions.

    By default this rolls up along the timeline: one bucket per hour, day or month
    of actual elapsed time. Setting ``part`` switches to a repeating slice, so all
    of a dataset's 1pms collapse into a single ``1pm`` bucket — which is how you
    ask "when in the day is this busiest?" rather than "what happened that day?".
    """

    id: ClassVar[str] = "agg.time_rollup"
    title: ClassVar[str] = "Roll up over time"
    Params: ClassVar[type[BaseModel]] = TimeRollupParams
    accepts: ClassVar[Accepts] = Accepts(semantic_types=("temporal",))
    produces: ClassVar[Produces] = Produces(dataset_kind="aggregate")

    def plan(self, ctx: AggregateCtx) -> AggregatePlan:
        p: TimeRollupParams = ctx.params
        select = [Select(column="*", agg="count", alias="n")]
        if p.measure:
            select.append(Select(column=p.measure, agg="sum", alias=f"sum_{p.measure}"))
            select.append(Select(column=p.measure, agg="avg", alias=f"avg_{p.measure}"))
        bucket = TimeBucket(column=p.time_column, interval=p.interval, part=p.part)
        # In part mode the label ("Thu", "1pm") does not sort usefully as text, so
        # order by the ordinal the compiler emits alongside it.
        order_column = bucket.ordinal_alias if p.part else bucket.alias
        return AggregatePlan(
            spec=QuerySpec(
                dataset="",
                time_bucket=bucket,
                group_by=list(p.dimensions),
                select=select,
                order_by=[Sort(column=order_column)],
                limit=1_000_000,
            )
        )


class TopKParams(BaseModel):
    dimension: str
    k: int = Field(default=20, ge=1, le=10_000)
    measure: str | None = Field(default=None, description="Column to sum; counts when omitted")


@register
class TopKAggregate(Aggregator):
    """Top-K values of a dimension by count or by a summed measure."""

    id: ClassVar[str] = "agg.topk"
    title: ClassVar[str] = "Top values"
    Params: ClassVar[type[BaseModel]] = TopKParams
    accepts: ClassVar[Accepts] = Accepts(semantic_types=("categorical", "text"))
    produces: ClassVar[Produces] = Produces(dataset_kind="aggregate")

    def plan(self, ctx: AggregateCtx) -> AggregatePlan:
        p: TopKParams = ctx.params
        select = [Select(column="*", agg="count", alias="n")]
        order = "n"
        if p.measure:
            select.append(Select(column=p.measure, agg="sum", alias=f"sum_{p.measure}"))
            order = f"sum_{p.measure}"
        return AggregatePlan(
            spec=QuerySpec(
                dataset="",
                group_by=[p.dimension],
                select=select,
                order_by=[Sort(column=order, desc=True)],
                limit=p.k,
            )
        )
