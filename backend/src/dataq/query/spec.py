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


class Filter(BaseModel):
    column: str
    op: FilterOp = "="
    value: Any = None


class TimeBucket(BaseModel):
    """Truncate a temporal column into buckets and group by the result."""

    column: str
    interval: Interval = "day"
    alias: str = "bucket"


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
