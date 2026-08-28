"""Resolve a ``ChartSpec`` against the data it will actually draw.

Two jobs, both impossible if presentation is opaque JSON:

  * **Validate** every encoded field against the query's real output columns. A
    field that does not exist used to render as a silently empty chart; now it
    is an error naming the columns that do exist.
  * **Infer** each encoding's measurement type from the column's *semantic*
    type, which the catalog already tracks. A ``time.timestamp`` is temporal, a
    ``money.amount`` is quantitative, a ``geo.country_iso2`` is nominal -- so
    charts get correct axes without anyone restating what a column means.
"""

from __future__ import annotations

from ..core.chart import ChartSpec, Encoding, EncodingType
from ..core.profile import ColumnProfile, DatasetProfile
from ..core.semantic import SEMANTIC_TYPES
from ..core.timeline import TimelineSpec

# A cyclical time part emits a readable label plus this suffix carrying the
# sortable ordinal -- "Thu" does not sort as text. See query/spec.py TimeBucket.
ORDINAL_SUFFIX = "_ord"

# Physical types, for columns the profiler never saw (an aggregate's derived
# `share`, say). Only consulted when there is no semantic type.
_PHYSICAL_PREFIXES: tuple[tuple[tuple[str, ...], EncodingType], ...] = (
    (("TIMESTAMP", "DATE", "TIME"), "temporal"),
    (
        ("BIGINT", "INTEGER", "SMALLINT", "TINYINT", "HUGEINT", "UBIGINT",
         "DOUBLE", "FLOAT", "REAL", "DECIMAL"),
        "quantitative",
    ),
    (("BOOLEAN",), "nominal"),
)


class ChartError(ValueError):
    """Raised when a chart cannot be drawn from the data it was given."""


def _from_physical(physical: str) -> EncodingType | None:
    upper = physical.upper()
    for prefixes, encoding_type in _PHYSICAL_PREFIXES:
        if upper.startswith(prefixes):
            return encoding_type
    return None


def _infer_type(
    column: ColumnProfile | None,
    field: str,
    output_columns: list[str],
    output_types: dict[str, str] | None = None,
) -> tuple[EncodingType, str]:
    """Pick a measurement type for a column, and say why."""
    # A time-part label ("1pm", "Thu") is ordinal: it has a natural order that
    # its text does not express, which is exactly why the compiler emits a
    # sibling ordinal column alongside it.
    if f"{field}{ORDINAL_SUFFIX}" in output_columns:
        return "ordinal", "cyclical time part, ordered by its ordinal column"

    semantic = column.semantic_type if column else None
    if semantic:
        if SEMANTIC_TYPES.matches_any(semantic, ("temporal",)):
            return "temporal", f"semantic type {semantic}"
        if SEMANTIC_TYPES.matches_any(semantic, ("numeric",)):
            return "quantitative", f"semantic type {semantic}"
        if SEMANTIC_TYPES.matches_any(semantic, ("categorical", "boolean", "text")):
            return "nominal", f"semantic type {semantic}"

    if column is not None:
        if column.role == "measure":
            return "quantitative", "column role is measure"
        if column.role == "time":
            return "temporal", "column role is time"
        from_profile = _from_physical(column.physical_type)
        if from_profile:
            return from_profile, f"physical type {column.physical_type}"

    # The query's own output type. Authoritative for derived columns the source
    # profile has never seen -- a truncated `bucket` really is a TIMESTAMP, and
    # nothing else here would know that.
    physical = (output_types or {}).get(field)
    if physical:
        from_output = _from_physical(physical)
        if from_output:
            return from_output, f"query returns {physical}"

    # Last resort for a result set whose types were not supplied.
    if field in ("n", "count", "share", "rarity") or field.startswith(
        ("count_", "sum_", "avg_", "min_", "max_", "median_")
    ):
        return "quantitative", "aggregate output column"

    return "nominal", "no type information; defaulted"


