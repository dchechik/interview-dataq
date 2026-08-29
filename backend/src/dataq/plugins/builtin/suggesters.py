"""Built-in suggesters.

A ``Suggestion`` carries an ``action`` that is an executable payload -- the UI
renders it as a button that POSTs to /api/operations (or /api/inspect), and the
agent invokes it directly. Suggestions are never prose-only, which is what keeps
"the tool suggests things" from degenerating into a wall of advice.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from ...core.profile import (
    ColumnProfile,
    DatasetProfile,
    entity_columns,
    is_temporal,
)
from ...core.semantic import SEMANTIC_TYPES
from ..base import NoParams, Produces, register
from ..kinds import SuggestCtx, Suggester, Suggestion


def _inspect(plugin_id: str, dataset_id: str, params: dict) -> dict:
    return {"op": "inspect", "plugin_id": plugin_id, "dataset_id": dataset_id,
            "params": params}


def _operation(op: str, plugin_id: str, dataset_id: str, params: dict) -> dict:
    return {"op": op, "plugin_id": plugin_id,
            "inputs": [{"dataset_id": dataset_id}], "params": params}


# Below this a column is a flag or a status, not something you study behaviour
# per-instance of.
MIN_ACTOR_DISTINCT = 3


def _interesting_measures(profile: DatasetProfile) -> list[ColumnProfile]:
    """Measures worth charting, best first.

    Money beats an arbitrary numeric column, and derived integer encodings (an IP
    rendered as UBIGINT, say) are never interesting to average, so they go last.
    """
    def rank(c: ColumnProfile) -> tuple[int, str]:
        if c.semantic_type == "money.amount":
            return (0, c.name)
        if c.name.endswith(("_int", "_id", "_count", "_version")):
            return (2, c.name)
        return (1, c.name)

    measures = [c for c in profile.by_role("measure") if not c.name.endswith("_int")]
    return sorted(measures, key=rank)


@register
class VizSuggester(Suggester):
    """Propose charts that suit the dataset's shape."""

    id: ClassVar[str] = "suggest.viz"
    title: ClassVar[str] = "Suggested charts"
    Params: ClassVar[type[BaseModel]] = NoParams
    produces: ClassVar[Produces] = Produces(description="Chart suggestions")

    def suggest(self, ctx: SuggestCtx) -> list[Suggestion]:
        p = ctx.profile
        out: list[Suggestion] = []
        times = p.time_columns()
        measures = _interesting_measures(p)
        dims = [c for c in p.columns if c.role == "dimension"
                and SEMANTIC_TYPES.matches_any(c.semantic_type, ("categorical",))]
        lats = p.by_semantic("geo.lat")
        lngs = p.by_semantic("geo.lng")

        if lats and lngs:
            out.append(Suggestion(
                title="Map of pickup locations" if "pickup" in lats[0].name
                else "Map of points",
                rationale=f"{lats[0].name} and {lngs[0].name} are geographic coordinates",
                kind="viz", score=0.95,
                action=_inspect("viz.map_points", p.dataset_id,
                                {"lat_column": lats[0].name, "lng_column": lngs[0].name}),
            ))
            if times:
                out.append(Suggestion(
                    title=f"Animated map over {times[0].name}",
                    rationale="geographic columns plus a time column support an "
                              "animated timeline",
                    kind="viz", score=0.72,
                    action=_inspect("viz.map_points", p.dataset_id, {
                        "lat_column": lats[0].name, "lng_column": lngs[0].name,
                        "animate_by": times[0].name, "interval": "day",
                    }),
                ))

        for t in times[:1]:
            out.append(Suggestion(
                title=f"Volume over time ({t.name})",
                rationale=f"{t.name} is a timestamp, so trend over time is the "
                          "usual first question",
                kind="viz", score=0.9,
                action=_inspect("viz.timeseries", p.dataset_id,
                                {"time_column": t.name, "interval": "day"}),
            ))
            for m in measures[:1]:
                out.append(Suggestion(
                    title=f"Average {m.name} over time",
                    rationale=f"{m.name} is a measure and {t.name} is a timestamp",
                    kind="viz", score=0.8,
                    action=_inspect("viz.timeseries", p.dataset_id, {
                        "time_column": t.name, "interval": "day", "measure": m.name}),
                ))

        # A timeline reads individual events rather than their distribution, so
        # it is proposed whenever there is a time column -- and ranked above the
        # charts when the dataset carries a rarity column, because then the view
        # can point straight at the events that stand out.
        if times:
            # Asked of the semantic layer rather than by name, so a computed
            # feature counts as an annotation just as a joined-in share does.
            annotated = any(
                SEMANTIC_TYPES.matches_any(c.semantic_type,
                                           ("numeric.share", "numeric.rarity"))
                for c in p.columns
            ) or any(c.name in ("share", "rarity") for c in p.columns)
            headline = next(
                (c.name for c in p.columns
                 if SEMANTIC_TYPES.matches_any(c.semantic_type, ("categorical",))),
                None,
            )
            out.append(Suggestion(
                title=("Timeline, with unusual events highlighted" if annotated
                       else f"Timeline of events by {times[0].name}"),
                rationale=(
                    "this dataset carries how common each value is, so rare "
                    "events can be flagged as you read down"
                    if annotated else
                    f"{times[0].name} orders the rows, so they can be read as events"
                ),
                kind="viz", score=0.93 if annotated else 0.65,
                action=_inspect("viz.timeline", p.dataset_id, {
                    "time_column": times[0].name, "title_column": headline,
                }),
            ))

        for d in dims[:3]:
            out.append(Suggestion(
                title=f"Top values of {d.name}",
                rationale=f"{d.name} has few distinct values, so a bar chart reads well",
                kind="viz", score=0.7,
                action=_inspect("viz.bar", p.dataset_id, {"dimension": d.name}),
            ))

        for m in measures[:2]:
            out.append(Suggestion(
                title=f"Distribution of {m.name}",
                rationale=f"{m.name} is numeric",
                kind="viz", score=0.6,
                action=_inspect("viz.histogram", p.dataset_id, {"column": m.name}),
            ))

        return sorted(out, key=lambda s: s.score, reverse=True)


