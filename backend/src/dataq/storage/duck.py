"""Single-file DuckDB storage: the easy-to-host option.

Every version is a table inside one ``warehouse.duckdb``. Mount one volume, or scp
the file. The tradeoff is DuckDB's single-writer lock, so this mode cannot be used
with out-of-process job workers -- see the README.

Resume works the same way as in the Parquet backend: parts are appended to a
staging table tagged with a part number, so ``discard_from`` is a DELETE and
``finalize`` promotes the staging table to the real one.
"""

from __future__ import annotations

from collections.abc import Iterable

import pyarrow as pa

from .base import PartWriter, StorageBackend, StoredRef, VersionRef

PART_COL = "_dq_part"


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class DuckPartWriter(PartWriter):
    def __init__(self, conn, table: str, schema: pa.Schema) -> None:
        self.conn = conn
        self.table = table
        self.stage = f"_stage_{table}"
        self.schema = schema
        cols = [f"{quote_ident(f.name)} {_arrow_to_duck(f.type)}" for f in schema]
        # A zero-column schema is legal (an empty source produces one), but
        # "CREATE TABLE t ()" is not, so the part marker carries the table alone.
        self.conn.execute(
            f"CREATE TABLE IF NOT EXISTS {quote_ident(self.stage)} "
            f"({', '.join([*cols, f'{PART_COL} INTEGER'])})"
        )

    def committed_parts(self) -> int:
        row = self.conn.execute(
            f"SELECT coalesce(max({PART_COL}), -1) + 1 FROM {quote_ident(self.stage)}"
        ).fetchone()
        return int(row[0]) if row else 0

    def discard_from(self, part_no: int) -> None:
        self.conn.execute(
            f"DELETE FROM {quote_ident(self.stage)} WHERE {PART_COL} >= ?", [part_no]
        )

    def write_part(self, part_no: int, batches: Iterable[pa.RecordBatch]) -> int:
        materialised = list(batches)
        if not materialised:
            return 0
        tbl = pa.Table.from_batches(materialised, schema=self.schema)
        rows = tbl.num_rows
        if rows == 0:
            return 0
        tbl = tbl.append_column(
            PART_COL, pa.array([part_no] * rows, type=pa.int32())
        )
        self.conn.register("_dq_incoming", tbl)
        try:
            self.conn.execute(
                f"INSERT INTO {quote_ident(self.stage)} SELECT * FROM _dq_incoming"
            )
        finally:
            self.conn.unregister("_dq_incoming")
        return rows

    def finalize(self) -> StoredRef:
        self.conn.execute(f"DROP TABLE IF EXISTS {quote_ident(self.table)}")
        # Ordering by part keeps output row order identical to the Parquet backend.
        self.conn.execute(
            f"CREATE TABLE {quote_ident(self.table)} AS "
            f"SELECT * EXCLUDE ({PART_COL}) FROM {quote_ident(self.stage)} "
            f"ORDER BY {PART_COL}"
        )
        self.conn.execute(f"DROP TABLE IF EXISTS {quote_ident(self.stage)}")
        rows = self.conn.execute(
            f"SELECT count(*) FROM {quote_ident(self.table)}"
        ).fetchone()[0]
        return StoredRef(backend="duckdb", location=self.table, parts=1, rows=int(rows))

    def abort(self) -> None:
        """Discard in-flight work only -- committed parts must survive.

        Each ``write_part`` is a single INSERT, so there is no torn part to clean
        up, and the staging table is exactly what a later resume reads. Dropping it
        here would silently destroy the checkpoints that make resume possible.
        """
        return


def _arrow_to_duck(t: pa.DataType) -> str:
    if pa.types.is_boolean(t):
        return "BOOLEAN"
    if pa.types.is_integer(t):
        return "BIGINT"
    if pa.types.is_floating(t) or pa.types.is_decimal(t):
        return "DOUBLE"
    if pa.types.is_timestamp(t):
        return "TIMESTAMP"
    if pa.types.is_date(t):
        return "DATE"
    if pa.types.is_time(t):
        return "TIME"
    return "VARCHAR"


class DuckDBTableStorage(StorageBackend):
    name = "duckdb"

    def _table(self, ref: VersionRef) -> str:
        return f"ds_{ref.dataset_id}__v{ref.version}"

    def write_relation(self, ref: VersionRef, rel_sql: str, conn) -> StoredRef:
        table = self._table(ref)
        conn.execute(f"DROP TABLE IF EXISTS {quote_ident(table)}")
        conn.execute(f"CREATE TABLE {quote_ident(table)} AS {rel_sql}")
        rows = conn.execute(f"SELECT count(*) FROM {quote_ident(table)}").fetchone()[0]
        return StoredRef(backend="duckdb", location=table, parts=1, rows=int(rows))

    def open_writer(self, ref: VersionRef, schema: pa.Schema, conn) -> PartWriter:
        return DuckPartWriter(conn, self._table(ref), schema)

    def sql_source(self, stored: StoredRef) -> str:
        return quote_ident(stored.location)

    def drop(self, stored: StoredRef, conn) -> None:
        conn.execute(f"DROP TABLE IF EXISTS {quote_ident(stored.location)}")
