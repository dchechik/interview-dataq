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

    # Directories the file browser may list, colon-separated. Browsing is
    # confined to these, so a hosted deployment cannot be walked from the UI.
    # Unset means the user's home directory plus the working directory, which is
    # what you want when the "server" is your own laptop.
    browse_roots: str | None = None
    # Cap on files uploaded through the browser. Server-side files are read in
    # place by DuckDB and are not subject to this.
    max_upload_mb: int = 2_048

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

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    def resolved_browse_roots(self) -> list[Path]:
        """Directories the browser may list. Always includes the upload dir, so
        files sent from the browser are visible to it afterwards."""
        if self.browse_roots:
            roots = [Path(p).expanduser() for p in self.browse_roots.split(":") if p.strip()]
        else:
            roots = [Path.home(), Path.cwd()]
        # Created eagerly: a root that does not exist is dropped below, and an
        # upload would then land somewhere the browser cannot show.
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        roots.append(self.upload_dir)
        seen: list[Path] = []
        for r in roots:
            try:
                resolved = r.resolve()
            except OSError:
                continue
            if resolved.is_dir() and resolved not in seen:
                seen.append(resolved)
        return seen

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lake_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
