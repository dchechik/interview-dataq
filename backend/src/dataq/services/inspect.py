"""The synchronous path for ``inspect``-mode plugins.

Visualizers and suggesters produce a *document*, not data. They are cheap and
read-only, so they run in the request thread and never create a job.
"""

from __future__ import annotations

from ..core.viz import RenderedViz, VizSpec
from ..plugins.base import REGISTRY
from ..plugins.kinds import SuggestCtx, Suggester, Suggestion, Visualizer, VizCtx
from .chart import resolve_chart, resolve_timeline
from .context import AppContext
from .query import rows_as_dicts, run_query


def _profile(ctx: AppContext, dataset_id: str, version: int | None):
    profile = ctx.catalog.get_profile(dataset_id, version)
    if profile is None:
        raise KeyError(f"unknown dataset or version: {dataset_id}")
    return profile


def render_viz(
    ctx: AppContext, plugin_id: str, dataset_id: str, params: dict,
    version: int | None = None, limit: int | None = None,
) -> RenderedViz:
    plugin_cls = REGISTRY.require(plugin_id)
    if not issubclass(plugin_cls, Visualizer):
        raise TypeError(f"{plugin_id} is not a visualizer")
    plugin: Visualizer = plugin_cls()  # type: ignore[assignment]
    profile = _profile(ctx, dataset_id, version)

    spec: VizSpec = plugin.spec(
        VizCtx(profile=profile, params=plugin_cls.parse_params(params))
    )
    # The plugin builds the shape of the query; the service binds it to the actual
    # dataset, so a visualizer can never read from somewhere it was not asked to.
    spec.query.dataset = dataset_id
    spec.query.version = version
    if limit is not None:
        spec.query.limit = limit

    result = run_query(ctx, spec.query)

    # Resolve the chart against the columns the query actually returned, and
    # against the semantic types of the source. A field the query does not
    # return is an error here rather than an unexplained empty chart.
    if spec.timeline is not None:
        spec.timeline = resolve_timeline(spec.timeline, result.columns, profile)

    if spec.chart is not None:
        spec.chart = resolve_chart(
            spec.chart, result.columns, profile,
            output_types=dict(zip(result.columns, result.types, strict=False)),
        )

    return RenderedViz(
        spec=spec, data=rows_as_dicts(result), row_count=result.row_count,
        sql=result.sql, elapsed_ms=result.elapsed_ms, truncated=result.truncated,
    )


def suggest(
    ctx: AppContext, dataset_id: str, kinds: tuple[str, ...] = (), version: int | None = None,
) -> list[Suggestion]:
    profile = _profile(ctx, dataset_id, version)

    # Join suggestion needs to see the rest of the catalog.
    peers = []
    for ds in ctx.catalog.list_datasets():
        if ds.id == dataset_id:
            continue
        peer = ctx.catalog.get_profile(ds.id)
        if peer is not None:
            peers.append(peer)

    out: list[Suggestion] = []
    for cls in REGISTRY.list(kind="suggester"):
        suggester: Suggester = cls()  # type: ignore[assignment]
        params = cls.parse_params({})
        found = suggester.suggest(SuggestCtx(profile=profile, params=params, peers=peers))
        if kinds:
            found = [s for s in found if s.kind in kinds]
        out.extend(found)
    return sorted(out, key=lambda s: s.score, reverse=True)


def applicable_plugins(ctx: AppContext, dataset_id: str, version: int | None = None):
    """"What can I do with this dataset?" -- drives the UI action list and the agent."""
    profile = _profile(ctx, dataset_id, version)
    ds = ctx.catalog.get_dataset(dataset_id)
    kind = ds.kind if ds else "source"
    return [
        p.descriptor()
        for p in REGISTRY.applicable_to(profile.columns, profile.row_count, kind)
    ]
