"""``ChartSpec`` -- a typed grammar of graphics.

Presentation used to live in an untyped ``dict[str, Any]`` on ``VizSpec``. Nothing
validated it, Python and TypeScript shared no definition of it, and -- worst --
nothing checked that a field named in an encoding actually existed in the query's
output, so a typo or a renamed alias rendered as a silently empty chart.

This is a deliberate *subset* of Vega-Lite's vocabulary, so it compiles to
Vega-Lite almost 1:1. The point of owning it rather than passing Vega-Lite
through is that it can be resolved against things the rest of DataQ already
knows: the query's real output columns, and each column's semantic type. See
:mod:`dataq.services.chart`.

``raw_vega_lite`` is the escape hatch, following the pattern already used twice
in this codebase -- ``QuerySpec`` with its read-only raw-SQL path, and
``AggregatePlan.derive`` for window functions. Structure for the common case, a
documented way out for the rest.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..query.spec import AggFunc

Mark = Literal[
    "bar", "line", "area", "point", "tick", "rect", "arc", "boxplot",
]

# Vega-Lite's encoding channels, minus the ones this IR has no use for.
Channel = Literal[
    "x", "y", "color", "size", "shape", "opacity", "theta",
    "detail", "row", "column", "tooltip",
]

# Vega-Lite's four measurement types. Left unset, the resolver infers this from
# the column's semantic type, which is nearly always what you want.
EncodingType = Literal["quantitative", "nominal", "ordinal", "temporal"]


class Encoding(BaseModel):
    """One channel: which column, and how to read it."""

    field: str = Field(description="An output column of the panel's query")
    type: EncodingType | None = Field(
        default=None,
        description="Inferred from the column's semantic type when omitted",
    )
    aggregate: AggFunc | None = Field(
        default=None, description="Client-side aggregation, e.g. count for a histogram"
    )
    bin: bool | int | None = Field(
        default=None, description="true, or a maxbins count, to bin a quantitative field"
    )
    sort: str | None = Field(
        default=None,
        description="'-x' / 'y' to sort by a channel, or an output column name",
    )
    title: str | None = None
    stack: bool | None = None
    # Not settable by callers: the resolver records why it chose a type, so the
    # UI can explain an axis and a human can spot a bad inference.
    inferred_from: str | None = None


class ChartSpec(BaseModel):
    """A mark plus its encodings -- the whole grammar, in one object."""

    mark: Mark
    encodings: dict[Channel, Encoding] = {}
    # Overlaid marks sharing the panel's data, e.g. a line with points on top.
    layers: list[ChartSpec] = []
    # Merged over the compiled output, last. Trust-scoped the same way as
    # AggregatePlan.derive: written by plugin code or a deliberate power user.
    raw_vega_lite: dict[str, Any] = {}
    description: str = ""

    def fields(self) -> list[str]:
        """Every column this chart reads, including through its layers."""
        names = [e.field for e in self.encodings.values() if e.field != "*"]
        for layer in self.layers:
            names.extend(layer.fields())
        return names


# Pydantic needs this for the self-reference in `layers`.
ChartSpec.model_rebuild()
