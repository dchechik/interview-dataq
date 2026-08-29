"""Built-in readers.

Readers hand back a DuckDB relation rather than Arrow batches, so DuckDB streams
and parallelises the scan of a multi-GB file itself instead of paying Python's
per-batch overhead.
"""

from __future__ import annotations

from typing import ClassVar

import duckdb
from pydantic import BaseModel, Field

from ..base import register
from ..kinds import Reader


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class CsvParams(BaseModel):
    delimiter: str | None = Field(default=None, description="Auto-detected when omitted")
    header: bool = True
    sample_size: int = Field(default=20_480, description="Rows DuckDB sniffs for typing")
    all_varchar: bool = Field(
        default=False, description="Read every column as text and normalize later"
    )
    ignore_errors: bool = Field(
        default=False, description="Skip malformed rows instead of failing the import"
    )
    # DuckDB's sniffer decides for itself whether 03/04/2016 is March or April,
    # and does not report which it chose. These pin it. Profiling raises a
    # warning naming the column when the raw text cannot settle the question.
    dateformat: str | None = Field(
        default=None, description="strptime format for DATE columns, e.g. %d/%m/%Y"
    )
    timestampformat: str | None = Field(
        default=None, description="strptime format for TIMESTAMP columns"
    )
    # Per-column type overrides. Used to hold a column back as text so the
    # import can apply a chosen reading: once the sniffer has turned 03/04/2016
    # into a DATE, which reading it took is unrecoverable. Forcing *to* VARCHAR
    # is the safe direction -- no value fails to become text -- whereas forcing
    # to a real type aborts the whole read on the first row that will not
    # convert. Casting to real types happens after the read, in the projection.
    column_types: dict[str, str] = Field(
        default_factory=dict, description="Column name -> type to read it as"
    )


@register
class CsvReader(Reader):
    """Delimited text (CSV/TSV), with DuckDB's type sniffer."""

    id: ClassVar[str] = "read.csv"
    title: ClassVar[str] = "CSV / TSV"
    extensions: ClassVar[tuple[str, ...]] = (".csv", ".tsv", ".txt", ".csv.gz", ".tsv.gz")
    Params: ClassVar[type[BaseModel]] = CsvParams

    def to_relation(self, conn, uri: str, params: CsvParams) -> duckdb.DuckDBPyRelation:
        opts = [
            f"header={'true' if params.header else 'false'}",
            f"sample_size={int(params.sample_size)}",
        ]
        if params.delimiter:
            opts.append(f"delim={_lit(params.delimiter)}")
        if params.all_varchar:
            opts.append("all_varchar=true")
        if params.ignore_errors:
            opts.append("ignore_errors=true")
        if params.dateformat:
            opts.append(f"dateformat={_lit(params.dateformat)}")
        if params.timestampformat:
            opts.append(f"timestampformat={_lit(params.timestampformat)}")
        if params.column_types:
            pairs = ", ".join(f"{_lit(name)}: {_lit(kind)}"
                              for name, kind in params.column_types.items())
            opts.append(f"types={{{pairs}}}")
        return conn.sql(f"SELECT * FROM read_csv({_lit(uri)}, {', '.join(opts)})")


class ParquetParams(BaseModel):
    union_by_name: bool = Field(
        default=False, description="Union files with differing column orders by name"
    )


@register
class ParquetReader(Reader):
    """Parquet files, including globs and Hive-partitioned directories."""

    id: ClassVar[str] = "read.parquet"
    title: ClassVar[str] = "Parquet"
    extensions: ClassVar[tuple[str, ...]] = (".parquet", ".pq")
    Params: ClassVar[type[BaseModel]] = ParquetParams

    def to_relation(self, conn, uri: str, params: ParquetParams) -> duckdb.DuckDBPyRelation:
        opts = ", union_by_name=true" if params.union_by_name else ""
        return conn.sql(f"SELECT * FROM read_parquet({_lit(uri)}{opts})")


class JsonParams(BaseModel):
    format: str = Field(default="auto", description="auto | newline_delimited | array")
    sample_size: int = 20_480


@register
class JsonReader(Reader):
    """JSON and newline-delimited JSON."""

    id: ClassVar[str] = "read.json"
    title: ClassVar[str] = "JSON / NDJSON"
    extensions: ClassVar[tuple[str, ...]] = (".json", ".ndjson", ".jsonl", ".json.gz")
    Params: ClassVar[type[BaseModel]] = JsonParams

    def to_relation(self, conn, uri: str, params: JsonParams) -> duckdb.DuckDBPyRelation:
        return conn.sql(
            f"SELECT * FROM read_json({_lit(uri)}, format={_lit(params.format)}, "
            f"sample_size={int(params.sample_size)})"
        )


def pick_reader(uri: str) -> type[Reader] | None:
    """Choose a reader by extension. Falls back to CSV, which sniffs aggressively."""
    from ..base import REGISTRY

    readers = [r for r in REGISTRY.list(kind="reader")]
    for r in readers:
        if r.can_read(uri):  # type: ignore[attr-defined]
            return r  # type: ignore[return-value]
    return None
