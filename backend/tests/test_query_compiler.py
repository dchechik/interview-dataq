from __future__ import annotations

import pytest

from dataq.query.compiler import QueryCompiler, QueryError, ResolvedSource
from dataq.query.spec import Filter, QuerySpec, Select, Sort, TimeBucket

SRC = ResolvedSource(
    sql="read_parquet('/tmp/x/*.parquet')",
    columns={"src_ip": "VARCHAR", "country": "VARCHAR", "ts": "TIMESTAMP", "amount": "DOUBLE"},
)


@pytest.fixture
def compiler() -> QueryCompiler:
    return QueryCompiler(lambda dataset_id, version: SRC)


def test_plain_select_all(compiler):
    c = compiler.compile(QuerySpec(dataset="d"))
    assert c.sql.startswith("SELECT * FROM read_parquet(")
    assert c.params == [1000]


def test_group_by_with_aggregate(compiler):
    c = compiler.compile(
        QuerySpec(
            dataset="d",
            group_by=["country"],
            select=[Select(column="*", agg="count", alias="events")],
            order_by=[Sort(column="events", desc=True)],
            limit=10,
        )
    )
    assert 'GROUP BY "country"' in c.sql
    assert 'count(*) AS "events"' in c.sql
    assert 'ORDER BY "events" DESC' in c.sql
    assert c.output_columns == ["country", "events"]


def test_time_bucket(compiler):
    c = compiler.compile(
        QuerySpec(
            dataset="d",
            time_bucket=TimeBucket(column="ts", interval="hour"),
            select=[Select(column="amount", agg="sum")],
        )
    )
    assert """date_trunc('hour', "ts") AS "bucket\"""" in c.sql
    assert 'GROUP BY date_trunc(\'hour\', "ts")' in c.sql


@pytest.mark.parametrize(
    "flt,frag,params",
    [
        (Filter(column="country", op="=", value="FR"), '"country" = ?', ["FR"]),
        (Filter(column="country", op="in", value=["FR", "DE"]), 'IN (?, ?)', ["FR", "DE"]),
        (Filter(column="country", op="in", value=[]), "FALSE", []),
        (Filter(column="src_ip", op="contains", value="10."), "ILIKE '%' || ? || '%'", ["10."]),
        (Filter(column="amount", op="between", value=[1, 5]), "BETWEEN ? AND ?", [1, 5]),
        (Filter(column="amount", op="is_null"), '"amount" IS NULL', []),
    ],
)
def test_filters(compiler, flt, frag, params):
    c = compiler.compile(QuerySpec(dataset="d", filters=[flt]))
    assert frag in c.sql
    assert c.params[: len(params)] == params


def test_unknown_column_is_rejected(compiler):
    with pytest.raises(QueryError, match="unknown column"):
        compiler.compile(QuerySpec(dataset="d", filters=[Filter(column="evil", value=1)]))
    with pytest.raises(QueryError, match="unknown column"):
        compiler.compile(QuerySpec(dataset="d", group_by=["nope"]))


def test_injection_attempt_is_not_interpolated(compiler):
    """A hostile value must land in params, never in the SQL text."""
    c = compiler.compile(
        QuerySpec(dataset="d", filters=[Filter(column="country", value="'; DROP TABLE x;--")])
    )
    assert "DROP TABLE" not in c.sql
    assert "'; DROP TABLE x;--" in c.params


def test_injection_attempt_via_column_name(compiler):
    with pytest.raises(QueryError):
        compiler.compile(QuerySpec(dataset="d", group_by=['country" ; DROP TABLE x --']))


def test_order_by_must_reference_output_column(compiler):
    with pytest.raises(QueryError, match="not an output column"):
        compiler.compile(
            QuerySpec(
                dataset="d",
                group_by=["country"],
                select=[Select(column="*", agg="count", alias="n")],
                order_by=[Sort(column="amount")],
            )
        )


def test_star_only_valid_with_count(compiler):
    with pytest.raises(QueryError, match="only valid with count"):
        compiler.compile(QuerySpec(dataset="d", select=[Select(column="*", agg="sum")]))
