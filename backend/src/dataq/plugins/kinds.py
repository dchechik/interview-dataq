"""The six plugin kinds.

Each kind has its own interface because each genuinely consumes and produces
different things. What they share is the *invocation envelope* and the *execution
model* -- see :mod:`dataq.plugins.base` and :mod:`dataq.jobs`.

  Reader      URI            -> relation             (import)
  Detector    column stats   -> semantic guesses     (inspect)
  Transform   DatasetVersion -> DatasetVersion       (normalize / extract / annotate)
  Aggregator  dataset(s)     -> QuerySpec            (new aggregate dataset)
  Suggester   catalog ctx    -> Suggestions          (inspect)
  Visualizer  dataset        -> VizSpec              (inspect)
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

import pyarrow as pa
from pydantic import BaseModel

from ..core.profile import ColumnStats, DatasetProfile, SemanticGuess
from ..core.viz import VizSpec
from ..query.spec import QuerySpec
from .base import Plugin

if TYPE_CHECKING:
    import duckdb

    from ..jobs.external import ExternalCtx


# --------------------------------------------------------------------------- #
# 1. Reader
# --------------------------------------------------------------------------- #
class Reader(Plugin, abc.ABC):
    """Turns a source URI into a DuckDB relation.

    Returns a *relation*, not Arrow batches, so DuckDB streams a 10GB file itself
    instead of round-tripping it through Python.
    """

    kind: ClassVar[str] = "reader"
    mode: ClassVar[str] = "pushdown"
    extensions: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def can_read(cls, uri: str) -> bool:
        return any(uri.lower().split("?")[0].endswith(e) for e in cls.extensions)

    @abc.abstractmethod
    def to_relation(
        self, conn: duckdb.DuckDBPyConnection, uri: str, params: BaseModel
    ) -> duckdb.DuckDBPyRelation: ...


# --------------------------------------------------------------------------- #
# 2. Detector
# --------------------------------------------------------------------------- #
class Detector(Plugin, abc.ABC):
    """Guesses a column's semantic type from its stats and sample values."""

    kind: ClassVar[str] = "detector"
    mode: ClassVar[str] = "inspect"
    # Physical types this detector can apply to; empty means "any".
    physical: ClassVar[tuple[str, ...]] = ()

    @abc.abstractmethod
    def detect(self, stats: ColumnStats) -> list[SemanticGuess]:
        """Return zero or more guesses. Confidence is 0..1; the profiler keeps the
        highest-confidence guess above a threshold."""


# --------------------------------------------------------------------------- #
# 3. Transform  (normalize / extract / annotate -- one kind, three modes)
# --------------------------------------------------------------------------- #
@dataclass
class ParseCheck:
    """A post-condition on a produced column: it must actually have parsed.

    Every parsing expression DuckDB offers -- try_cast, try_strptime -- answers
    failure with NULL rather than an error, by design. A transform that gets the
    format wrong therefore *succeeds*, writes a column of NULLs, and reports the
    full row count. The dataset looks fine until something downstream quietly has
    nothing to work with.

    Declaring the check moves that from silent to loud: the runtime measures how
    many non-null inputs produced non-null outputs, on a sample, before writing
    anything.
    """

    column: str          # the produced column
    source: str          # the input column it claims to be a parse of
    min_success: float = 0.5
    hint: str = ""       # what to try instead, shown in the error


@dataclass
class JoinPlan:
    """Columns to bring across from another dataset, keyed on any number of columns.

    The standalone join op matches on exactly one column pair, which is enough to
    attach a value-frequency table but not a per-``(user, activity_type)`` one --
    the statistic you actually want about behaviour. This carries a composite
    key, and stays a *transform*: the result is a new version of the dataset it
    was given, not a new dataset.

    Cardinality is still preserved, but only because the runtime checks it. A
    left join onto a right side with duplicate keys multiplies rows, so the
    right side is verified unique on ``on`` before anything is written.
    """

    # A FROM-clause fragment for the right side, resolved by the service (a
    # plugin never sees storage paths).
    source_sql: str
    # (left SQL expression, right column). The left side is qualified with the
    # alias ``l``, so a derived key reads ``date_trunc('day', l."ts")``.
    on: tuple[tuple[str, str], ...]
    # (right column, output name). Output names must not collide with the left
    # side's, which the runtime enforces.
    select: tuple[tuple[str, str], ...]


@dataclass
class SqlPlan:
    """What a ``pushdown`` transform returns: column expressions, not a query.

    The runtime assembles the SELECT, so a transform cannot accidentally drop the
    projection, reorder rows, or change cardinality.
    """

    add: dict[str, str] = field(default_factory=dict)      # new column -> SQL expr
    replace: dict[str, str] = field(default_factory=dict)  # existing column -> SQL expr
    drop: tuple[str, ...] = ()
    where: str | None = None
    # Post-conditions the runtime enforces before writing. See ParseCheck.
    checks: tuple[ParseCheck, ...] = ()
    # Optionally widen the source with columns from another dataset first, so
    # ``add`` expressions can reference them. See JoinPlan.
    join: JoinPlan | None = None


@dataclass
class TransformCtx:
    """Everything a transform needs to describe its work."""

    conn: duckdb.DuckDBPyConnection
    source_sql: str
    profile: DatasetProfile
    params: Any
    # Resolves another dataset to a FROM-clause fragment and its columns, for a
    # transform that declares a JoinPlan. A plugin never sees storage paths or
    # the catalog -- it names a dataset id and gets back something it can join
    # to, which is the same containment the query compiler has.
    resolve: Any = None

    def resolve_dataset(self, dataset_id: str, version: int | None = None):
        if self.resolve is None:
            raise RuntimeError(
                "this transform asked to join another dataset, but the runtime "
                "gave it no resolver"
            )
        return self.resolve(dataset_id, version)

    def col(self, name: str) -> str:
        """Safely quoted reference to a source column."""
        if self.profile.column(name) is None:
            raise KeyError(f"column not in dataset: {name}")
        return '"' + name.replace('"', '""') + '"'


