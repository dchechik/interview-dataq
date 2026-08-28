"""The timeline visualizer.

A timeline is a filtered slice of raw events in time order -- "everything this
IP did on Tuesday" rather than "how many events per day". The query is therefore
deliberately un-aggregated: filters, a sort on time, and a limit.

Because the source is just a dataset, this works unchanged on a raw import, on a
transformed version, and on a join. That last one is the interesting case: join
a frequency aggregate back onto its source and every event carries how common
its own value is, which is exactly what the abnormality rule reads.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from ...core.semantic import SEMANTIC_TYPES
from ...core.timeline import AbnormalityRule, EventAttribute, TimelineSpec
from ...core.viz import VizSpec
from ...query.spec import Filter, QuerySpec, Sort
from ..base import Accepts, Produces, register
from ..kinds import Visualizer, VizCtx

# Columns a frequency aggregate contributes when joined back onto its source.
# `share` is the fraction of rows holding this value, so *small* is rare.
RARITY_COLUMNS = ("share", "rarity")

# Columns that identify a subject worth pivoting to. Clicking one of these in
# the UI should mean "show me only this user / IP / vehicle".
SUBJECT_TYPES = ("net.ip", "identity.email", "identity.key", "geo.country_iso2")

# A subject is something you have *several* events for. A column with roughly as
# many distinct values as rows is a per-row identifier, not a subject: filtering
# by it yields a timeline of one event, which is not a timeline. This is the
# difference between src_ip and event_id, both of which are identity types.
SUBJECT_MAX_DISTINCT_FRAC = 0.9

# A timeline is for reading, not for scrolling forever.
DEFAULT_LIMIT = 200


def _is_subject(column) -> bool:
    """Is this a thing you would want to see the timeline *of*?"""
    if not SEMANTIC_TYPES.matches_any(column.semantic_type, SUBJECT_TYPES):
        return False
    stats = column.stats
    if stats is None:
        return True  # unprofiled: assume it is usable rather than hide it
    return stats.distinct_frac < SUBJECT_MAX_DISTINCT_FRAC


class TimelineParams(BaseModel):
    time_column: str = Field(description="The temporal column events are ordered by")
    title_column: str | None = Field(
        default=None, description="Headline for each event, e.g. the action taken"
    )
    attributes: list[str] = Field(
        default_factory=list,
        description="Columns shown on each event; empty means choose automatically",
    )
    highlight: list[str] = Field(
        default_factory=list, description="Which of those attributes to emphasise"
    )
    filters: list[Filter] = Field(
        default_factory=list,
        description="Narrow to a subject or a period, e.g. src_ip = 1.2.3.4",
    )
    abnormality_column: str | None = Field(
        default=None,
        description="Numeric column whose value can mark an event abnormal",
    )
    abnormality_op: str = "<"
    abnormality_value: float | None = None
    abnormality_label: str = "unusual"
    descending: bool = Field(default=True, description="Newest first")
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=5_000)


@register
class Timeline(Visualizer):
    """Events in time order, filtered to a subject.

    Attributes and the abnormality rule are chosen from the dataset's semantic
    types when the caller does not name them, so a timeline of an annotated
    dataset arrives already pointing at the interesting column.
    """

    id: ClassVar[str] = "viz.timeline"
    title: ClassVar[str] = "Timeline"
    Params: ClassVar[type[BaseModel]] = TimelineParams
    accepts: ClassVar[Accepts] = Accepts(semantic_types=("temporal",))
    produces: ClassVar[Produces] = Produces(description="A list of timed events")

    @staticmethod
    def _default_attributes(ctx: VizCtx, p: TimelineParams) -> list[EventAttribute]:
        """Pick columns worth showing when the caller did not say.

        Subjects first (they are what you filter by), then the rest of the
        dimensions. Rarity columns are excluded -- they drive the highlight, and
        repeating them as a chip would be noise.
        """
        skip = {p.time_column, p.title_column, *RARITY_COLUMNS}
        chosen: list[EventAttribute] = []

        for column in ctx.profile.columns:
            if column.name in skip or column.role in ("ignore", "measure"):
                continue
            is_subject = _is_subject(column)
            chosen.append(EventAttribute(
                column=column.name,
                highlight=is_subject,
                filterable=is_subject,
            ))

        # Subjects first, then everything else, capped so an event stays readable.
        chosen.sort(key=lambda a: not a.highlight)
        return chosen[:6]

    @staticmethod
    def _default_abnormality(ctx: VizCtx, p: TimelineParams) -> AbnormalityRule | None:
        """Use a joined-in rarity column if the dataset has one.

        This is the payoff of the aggregate-then-join workflow: `share` is the
        fraction of rows carrying this value, so a small share means the event
        is unlike its neighbours.
        """
        if p.abnormality_column:
            return AbnormalityRule(
                column=p.abnormality_column,
                op=p.abnormality_op,  # type: ignore[arg-type]
                value=p.abnormality_value if p.abnormality_value is not None else 0.01,
                label=p.abnormality_label,
            )
        for name in RARITY_COLUMNS:
            if ctx.profile.column(name) is None:
                continue
            if name == "share":
                return AbnormalityRule(
                    column="share", op="<", value=0.01, label="rare",
                    rationale="fewer than 1% of rows share this value",
                )
            return AbnormalityRule(
                column="rarity", op=">", value=0.99, label="rare",
                rationale="fewer than 1% of rows share this value",
            )
        return None

    def spec(self, ctx: VizCtx) -> VizSpec:
        p: TimelineParams = ctx.params

        time_column = ctx.profile.column(p.time_column)
        if time_column is None:
            raise ValueError(f"no column named {p.time_column!r} to build a timeline on")
        if not time_column.physical_type.upper().startswith(("TIMESTAMP", "DATE")):
            raise ValueError(
                f"cannot build a timeline on {p.time_column!r}: it is "
                f"{time_column.physical_type}, not a date or timestamp"
            )

        attributes = (
            [
                EventAttribute(
                    column=name,
                    highlight=name in p.highlight,
                    filterable=(
                        _is_subject(column) if (column := ctx.profile.column(name)) else False
                    ),
                )
                for name in p.attributes
            ]
            if p.attributes
            else self._default_attributes(ctx, p)
        )

        timeline = TimelineSpec(
            time_column=p.time_column,
            title_column=p.title_column,
            attributes=attributes,
            abnormality=self._default_abnormality(ctx, p),
            descending=p.descending,
        )

        # Select only what is rendered. A timeline over a wide table would
        # otherwise ship every column for every row.
        select = [{"column": c} for c in timeline.columns()]

        subject = next((f for f in p.filters if f.op == "="), None)
        title = f"Timeline of {subject.column} = {subject.value}" if subject else "Timeline"

        return VizSpec(
            renderer="timeline",
            title=title,
            query=QuerySpec(
                dataset="",
                filters=list(p.filters),
                select=select,  # type: ignore[arg-type]
                order_by=[Sort(column=p.time_column, desc=p.descending)],
                limit=p.limit,
            ),
            timeline=timeline,
        )
