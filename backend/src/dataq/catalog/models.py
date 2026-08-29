"""Catalog tables (SQLite via SQLModel).

The catalog is deliberately *not* stored in the DuckDB warehouse. It takes many
small transactional writes -- job heartbeats, step status, column metadata -- which
is exactly DuckDB's weak spot, and co-locating it would make DuckDB's single write
lock the bottleneck for the whole API.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Column, Index
from sqlalchemy.types import JSON
from sqlmodel import Field, SQLModel


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def utcnow() -> datetime:
    return datetime.now(UTC)


def _json(default: Any) -> Any:
    return Field(default_factory=lambda: default, sa_column=Column(JSON))


class DatasetRow(SQLModel, table=True):
    __tablename__ = "datasets"

    id: str = Field(default_factory=new_id, primary_key=True)
    name: str = Field(index=True)
    kind: str = "source"  # source | derived | aggregate | join
    description: str = ""
    source_uri: str = ""
    # For kind="join"/"aggregate": the SQL view backing it, if not materialised.
    view_sql: str = ""
    latest_version: int = 0
    created_at: datetime = Field(default_factory=utcnow)


class VersionRow(SQLModel, table=True):
    __tablename__ = "dataset_versions"
    __table_args__ = (Index("ix_version_dataset", "dataset_id", "version", unique=True),)

    id: str = Field(default_factory=new_id, primary_key=True)
    dataset_id: str = Field(index=True, foreign_key="datasets.id")
    version: int = 1
    row_count: int = 0
    # storage.StoredRef, serialised.
    stored_ref: dict = _json({})
    # [{"name": ..., "physical_type": ...}] in column order.
    columns_schema: list = _json([])
    produced_by_step: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class ColumnRow(SQLModel, table=True):
    """Per-version column metadata: the semantic layer that drives suggestion."""

    __tablename__ = "columns"
    __table_args__ = (Index("ix_column_version", "version_id", "name", unique=True),)

    id: str = Field(default_factory=new_id, primary_key=True)
    version_id: str = Field(index=True, foreign_key="dataset_versions.id")
    position: int = 0
    name: str = ""
    physical_type: str = ""
    semantic_type: str | None = None
    confidence: float = 0.0
    role: str = "dimension"
    # Set when a human edits the type; freezes it against re-detection.
    pinned: bool = False
    stats: dict = _json({})
    candidates: list = _json([])
    # Something the reader decided that the data could not settle -- see
    # ColumnProfile.warning.
    warning: str | None = None


class JobRow(SQLModel, table=True):
    __tablename__ = "jobs"

    id: str = Field(default_factory=new_id, primary_key=True)
    title: str = ""
    status: str = Field(default="queued", index=True)
    # {"rows_done":, "rows_total":, "pct":, "eta_s":}
    progress: dict = _json({})
    logs: list = _json([])
    error: str = ""
    cancel_requested: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class StepRow(SQLModel, table=True):
    """One plugin invocation. Collectively these form the replayable lineage DAG."""

    __tablename__ = "steps"

    id: str = Field(default_factory=new_id, primary_key=True)
    job_id: str = Field(index=True, foreign_key="jobs.id")
    op: str = ""            # import | transform | aggregate | join
    plugin_id: str = ""
    plugin_version: str = ""
    params: dict = _json({})
    inputs: list = _json([])    # [{"dataset_id":, "version":}]
    outputs: list = _json([])   # [{"dataset_id":, "version":}]
    status: str = "queued"
    # Checkpoint watermark: parts fully written and durable.
    parts_committed: int = 0
    rows_committed: int = 0
    # External-mode accounting: {"calls":, "tokens_in":, "tokens_out":, "usd":, "cache_hits":}
    cost: dict = _json({})
    error: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None


class DashboardRow(SQLModel, table=True):
    __tablename__ = "dashboards"

    id: str = Field(default_factory=new_id, primary_key=True)
    name: str = ""
    description: str = ""
    # [VizSpec, ...] plus layout hints.
    panels: list = _json([])
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
