"""DuckDB connection management.

One DuckDB instance per process. Each unit of work takes a ``cursor()``, which is an
independent, thread-safe execution context sharing the same catalog and buffer pool
-- this is how DuckDB is meant to be used from a threaded server.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

import duckdb

from .config import Settings


class UnsafeSQLError(ValueError):
    """Raised when the raw-SQL escape hatch is handed something that is not a
    single read-only SELECT."""


class Warehouse:
    def __init__(self, settings: Settings) -> None:
        settings.ensure_dirs()
        self.settings = settings
        self._conn = duckdb.connect(str(settings.warehouse_path))
        self._conn.execute(f"SET memory_limit='{settings.duckdb_memory_limit}'")
        self._conn.execute(f"SET threads={settings.duckdb_threads}")
        # Serialises DDL so two jobs cannot race on CREATE/DROP TABLE.
        self.ddl_lock = threading.Lock()

    def cursor(self) -> duckdb.DuckDBPyConnection:
        return self._conn.cursor()

    @contextmanager
    def cur(self) -> Iterator[duckdb.DuckDBPyConnection]:
        c = self._conn.cursor()
        try:
            yield c
        finally:
            c.close()

    def close(self) -> None:
        self._conn.close()


def assert_read_only(sql: str) -> str:
    """Gate the raw-SQL path using DuckDB's own parser.

    Requires exactly one statement, of type SELECT. Parsing with DuckDB rather than
    a regex means comment tricks and stacked statements cannot slip through.

    Note: this prevents writes, not filesystem reads -- a SELECT may still call
    ``read_csv``. That is acceptable for a single-user local tool; a multi-tenant
    deployment should additionally run this path in a process with
    ``enable_external_access=false``.
    """
    try:
        statements = duckdb.extract_statements(sql)
    except Exception as exc:  # duckdb raises ParserException
        raise UnsafeSQLError(f"could not parse SQL: {exc}") from exc
    if len(statements) != 1:
        raise UnsafeSQLError(
            f"expected exactly one statement, got {len(statements)}"
        )
    st_type = str(statements[0].type).rsplit(".", 1)[-1]
    if st_type != "SELECT":
        raise UnsafeSQLError(f"only SELECT statements are allowed here, got {st_type}")
    return sql