@register
class AggregateSuggester(Suggester):
    """Propose supporting aggregate datasets."""

    id: ClassVar[str] = "suggest.aggregate"
    title: ClassVar[str] = "Suggested aggregates"
    Params: ClassVar[type[BaseModel]] = NoParams

    def suggest(self, ctx: SuggestCtx) -> list[Suggestion]:
        p = ctx.profile
        out: list[Suggestion] = []

        # Frequency tables over low-cardinality dimensions: the basis of rarity
        # annotation ("how common is a login from France?").
        for c in p.columns:
            if not SEMANTIC_TYPES.matches_any(c.semantic_type, ("categorical",)):
                continue
            ndv = c.stats.distinct_count if c.stats else 0
            if not (0 < ndv <= 500):
                continue
            out.append(Suggestion(
                title=f"How common is each {c.name}?",
                rationale=f"{c.name} has {ndv} distinct values; a frequency table lets "
                          "you annotate every row with how common its value is",
                kind="aggregate", score=0.85 if c.semantic_type == "geo.country_iso2" else 0.6,
                action=_operation("aggregate", "agg.frequency", p.dataset_id,
                                  {"column": c.name}),
            ))

        times = p.time_columns()
        measures = _interesting_measures(p)
        if times:
            out.append(Suggestion(
                title=f"Daily rollup by {times[0].name}",
                rationale="a time rollup makes trend queries cheap on large data",
                kind="aggregate", score=0.75,
                action=_operation("aggregate", "agg.time_rollup", p.dataset_id, {
                    "time_column": times[0].name, "interval": "day",
                    "measure": measures[0].name if measures else None,
                }),
            ))
            # Cyclical rollups answer a different question from the timeline one:
            # not "what happened that day" but "when is this busiest". Suggested
            # explicitly because a user would not otherwise know to ask.
            out.append(Suggestion(
                title="What time of day is busiest?",
                rationale=f"groups every hour of {times[0].name} together, "
                          "so all the 1pms count as one bucket",
                kind="aggregate", score=0.7,
                action=_operation("aggregate", "agg.time_rollup", p.dataset_id, {
                    "time_column": times[0].name, "part": "hour_of_day",
                    "measure": measures[0].name if measures else None,
                }),
            ))
            out.append(Suggestion(
                title="Which day of the week is busiest?",
                rationale=f"groups {times[0].name} by weekday across the whole dataset",
                kind="aggregate", score=0.65,
                action=_operation("aggregate", "agg.time_rollup", p.dataset_id, {
                    "time_column": times[0].name, "part": "day_of_week",
                    "measure": measures[0].name if measures else None,
                }),
            ))

        for c in p.columns:
            if c.role == "dimension" and c.stats and 0 < c.stats.distinct_count <= 10_000:
                out.append(Suggestion(
                    title=f"Top {c.name}",
                    rationale=f"rank {c.name} by frequency",
                    kind="aggregate", score=0.5,
                    action=_operation("aggregate", "agg.topk", p.dataset_id,
                                      {"dimension": c.name, "k": 20}),
                ))
                break

        return sorted(out, key=lambda s: s.score, reverse=True)


