"""Parquet-lake storage: the default.

Each version is a directory of immutable ``part-NNNN.parquet`` files. Immutability
gives free checkpoints; part files give resumable jobs; and ``base_uri`` can become
``s3://...`` (with httpfs) without changing anything else.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .base import PartWriter, StorageBackend, StoredRef, VersionRef


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class ParquetPartWriter(PartWriter):
    def __init__(self, directory: Path, schema: pa.Schema) -> None:
        self.dir = directory
        self.schema = schema
        self.dir.mkdir(parents=True, exist_ok=True)
        self._rows = 0

    def _path(self, part_no: int) -> Path:
        return self.dir / f"part-{part_no:05d}.parquet"

    def committed_parts(self) -> int:
        return len(sorted(self.dir.glob("part-*.parquet")))

    def discard_from(self, part_no: int) -> None:
        for p in sorted(self.dir.glob("part-*.parquet")):
            if int(p.stem.split("-")[1]) >= part_no:
                p.unlink()

    def write_part(self, part_no: int, batches: Iterable[pa.RecordBatch]) -> int:
        # Write to a temp name and rename, so a crash mid-write never leaves a
        # torn part that resume would mistake for committed.
        tmp = self._path(part_no).with_suffix(".parquet.tmp")
        rows = 0
        writer = pq.ParquetWriter(tmp, self.schema, compression="zstd")
        try:
            for b in batches:
                writer.write_batch(b)
                rows += b.num_rows
        finally:
            writer.close()
        if rows == 0:
            tmp.unlink(missing_ok=True)
            return 0
        tmp.rename(self._path(part_no))
        self._rows += rows
        return rows

    def finalize(self) -> StoredRef:
        parts = sorted(self.dir.glob("part-*.parquet"))
        rows = sum(pq.ParquetFile(p).metadata.num_rows for p in parts)
        return StoredRef(
            backend="parquet",
            location=str(self.dir),
            parts=len(parts),
            rows=rows,
            bytes=sum(p.stat().st_size for p in parts),
        )

    def abort(self) -> None:
        for p in self.dir.glob("part-*.parquet.tmp"):
            p.unlink(missing_ok=True)


class ParquetStorage(StorageBackend):
    name = "parquet"

    def __init__(self, base_dir: Path) -> None:
        self.base = Path(base_dir)

    def _dir(self, ref: VersionRef) -> Path:
        return self.base / ref.dataset_id / f"v{ref.version}"

    def write_relation(
        self, ref: VersionRef, rel_sql: str, conn, params: list | None = None
    ) -> StoredRef:
        d = self._dir(ref)
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
        target = d / "part-00000.parquet"
        conn.execute(
            f"COPY ({rel_sql}) TO {_sql_str(str(target))} "
            "(FORMAT PARQUET, COMPRESSION ZSTD)",
            params or [],
        )
        rows = pq.ParquetFile(target).metadata.num_rows if target.exists() else 0
        return StoredRef(
            backend="parquet",
            location=str(d),
            parts=1,
            rows=rows,
            bytes=target.stat().st_size if target.exists() else 0,
        )

    def open_writer(self, ref: VersionRef, schema: pa.Schema, conn) -> PartWriter:
        return ParquetPartWriter(self._dir(ref), schema)

    def sql_source(self, stored: StoredRef) -> str:
        glob = str(Path(stored.location) / "*.parquet")
        return f"read_parquet({_sql_str(glob)})"

    def drop(self, stored: StoredRef, conn) -> None:
        shutil.rmtree(stored.location, ignore_errors=True)
