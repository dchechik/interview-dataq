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

from ...core.profile import is_entity
from ...core.semantic import SEMANTIC_TYPES
from ...core.timeline import AbnormalityRule, EventAttribute, TimelineSpec
from ...core.viz import VizSpec
from ...query.spec import Filter, QuerySpec, Sort
from ..base import Accepts, Produces, register
from ..kinds import Visualizer, VizCtx

# Columns saying how common a value is. Found by semantic type, so a computed
# feature qualifies and not merely the two names the frequency aggregate emits;
# the names remain as a fallback for datasets profiled before the type existed.
RARITY_TYPES = ("numeric.share", "numeric.rarity")
RARITY_COLUMNS = ("share", "rarity")


def _rarity_columns(profile) -> list:
    """Columns that say how unusual a row is, most specific first."""
    typed = [c for c in profile.columns
             if SEMANTIC_TYPES.matches_any(c.semantic_type, RARITY_TYPES)]
    if typed:
        return typed
    return [c for c in (profile.column(n) for n in RARITY_COLUMNS) if c is not None]

# Columns that identify a subject worth pivoting to. Clicking one of these in
# the UI should mean "show me only this user / IP / vehicle". Narrower than the
# shared ENTITY_TYPES: a plain category makes a fine thing to group behaviour
# by, but a poor thing to read a timeline of.
SUBJECT_TYPES = ("net.ip", "identity.email", "identity.key", "geo.country_iso2")

# A timeline is for reading, not for scrolling forever.
DEFAULT_LIMIT = 200


def _is_subject(column) -> bool:
    """Is this a thing you would want to see the timeline *of*?"""
    return is_entity(column, SUBJECT_TYPES)


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
    # None so an inferred rule can keep its own wording ("rare") while an
    # explicit one still defaults to something neutral.
    abnormality_label: str | None = None
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
        skip = {p.time_column, p.title_column,
                *(c.name for c in _rarity_columns(ctx.profile))}
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
                label=p.abnormality_label or "unusual",
            )
        for column in _rarity_columns(ctx.profile):
            # Share counts down and rarity counts up, so the comparison flips.
            inverted = (SEMANTIC_TYPES.is_a(column.semantic_type or "", "numeric.rarity")
                        or column.name == "rarity")
            default = 0.99 if inverted else 0.01
            # An explicit threshold is honoured even when the column was
            # inferred. Requiring the caller to name the column as well made the
            # UI's threshold control silently do nothing, which is worse than
            # not having one: it looks like an answer.
            value = p.abnormality_value if p.abnormality_value is not None else default
            share = (1 - value) if inverted else value
            return AbnormalityRule(
                column=column.name, op=">" if inverted else "<", value=value,
                label=p.abnormality_label or "rare",
                rationale=f"fewer than {share:.3%} of rows share this {column.name}"
                          .replace(".000%", "%"),
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
