from __future__ import annotations

import pytest

from dataq.config import Settings
from dataq.db import Warehouse
from dataq.storage import make_storage


@pytest.fixture(params=["parquet", "duckdb"])
def storage_mode(request) -> str:
    """Every storage-touching test runs against both backends."""
    return request.param


@pytest.fixture
def settings(tmp_path, storage_mode) -> Settings:
    return Settings(data_dir=tmp_path / "data", storage=storage_mode, duckdb_threads=2)


@pytest.fixture
def warehouse(settings) -> Warehouse:
    wh = Warehouse(settings)
    yield wh
    wh.close()


@pytest.fixture
def storage(settings):
    return make_storage(settings)
