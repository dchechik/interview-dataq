"""The plugin framework: descriptors and the registry.

Design: plugins are *heterogeneous in contract, homogeneous in execution*.

  ``kind``  -- what the plugin consumes and produces. Determines its Python
               interface (see :mod:`dataq.plugins.kinds`).
  ``mode``  -- how the runtime must execute it. Determines scheduling and which
               facilities the runtime hands it (see :mod:`dataq.jobs`).

These are independent. A text extractor may be a cheap regex (``pushdown``) or an
LLM call (``external``); the runtime handles both and nothing downstream can tell
the difference.

Every plugin declares a single Pydantic ``Params`` model. That one model becomes the
request schema, the OpenAPI docs, the auto-rendered UI form and the agent tool
schema -- there is no second place where plugin metadata is written down.
"""

from __future__ import annotations

import abc
from importlib.metadata import entry_points
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ..core.semantic import SEMANTIC_TYPES
from ..core.types import CostClass, DatasetKind, ExecMode, PluginKind

ENTRY_POINT_GROUP = "dataq.plugins"


class NoParams(BaseModel):
    """Default params model for plugins that take no configuration."""


class ColumnParams(BaseModel):
    """Convention for plugins that operate on a single column."""

    column: str = Field(description="Name of the column to operate on")


class Accepts(BaseModel):
    """Preconditions a dataset must satisfy for this plugin to be offered.

    Used by ``GET /api/plugins?applicable_to=`` to answer "what can I do with this
    dataset?" -- which drives both the UI action list and the agent's choices.
    """

    # Needs at least one column of one of these semantic types (descendants count).
    semantic_types: tuple[str, ...] = ()
    dataset_kinds: tuple[DatasetKind, ...] = ()
    min_rows: int = 0

    def matching_columns(self, columns: list[Any]) -> list[str]:
        """Names of columns whose semantic type satisfies ``semantic_types``."""
        if not self.semantic_types:
            return [c.name for c in columns]
        return [
            c.name
            for c in columns
            if SEMANTIC_TYPES.matches_any(getattr(c, "semantic_type", None), self.semantic_types)
        ]


class Produces(BaseModel):
    """What the plugin adds, for lineage display and downstream suggestion."""

    semantic_types: tuple[str, ...] = ()
    dataset_kind: DatasetKind | None = None
    description: str = ""


class PluginDescriptor(BaseModel):
    """The serialisable, single source of truth about a plugin.

    Returned verbatim by ``GET /api/plugins``; consumed by the UI form renderer and
    by the agent tool-schema generator.
    """

    id: str
    kind: PluginKind
    mode: ExecMode
    version: str
    title: str
    summary: str
    cost_class: CostClass
    params_schema: dict[str, Any]
    accepts: Accepts
    produces: Produces


class Plugin(abc.ABC):
    """Base class for every plugin. Subclass one of the kind ABCs, not this."""

    id: ClassVar[str]
    kind: ClassVar[PluginKind]
    mode: ClassVar[ExecMode]
    title: ClassVar[str]
    # Participates in external-mode cache keys: bump it to invalidate cached results.
    version: ClassVar[str] = "1"
    summary: ClassVar[str] = ""
    cost_class: ClassVar[CostClass] = "cheap"
    Params: ClassVar[type[BaseModel]] = NoParams
    accepts: ClassVar[Accepts] = Accepts()
    produces: ClassVar[Produces] = Produces()

    @classmethod
    def descriptor(cls) -> PluginDescriptor:
        return PluginDescriptor(
            id=cls.id,
            kind=cls.kind,
            mode=cls.mode,
            version=cls.version,
            title=cls.title,
            summary=cls.summary or (cls.__doc__ or "").strip().split("\n")[0],
            cost_class=cls.cost_class,
            params_schema=cls.Params.model_json_schema(),
            accepts=cls.accepts,
            produces=cls.produces,
        )

    @classmethod
    def parse_params(cls, raw: dict[str, Any] | None) -> BaseModel:
        return cls.Params.model_validate(raw or {})


class PluginRegistry:
    """Holds every known plugin class, keyed by id."""

    def __init__(self) -> None:
        self._plugins: dict[str, type[Plugin]] = {}
        self._loaded_entry_points = False

    def register(self, cls: type[Plugin]) -> type[Plugin]:
        """Decorator. Validates the declaration so mistakes fail at import time."""
        for attr in ("id", "kind", "mode", "title"):
            if not getattr(cls, attr, None):
                raise TypeError(f"{cls.__name__} must declare a non-empty '{attr}'")
        if cls.id in self._plugins and self._plugins[cls.id] is not cls:
            raise ValueError(f"duplicate plugin id: {cls.id}")
        for st in (*cls.accepts.semantic_types, *cls.produces.semantic_types):
            if SEMANTIC_TYPES.get(st) is None:
                raise ValueError(f"{cls.id}: unknown semantic type {st!r}")
        self._plugins[cls.id] = cls
        return cls

    def get(self, plugin_id: str) -> type[Plugin] | None:
        self._ensure_entry_points()
        return self._plugins.get(plugin_id)

    def require(self, plugin_id: str) -> type[Plugin]:
        plugin = self.get(plugin_id)
        if plugin is None:
            raise KeyError(f"unknown plugin: {plugin_id}")
        return plugin

    def list(
        self, kind: PluginKind | None = None, mode: ExecMode | None = None
    ) -> list[type[Plugin]]:
        self._ensure_entry_points()
        out = [
            p
            for p in self._plugins.values()
            if (kind is None or p.kind == kind) and (mode is None or p.mode == mode)
        ]
        return sorted(out, key=lambda p: p.id)

    def applicable_to(
        self, columns: list[Any], row_count: int, dataset_kind: DatasetKind,
        kind: PluginKind | None = None,
    ) -> list[type[Plugin]]:
        """Plugins whose ``accepts`` preconditions this dataset satisfies."""
        out = []
        for p in self.list(kind=kind):
            a = p.accepts
            if a.dataset_kinds and dataset_kind not in a.dataset_kinds:
                continue
            if row_count < a.min_rows:
                continue
            if a.semantic_types and not a.matching_columns(columns):
                continue
            out.append(p)
        return out

    def _ensure_entry_points(self) -> None:
        """Third-party plugins register via the ``dataq.plugins`` entry-point group,
        so ``pip install dataq-plugin-geoip`` adds capability with no core change."""
        if self._loaded_entry_points:
            return
        self._loaded_entry_points = True
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            loaded = ep.load()
            if isinstance(loaded, type) and issubclass(loaded, Plugin):
                self.register(loaded)
            elif callable(loaded):
                loaded(self)  # a register(registry) hook


REGISTRY = PluginRegistry()
register = REGISTRY.register