class Transform(Plugin, abc.ABC):
    """DatasetVersion -> DatasetVersion.

    Normalization, extraction and annotation are all this kind; they differ only in
    ``mode``, which selects which method the runtime calls:

      mode="pushdown" -> ``sql(ctx)``            one DuckDB statement
      mode="batch"    -> ``process(batch)``      streamed Arrow, checkpointed
      mode="external" -> ``process_rows(...)``   async pool, cached, cost-capped

    The output contract is identical in all three cases, so versioning, profiling,
    the UI and the agent are unaware of which mode ran.
    """

    kind: ClassVar[str] = "transform"

    # --- mode="pushdown" ---
    def sql(self, ctx: TransformCtx) -> SqlPlan:
        raise NotImplementedError

    # --- mode="batch" ---
    def process(self, batch: pa.RecordBatch, params: Any) -> pa.RecordBatch:
        raise NotImplementedError

    # --- mode="external" ---
    # Declared limits the runtime enforces; the plugin never manages concurrency.
    batch_size: ClassVar[int] = 20
    max_concurrency: ClassVar[int] = 4
    supports_batch_api: ClassVar[bool] = False
    # Columns appended to every row. Declared so the output schema is known upfront.
    output_columns: ClassVar[tuple[tuple[str, str], ...]] = ()

    def cache_key_fields(self, row: dict[str, Any], params: Any) -> Sequence[Any]:
        """The row values that determine the result. Anything not listed here is
        excluded from the cache key, so unrelated columns don't cause cache misses."""
        raise NotImplementedError

    async def process_rows(
        self, rows: list[dict[str, Any]], ctx: ExternalCtx
    ) -> list[dict[str, Any]]:
        """One result dict per input row, in order. Keys must match
        ``output_columns``. Raising fails only these rows, not the job."""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# 4. Aggregator
# --------------------------------------------------------------------------- #
@dataclass
class AggregateCtx:
    profile: DatasetProfile
    params: Any


@dataclass
class AggregatePlan:
    """A ``QuerySpec`` plus optional derived expressions layered on top of it.

    ``derive`` exists because some genuinely useful aggregates need window
    functions -- a rarity share is ``n / sum(n) over ()`` -- which the user-facing
    ``QuerySpec`` deliberately cannot express. These expressions are authored by
    plugin code, which is trusted, never by a user or an agent, so widening the
    plugin return type does not widen the injection surface.
    """

    spec: QuerySpec
    derive: dict[str, str] = field(default_factory=dict)
    # Set when the limit is the point rather than a safety cap -- top-K asks for
    # exactly K rows, so hitting the limit is success, not truncation. Without
    # the distinction the runtime cannot tell a deliberate K from a feature
    # table that quietly stopped short.
    limited_on_purpose: bool = False


class Aggregator(Plugin, abc.ABC):
    """Produces a *new* aggregate Dataset (it changes cardinality, so it cannot be
    a new version of the input). Expressed as a query-plan generator, which makes
    it pushdown by construction and composable with the query layer."""

    kind: ClassVar[str] = "aggregator"
    mode: ClassVar[str] = "pushdown"

    @abc.abstractmethod
    def plan(self, ctx: AggregateCtx) -> AggregatePlan: ...


# --------------------------------------------------------------------------- #
# 5. Suggester
# --------------------------------------------------------------------------- #
class Suggestion(BaseModel):
    """A proposed next step. Crucially, ``action`` is an executable payload: the UI
    renders it as a button that POSTs to /api/operations, and the agent calls it
    directly. Suggestions are never prose-only."""

    title: str
    rationale: str = ""
    kind: str = ""            # "viz" | "aggregate" | "join"
    score: float = 0.5
    # POST /api/operations body, or {"op": "inspect", ...} for inspect-mode actions.
    action: dict[str, Any] = {}


@dataclass
class SuggestCtx:
    """Read-only view of the catalog handed to suggesters."""

    profile: DatasetProfile
    params: Any
    # Other datasets in the catalog, for cross-dataset join suggestion.
    peers: list[DatasetProfile] = field(default_factory=list)
    # dataset_id -> catalog name. A profile carries only the id, and an id is
    # not something a user recognises: a join suggestion that says which
    # dataset it would pull in has to get the name from here.
    names: dict[str, str] = field(default_factory=dict)

    def label(self, dataset_id: str) -> str:
        """How a dataset is named in suggestion text: "logon [ee60f9f6]"."""
        name = self.names.get(dataset_id)
        return f"{name} [{dataset_id[:8]}]" if name else dataset_id[:8]


class Suggester(Plugin, abc.ABC):
    kind: ClassVar[str] = "suggester"
    mode: ClassVar[str] = "inspect"

    @abc.abstractmethod
    def suggest(self, ctx: SuggestCtx) -> list[Suggestion]: ...


# --------------------------------------------------------------------------- #
# 6. Visualizer
# --------------------------------------------------------------------------- #
@dataclass
class VizCtx:
    profile: DatasetProfile
    params: Any


class Visualizer(Plugin, abc.ABC):
    kind: ClassVar[str] = "visualizer"
    mode: ClassVar[str] = "inspect"

    @abc.abstractmethod
    def spec(self, ctx: VizCtx) -> VizSpec: ...
