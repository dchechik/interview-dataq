"""Application configuration. All settings are env-overridable (prefix ``DATAQ_``)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DATAQ_", env_file=".env", extra="ignore")

    # Where all state lives. A single directory so deployment is "mount one volume".
    data_dir: Path = Path("./data")

    # Which StorageBackend to use for dataset versions.
    #   parquet -> immutable Parquet parts (resumable jobs, S3-portable)  [default]
    #   duckdb  -> tables inside one .duckdb file (single-file, easy to host)
    storage: Literal["parquet", "duckdb"] = "parquet"

    # DuckDB runtime limits. Kept modest so a laptop stays responsive on 10GB files.
    duckdb_memory_limit: str = "4GB"
    duckdb_threads: int = 4

    # Job execution.
    job_workers: int = 2
    batch_rows: int = 100_000
    checkpoint_every_batches: int = 5

    # Profiling.
    profile_sample_rows: int = 10_000
    dry_run_rows: int = 1_000

    # Agent / LLM plugins.
    anthropic_api_key: str | None = None
    model: str = "claude-opus-5"

    # Built frontend bundle. Served at / when present, so production is a single
    # container. Unset in dev, where Vite serves the SPA and proxies /api.
    static_dir: Path | None = None

    @property
    def catalog_path(self) -> Path:
        return self.data_dir / "catalog.sqlite"

    @property
    def warehouse_path(self) -> Path:
        """The .duckdb file: the warehouse in duckdb storage mode, and the home of
        the external-plugin result cache in both modes."""
        return self.data_dir / "warehouse.duckdb"

    @property
    def lake_dir(self) -> Path:
        return self.data_dir / "lake"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lake_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
