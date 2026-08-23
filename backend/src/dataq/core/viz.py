"""``VizSpec`` -- the backend/frontend visualization contract.

The backend never renders. A ``Visualizer`` plugin returns a ``VizSpec`` naming a
``renderer``; the frontend has its own registry keyed on that field. Consequence: a
new chart type that reuses an existing renderer is a backend-only change.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from ..query.spec import QuerySpec

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
    # Renderer-specific payload: a Vega-Lite spec, a MapLibre layer config, etc.
    spec: dict[str, Any] = {}
    animate: Animate | None = None
    description: str = ""


class RenderedViz(BaseModel):
    """A ``VizSpec`` with its data attached -- what ``/api/viz/render`` returns."""

    spec: VizSpec
    data: list[dict[str, Any]]
    row_count: int
