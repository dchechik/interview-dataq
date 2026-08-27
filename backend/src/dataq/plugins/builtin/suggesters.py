"""Built-in suggesters.

A ``Suggestion`` carries an ``action`` that is an executable payload -- the UI
renders it as a button that POSTs to /api/operations (or /api/inspect), and the
agent invokes it directly. Suggestions are never prose-only, which is what keeps
"the tool suggests things" from degenerating into a wall of advice.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from ...core.profile import ColumnProfile, DatasetProfile
from ...core.semantic import SEMANTIC_TYPES
from ..base import NoParams, Produces, register
from ..kinds import SuggestCtx, Suggester, Suggestion


def _inspect(plugin_id: str, dataset_id: str, params: dict) -> dict:
    return {"op": "inspect", "plugin_id": plugin_id, "dataset_id": dataset_id,
            "params": params}


def _operation(op: str, plugin_id: str, dataset_id: str, params: dict) -> dict:
    return {"op": op, "plugin_id": plugin_id,
            "inputs": [{"dataset_id": dataset_id}], "params": params}


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
        times = p.by_role("time")
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

        times = p.by_role("time")
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
