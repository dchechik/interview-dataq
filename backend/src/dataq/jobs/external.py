"""Facilities for ``external``-mode plugins (network / LLM calls).

A plugin author writes only ``process_rows``. Everything that makes such a plugin
survivable in production is provided here, because getting it wrong is expensive:

  * a persistent result cache, so re-running over a superset of rows only pays for
    the new rows -- the single biggest thing that makes LLM plugins usable;
  * a bounded async pool, so the plugin never manages concurrency itself;
  * retry with backoff on transient failures;
  * per-step cost accounting and a hard ``max_cost_usd`` cap;
  * row-level failure isolation: a row that fails lands as NULL plus an error
    string rather than killing an hour-long job.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config import Settings
from .context import BudgetExceeded, Cost, JobCtx

CACHE_TABLE = "_dq_external_cache"
ERROR_COLUMN_SUFFIX = "_error"


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


class ResultCache:
    """Content-addressed cache of external results, stored in the DuckDB warehouse.

    The key deliberately excludes columns the plugin did not declare in
    ``cache_key_fields``, so unrelated schema churn does not cause a cache miss.
    """

    def __init__(self, conn) -> None:
        self.conn = conn
        self.conn.execute(
            f"CREATE TABLE IF NOT EXISTS {CACHE_TABLE} ("
            "  key VARCHAR PRIMARY KEY,"
            "  plugin_id VARCHAR,"
            "  payload VARCHAR"
            ")"
        )

    @staticmethod
    def make_key(
        plugin_id: str, plugin_version: str, params: Any, model: str, fields: Any
    ) -> str:
        raw = _stable_json([plugin_id, plugin_version, params, model, fields])
        return hashlib.sha256(raw.encode()).hexdigest()

    def get_many(self, keys: list[str]) -> dict[str, dict]:
        if not keys:
            return {}
        holes = ", ".join("?" * len(keys))
        rows = self.conn.execute(
            f"SELECT key, payload FROM {CACHE_TABLE} WHERE key IN ({holes})", keys
        ).fetchall()
        return {k: json.loads(p) for k, p in rows}

    def put_many(self, plugin_id: str, items: list[tuple[str, dict]]) -> None:
        if not items:
            return
        self.conn.executemany(
            f"INSERT OR REPLACE INTO {CACHE_TABLE} (key, plugin_id, payload) VALUES (?, ?, ?)",
            [(k, plugin_id, json.dumps(v, default=str)) for k, v in items],
        )


class ModelClient(Protocol):
    """What an external plugin may call. Narrow on purpose: it keeps plugins
    testable (inject a fake) and keeps auth/model choice centralised."""

    async def complete(
        self, *, system: str, prompt: str, output_schema: dict | None = None, **kwargs: Any
    ) -> tuple[Any, Cost]: ...


@dataclass
class ExternalCtx:
    """Handed to ``Transform.process_rows``."""

    model: ModelClient | None
    settings: Settings
    params: Any
    job: JobCtx
    # Accumulated by the plugin via record_cost; enforced by the runner.
    cost: Cost = field(default_factory=Cost)

    def record_cost(self, cost: Cost) -> None:
        self.cost.merge(cost)
        self.job.cost.merge(cost)


class ExternalRunner:
    """Drives one external-mode transform over an iterable of rows."""

    def __init__(
        self,
        plugin,
        params: Any,
        ctx: JobCtx,
        cache: ResultCache,
        settings: Settings,
        model: ModelClient | None,
        max_cost_usd: float | None = None,
    ) -> None:
        self.plugin = plugin
        self.params = params
        self.ctx = ctx
        self.cache = cache
        self.settings = settings
        self.model = model
        self.max_cost_usd = max_cost_usd
        self.output_names = [n for n, _ in plugin.output_columns]

    def _null_result(self, error: str = "") -> dict[str, Any]:
        out: dict[str, Any] = dict.fromkeys(self.output_names)
        out[f"{self.plugin.id.replace('.', '_')}{ERROR_COLUMN_SUFFIX}"] = error or None
        return out

    async def _run_chunk(
        self, chunk: list[dict[str, Any]], ext: ExternalCtx, sem: asyncio.Semaphore
    ) -> list[dict[str, Any]]:
        async with sem:
            delay = 0.5
            for attempt in range(3):
                try:
                    results = await self.plugin.process_rows(chunk, ext)
                    if len(results) != len(chunk):
                        raise ValueError(
                            f"{self.plugin.id} returned {len(results)} results "
                            f"for {len(chunk)} rows"
                        )
                    return results
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    if attempt == 2:
                        self.ctx.log(
                            f"{self.plugin.id}: chunk of {len(chunk)} failed after "
                            f"3 attempts: {type(exc).__name__}: {exc}"
                        )
                        # Isolate the failure to these rows.
                        return [self._null_result(str(exc)) for _ in chunk]
                    await asyncio.sleep(delay)
                    delay *= 2
            return [self._null_result("unreachable") for _ in chunk]

    async def process(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Resolve a list of rows to results, using the cache where possible."""
        keys = [
            ResultCache.make_key(
                self.plugin.id,
                self.plugin.version,
                self.params.model_dump() if hasattr(self.params, "model_dump") else self.params,
                self.settings.model,
                list(self.plugin.cache_key_fields(row, self.params)),
            )
            for row in rows
        ]
        cached = self.cache.get_many(list(dict.fromkeys(keys)))

        results: list[dict[str, Any] | None] = [None] * len(rows)
        pending: list[int] = []
        for i, k in enumerate(keys):
            if k in cached:
                results[i] = cached[k]
                self.ctx.cost.cache_hits += 1
            else:
                pending.append(i)

        if pending:
            if self.max_cost_usd is not None and self.ctx.cost.usd >= self.max_cost_usd:
                raise BudgetExceeded(
                    f"cost cap ${self.max_cost_usd:.2f} reached "
                    f"(spent ${self.ctx.cost.usd:.2f})"
                )
            ext = ExternalCtx(
                model=self.model, settings=self.settings, params=self.params, job=self.ctx
            )
            sem = asyncio.Semaphore(self.plugin.max_concurrency)
            size = max(1, self.plugin.batch_size)
            chunks = [pending[i : i + size] for i in range(0, len(pending), size)]
            gathered = await asyncio.gather(
                *[self._run_chunk([rows[i] for i in c], ext, sem) for c in chunks]
            )
            fresh: list[tuple[str, dict]] = []
            for c, out in zip(chunks, gathered):
                for idx, res in zip(c, out):
                    results[idx] = res
                    fresh.append((keys[idx], res))
            self.cache.put_many(self.plugin.id, fresh)

        if self.max_cost_usd is not None and self.ctx.cost.usd > self.max_cost_usd:
            raise BudgetExceeded(
                f"cost cap ${self.max_cost_usd:.2f} exceeded (spent ${self.ctx.cost.usd:.2f})"
            )
        return [r if r is not None else self._null_result("no result") for r in results]
