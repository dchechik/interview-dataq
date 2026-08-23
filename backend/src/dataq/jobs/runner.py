"""Job scheduling.

``JobRunner`` is a protocol so the in-process implementation can be swapped for
RQ/Celery/Temporal without touching a single plugin. The v0 implementation is a
thread pool, which is the right shape here because the work is either inside
DuckDB (which releases the GIL) or awaiting network I/O.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Protocol

from ..catalog.repo import Catalog
from .context import BudgetExceeded, JobCancelled


class JobRunner(Protocol):
    def submit(self, job_id: str, fn: Callable[[], None]) -> None: ...
    def shutdown(self) -> None: ...


class ThreadPoolJobRunner:
    def __init__(self, catalog: Catalog, workers: int = 2) -> None:
        self.catalog = catalog
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dataq-job")
        self._futures: dict[str, Future] = {}
        self._lock = threading.Lock()

    def submit(self, job_id: str, fn: Callable[[], None]) -> None:
        def wrapped() -> None:
            self.catalog.update_job(
                job_id, status="running", started_at=datetime.now(UTC)
            )
            try:
                fn()
            except JobCancelled:
                self._finish(job_id, "cancelled", "cancelled by user")
            except BudgetExceeded as exc:
                # Not a failure: partial results are committed on purpose.
                self._finish(job_id, "succeeded", "")
                self.catalog.append_job_log(job_id, f"stopped early: {exc}")
            except Exception as exc:  # noqa: BLE001 - surfaced to the user via the job row
                self.catalog.append_job_log(job_id, f"ERROR {type(exc).__name__}: {exc}")
                self._finish(job_id, "failed", f"{type(exc).__name__}: {exc}")
            else:
                self._finish(job_id, "succeeded", "")
            finally:
                with self._lock:
                    self._futures.pop(job_id, None)

        with self._lock:
            self._futures[job_id] = self._pool.submit(wrapped)

    def _finish(self, job_id: str, status: str, error: str) -> None:
        self.catalog.update_job(
            job_id, status=status, error=error, finished_at=datetime.now(UTC)
        )

    def wait(self, job_id: str, timeout: float | None = None) -> None:
        """Block until a job finishes. Used by tests and the synchronous API path."""
        with self._lock:
            fut = self._futures.get(job_id)
        if fut is not None:
            fut.result(timeout=timeout)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