def resolve_encoding(
    channel: str,
    encoding: Encoding,
    output_columns: list[str],
    profile: DatasetProfile | None,
    output_types: dict[str, str] | None = None,
) -> Encoding:
    if encoding.field not in output_columns:
        raise ChartError(
            f"chart encodes {channel}={encoding.field!r}, which the query does not "
            f"return; available columns: {sorted(output_columns)}"
        )

    resolved = encoding.model_copy()
    column = profile.column(encoding.field) if profile else None

    if resolved.type is None:
        resolved.type, resolved.inferred_from = _infer_type(
            column, encoding.field, output_columns, output_types
        )

    # Order a cyclical part by its ordinal rather than alphabetically, unless the
    # caller asked for something specific.
    ordinal = f"{encoding.field}{ORDINAL_SUFFIX}"
    if resolved.sort is None and ordinal in output_columns:
        resolved.sort = ordinal

    if resolved.title is None and column is not None and column.name != encoding.field:
        resolved.title = column.name

    return resolved


def resolve_chart(
    chart: ChartSpec,
    output_columns: list[str],
    profile: DatasetProfile | None = None,
    output_types: dict[str, str] | None = None,
) -> ChartSpec:
    """Validate and fill in a chart against the columns its query returns.

    ``output_columns`` comes from the compiled query, so this catches a spec that
    references a column the query stopped selecting -- the failure that used to
    show up as an empty chart with no explanation.
    """
    if not chart.encodings and not chart.layers and not chart.raw_vega_lite:
        raise ChartError("chart has no encodings")

    resolved = chart.model_copy()
    resolved.encodings = {
        channel: resolve_encoding(channel, encoding, output_columns, profile, output_types)
        for channel, encoding in chart.encodings.items()
    }
    resolved.layers = [
        resolve_chart(layer, output_columns, profile, output_types)
        for layer in chart.layers
    ]
    return resolved


def default_chart_for(
    profile: DatasetProfile, output_columns: list[str] | None = None
) -> ChartSpec | None:
    """A reasonable chart for a dataset, from its shape alone.

    Used to answer "chart this" for an aggregate the user just materialised,
    where the columns follow the conventions the aggregators emit.
    """
    columns = output_columns or [c.name for c in profile.columns]
    present = set(columns)

    def first(names: list[str]) -> str | None:
        return next((n for n in names if n in present), None)

    measure = first(["n", "share"]) or next(
        (c.name for c in profile.by_role("measure") if c.name in present), None
    )
    if measure is None:
        return None

    # A time rollup: bucket on x, measure on y. The resolver handles ordering a
    # cyclical part by its ordinal.
    bucket = first(["bucket"])
    if bucket:
        return ChartSpec(
            mark="bar" if f"bucket{ORDINAL_SUFFIX}" in present else "line",
            encodings={
                "x": Encoding(field=bucket),
                "y": Encoding(field=measure),
            },
            description="Rolled up over time",
        )

    # A frequency or top-k table: the grouped dimension on y, measure on x.
    dimension = next(
        (
            c.name
            for c in profile.columns
            if c.name in present
            and c.name not in ("n", "share", "rarity")
            and c.role in ("dimension", "key")
        ),
        None,
    )
    if dimension:
        return ChartSpec(
            mark="bar",
            encodings={
                "y": Encoding(field=dimension, sort="-x"),
                "x": Encoding(field=measure),
            },
            description="Values by frequency",
        )
    return None


def resolve_timeline(
    timeline: TimelineSpec,
    output_columns: list[str],
    profile: DatasetProfile | None = None,
) -> TimelineSpec:
    """Validate a timeline against the columns its query returns.

    Same contract as ``resolve_chart``: a column the query does not return is an
    error naming what it does return, rather than an event list that silently
    renders blank chips. Attributes are dropped with a reason instead of failing
    the whole view, because losing one chip is recoverable and losing the
    timeline is not -- but the time column and the abnormality rule are load
    bearing, so those raise.
    """
    present = set(output_columns)
    if timeline.time_column not in present:
        raise ChartError(
            f"timeline is ordered by {timeline.time_column!r}, which the query does "
            f"not return; available columns: {sorted(present)}"
        )

    resolved = timeline.model_copy()

    if timeline.title_column and timeline.title_column not in present:
        resolved.title_column = None

    resolved.attributes = [a for a in timeline.attributes if a.column in present]

    if timeline.abnormality and timeline.abnormality.column not in present:
        raise ChartError(
            f"the abnormality rule reads {timeline.abnormality.column!r}, which the "
            f"query does not return; available columns: {sorted(present)}"
        )

    # Fill in labels from the profile so a chip reads as a person would name it.
    if profile is not None:
        for attribute in resolved.attributes:
            if attribute.label is None:
                column = profile.column(attribute.column)
                if column and column.semantic_type:
                    attribute.label = attribute.column
    return resolved
