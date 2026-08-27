"""``QuerySpec`` -- the structured query IR.

One IR with four producers: the UI filter builder, ``Aggregator`` plugins,
``VizSpec``s, and the agent. Compiled to DuckDB SQL by :mod:`dataq.query.compiler`,
which validates every identifier against the dataset schema and binds every literal
as a parameter, so an agent-authored spec cannot inject SQL.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

FilterOp = Literal[
    "=", "!=", "<", "<=", ">", ">=",
    "in", "not_in", "contains", "starts_with", "between", "is_null", "is_not_null",
]

AggFunc = Literal[
    "count", "count_distinct", "sum", "avg", "min", "max", "median", "stddev", "any_value"
]

Interval = Literal["minute", "hour", "day", "week", "month", "quarter", "year"]

# A *cyclical* slice of a timestamp: every 1pm across the whole dataset collapses
# into one bucket, rather than each date's 1pm staying separate. This is what
# "busiest hour" or "Thursday nights" needs, which truncation cannot express.
TimePart = Literal[
    "minute_of_hour",
    "hour_of_day",
    "day_of_week",
    "day_of_month",
    "month_of_year",
    "quarter_of_year",
    "week_of_year",
]


class Filter(BaseModel):
    column: str
    op: FilterOp = "="
    value: Any = None


class TimeBucket(BaseModel):
    """Group a temporal column into buckets.

    Two modes. By default the column is *truncated* to ``interval``, so each
    calendar hour or day is its own bucket (``2012-01-01 13:00``). Setting
    ``part`` instead takes a *cyclical* slice, collapsing every occurrence of that
    slice together (``1pm``) — which is how you ask for the busiest hour of the
    day, or Thursday-night activity, across a whole dataset.

    In part mode the compiler emits a readable label under ``alias`` plus an
    ``{alias}_ord`` ordinal, because "Thu" sorts alphabetically and would
    otherwise scramble the chart.
    """

    column: str
    interval: Interval = "day"
    part: TimePart | None = None
    alias: str = "bucket"

    @property
    def ordinal_alias(self) -> str:
        return f"{self.alias}_ord"


class Select(BaseModel):
    column: str = Field(description="Column name, or '*' when agg='count'")
    agg: AggFunc | None = None
    alias: str | None = None

    @property
    def output_name(self) -> str:
        if self.alias:
            return self.alias
        if self.agg:
            return f"{self.agg}_{'all' if self.column == '*' else self.column}"
        return self.column


class Sort(BaseModel):
    column: str = Field(description="An output column name (alias), not a source column")
    desc: bool = False


class QuerySpec(BaseModel):
    dataset: str = Field(description="Dataset id")
    version: int | None = Field(default=None, description="Defaults to the latest version")
    filters: list[Filter] = []
    time_bucket: TimeBucket | None = None
    select: list[Select] = []
    group_by: list[str] = []
    order_by: list[Sort] = []
    limit: int = Field(default=1000, ge=1, le=1_000_000)
    offset: int = Field(default=0, ge=0)

    @property
    def is_aggregate(self) -> bool:
        return bool(self.group_by) or self.time_bucket is not None or any(
            s.agg for s in self.select
        )


class QueryResult(BaseModel):
    columns: list[str]
    types: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool = False
    sql: str = ""
    elapsed_ms: float = 0.0
