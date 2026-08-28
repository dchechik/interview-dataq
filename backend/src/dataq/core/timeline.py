"""``TimelineSpec`` -- a list of timed events, and how to read them.

A timeline answers a different question from a chart. A chart asks "what is the
shape of this data"; a timeline asks "what happened, to this thing, in this
order". You reach for it when the individual events matter -- one user's session,
one IP's activity, one taxi's shifts -- rather than their distribution.

Structurally it is the humblest thing in the codebase: a ``QuerySpec`` with a
time column, no aggregation, ordered by time. Everything here is presentation:
which column is the headline, which attributes ride along, which of those are
worth emphasising, and which values make an event worth a second look.

The abnormality rule exists because DataQ can already compute the input for it.
``agg.frequency`` produces ``share`` and ``rarity`` columns; joined back onto the
events they came from, every row carries how common its own value is. A rule of
``share < 0.01`` then means "this login came from somewhere we almost never see
logins from" -- which is the spec's own worked example, made visible.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Deliberately numeric-only. Abnormality is a threshold on a measure, not a
# membership test -- "rare" is a number, and keeping it a number means the rule
# reads the same whether it came from a suggester, the UI, or an agent.
AbnormalityOp = Literal["<", "<=", ">", ">=", "==", "!="]


class AbnormalityRule(BaseModel):
    """When to mark a whole event as worth attention.

    Evaluated on the client: the rows are already there, so a threshold can be
    dragged without another round trip.
    """

    column: str = Field(description="A numeric column of the timeline's query")
    op: AbnormalityOp = "<"
    value: float
    label: str = Field(default="unusual", description="Badge text on a matching event")
    # Why this rule was proposed, when a suggester wrote it. Shown in the UI so a
    # highlight never looks arbitrary.
    rationale: str = ""


class EventAttribute(BaseModel):
    """One column shown alongside an event."""

    column: str
    label: str | None = None
    # Emphasised attributes read as part of the event; the rest are secondary
    # detail. This is the "configure which attributes are highlighted" knob.
    highlight: bool = False
    # Offer "show only this value" on click. Set for columns whose values
    # identify a subject worth pivoting to -- a user, an IP, a vehicle.
    filterable: bool = False


class TimelineSpec(BaseModel):
    """How to render a set of timed events."""

    time_column: str
    # The headline of each event. Without one, the time carries the row alone.
    title_column: str | None = None
    attributes: list[EventAttribute] = []
    abnormality: AbnormalityRule | None = None
    # Newest first is right for "what just happened"; oldest first for reading a
    # session as a story. Suggesters pick descending; the UI can flip it.
    descending: bool = True
    # Break the list with date headings. Useful over days, noise over minutes.
    group_by_day: bool = True
    description: str = ""

    def columns(self) -> list[str]:
        """Every column this timeline reads."""
        names = [self.time_column]
        if self.title_column:
            names.append(self.title_column)
        names.extend(a.column for a in self.attributes)
        if self.abnormality:
            names.append(self.abnormality.column)
        # Preserve order, drop duplicates.
        return list(dict.fromkeys(names))
