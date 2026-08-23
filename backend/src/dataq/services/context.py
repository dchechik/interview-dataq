"""Application context: the wiring shared by the API, the job runner and the agent.

Handlers hold no business logic; they resolve a plugin and call into services that
take this context. The agent binds its tools to the same services, not to HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..catalog.repo import Catalog, make_engine
from ..config import Settings, get_settings
from ..db import Warehouse
from ..query.compiler import QueryCompiler, QueryError, ResolvedSource
from ..storage import StorageBackend, make_storage
from ..storage.base import StoredRef


@dataclass
class AppContext:
    settings: Settings
    catalog: Catalog
    warehouse: Warehouse
    storage: StorageBackend
    runner: object | None = None  # ThreadPoolJobRunner; set at startup

    def resolve_source(self, dataset_id: str, version: int | None = None) -> ResolvedSource:
        """Turn a dataset reference into something usable in a FROM clause."""
        ds = self.catalog.get_dataset(dataset_id)
        if ds is None:
            raise QueryError(f"unknown dataset: {dataset_id}")

        row = self.catalog.get_version(dataset_id, version)
        if row is not None:
            stored = StoredRef(**row.stored_ref)
            columns = {c["name"]: c["physical_type"] for c in row.columns_schema}
            return ResolvedSource(sql=self.storage.sql_source(stored), columns=columns)

        # Unmaterialised join/aggregate datasets are backed by a view.
        if ds.view_sql:
            with self.warehouse.cur() as conn:
                rel = conn.sql(ds.view_sql)
                columns = dict(zip(rel.columns, [str(t) for t in rel.types]))
            return ResolvedSource(sql=f"({ds.view_sql})", columns=columns)

        raise QueryError(f"dataset {dataset_id} has no materialised version")

    def compiler(self) -> QueryCompiler:
        return QueryCompiler(self.resolve_source)


def build_context(settings: Settings | None = None) -> AppContext:
    settings = settings or get_settings()
    settings.ensure_dirs()
    catalog = Catalog(make_engine(settings))
    return AppContext(
        settings=settings,
        catalog=catalog,
        warehouse=Warehouse(settings),
        storage=make_storage(settings),
    )
