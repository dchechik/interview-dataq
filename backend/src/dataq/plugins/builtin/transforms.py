"""Built-in transforms.

Normalization, extraction and annotation are all the ``transform`` kind; they
differ only in execution mode. This module contains one of each so the three code
paths are all exercised by the test suite.
"""

from __future__ import annotations

import ipaddress
from typing import Any, ClassVar

import pyarrow as pa
from pydantic import BaseModel, Field

from ...core.datefmt import ambiguous
from ..base import Accepts, ColumnParams, Produces, register
from ..kinds import ParseCheck, SqlPlan, Transform, TransformCtx

# --------------------------------------------------------------------------- #
# pushdown: normalization
# --------------------------------------------------------------------------- #


class IpNormalizeParams(ColumnParams):
    suffix: str = Field(default="_canon", description="Suffix for the normalized column")
    add_integer: bool = Field(
        default=True, description="Also emit a sortable UBIGINT form of IPv4 addresses"
    )


@register
class NormalizeIp(Transform):
    """Canonicalise an IP column, and optionally add a sortable integer form."""

    id: ClassVar[str] = "normalize.ip"
    title: ClassVar[str] = "Normalize IP address"
    mode: ClassVar[str] = "pushdown"
    Params: ClassVar[type[BaseModel]] = IpNormalizeParams
    accepts: ClassVar[Accepts] = Accepts(semantic_types=("net.ip",))
    produces: ClassVar[Produces] = Produces(
        semantic_types=("net.ip",), description="Canonical IP text plus an integer form"
    )

    def sql(self, ctx: TransformCtx) -> SqlPlan:
        p: IpNormalizeParams = ctx.params
        col = ctx.col(p.column)
        add = {f"{p.column}{p.suffix}": f"lower(trim(CAST({col} AS VARCHAR)))"}
        if p.add_integer:
            # Only IPv4 maps cleanly to an integer; anything else stays NULL.
            parts = [
                f"CAST(split_part(trim(CAST({col} AS VARCHAR)), '.', {i}) AS UBIGINT)"
                for i in range(1, 5)
            ]
            expr = (
                f"CASE WHEN regexp_matches(trim(CAST({col} AS VARCHAR)), "
                r"'^\d{1,3}(\.\d{1,3}){3}$') THEN "
                f"({parts[0]} * 16777216 + {parts[1]} * 65536 + "
                f"{parts[2]} * 256 + {parts[3]}) END"
            )
            add[f"{p.column}_int"] = expr
        return SqlPlan(add=add)


class TimestampParams(ColumnParams):
    format: str | None = Field(
        default=None,
        description="strptime format, or epoch:s / epoch:ms / epoch:us. "
                    "Taken from the detected format when omitted.",
    )
    suffix: str = "_ts"


def _epoch_expr(col: str, unit: str) -> str:
    return {
        "s": f"to_timestamp(CAST({col} AS BIGINT))",
        "ms": f"to_timestamp(CAST({col} AS BIGINT) / 1000.0)",
        "us": f"to_timestamp(CAST({col} AS BIGINT) / 1000000.0)",
    }[unit]


