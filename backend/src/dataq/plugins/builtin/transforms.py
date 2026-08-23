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

from ..base import Accepts, ColumnParams, Produces, register
from ..kinds import SqlPlan, Transform, TransformCtx

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
        default=None, description="strptime format; auto-detected when omitted"
    )
    suffix: str = "_ts"


@register
class NormalizeTimestamp(Transform):
    """Parse a text column into a real TIMESTAMP."""

    id: ClassVar[str] = "normalize.timestamp"
    title: ClassVar[str] = "Parse timestamp"
    mode: ClassVar[str] = "pushdown"
    Params: ClassVar[type[BaseModel]] = TimestampParams
    accepts: ClassVar[Accepts] = Accepts(semantic_types=("temporal", "text"))
    produces: ClassVar[Produces] = Produces(semantic_types=("time.timestamp",))

    def sql(self, ctx: TransformCtx) -> SqlPlan:
        p: TimestampParams = ctx.params
        col = ctx.col(p.column)
        if p.format:
            fmt = "'" + p.format.replace("'", "''") + "'"
            expr = f"try_strptime(CAST({col} AS VARCHAR), {fmt})"
        else:
            expr = f"try_cast({col} AS TIMESTAMP)"
        return SqlPlan(add={f"{p.column}{p.suffix}": expr})


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
        return SqlPlan(add={f"{p.column}{p.suffix}": f"upper(trim(CAST({ctx.col(p.column)} AS VARCHAR)))"})


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
        return SqlPlan(add={f"{p.column}{p.suffix}": f"try_cast({cleaned} AS DOUBLE)"})


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
