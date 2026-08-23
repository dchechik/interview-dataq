"""Job execution context: progress, logging, cancellation and checkpointing."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..catalog.repo import Catalog


class JobCancelled(Exception):
    """Raised inside a running job when the user requests cancellation."""


class BudgetExceeded(Exception):
    """Raised when an external-mode step hits its ``max_cost_usd`` cap."""


@dataclass
class Cost:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0
    cache_hits: int = 0

    def merge(self, other: Cost) -> None:
        self.calls += other.calls
        self.tokens_in += other.tokens_in
        self.tokens_out += other.tokens_out
        self.usd += other.usd
        self.cache_hits += other.cache_hits

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "usd": round(self.usd, 6),
            "cache_hits": self.cache_hits,
        }


@dataclass
class JobCtx:
    """Handed to every executing step. The only channel a plugin has to the outside
    world besides its own return value."""

    catalog: Catalog
    job_id: str
    step_id: str
    rows_total: int = 0
    cost: Cost = field(default_factory=Cost)
    _started: float = field(default_factory=time.monotonic)
    _last_progress: float = 0.0

    def log(self, message: str) -> None:
        self.catalog.append_job_log(self.job_id, message)

    def cancelled(self) -> bool:
        return self.catalog.is_cancelled(self.job_id)

    def check_cancelled(self) -> None:
        if self.cancelled():
            raise JobCancelled(f"job {self.job_id} cancelled")

    def progress(self, rows_done: int, force: bool = False) -> None:
        """Throttled so a fast batch loop does not hammer SQLite."""
        now = time.monotonic()
        if not force and now - self._last_progress < 0.25:
            return
        self._last_progress = now
        elapsed = now - self._started
        rate = rows_done / elapsed if elapsed > 0 else 0.0
        remaining = max(0, self.rows_total - rows_done)
        self.catalog.update_job(
            self.job_id,
            progress={
                "rows_done": rows_done,
                "rows_total": self.rows_total,
                "pct": round(100.0 * rows_done / self.rows_total, 2)
                if self.rows_total
                else None,
                "rows_per_s": round(rate, 1),
                "eta_s": round(remaining / rate, 1) if rate > 0 and self.rows_total else None,
                "cost": self.cost.as_dict(),
            },
        )

    def checkpoint(self, parts: int, rows: int) -> None:
        """Record the durable watermark. Resume reads exactly these two numbers."""
        self.catalog.update_step(self.step_id, parts_committed=parts, rows_committed=rows)