@register
class NormalizeTimestamp(Transform):
    """Parse a text column into a real TIMESTAMP.

    The format is normally not supplied: profiling already worked out how the
    column reads, so this takes the detected format rather than making the user
    retype it. Two cases are deliberately not automated.

    A column whose format is *ambiguous* -- 03/04/2016 being March or April
    depending on a convention the file does not record -- is refused rather than
    guessed at. Guessing here silently shifts up to twelve days of every date,
    which no downstream check would catch because both readings are valid dates.

    A column nothing recognises is attempted with try_cast anyway, on the chance
    DuckDB's own parser does better than the format library; the parse check
    catches it if not.
    """

    id: ClassVar[str] = "normalize.timestamp"
    title: ClassVar[str] = "Parse timestamp"
    mode: ClassVar[str] = "pushdown"
    Params: ClassVar[type[BaseModel]] = TimestampParams
    accepts: ClassVar[Accepts] = Accepts(semantic_types=("temporal", "text"))
    produces: ClassVar[Produces] = Produces(semantic_types=("time.timestamp",))

    def sql(self, ctx: TransformCtx) -> SqlPlan:
        p: TimestampParams = ctx.params
        col = ctx.col(p.column)
        detected = self._detected_formats(ctx, p.column)

        fmt = p.format
        if fmt is None and detected:
            if ambiguous(detected):
                options = ", ".join(
                    f"{c.format!r} ({c.label})" for c in detected if c.conflict
                )
                raise ValueError(
                    f"{p.column} is a date column, but its format is ambiguous: "
                    f"{detected[0].conflict}. Re-run with an explicit format -- "
                    f"one of: {options}."
                )
            fmt = detected[0].format

        if fmt and fmt.startswith("epoch:"):
            expr = _epoch_expr(col, fmt.split(":", 1)[1])
            how = fmt
        elif fmt:
            literal = "'" + fmt.replace("'", "''") + "'"
            expr = f"try_strptime(CAST({col} AS VARCHAR), {literal})"
            how = fmt
        else:
            expr = f"try_cast({col} AS TIMESTAMP)"
            how = "DuckDB's own parser"

        out = f"{p.column}{p.suffix}"
        source_type = (ctx.profile.column(p.column).physical_type
                       if ctx.profile.column(p.column) else "")
        return SqlPlan(
            add={out: expr},
            checks=(ParseCheck(column=out, source=p.column,
                               hint=self._hint(how, detected, source_type)),),
        )

    @staticmethod
    def _detected_formats(ctx: TransformCtx, column: str):
        """Formats profiling found for this column, best first."""
        prof = ctx.profile.column(column)
        for guess in (prof.candidates if prof else []):
            if guess.formats:
                return guess.formats
        return []

    @staticmethod
    def _hint(attempted: str, detected, source_type: str = "") -> str:
        """What to try instead. The whole value of the check is in this string."""
        if detected:
            options = ", ".join(f"{c.format!r} ({c.label})" for c in detected[:3])
            return (f"Parsed with {attempted}. Formats that fit the sampled "
                    f"values: {options}.")
        if source_type.upper().startswith(("DATE", "TIMESTAMP")):
            # Explaining that no text format matches would send the user hunting
            # for one. The column is already temporal; there is nothing to parse.
            return (f"Parsed with {attempted}, but {source_type} columns are "
                    "already temporal -- drop the `format`, or skip this "
                    "transform entirely.")
        return (f"Parsed with {attempted}, and no known date format matches this "
                "column either. Pass an explicit `format` (strptime syntax).")


class CountryParams(ColumnParams):
    suffix: str = "_iso2"


@register
class NormalizeCountry(Transform):
    """Upper-case and trim a country code column."""

    id: ClassVar[str] = "normalize.country"
    title: ClassVar[str] = "Normalize country code"
    mode: ClassVar[str] = "pushdown"
    Params: ClassVar[type[BaseModel]] = CountryParams
    accepts: ClassVar[Accepts] = Accepts(semantic_types=("geo.country_iso2", "categorical"))
    produces: ClassVar[Produces] = Produces(semantic_types=("geo.country_iso2",))

    def sql(self, ctx: TransformCtx) -> SqlPlan:
        p: CountryParams = ctx.params
        expr = f"upper(trim(CAST({ctx.col(p.column)} AS VARCHAR)))"
        return SqlPlan(add={f"{p.column}{p.suffix}": expr})


class NumericParams(ColumnParams):
    suffix: str = "_num"


@register
class NormalizeNumeric(Transform):
    """Strip currency symbols and thousands separators, then cast to DOUBLE."""

    id: ClassVar[str] = "normalize.numeric"
    title: ClassVar[str] = "Parse number"
    mode: ClassVar[str] = "pushdown"
    Params: ClassVar[type[BaseModel]] = NumericParams
    accepts: ClassVar[Accepts] = Accepts(semantic_types=("text", "numeric"))
    produces: ClassVar[Produces] = Produces(semantic_types=("numeric",))

    def sql(self, ctx: TransformCtx) -> SqlPlan:
        p: NumericParams = ctx.params
        col = ctx.col(p.column)
        cleaned = f"regexp_replace(CAST({col} AS VARCHAR), '[^0-9eE+\\-.]', '', 'g')"
        out = f"{p.column}{p.suffix}"
        return SqlPlan(
            add={out: f"try_cast({cleaned} AS DOUBLE)"},
            # try_cast answers failure with NULL like everything else here, so
            # pointing this at a column of words yields a column of nothing.
            checks=(ParseCheck(
                column=out, source=p.column,
                hint="Stripping currency symbols and separators still left "
                     "something that is not a number.",
            ),),
        )


