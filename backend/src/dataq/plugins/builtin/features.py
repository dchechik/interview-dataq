"""Behavioural features, in two steps.

The spec asks to *"build traces of user behavior [and] annotate each trace with
how common the attributes of the event are"*. `agg.frequency` + join already
answers the global version of that -- how common a login from France is. These
two plugins answer the per-actor, time-windowed version: how common it is *for
this user*, *this month*, and how long since they last did it.

Why two steps rather than one. The intermediate is a real dataset -- one row per
(user, activity_type[, day]) with counts, shares and rolling totals -- so you can
chart it, query it, and join it to something else before ever attaching it to the
events. That is the spec's *"supporting aggregate datasets"*, and it is what
feature stores call a tile.

    events ──▶ agg.features ──▶ user_activity_features ──▶ enrich.features ──▶ events v2

The split is not merely organisational. Whole-partition and calendar-bucket
statistics are exactly what a GROUP BY computes, so step 1 gets them right by
construction. Per-row questions -- "the 30 days before *this* event", "days since
the previous one" -- have a different answer on every row and cannot be a grouped
table at all, so step 2 computes those directly. Each feature is routed to
whichever step can answer it honestly, and the job log says which.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from ...core import features as F
from ...core.profile import is_temporal
from ...core.semantic import SEMANTIC_TYPES
from ...query.spec import QuerySpec, Select, Sort, TimeBucket
from ..base import Accepts, Produces, register
from ..kinds import (
    AggregateCtx,
    AggregatePlan,
    Aggregator,
    JoinPlan,
    SqlPlan,
    Transform,
    TransformCtx,
)


def _time_column(profile, named: str | None = None) -> str | None:
    """The column to order events by, checked for being usable as one.

    A named column is verified rather than trusted: a text column that *means* a
    timestamp fails as `VARCHAR - INTERVAL` deep inside DuckDB, and that error
    names a type mismatch rather than the column or the fix.
    """
    if named:
        column = profile.column(named)
        if column is None:
            raise ValueError(f"no column named {named!r}")
    else:
        times = profile.time_columns()
        if not times:
            return None
        # Checked even though the role says it is a time column: a dataset
        # profiled before roles took storage into account still carries
        # role="time" on a text column, and trusting it reproduces the original
        # binder error on exactly the datasets that already exist.
        column = times[0]
    if not is_temporal(column.physical_type):
        raise ValueError(_not_temporal(column))
    return column.name


def _not_temporal(column) -> str:
    """Why a column cannot be a time axis, and what to do about it."""
    detail = (f"{column.name} is {column.physical_type}, not a date or timestamp, "
              "so it cannot be used as a time axis.")
    if SEMANTIC_TYPES.matches_any(column.semantic_type, ("temporal",)):
        formats = next((g.formats for g in column.candidates if g.formats), [])
        how = f" (it reads as {formats[0].label})" if formats else ""
        detail += (f" It does hold dates{how} -- parse it first with "
                   "normalize.timestamp, which will add a real timestamp column.")
    return detail


# --------------------------------------------------------------------------- #
# step 1: the feature table
# --------------------------------------------------------------------------- #
class FeatureTableParams(BaseModel):
    by: list[str] = Field(
        description="Entity columns to group by, e.g. user, activity_type"
    )
    time_column: str | None = Field(
        default=None, description="Detected from the dataset when omitted"
    )
    grain: str | None = Field(
        default=None,
        description="Bucket size: day, week or month. Omit for one row per entity.",
    )
    measures: list[str] = Field(
        default_factory=list, description="Numeric columns to total and average"
    )
    windows: list[str] = Field(
        default_factory=list,
        description="Rolling totals over the grain, e.g. 7d, 30d (needs a grain)",
    )


@register
class FeatureTableAggregate(Aggregator):
    """Build a behavioural feature table for one or more entity columns.

    One row per entity (per bucket, if given a grain), carrying how often it
    occurs, what share of the data it is, when it was first and last seen, and
    optionally rolling totals over the preceding buckets.

    The rolling totals deserve a word, because they are the reason this is worth
    doing at the table level. Read off day-buckets, "the last 30 days" is
    computed once per (entity, day) rather than once per event -- far cheaper --
    but a day-bucket contains the whole day, including events *after* the one
    being described. So the frame stops at the previous bucket: these columns
    mean "the 30 days **before** today", which is well-defined and cannot see
    the future. For a window measured from the event itself, ask
    ``enrich.features`` for it instead.
    """

    id: ClassVar[str] = "agg.features"
    title: ClassVar[str] = "Build a feature table"
    Params: ClassVar[type[BaseModel]] = FeatureTableParams
    accepts: ClassVar[Accepts] = Accepts(semantic_types=("categorical", "text",
                                                        "net.ip", "identity.email",
                                                        "identity.key"))
    produces: ClassVar[Produces] = Produces(
        dataset_kind="aggregate",
        description="One row per entity with counts, shares and rolling totals",
    )

    def plan(self, ctx: AggregateCtx) -> AggregatePlan:
        p: FeatureTableParams = ctx.params
        known = {c.name for c in ctx.profile.columns}
        if not p.by:
            raise ValueError("name at least one entity column to group by")
        for name in [*p.by, *p.measures]:
            if name not in known:
                raise ValueError(f"no column named {name!r}")

        time_column = _time_column(ctx.profile, p.time_column)
        if (p.grain or p.windows) and not time_column:
            raise ValueError(
                "a grain or a rolling window needs a time column, and this "
                "dataset has none. Parse one with normalize.timestamp first."
            )
        if p.windows and not p.grain:
            raise ValueError(
                "rolling windows are computed over buckets, so they need a "
                "grain -- try grain='day'"
            )

        select = [Select(column="*", agg="count", alias="n")]
        for m in p.measures:
            select.append(Select(column=m, agg="sum", alias=f"sum_{m}"))
            select.append(Select(column=m, agg="avg", alias=f"avg_{m}"))
        if time_column:
            select.append(Select(column=time_column, agg="min", alias="first_seen"))
            select.append(Select(column=time_column, agg="max", alias="last_seen"))

        bucket = None
        if p.grain:
            bucket = TimeBucket(column=time_column, interval=p.grain,  # type: ignore[arg-type]
                                alias="bucket")

        spec = QuerySpec(
            dataset="", group_by=list(p.by), time_bucket=bucket, select=select,
            # No limit: a feature table keyed by (user, activity, day) runs to
            # tens of millions of rows on real data, and a truncated one
            # annotates only part of the events it is attached to.
            order_by=[Sort(column="n", desc=True)], limit=None,
        )

        # Window functions are not expressible in QuerySpec by design; `derive`
        # is the sanctioned way for plugin code to layer them on, and is already
        # how agg.frequency computes its share.
        keys = ", ".join(F.quote(k) for k in p.by)
        derive = {
            "share": "n::DOUBLE / nullif(SUM(n) OVER (), 0)",
            "rarity": "1.0 - (n::DOUBLE / nullif(SUM(n) OVER (), 0))",
        }
        grain_unit = {"day": "DAY", "week": "WEEK", "month": "MONTH"}.get(
            p.grain or "day")
        if p.windows and grain_unit is None:
            raise ValueError(f"cannot roll up over a {p.grain!r} grain; "
                             "use day, week or month")
        for w in p.windows:
            n, unit = F.parse_duration(w)
            derive[f"n_{w}"] = (
                f"SUM(n) OVER (PARTITION BY {keys} ORDER BY bucket "
                f"RANGE BETWEEN INTERVAL {n} {unit} PRECEDING "
                # Stop at the previous bucket: the current one holds events that
                # come after the row this will eventually annotate.
                f"AND INTERVAL 1 {grain_unit} PRECEDING)"
            )
        return AggregatePlan(spec=spec, derive=derive)


# --------------------------------------------------------------------------- #
# step 2: attach them to the events
# --------------------------------------------------------------------------- #
class EnrichParams(BaseModel):
    features: list[str] = Field(
        default_factory=list,
        description=(
            "One per line, e.g. 'count() by user, activity_type over 30d'. "
            "Functions: count, count_distinct, sum, avg, min, max, stddev, "
            "median, share, days_since_last, days_since_first, event_index."
        ),
        json_schema_extra={"format": "textarea"},
    )
    time_column: str | None = Field(
        default=None, description="Detected from the dataset when omitted"
    )
    from_dataset: str | None = Field(
        default=None,
        description="Feature table to attach (the output of agg.features)",
    )
    join_on: list[str] = Field(
        default_factory=list,
        description="Columns keying that table; its bucket column is matched too",
    )
    prefix: str = Field(default="", description="Prefix for attached columns")


@register
class EnrichFeatures(Transform):
    """Attach behavioural features to every row.

    Does two jobs, because they are the two halves of the same question. It
    joins a feature table built by ``agg.features`` onto the events -- on as many
    key columns as it takes, which the standalone join op cannot do -- and it
    computes the features no grouped table can hold, the ones whose answer is
    different on every row.

    Cardinality is preserved: this is an annotation, and the runtime refuses the
    join if the feature table has duplicate keys rather than quietly multiplying
    rows.
    """

    id: ClassVar[str] = "enrich.features"
    title: ClassVar[str] = "Add behavioural features"
    mode: ClassVar[str] = "pushdown"
    Params: ClassVar[type[BaseModel]] = EnrichParams
    accepts: ClassVar[Accepts] = Accepts()
    produces: ClassVar[Produces] = Produces(
        description="Per-row features: frequency, recency, rarity"
    )

    def sql(self, ctx: TransformCtx) -> SqlPlan:
        p: EnrichParams = ctx.params
        known = {c.name for c in ctx.profile.columns}
        time_column = _time_column(ctx.profile, p.time_column)

        join = self._join(ctx, p, known) if p.from_dataset else None
        if join:
            known |= {alias for _, alias in join.select}

        add: dict[str, str] = {}
        for feature in F.coerce(list(p.features)):
            F.validate(feature, known, time_column)
            name = feature.output_name()
            if name in add:
                raise ValueError(
                    f"two features would both be called {name!r}; name one of "
                    "them with 'as'"
                )
            add[name] = F.to_sql(feature, time_column)

        if not add and not join:
            raise ValueError("give at least one feature, or a table to attach")
        return SqlPlan(add=add, join=join)

    @staticmethod
    def _join(ctx: TransformCtx, p: EnrichParams, known: set[str]) -> JoinPlan:
        """Match the feature table onto the events on its full key."""
        right = ctx.resolve_dataset(p.from_dataset)
        keys = p.join_on or [k for k in right.columns if k in known]
        if not keys:
            raise ValueError(
                f"nothing to join on: {p.from_dataset} shares no column names "
                "with this dataset, so name the keys with join_on"
            )
        missing = [k for k in keys if k not in known or k not in right.columns]
        if missing:
            raise ValueError(f"join keys not on both sides: {', '.join(missing)}")

        on: list[tuple[str, str]] = [(f"l.{F.quote(k)}", k) for k in keys]

        # A bucketed feature table is keyed by time as well, and the events side
        # has to be truncated to the same grain to match it.
        if "bucket" in right.columns:
            time_column = _time_column(ctx.profile, p.time_column)
            if not time_column:
                raise ValueError(
                    f"{p.from_dataset} is bucketed by time, but this dataset has "
                    "no time column to match it against"
                )
            grain = _detect_grain(ctx, right.sql)
            on.append((f"date_trunc('{grain}', l.{F.quote(time_column)})", "bucket"))

        brought = [c for c in right.columns if c not in keys and c != "bucket"]
        return JoinPlan(
            source_sql=right.sql,
            on=tuple(on),
            select=tuple((c, p.prefix + c) for c in brought),
        )


def _detect_grain(ctx: TransformCtx, source_sql: str) -> str:
    """Work out what a feature table's buckets are, from the buckets themselves.

    The grain is not recorded on the dataset, and asking the user to restate it
    invites them to get it wrong -- a mismatched truncation matches nothing and
    yields a column of NULLs with no error.

    Alignment answers it, not spacing. Spacing is tempting and wrong: events on
    Jan 1, 2, 3, 5 and Mar 1 are five *daily* buckets averaging fifteen days
    apart. But `date_trunc` leaves a fingerprint -- month buckets all land on the
    1st, week buckets all on the same weekday -- and that holds however sparse
    the data is.
    """
    row = ctx.conn.execute(
        f"SELECT count(*) FILTER (WHERE date_part('day', bucket) <> 1), "
        f"       count(DISTINCT date_part('isodow', bucket)), "
        f"       count(*) "
        f"FROM {source_sql} WHERE bucket IS NOT NULL"
    ).fetchone()
    not_first_of_month, weekdays, total = int(row[0]), int(row[1]), int(row[2])
    if total == 0:
        return "day"
    if not_first_of_month == 0:
        return "month"
    if weekdays == 1:
        return "week"
    return "day"
