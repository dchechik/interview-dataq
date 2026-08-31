"""Catalog repository: the only module that talks to SQLite."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import Engine, inspect
from sqlmodel import Session, SQLModel, create_engine, select

from ..config import Settings
from ..core.profile import ColumnProfile, ColumnStats, DatasetProfile, SemanticGuess
from ..storage.base import StoredRef
from .models import (
    ColumnRow,
    DashboardRow,
    DatasetRow,
    JobRow,
    SemanticTypeRow,
    StepRow,
    VersionRow,
)


def make_engine(settings: Settings) -> Engine:
    settings.ensure_dirs()
    engine = create_engine(
        f"sqlite:///{settings.catalog_path}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)
    with engine.connect() as c:
        # WAL lets the API read while a job worker writes.
        c.exec_driver_sql("PRAGMA journal_mode=WAL")
        c.exec_driver_sql("PRAGMA busy_timeout=5000")
    return engine


def _add_missing_columns(engine: Engine) -> None:
    """Bring an existing catalog up to the current models.

    ``create_all`` creates missing *tables* and silently ignores missing
    *columns*, so adding a field to a model works perfectly against a fresh
    database -- every test -- and breaks the first query against a catalog that
    already exists. Which is to say: it breaks only in deployment, and only
    after the schema change looked fine.

    Additive changes are the only ones handled, because they are the only ones
    that are safe without knowing what the data means. A column that is neither
    nullable nor defaulted cannot be filled in for existing rows, so it is left
    alone and the error it eventually causes says something true.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in have:
                    continue
                if not (column.nullable or column.default is not None):
                    continue
                ddl = column.type.compile(engine.dialect)
                conn.exec_driver_sql(
                    f"ALTER TABLE {table.name} ADD COLUMN {column.name} {ddl}"
                )