# --------------------------------------------------------------------------- #
# batch: needs a Python library, so it cannot be pushed into SQL
# --------------------------------------------------------------------------- #


class IpClassParams(ColumnParams):
    pass


@register
class ClassifyIp(Transform):
    """Classify each IP as public / private / loopback / reserved / multicast.

    A genuine ``batch``-mode case: Python's ``ipaddress`` module encodes the full
    IANA special-purpose registry, which would be miserable to reimplement in SQL.
    """

    id: ClassVar[str] = "transform.ip_class"
    title: ClassVar[str] = "Classify IP address"
    mode: ClassVar[str] = "batch"
    Params: ClassVar[type[BaseModel]] = IpClassParams
    accepts: ClassVar[Accepts] = Accepts(semantic_types=("net.ip",))
    produces: ClassVar[Produces] = Produces(
        semantic_types=("categorical",), description="Adds <column>_class and <column>_version"
    )

    @staticmethod
    def _classify(value: Any) -> tuple[str | None, int | None]:
        if value is None:
            return None, None
        try:
            ip = ipaddress.ip_address(str(value).strip())
        except ValueError:
            return "invalid", None
        if ip.is_loopback:
            kind = "loopback"
        elif ip.is_private:
            kind = "private"
        elif ip.is_multicast:
            kind = "multicast"
        elif ip.is_reserved or ip.is_link_local:
            kind = "reserved"
        else:
            kind = "public"
        return kind, ip.version

    def process(self, batch: pa.RecordBatch, params: IpClassParams) -> pa.RecordBatch:
        values = batch.column(batch.schema.get_field_index(params.column)).to_pylist()
        pairs = [self._classify(v) for v in values]
        arrays = [*batch.columns,
                  pa.array([p[0] for p in pairs], type=pa.string()),
                  pa.array([p[1] for p in pairs], type=pa.int32())]
        schema = pa.schema([
            *list(batch.schema),
            pa.field(f"{params.column}_class", pa.string()),
            pa.field(f"{params.column}_version", pa.int32()),
        ])
        return pa.RecordBatch.from_arrays(arrays, schema=schema)


# --------------------------------------------------------------------------- #
# external: LLM-backed
# --------------------------------------------------------------------------- #


class EntityParams(ColumnParams):
    entity_types: list[str] = Field(
        default_factory=lambda: ["person", "organization", "location"],
        description="Entity categories to extract",
    )


@register
class ExtractEntities(Transform):
    """Extract named entities from a text column using Claude.

    The plugin implements only ``process_rows``. Caching, concurrency, retries,
    cost accounting, the budget cap and row-level failure isolation are all
    supplied by the runtime -- see :mod:`dataq.jobs.external`.
    """

    id: ClassVar[str] = "extract.entities"
    title: ClassVar[str] = "Extract entities (LLM)"
    mode: ClassVar[str] = "external"
    cost_class: ClassVar[str] = "expensive"
    Params: ClassVar[type[BaseModel]] = EntityParams
    accepts: ClassVar[Accepts] = Accepts(semantic_types=("text",))
    produces: ClassVar[Produces] = Produces(description="Adds an 'entities' column")

    batch_size: ClassVar[int] = 10
    max_concurrency: ClassVar[int] = 4
    output_columns: ClassVar[tuple[tuple[str, str], ...]] = (("entities", "VARCHAR"),)

    def cache_key_fields(self, row: dict[str, Any], params: EntityParams):
        # Only the text and the requested entity types affect the result, so
        # unrelated columns never cause a cache miss.
        return [row.get(params.column), sorted(params.entity_types)]

    async def process_rows(self, rows, ctx):
        params: EntityParams = ctx.params
        texts = [str(r.get(params.column) or "") for r in rows]
        numbered = "\n".join(f"[{i}] {t[:2000]}" for i, t in enumerate(texts))
        schema = {
            "type": "object",
            "properties": {
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer"},
                            "entities": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["index", "entities"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["results"],
            "additionalProperties": False,
        }
        payload, cost = await ctx.model.complete(
            system=(
                "Extract named entities from each numbered text. "
                f"Only these types: {', '.join(params.entity_types)}. "
                "Return one result object per input index, preserving indices."
            ),
            prompt=numbered,
            output_schema=schema,
        )
        ctx.record_cost(cost)
        by_index = {r["index"]: r.get("entities", []) for r in payload.get("results", [])}
        return [{"entities": ", ".join(by_index.get(i, []))} for i in range(len(rows))]
