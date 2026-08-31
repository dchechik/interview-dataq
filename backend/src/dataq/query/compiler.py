"""Compile a ``QuerySpec`` into parameterised DuckDB SQL.

Two safety rules, because specs arrive from the UI *and* from an LLM agent:

  1. Every identifier is validated against the resolved source schema and then
     quoted. An unknown column is an error, never interpolated text.
  2. Every literal is bound as a ``?`` parameter, never formatted into the string.

Together these mean a malicious or confused spec cannot inject SQL.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .spec import AggFunc, Filter, QuerySpec, Select

_AGG_SQL: dict[str, str] = {
    "count": "count({})",
    "count_distinct": "count(DISTINCT {})",
    "sum": "sum({})",
    "avg": "avg({})",
    "min": "min({})",
    "max": "max({})",
    "median": "median({})",
    "stddev": "stddev({})",
    "any_value": "any_value({})",
}

_INTERVALS = {"minute", "hour", "day", "week", "month", "quarter", "year"}


@dataclass(frozen=True)
class _PartSpec:
    """How to render one cyclical time part: a sortable ordinal and a label.

    The label is what a reader sees ("1pm", "Thu"); the ordinal is what the chart
    sorts on, since those labels do not sort usefully as text.
    """

    ordinal: str   # format string taking the quoted column
    label: str
    title: str

    def ordinal_sql(self, column: str) -> str:
        return self.ordinal.format(c=column)

    def label_sql(self, column: str) -> str:
        return self.label.format(c=column)


TIME_PARTS: dict[str, _PartSpec] = {
    "minute_of_hour": _PartSpec(
        "date_part('minute', {c})", "lpad(CAST(date_part('minute', {c}) AS VARCHAR), 2, '0')",
        "Minute of hour",
    ),
    # ltrim strips the leading zero from %I so 01PM reads as 1pm.
    "hour_of_day": _PartSpec(
        "date_part('hour', {c})", "lower(ltrim(strftime({c}, '%I%p'), '0'))", "Hour of day",
    ),
    # isodow is 1=Monday..7=Sunday, so the week reads in the expected order.
    "day_of_week": _PartSpec(
        "date_part('isodow', {c})", "strftime({c}, '%a')", "Day of week",
    ),
    "day_of_month": _PartSpec(
        "date_part('day', {c})", "CAST(date_part('day', {c}) AS VARCHAR)", "Day of month",
    ),
    "month_of_year": _PartSpec(
        "date_part('month', {c})", "strftime({c}, '%b')", "Month of year",
    ),
    "quarter_of_year": _PartSpec(
        "date_part('quarter', {c})", "'Q' || CAST(date_part('quarter', {c}) AS VARCHAR)",
        "Quarter of year",
    ),
    "week_of_year": _PartSpec(
        "date_part('week', {c})", "'W' || CAST(date_part('week', {c}) AS VARCHAR)",
        "Week of year",
    ),
}


class QueryError(ValueError):
    pass


@dataclass
class ResolvedSource:
    """A dataset version resolved to something SQL can read."""

    sql: str                      # e.g. read_parquet('...') or "ds_x__v1"
    columns: dict[str, str]       # column name -> physical type


class SourceResolver(Protocol):
    def __call__(self, dataset_id: str, version: int | None) -> ResolvedSource: ...


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def inline_params(sql: str, params: Sequence[Any]) -> str:
    """Fold bound parameters back into the SQL, as quoted literals.

    Compilation binds every literal as a ``?`` so nothing user-supplied reaches
    the parser as text. That is exactly wrong for *showing* someone the query:
    a bare ``?`` will not run. This is the display form -- what the SQL editor
    is seeded with when you carry a built query over to it -- so it goes back
    through :func:`quote_literal` rather than through the parameter binder.

    Placeholders inside quoted strings and identifiers are left alone, since a
    literal path or a column named ``why?`` is not a parameter slot.
    """
    out: list[str] = []
    remaining = list(params)
    quote: str | None = None
    for ch in sql:
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "?":
            if not remaining:
                raise QueryError("more placeholders than parameters")
            out.append(quote_literal(remaining.pop(0)))
            continue
        out.append(ch)
    if remaining:
        raise QueryError("more parameters than placeholders")
    return "".join(out)


@dataclass
class CompiledQuery:
    sql: str
    params: list[Any]
    output_columns: list[str]


class QueryCompiler:
    def __init__(self, resolve: SourceResolver) -> None:
        self._resolve = resolve

    def compile(self, spec: QuerySpec) -> CompiledQuery:
        src = self._resolve(spec.dataset, spec.version)
        params: list[Any] = []

        def col(name: str) -> str:
            if name not in src.columns:
                raise QueryError(
                    f"unknown column {name!r}; available: {sorted(src.columns)[:20]}"
                )
            return quote_ident(name)

        # --- projection ---
        projection: list[str] = []
        group_exprs: list[str] = []
        output_columns: list[str] = []

        if spec.time_bucket is not None:
            tb = spec.time_bucket
            if tb.part is not None:
                part = TIME_PARTS.get(tb.part)
                if part is None:
                    raise QueryError(f"bad time part: {tb.part}")
                c = col(tb.column)
                label_expr = part.label_sql(c)
                ordinal_expr = part.ordinal_sql(c)
                projection.append(f"{label_expr} AS {quote_ident(tb.alias)}")
                projection.append(f"{ordinal_expr} AS {quote_ident(tb.ordinal_alias)}")
                # Both are grouped: the ordinal is functionally dependent on the
                # label, but grouping it too makes it orderable without an aggregate.
                group_exprs += [label_expr, ordinal_expr]
                output_columns += [tb.alias, tb.ordinal_alias]
            else:
                if tb.interval not in _INTERVALS:
                    raise QueryError(f"bad interval: {tb.interval}")
                expr = f"date_trunc('{tb.interval}', {col(tb.column)})"
                projection.append(f"{expr} AS {quote_ident(tb.alias)}")
                group_exprs.append(expr)
                output_columns.append(tb.alias)

        for name in spec.group_by:
            projection.append(f"{col(name)} AS {quote_ident(name)}")
            group_exprs.append(col(name))
            output_columns.append(name)

        for sel in spec.select:
            projection.append(self._select_sql(sel, col))
            output_columns.append(sel.output_name)

        if not projection:
            projection.append("*")
            output_columns = list(src.columns)

        # --- where ---
        where_parts: list[str] = []
        for f in spec.filters:
            frag, fparams = self._filter_sql(f, col)
            where_parts.append(frag)
            params.extend(fparams)

        sql = f"SELECT {', '.join(projection)} FROM {src.sql}"
        if where_parts:
            sql += " WHERE " + " AND ".join(where_parts)
        if group_exprs:
            sql += " GROUP BY " + ", ".join(group_exprs)

        if spec.order_by:
            valid = set(output_columns)
            order_parts = []
            for s in spec.order_by:
                if s.column not in valid:
                    raise QueryError(
                        f"cannot order by {s.column!r}; not an output column of this query"
                    )
                order_parts.append(f"{quote_ident(s.column)}{' DESC' if s.desc else ''}")
            sql += " ORDER BY " + ", ".join(order_parts)

        if spec.limit is not None:
            sql += " LIMIT ?"
            params.append(spec.limit)
        if spec.offset:
            sql += " OFFSET ?"
            params.append(spec.offset)

        return CompiledQuery(sql=sql, params=params, output_columns=output_columns)

    # ------------------------------------------------------------------ #
    def _select_sql(self, sel: Select, col) -> str:
        alias = quote_ident(sel.output_name)
        if sel.agg is None:
            return f"{col(sel.column)} AS {alias}"
        agg: AggFunc = sel.agg
        if agg not in _AGG_SQL:
            raise QueryError(f"unsupported aggregate: {agg}")
        inner = "*" if sel.column == "*" else col(sel.column)
        if sel.column == "*" and agg != "count":
            raise QueryError(f"'*' is only valid with count, not {agg}")
        return f"{_AGG_SQL[agg].format(inner)} AS {alias}"

    def _filter_sql(self, f: Filter, col) -> tuple[str, list[Any]]:
        c = col(f.column)
        op = f.op
        if op in ("=", "!=", "<", "<=", ">", ">="):
            return f"{c} {op} ?", [f.value]
        if op in ("in", "not_in"):
            values = list(f.value or [])
            if not values:
                # An empty IN list is degenerate; make it explicit rather than
                # emitting invalid SQL.
                return ("FALSE" if op == "in" else "TRUE"), []
            holes = ", ".join("?" * len(values))
            return f"{c} {'IN' if op == 'in' else 'NOT IN'} ({holes})", values
        if op == "contains":
            return f"CAST({c} AS VARCHAR) ILIKE '%' || ? || '%'", [f.value]
        if op == "starts_with":
            return f"CAST({c} AS VARCHAR) ILIKE ? || '%'", [f.value]
        if op in ("between", "not_between"):
            lo, hi = (f.value or [None, None])[:2]
            negate = "NOT " if op == "not_between" else ""
            return f"{c} {negate}BETWEEN ? AND ?", [lo, hi]
        if op == "is_null":
            return f"{c} IS NULL", []
        if op == "is_not_null":
            return f"{c} IS NOT NULL", []
        raise QueryError(f"unsupported filter op: {op}")