@register
class NormalizeSuggester(Suggester):
    """Propose the parses that unlock everything else.

    A text column that means a timestamp cannot be bucketed, charted over time,
    laid out on a timeline, or windowed -- and until it is parsed, none of those
    are even suggested, because they would fail. So this is ranked above them
    all: it is the step that makes the rest of the dataset usable.
    """

    id: ClassVar[str] = "suggest.normalize"
    title: ClassVar[str] = "Suggested clean-up"
    Params: ClassVar[type[BaseModel]] = NoParams

    def suggest(self, ctx: SuggestCtx) -> list[Suggestion]:
        out: list[Suggestion] = []
        for c in ctx.profile.columns:
            temporal = SEMANTIC_TYPES.matches_any(c.semantic_type, ("temporal",))
            if not temporal or is_temporal(c.physical_type):
                continue
            formats = next((g.formats for g in c.candidates if g.formats), [])
            best = formats[0] if formats else None
            if best and best.conflict:
                # Ambiguous: the transform will refuse to guess, so say which
                # readings are on offer rather than sending them into that error.
                rationale = (f"{c.name} holds dates, but ambiguously -- "
                             f"{best.conflict}. Pick a format when parsing.")
                params = {"column": c.name}
            elif best:
                rationale = (f"{c.name} holds dates stored as text ({best.label}), "
                             "so it cannot be charted over time or windowed until "
                             "it is parsed")
                params = {"column": c.name, "format": best.format}
            else:
                continue
            out.append(Suggestion(
                title=f"Parse {c.name} into a real timestamp",
                rationale=rationale,
                kind="transform", score=0.97,
                action=_operation("transform", "normalize.timestamp",
                                  ctx.profile.dataset_id, params),
            ))
        return out


