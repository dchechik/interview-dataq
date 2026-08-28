"""``VizSpec`` -- the backend/frontend visualization contract.

The backend never renders. A ``Visualizer`` plugin returns a ``VizSpec`` naming a
``renderer``; the frontend has its own registry keyed on that field. Consequence: a
new chart type that reuses an existing renderer is a backend-only change.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from ..query.spec import QuerySpec
from .chart import ChartSpec

Renderer = Literal["vega-lite", "maplibre", "table"]


class Animate(BaseModel):
    """Drives the client-side timeline scrubber (the Trendalyzer effect).

    The frontend fetches all frames at once and steps through distinct values of
    ``field``, so scrubbing costs no extra queries.
    """

    field: str
    label: str = ""
    fps: int = 2


class VizSpec(BaseModel):
    renderer: Renderer
    title: str
    # The data the renderer needs. Executed by the backend; rows are inlined into
    # the response so the frontend makes exactly one request per chart.
    query: QuerySpec
    # The typed grammar, for renderers that draw from data columns. Preferred
    # over ``spec`` when set.
    chart: ChartSpec | None = None
    # Renderer-specific payload: a MapLibre layer config, or a raw Vega-Lite spec
    # from a panel saved before ``chart`` existed. Dashboards persist the recipe
    # rather than a snapshot, so this stays supported for them.
    spec: dict[str, Any] = {}
    animate: Animate | None = None
    description: str = ""


class RenderedViz(BaseModel):
    """A ``VizSpec`` with its data attached -- what ``/api/inspect`` returns.

    ``sql`` and ``elapsed_ms`` are carried so a chart can show exactly what it was
    built from. A chart whose provenance you cannot inspect is hard to trust.
    """

    spec: VizSpec
    data: list[dict[str, Any]]
    row_count: int
    sql: str = ""
    elapsed_ms: float = 0.0
    truncated: bool = False
