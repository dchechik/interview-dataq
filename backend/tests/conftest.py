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


@pytest.fixture
def app_ctx(settings):
    """A fully wired AppContext with a job runner, torn down after each test."""
    import dataq.plugins.builtin  # noqa: F401  (registers plugins)
    from dataq.jobs.runner import ThreadPoolJobRunner
    from dataq.services.context import build_context

    ctx = build_context(settings)
    ctx.runner = ThreadPoolJobRunner(ctx.catalog, workers=2)
    yield ctx
    ctx.runner.shutdown()
    ctx.warehouse.close()


@pytest.fixture
def run_op(app_ctx):
    """Submit an operation and block until it finishes; assert it succeeded."""
    from dataq.services.operations import OperationRequest, submit_operation

    def _run(**kwargs) -> str:
        accepted = submit_operation(app_ctx, OperationRequest(**kwargs))
        app_ctx.runner.wait(accepted.job_id, timeout=120)
        job = app_ctx.catalog.get_job(accepted.job_id)
        assert job.status == "succeeded", f"{job.status}: {job.error}\n{job.logs}"
        step = app_ctx.catalog.get_step(accepted.step_id)
        return step.outputs[0]["dataset_id"]

    return _run
