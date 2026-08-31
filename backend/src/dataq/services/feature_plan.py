"""Proposing a feature set from the shape of a table.

Knowing that behavioural features exist is not the same as knowing what to write.
The expressions are short, but the useful ones follow from the table rather than
from imagination: pick whoever acts, pick the clock, and then every categorical
column has the same two questions asked of it and every numeric column the same
one.

Those questions are:

* **How often does this actor see this value, lately?** -- per-actor frequency
  over a recent window. Unusual for *this* person.
* **How long since they last saw it?** -- recency. First contact reads as empty.
* **How common is the value across everyone?** -- the population share. Unusual
  in general, which is a different claim from unusual for one person, and the
  two together are what "suspicious" usually means.
* **Where does this number sit in the distribution?** -- percentile, both
  overall and within the actor.

The output is *shorthand text*, not a plan object, because it is going into an
editable box. A proposal you cannot amend is worse than none: the tool cannot
know that `sender_domain` matters more than `sender_subject` here, and the
person reading it does.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..core import features as F
from ..core.profile import ColumnProfile, DatasetProfile, entity_columns
from ..core.semantic import SEMANTIC_TYPES

# A categorical column worth asking about. Above this many distinct values it is
# closer to an identifier than a category, and "how often does this user see
# this value" stops having repeat observations to average over.
MAX_CATEGORY_DISTINCT = 500
# And below this it is a flag, where per-actor frequency says little.
MIN_CATEGORY_DISTINCT = 2

# How many of each kind to propose. The point is a starting draft, not a
# complete one: a dozen expressions is already more than most people will keep,
# and every extra distinct partition is another sort at run time.
MAX_CATEGORIES = 3
MAX_MEASURES = 2

DEFAULT_WINDOW = "30d"


class ActorChoice(BaseModel):
    """A column that could be the thing behaviour is measured per."""

    column: str
    distinct: int = 0
    reason: str = ""


class ProposedFeature(BaseModel):
    expression: str
    explains: str = Field(description="What the column will mean, in a sentence")


class FeatureProposal(BaseModel):
    """A draft feature set, and the choices behind it."""

    actor: str | None = None
    # Other columns that could be the actor. The caller picks; this is the part
    # a table's shape cannot settle on its own.
    actor_options: list[ActorChoice] = []
    time_column: str | None = None
    window: str = DEFAULT_WINDOW
    features: list[ProposedFeature] = []
    # Sorts this set will cost, which is what predicts the wait -- not the
    # number of features.
    distinct_windows: int = 0
    # Why there is nothing to propose, when there is nothing to propose.
    blocked: str | None = None

    @property
    def text(self) -> str:
        return "\n".join(f.expression for f in self.features)


def _categoricals(profile: DatasetProfile, actor: str) -> list[ColumnProfile]:
    """Columns whose *value* is worth asking about, least varied first.

    Ordered the opposite way to actors: a country with seven values makes a
    better "how common is this" question than a subject line with hundreds.
    """
    out = []
    for c in profile.columns:
        if c.name == actor or c.role in ("time", "measure", "ignore"):
            continue
        distinct = c.stats.distinct_count if c.stats else 0
        if not (MIN_CATEGORY_DISTINCT <= distinct <= MAX_CATEGORY_DISTINCT):
            continue
        out.append(c)
    return sorted(out, key=lambda c: (c.stats.distinct_count if c.stats else 0))


def _measures(profile: DatasetProfile) -> list[ColumnProfile]:
    """Numeric columns worth ranking. Identifiers stored as numbers are not."""
    out = []
    for c in profile.columns:
        if c.role != "measure":
            continue
        if c.name.endswith(("_id", "_seq", "_index", "_count", "_version")):
            continue
        if SEMANTIC_TYPES.matches_any(c.semantic_type,
                                      ("numeric.share", "numeric.rarity")):
            continue  # already a rarity score; ranking it again says nothing
        distinct = c.stats.distinct_count if c.stats else 0
        if distinct < 3:
            continue  # a flag in numeric clothing
        out.append(c)
    return sorted(out, key=lambda c: -(c.stats.distinct_count if c.stats else 0))


def propose(profile: DatasetProfile, actor: str | None = None,
            window: str = DEFAULT_WINDOW) -> FeatureProposal:
    """Draft a feature set for this table.

    ``actor`` overrides the guess, which is the whole reason it is offered
    rather than assumed: whether behaviour is per-recipient or per-sending-host
    is a question about intent, and the table cannot answer it.
    """
    candidates = entity_columns(profile)
    options = [
        ActorChoice(
            column=c.name,
            distinct=c.stats.distinct_count if c.stats else 0,
            reason=(f"{c.stats.distinct_count:,} distinct values"
                    if c.stats else "no statistics"),
        )
        for c in candidates
    ]
    proposal = FeatureProposal(actor_options=options, window=window)

    times = profile.time_columns()
    if not times:
        proposal.blocked = (
            "No usable time column. Behavioural features are about what happened "
            "when, so there is nothing to propose until one is parsed -- see the "
            "clean-up suggestions on this dataset."
        )
        return proposal
    proposal.time_column = times[0].name

    chosen = actor or (candidates[0].name if candidates else None)
    if chosen is None:
        proposal.blocked = (
            "No column looks like an actor -- something with several events "
            "each, such as a user, an account or a machine. Features can still "
            "be written by hand against any column."
        )
        return proposal
    if actor and not any(o.column == actor for o in options):
        known = ", ".join(o.column for o in options) or "none"
        raise ValueError(f"{actor!r} is not a usable actor column; candidates: {known}")
    proposal.actor = chosen

    features: list[ProposedFeature] = []
    for c in _categoricals(profile, chosen)[:MAX_CATEGORIES]:
        features.append(ProposedFeature(
            expression=f"count() by {chosen}, {c.name} over {window}",
            explains=f"How often this {chosen} has seen this {c.name} "
                     f"in the last {window}",
        ))
        features.append(ProposedFeature(
            expression=f"days_since_last() by {chosen}, {c.name}",
            explains=f"Days since this {chosen} last saw this {c.name}; "
                     "empty on first contact",
        ))
        features.append(ProposedFeature(
            expression=f"share() by {c.name}",
            explains=f"What fraction of all rows carry this {c.name}, "
                     "across everyone",
        ))

    for c in _measures(profile)[:MAX_MEASURES]:
        features.append(ProposedFeature(
            expression=f"percentile({c.name})",
            explains=f"Where this {c.name} sits in the whole column, 0 to 1",
        ))
        features.append(ProposedFeature(
            expression=f"percentile({c.name}) by {chosen}",
            explains=f"Where it sits among this {chosen}'s own {c.name} values",
        ))

    proposal.features = features
    parsed = F.parse_all([f.expression for f in features]) if features else []
    proposal.distinct_windows = F.distinct_windows(parsed, proposal.time_column)
    if not features:
        proposal.blocked = (
            f"Nothing to summarise beside {chosen}: this table has no repeated "
            "category and no numeric column to rank."
        )
    return proposal
