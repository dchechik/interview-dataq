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
