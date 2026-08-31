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


# Rejected rows DuckDB will keep a record of. Bounded because the record is a
# diagnostic, not the data: a file whose every line is malformed should not cost
# memory proportional to itself. Hitting the cap is reported as "at least N"
# rather than silently rounding the truth down.
REJECT_LIMIT = 10_000


class Rejected(BaseModel):
    """Rows a CSV read skipped, and why.

    Empty when nothing was skipped, which is the ordinary case: a caller can
    treat a falsy value as "nothing was lost".
    """

    rows: int = 0
    capped: bool = False
    # "line 30001: Expected Number of Columns: 2 Found: 3", a few of them.
    examples: list[str] = []

    def __bool__(self) -> bool:
        return self.rows > 0

    def describe(self) -> str:
        at_least = "at least " if self.capped else ""
        head = (f"skipped {at_least}{self.rows:,} row(s) that could not be parsed")
        return f"{head}: {'; '.join(self.examples)}" if self.examples else head


def rejected_rows(conn, examples: int = 3) -> Rejected:
    """What a ``store_rejects`` read threw away, on this connection.

    Counting distinct (file, line) rather than reject rows: DuckDB records one
    row per bad *column*, so a single broken line reports three times on a
    three-column file. Deduplicating also makes repeat scans of the same file
    -- the import does several, for the sniffer check and the cast measurement
    -- count once rather than once per pass.

    The reject tables are TEMP, so they exist only on the connection that did
    the read and only once such a read has happened. A missing table therefore
    means "nothing was skipped", not an error.
    """
    try:
        rows = conn.execute(
            "SELECT count(DISTINCT (s.file_path, e.line)) "
            "FROM reject_errors e JOIN reject_scans s USING (scan_id)"
        ).fetchone()
        stored = conn.execute("SELECT count(*) FROM reject_errors").fetchone()
        sample = conn.execute(
            "SELECT DISTINCT ON (s.file_path, e.line) e.line, e.error_message "
            "FROM reject_errors e JOIN reject_scans s USING (scan_id) "
            f"ORDER BY s.file_path, e.line LIMIT {int(examples)}"
        ).fetchall()
    except Exception:  # noqa: BLE001 -- no reject tables means nothing was skipped
        return Rejected()
    return Rejected(
        rows=int(rows[0]) if rows else 0,
        capped=bool(stored and stored[0] >= REJECT_LIMIT),
        examples=[f"line {line}: {message}" for line, message in sample],
    )


class CsvParams(BaseModel):
    delimiter: str | None = Field(default=None, description="Auto-detected when omitted")
    header: bool = True
    sample_size: int = Field(default=20_480, description="Rows DuckDB sniffs for typing")
    all_varchar: bool = Field(
        default=False, description="Read every column as text and normalize later"
    )
    # What to do with a row that does not match the columns the sniffer settled
    # on. DuckDB stops on the first one, which is the right default -- a file
    # that does not parse is usually a file read with the wrong settings, and
    # silently dropping part of it would hide that. But some exports genuinely
    # do contain a handful of broken lines, and then the choice is the user's.
    #
    # The three flags are not interchangeable, and neither is the pair of
    # outcomes they produce:
    #   strict_mode=false   a row with *extra* values loads, the extras dropped
    #   null_padding=true   a row with *missing* values loads, padded with NULL
    #   ignore_errors=true  the row is skipped entirely, whichever way it is bad
    # Keeping a row therefore takes both of the first two: on its own, each
    # still fails on the shape it does not cover.
    ignore_errors: bool = Field(
        default=False, description="Skip malformed rows instead of failing the import"
    )
    strict_mode: bool = Field(
        default=True, description="Require rows to comply with the CSV standard"
    )
    null_padding: bool = Field(
        default=False, description="Pad rows with missing trailing values with NULL"
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
            # store_rejects rides along automatically rather than being a
            # separate setting: skipping rows without counting them is the one
            # version of this feature that must not exist. It is bounded so a
            # file that is broken all the way through cannot fill memory with a
            # record of it.
            opts.append("ignore_errors=true")
            opts.append("store_rejects=true")
            opts.append(f"rejects_limit={REJECT_LIMIT}")
        if not params.strict_mode:
            opts.append("strict_mode=false")
        if params.null_padding:
            opts.append("null_padding=true")
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
