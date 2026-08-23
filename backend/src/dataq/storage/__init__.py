"""Storage backend selection."""

from __future__ import annotations

from ..config import Settings
from .base import PartWriter, StorageBackend, StoredRef, VersionRef, batched
from .duck import DuckDBTableStorage
from .parquet import ParquetStorage

__all__ = [
    "DuckDBTableStorage",
    "ParquetStorage",
    "PartWriter",
    "StorageBackend",
    "StoredRef",
    "VersionRef",
    "batched",
    "make_storage",
]


def make_storage(settings: Settings) -> StorageBackend:
    if settings.storage == "duckdb":
        return DuckDBTableStorage()
    return ParquetStorage(settings.lake_dir)