@register
class FeatureSuggester(Suggester):
    """Propose behavioural features for event-shaped datasets.

    A dataset with a time column and something to call an actor is a log of
    behaviour, and the questions people ask of one are always the same shape:
    how often does this actor do this, how does that compare to everyone, and
    how long since they last did it. Suggested rather than left to be
    discovered, because the two-step shape -- build a feature table, then attach
    it -- is not something a user would think to ask for.
    """

    id: ClassVar[str] = "suggest.features"
    title: ClassVar[str] = "Suggested features"
    Params: ClassVar[type[BaseModel]] = NoParams

    def suggest(self, ctx: SuggestCtx) -> list[Suggestion]:
        p = ctx.profile
        times = p.time_columns()
        if not times:
            return []
        entities = entity_columns(p)
        # An actor has to take enough values for "per actor" to mean something.
        # Without this the NYC taxi data -- which has no driver or medallion
        # column at all -- gets features grouped by store_and_fwd_flag, a Y/N
        # flag, and the suggestion is worse than none. Real actors have hundreds
        # of values; this is a floor that excludes flags, not a quality bar.
        actors = [c for c in entities
                  if (c.stats.distinct_count if c.stats else 0) >= MIN_ACTOR_DISTINCT]
        if not actors:
            return []
        entities = actors + [c for c in entities if c not in actors]

        actor = entities[0].name
        # The second entity, if there is one, is what the actor *does*: an
        # activity type, an endpoint, a country. Features about the pair are the
        # interesting ones -- "this user, this action" rather than either alone.
        kinds = [c.name for c in entities[1:]]
        pair = [actor, *kinds[:1]]
        by = ", ".join(pair)
        measures = _interesting_measures(p)

        features = [
            f"count() by {by} over 30d",
            f"count() by {by} in day",
            f"days_since_last() by {by}",
            f"share() by {pair[-1]}",
        ]
        if measures:
            features.append(f"avg({measures[0].name}) by {actor} over 30d")

        out = [Suggestion(
            title=f"Behavioural features for {actor}",
            rationale=(
                f"{p.row_count:,} timestamped events with {actor} to group by, so "
                f"each row can carry how often this {actor} does this, recently "
                "and overall"
            ),
            kind="enrich", score=0.88,
            action=_operation("transform", "enrich.features", p.dataset_id,
                              {"features": features}),
        )]

        # The feature table is proposed separately because it is worth having on
        # its own -- you can chart it, and it answers "who is busiest" without
        # touching the events at all.
        out.append(Suggestion(
            title=f"Feature table for {by}",
            rationale="one row per actor with counts, shares and first/last seen "
                      "-- a dataset you can chart, and attach back to the events",
            kind="aggregate", score=0.62,
            action=_operation("aggregate", "agg.features", p.dataset_id,
                              {"by": pair, "grain": "day",
                               "measures": [m.name for m in measures[:1]],
                               "windows": ["7d", "30d"]}),
        ))
        return out


@register
class JoinSuggester(Suggester):
    """Propose joins to other datasets, matched by semantic type.

    This is the payoff of the semantic layer: two datasets are joinable when they
    share a *meaning*, not merely a column name.
    """

    id: ClassVar[str] = "suggest.join"
    title: ClassVar[str] = "Suggested joins"
    Params: ClassVar[type[BaseModel]] = NoParams

    def suggest(self, ctx: SuggestCtx) -> list[Suggestion]:
        p = ctx.profile
        out: list[Suggestion] = []
        joinable: list[ColumnProfile] = [
            c for c in p.columns if SEMANTIC_TYPES.joinable_with(c.semantic_type)
        ]
        for peer in ctx.peers:
            if peer.dataset_id == p.dataset_id:
                continue
            for left in joinable:
                for right in peer.columns:
                    if right.semantic_type != left.semantic_type:
                        continue
                    score = 0.9 if left.name == right.name else 0.7
                    # A join onto a small aggregate is an annotation, which is the
                    # most useful kind, so rank it up.
                    annotation = peer.row_count and peer.row_count < max(1, p.row_count) / 10
                    if annotation:
                        score = min(0.95, score + 0.15)
                    out.append(Suggestion(
                        title=(f"Annotate with {peer.dataset_id[:8]} on {left.name}"
                               if annotation
                               else f"Join {left.name} to {right.name}"),
                        rationale=f"both columns are {left.semantic_type}",
                        kind="join", score=score,
                        action={
                            "op": "join", "plugin_id": "",
                            "inputs": [{"dataset_id": p.dataset_id},
                                       {"dataset_id": peer.dataset_id}],
                            "params": {"left_column": left.name,
                                       "right_column": right.name, "how": "left"},
                        },
                    ))
        return sorted(out, key=lambda s: s.score, reverse=True)[:20]
