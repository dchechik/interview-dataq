"""Storage backend protocol.

DuckDB is DataQ's query *engine*; it is not necessarily the storage *format*. Two
DuckDB properties force that split:

  * single-writer -- one process may hold a .duckdb file for writing, so any
    out-of-process worker deadlocks;
  * a .duckdb file is opaque -- you cannot hand one version to another tool, and
    object storage is not a natural fit.

So dataset versions go through this protocol. ``parquet`` is the default (immutable
parts, resumable jobs, S3-portable); ``duckdb`` keeps everything in one file, which
is the easiest thing to host remotely.
"""

from __future__ import annotations

import abc
from collections.abc import Iterable, Iterator
from typing import Literal

import pyarrow as pa
from pydantic import BaseModel


class VersionRef(BaseModel):
    """Identifies the version being written."""

    dataset_id: str
    version: int

    @property
    def slug(self) -> str:
        return f"{self.dataset_id}_v{self.version}"


class StoredRef(BaseModel):
    """Where a materialised version actually lives."""

    backend: Literal["parquet", "duckdb"]
    location: str  # directory path (parquet) or table name (duckdb)
    parts: int = 0
    rows: int = 0
    bytes: int = 0


class PartWriter(abc.ABC):
    """Part-wise writer enabling checkpoint/resume of long jobs.

    A job flushes a part every N batches and records the part number on its Step.
    On resume it calls ``discard_from(watermark)`` and continues, so an interrupted
    run produces the same output as an uninterrupted one.
    """

    @abc.abstractmethod
    def committed_parts(self) -> int: ...

    @abc.abstractmethod
    def discard_from(self, part_no: int) -> None:
        """Drop any partially-written parts at or after ``part_no``."""

    @abc.abstractmethod
    def write_part(self, part_no: int, batches: Iterable[pa.RecordBatch]) -> int:
        """Write one part; return rows written."""

    @abc.abstractmethod
    def finalize(self) -> StoredRef: ...

    @abc.abstractmethod
    def abort(self) -> None: ...


class StorageBackend(abc.ABC):
    name: str

    @abc.abstractmethod
    def write_relation(
        self, ref: VersionRef, rel_sql: str, conn, params: list | None = None
    ) -> StoredRef:
        """One-shot materialisation of a SQL query (the ``pushdown`` path).

        ``params`` are bound, not interpolated, so a compiled QuerySpec keeps its
        literals parameterised all the way down to materialisation.
        """

    @abc.abstractmethod
    def open_writer(self, ref: VersionRef, schema: pa.Schema, conn) -> PartWriter:
        """Part-wise materialisation (the ``batch`` / ``external`` paths)."""

    @abc.abstractmethod
    def sql_source(self, stored: StoredRef) -> str:
        """A SQL expression usable in a FROM clause."""

    @abc.abstractmethod
    def drop(self, stored: StoredRef, conn) -> None: ...

    def drop_dataset(self, dataset_id: str, conn) -> None:
        """Remove every version of a dataset, listed or not.

        Deleting version by version trusts the catalog to know what exists, and
        it does not always: a run that failed between writing files and
        recording the version leaves data nothing points at. Dropping by
        dataset id closes that gap, so deletion frees the disk rather than
        usually freeing the disk.
        """
        return


def batched(it: Iterator[pa.RecordBatch], n: int) -> Iterator[list[pa.RecordBatch]]:
    """Group record batches into chunks of at most ``n`` batches."""
    chunk: list[pa.RecordBatch] = []
    for b in it:
        chunk.append(b)
        if len(chunk) >= n:
            yield chunk
            chunk = []
    if chunk:
        yield chunk