class Catalog:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @contextmanager
    def session(self) -> Iterator[Session]:
        with Session(self.engine) as s:
            yield s

    # --- datasets ----------------------------------------------------------
    def create_dataset(
        self, name: str, kind: str = "source", description: str = "",
        source_uri: str = "", view_sql: str = "",
    ) -> DatasetRow:
        with self.session() as s:
            row = DatasetRow(
                name=name, kind=kind, description=description,
                source_uri=source_uri, view_sql=view_sql,
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            return row

    def get_dataset(self, dataset_id: str) -> DatasetRow | None:
        with self.session() as s:
            return s.get(DatasetRow, dataset_id)

    def list_datasets(self) -> list[DatasetRow]:
        with self.session() as s:
            return list(s.exec(select(DatasetRow).order_by(DatasetRow.created_at.desc())))

    def delete_dataset(self, dataset_id: str) -> None:
        with self.session() as s:
            for v in s.exec(select(VersionRow).where(VersionRow.dataset_id == dataset_id)):
                for c in s.exec(select(ColumnRow).where(ColumnRow.version_id == v.id)):
                    s.delete(c)
                s.delete(v)
            ds = s.get(DatasetRow, dataset_id)
            if ds:
                s.delete(ds)
            s.commit()

    # --- versions ----------------------------------------------------------
    def next_version(self, dataset_id: str) -> int:
        with self.session() as s:
            ds = s.get(DatasetRow, dataset_id)
            return (ds.latest_version if ds else 0) + 1

    def add_version(
        self, dataset_id: str, version: int, stored: StoredRef,
        columns_schema: list[dict], row_count: int, step_id: str = "",
    ) -> VersionRow:
        with self.session() as s:
            row = VersionRow(
                dataset_id=dataset_id, version=version,
                stored_ref=stored.model_dump(), columns_schema=columns_schema,
                row_count=row_count, produced_by_step=step_id,
            )
            s.add(row)
            ds = s.get(DatasetRow, dataset_id)
            if ds and version > ds.latest_version:
                ds.latest_version = version
                s.add(ds)
            s.commit()
            s.refresh(row)
            return row

    def get_version(self, dataset_id: str, version: int | None = None) -> VersionRow | None:
        with self.session() as s:
            stmt = select(VersionRow).where(VersionRow.dataset_id == dataset_id)
            if version is None:
                stmt = stmt.order_by(VersionRow.version.desc())
            else:
                stmt = stmt.where(VersionRow.version == version)
            return s.exec(stmt).first()

    def delete_version(self, version_id: str) -> None:
        """Remove one version row and the column metadata hanging off it.

        Deliberately not touching ``DatasetRow.latest_version``: it is what
        ``next_version`` allocates from, so lowering it would hand a future
        write a number some step's provenance already refers to. The service
        layer keeps that safe by refusing to delete the newest version, which
        is what makes the high-water mark and the newest row the same number.
        """
        with self.session() as s:
            for c in s.exec(select(ColumnRow).where(ColumnRow.version_id == version_id)):
                s.delete(c)
            row = s.get(VersionRow, version_id)
            if row is not None:
                s.delete(row)
            s.commit()

    def list_versions(self, dataset_id: str) -> list[VersionRow]:
        with self.session() as s:
            return list(
                s.exec(
                    select(VersionRow)
                    .where(VersionRow.dataset_id == dataset_id)
                    .order_by(VersionRow.version.desc())
                )
            )

    # --- columns -----------------------------------------------------------
    def set_columns(self, version_id: str, columns: list[ColumnProfile]) -> None:
        with self.session() as s:
            for old in s.exec(select(ColumnRow).where(ColumnRow.version_id == version_id)):
                s.delete(old)
            for i, c in enumerate(columns):
                s.add(
                    ColumnRow(
                        version_id=version_id, position=i, name=c.name,
                        physical_type=c.physical_type, semantic_type=c.semantic_type,
                        confidence=c.confidence, role=c.role, pinned=c.pinned,
                        stats=c.stats.model_dump() if c.stats else {},
                        candidates=[g.model_dump() for g in c.candidates],
                        warning=c.warning,
                    )
                )
            s.commit()

    def pin_column_type(
        self, version_id: str, name: str, semantic_type: str | None, role: str | None = None
    ) -> None:
        """A human edit. Pinning freezes the type against future re-detection."""
        with self.session() as s:
            row = s.exec(
                select(ColumnRow).where(
                    ColumnRow.version_id == version_id, ColumnRow.name == name
                )
            ).first()
            if row is None:
                raise KeyError(f"no column {name!r} in version {version_id}")
            row.semantic_type = semantic_type
            row.confidence = 1.0
            row.pinned = True
            if role:
                row.role = role
            s.add(row)
            s.commit()

    # --- semantic types ----------------------------------------------------
    def list_semantic_types(self) -> list[SemanticTypeRow]:
        with self.session() as s:
            return list(s.exec(select(SemanticTypeRow).order_by(SemanticTypeRow.id)))

    def upsert_semantic_type(self, row: SemanticTypeRow) -> SemanticTypeRow:
        """Define a meaning, or edit one that exists.

        Upsert rather than insert because the id *is* the identity: two people
        deciding independently that a column means ``machine.name`` should land
        on the same type, not on a conflict.
        """
        with self.session() as s:
            existing = s.get(SemanticTypeRow, row.id)
            if existing is not None:
                existing.title = row.title
                existing.parent = row.parent
                existing.role = row.role
                existing.joinable = row.joinable
                existing.description = row.description
                s.add(existing)
                s.commit()
                s.refresh(existing)
                return existing
            s.add(row)
            s.commit()
            s.refresh(row)
            return row

    def delete_semantic_type(self, type_id: str) -> bool:
        with self.session() as s:
            row = s.get(SemanticTypeRow, type_id)
            if row is None:
                return False
            s.delete(row)
            s.commit()
            return True

    def columns_using_type(self, type_id: str) -> list[tuple[str, str, str]]:
        """``(dataset_id, dataset_name, column)`` for every column of this type.

        Across all versions, not just the latest: a type still spelled out in an
        older version's metadata is still in use, and deleting it would leave
        that version describing itself in a vocabulary nothing can read.
        """
        with self.session() as s:
            rows = s.exec(
                select(DatasetRow.id, DatasetRow.name, ColumnRow.name)
                .join(VersionRow, VersionRow.dataset_id == DatasetRow.id)
                .join(ColumnRow, ColumnRow.version_id == VersionRow.id)
                .where(ColumnRow.semantic_type == type_id)
            ).all()
        seen: dict[tuple[str, str], tuple[str, str, str]] = {}
        for dataset_id, dataset_name, column in rows:
            seen.setdefault((dataset_id, column), (dataset_id, dataset_name, column))
        return sorted(seen.values(), key=lambda r: (r[1], r[2]))

    def get_profile(self, dataset_id: str, version: int | None = None) -> DatasetProfile | None:
        v = self.get_version(dataset_id, version)
        if v is None:
            return None
        with self.session() as s:
            rows = list(
                s.exec(
                    select(ColumnRow)
                    .where(ColumnRow.version_id == v.id)
                    .order_by(ColumnRow.position)
                )
            )
        if rows:
            columns = [
                ColumnProfile(
                    name=r.name, physical_type=r.physical_type,
                    semantic_type=r.semantic_type, confidence=r.confidence,
                    role=r.role, pinned=r.pinned,
                    stats=ColumnStats(**r.stats) if r.stats else None,
                    candidates=[SemanticGuess(**g) for g in r.candidates],
                    warning=r.warning,
                )
                for r in rows
            ]
        else:
            # Version exists but has not been profiled yet.
            columns = [
                ColumnProfile(name=c["name"], physical_type=c["physical_type"])
                for c in v.columns_schema
            ]
        return DatasetProfile(
            dataset_id=dataset_id, version=v.version, row_count=v.row_count, columns=columns
        )

    # --- jobs & steps ------------------------------------------------------
    def create_job(self, title: str) -> JobRow:
        with self.session() as s:
            row = JobRow(title=title)
            s.add(row)
            s.commit()
            s.refresh(row)
            return row

    def get_job(self, job_id: str) -> JobRow | None:
        with self.session() as s:
            return s.get(JobRow, job_id)

    def list_jobs(self, limit: int = 50) -> list[JobRow]:
        with self.session() as s:
            return list(
                s.exec(select(JobRow).order_by(JobRow.created_at.desc()).limit(limit))
            )

    def update_job(self, job_id: str, **fields) -> JobRow | None:
        with self.session() as s:
            row = s.get(JobRow, job_id)
            if row is None:
                return None
            for k, v in fields.items():
                setattr(row, k, v)
            s.add(row)
            s.commit()
            s.refresh(row)
            return row

    def append_job_log(self, job_id: str, message: str) -> None:
        with self.session() as s:
            row = s.get(JobRow, job_id)
            if row is None:
                return
            stamped = f"{datetime.now(UTC).isoformat(timespec='seconds')} {message}"
            # Reassign: SQLAlchemy does not track in-place mutation of JSON columns.
            row.logs = [*row.logs, stamped][-500:]
            s.add(row)
            s.commit()

    def is_cancelled(self, job_id: str) -> bool:
        with self.session() as s:
            row = s.get(JobRow, job_id)
            return bool(row and row.cancel_requested)

    def create_step(self, job_id: str, **fields) -> StepRow:
        with self.session() as s:
            row = StepRow(job_id=job_id, **fields)
            s.add(row)
            s.commit()
            s.refresh(row)
            return row

    def get_step(self, step_id: str) -> StepRow | None:
        with self.session() as s:
            return s.get(StepRow, step_id)

    def list_steps(self, job_id: str) -> list[StepRow]:
        with self.session() as s:
            return list(
                s.exec(select(StepRow).where(StepRow.job_id == job_id).order_by(StepRow.created_at))
            )

    def update_step(self, step_id: str, **fields) -> StepRow | None:
        with self.session() as s:
            row = s.get(StepRow, step_id)
            if row is None:
                return None
            for k, v in fields.items():
                setattr(row, k, v)
            s.add(row)
            s.commit()
            s.refresh(row)
            return row

    def lineage(self, dataset_id: str) -> list[StepRow]:
        """Every step that produced a version of this dataset, oldest first."""
        with self.session() as s:
            steps = list(s.exec(select(StepRow).order_by(StepRow.created_at)))
        return [st for st in steps if any(o.get("dataset_id") == dataset_id for o in st.outputs)]

    def derivation_steps(self) -> list[StepRow]:
        """Steps that created a *new dataset* from existing ones, oldest first.

        Only ``aggregate`` and ``join`` qualify. A ``transform`` produces a new
        version of the dataset it was given, not a child, and an ``import`` has no
        parent at all -- so neither forms an edge in the derivation tree.
        """
        with self.session() as s:
            steps = list(
                s.exec(
                    select(StepRow)
                    .where(StepRow.op.in_(("aggregate", "join")))  # type: ignore[attr-defined]
                    .where(StepRow.status == "succeeded")
                    .order_by(StepRow.created_at)
                )
            )
        return [st for st in steps if st.outputs and st.inputs]

    # --- dashboards --------------------------------------------------------
    def save_dashboard(
        self, name: str, panels: list[dict], description: str = "",
        dashboard_id: str | None = None,
    ) -> DashboardRow:
        with self.session() as s:
            row = s.get(DashboardRow, dashboard_id) if dashboard_id else None
            if row is None:
                row = DashboardRow(name=name, panels=panels, description=description)
            else:
                row.name, row.panels, row.description = name, panels, description
                row.updated_at = datetime.now(UTC)
            s.add(row)
            s.commit()
            s.refresh(row)
            return row

    def list_dashboards(self) -> list[DashboardRow]:
        with self.session() as s:
            return list(s.exec(select(DashboardRow).order_by(DashboardRow.updated_at.desc())))

    def get_dashboard(self, dashboard_id: str) -> DashboardRow | None:
        with self.session() as s:
            return s.get(DashboardRow, dashboard_id)
