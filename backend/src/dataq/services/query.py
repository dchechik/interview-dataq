"""Query execution."""

from __future__ import annotations

import time

from ..db import assert_read_only
from ..query.spec import QueryResult, QuerySpec
from .context import AppContext

MAX_ROWS = 100_000


def run_query(ctx: AppContext, spec: QuerySpec) -> QueryResult:
    compiled = ctx.compiler().compile(spec)
    started = time.monotonic()
    with ctx.warehouse.cur() as conn:
        cur = conn.execute(compiled.sql, compiled.params)
        rows = cur.fetchmany(MAX_ROWS)
        names = [d[0] for d in cur.description]
        types = [str(d[1]) for d in cur.description]
    return QueryResult(
        columns=names,
        types=types,
        rows=[list(r) for r in rows],
        row_count=len(rows),
        truncated=len(rows) >= MAX_ROWS,
        sql=compiled.sql,
        elapsed_ms=round((time.monotonic() - started) * 1000, 2),
    )


def run_sql(ctx: AppContext, sql: str, limit: int = 1000) -> QueryResult:
    """The raw-SQL escape hatch.

    Gated by DuckDB's own parser: exactly one statement, and it must be a SELECT.
    """
    assert_read_only(sql)
    started = time.monotonic()
    with ctx.warehouse.cur() as conn:
        cur = conn.execute(f"SELECT * FROM ({sql}) _q LIMIT {int(limit)}")
        rows = cur.fetchall()
        names = [d[0] for d in cur.description]
        types = [str(d[1]) for d in cur.description]
    return QueryResult(
        columns=names, types=types, rows=[list(r) for r in rows], row_count=len(rows),
        sql=sql, elapsed_ms=round((time.monotonic() - started) * 1000, 2),
    )


def rows_as_dicts(result: QueryResult) -> list[dict]:
    return [dict(zip(result.columns, r)) for r in result.rows]
