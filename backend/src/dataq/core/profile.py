"""Column statistics and semantic-type guesses produced by profiling."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .datefmt import FormatCandidate
from .types import ColumnRole


class ColumnStats(BaseModel):
    """Cheap statistics computed over a sample, handed to every ``Detector``."""

    name: str
    physical_type: str
    row_count: int = 0
    null_count: int = 0
    distinct_count: int = 0
    min: Any = None
    max: Any = None
    # Distinct non-null example values, capped. Detectors pattern-match on these.
    sample_values: list[Any] = []
    top_values: list[tuple[Any, int]] = []

    @property
    def null_frac(self) -> float:
        return self.null_count / self.row_count if self.row_count else 0.0

    @property
    def distinct_frac(self) -> float:
        return self.distinct_count / self.row_count if self.row_count else 0.0


class SemanticGuess(BaseModel):
    semantic_type: str
    confidence: float = 0.0
    rationale: str = ""
    detector_id: str = ""
    # How to read the column, when knowing its type is not enough to read it.
    # Only temporal detectors populate this: "this is a date" does not tell you
    # whether 03/04 is March or April, and the transform needs to be told.
    # Ranked best-first; a conflict on the first entry means the data cannot
    # settle it and a human must.
    formats: list[FormatCandidate] = []


class ColumnProfile(BaseModel):
    """A column's physical facts plus its resolved semantic identity."""

    name: str
    physical_type: str
    semantic_type: str | None = None
    confidence: float = 0.0
    role: ColumnRole = "dimension"
    # True once a human edits the type, which freezes it against re-detection.
    pinned: bool = False
    stats: ColumnStats | None = None
    candidates: list[SemanticGuess] = []
    # Something the user needs to know that the data cannot settle -- currently
    # only that the reader silently picked one reading of an ambiguous date.
    warning: str | None = None


class DatasetProfile(BaseModel):
    dataset_id: str
    version: int
    row_count: int
    columns: list[ColumnProfile]

    def column(self, name: str) -> ColumnProfile | None:
        return next((c for c in self.columns if c.name == name), None)

    def by_semantic(self, *type_ids: str) -> list[ColumnProfile]:
        from .semantic import SEMANTIC_TYPES

        return [c for c in self.columns if SEMANTIC_TYPES.matches_any(c.semantic_type, type_ids)]

    def by_role(self, *roles: ColumnRole) -> list[ColumnProfile]:
        return [c for c in self.columns if c.role in roles]

    def time_columns(self) -> list[ColumnProfile]:
        """Columns usable as a time axis, which is not quite role == "time".

        The role is recorded at profiling time and can be stale -- a dataset
        imported before roles took storage into account, or a column a user
        pinned by hand -- so the physical type is checked as well. Every caller
        of by_role("time") wanted this; asking for the role alone is how a text
        column reached DuckDB as `VARCHAR - INTERVAL`.
        """
        return [c for c in self.by_role("time") if is_temporal(c.physical_type)]


TEMPORAL_PHYSICAL = ("TIMESTAMP", "DATE", "TIME")


def is_temporal(physical_type: str) -> bool:
    """Whether a column can be used as a time axis *as stored*.

    Meaning and storage are different questions, and this is the one that
    decides whether date arithmetic will work. A VARCHAR of '03/07/2011
    08:07:29' means a timestamp -- detection says so, correctly -- but
    subtracting an interval from it is a type error, so nothing may treat it as
    a time axis until it has been parsed.
    """
    return physical_type.upper().startswith(TEMPORAL_PHYSICAL)


# Columns that identify a subject you would want several events for -- a user,
# an address, a vehicle. Shared rather than private to the timeline because
# feature engineering needs exactly the same judgement: these are the columns
# worth partitioning behaviour by.
ENTITY_TYPES: tuple[str, ...] = ("net.ip", "identity.email", "identity.key",
                                 "geo.country_iso2", "categorical")

# An entity is something you have *several* events for. A column with roughly as
# many distinct values as rows is a per-row identifier, not an entity: grouping
# by it puts one event in each group. This is the difference between src_ip and
# event_id, both of which are identity types.
ENTITY_MAX_DISTINCT_FRAC = 0.9


def is_entity(column: ColumnProfile, types: tuple[str, ...] = ENTITY_TYPES) -> bool:
    """Is this a thing you would group behaviour by?"""
    from .semantic import SEMANTIC_TYPES

    if not SEMANTIC_TYPES.matches_any(column.semantic_type, types):
        return False
    if column.stats is None:
        return True  # unprofiled: assume usable rather than hide it
    return column.stats.distinct_frac < ENTITY_MAX_DISTINCT_FRAC


def entity_columns(profile: DatasetProfile,
                   types: tuple[str, ...] = ENTITY_TYPES) -> list[ColumnProfile]:
    """Things you could group behaviour by, most actor-like first.

    Requiring a semantic type would be too strict here. Detection needs enough
    rows to be confident, so on a small or unusual dataset every column comes
    back untyped -- and a plain repeating dimension is a perfectly good thing to
    ask "how often does this one do X" about. So a typed entity is preferred,
    and an untyped repeating dimension still qualifies.

    An actor needs two things at once, and the weaker of them is what limits it:
    **many of them**, so the population means something, and **many events
    each**, so there is a history to compare against. Ranking on either alone
    gets it wrong in opposite directions -- cardinality picks a near-unique
    sender address over the recipient it was sent to, and events-per-value picks
    the seven-value country column over both. Scoring on the smaller of the two
    picks the recipient.

    Deliberately not ordered by whether the column has a semantic type:
    detection gives up on high-cardinality columns, so a real user id is often
    untyped while the five-value category beside it is a confident
    `categorical`, and ranking types first prefers the category.
    """
    def rank(c: ColumnProfile) -> float | None:
        if c.role in ("time", "measure", "ignore"):
            return None
        if not (is_entity(c, types) or c.role == "dimension"):
            return None
        stats = c.stats
        if stats is None:
            return 0.0
        if stats.distinct_frac >= ENTITY_MAX_DISTINCT_FRAC:
            return None  # a per-row identifier: grouping puts one event in each
        distinct = stats.distinct_count
        if not distinct:
            return None
        per_value = stats.row_count / distinct
        return -min(distinct, per_value)

    scored = [(r, c) for c in profile.columns if (r := rank(c)) is not None]
    return [c for _, c in sorted(scored, key=lambda pair: pair[0])]
