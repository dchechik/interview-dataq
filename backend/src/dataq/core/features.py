"""Behavioural features: per-entity, time-windowed statistics attached to rows.

The question this answers is "how unusual is this event, for this actor, right
now" -- how many times this user did this in the last 30 days, how common the
activity is across everyone, how long since they last did it. Every one of those
is the same shape: *an aggregate, over a partition, within a window, evaluated
per row*. That shape is this module.

Two notations for one thing. ``Feature`` is the typed IR, which is what the API
and the agent exchange; the shorthand is what a person types, because nested
JSON in a form field is not an interface:

    count() by user, activity_type over 30d
    share() by activity_type
    days_since_last() by user, activity_type
    avg(amount) by user over 7d as spend_7d

Both compile to a single SQL window expression. Nothing here touches a database
or a dataset -- it turns notation into a string, which is what makes it cheap to
test exhaustively.

On leakage: a feature with no window sees the whole dataset, including rows that
come *after* the one it describes. For "how common is this activity overall"
that is the intended reading, so it is allowed -- but it is recorded on the
feature rather than left for someone to rediscover. See ``Feature.sees_future``.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

# Aggregates evaluated over the window.
AggFn = Literal["count", "count_distinct", "sum", "avg", "min", "max",
                "stddev", "median", "share"]
# Features about a row's position in its entity's sequence. These have no
# meaningful window -- "days since the previous login" is about two adjacent
# events, not about a span of time -- so a window on them is rejected rather
# than silently ignored.
SeqFn = Literal["days_since_last", "days_since_first", "event_index"]

FeatureFn = Literal[
    "count", "count_distinct", "sum", "avg", "min", "max", "stddev", "median",
    "share", "days_since_last", "days_since_first", "event_index",
]

SEQUENCE_FNS: frozenset[str] = frozenset(("days_since_last", "days_since_first",
                                          "event_index"))
# Functions that take no column: they count rows rather than summarise a value.
NULLARY_FNS: frozenset[str] = frozenset(("count", "share", "days_since_last",
                                         "days_since_first", "event_index"))

CalendarUnit = Literal["hour", "day", "week", "month", "quarter", "year"]

# Duration suffixes. "m" is minutes and "mo" is months -- the ambiguity is real
# and resolved in favour of the one people write more often in this context.
_DURATION_UNITS: dict[str, str] = {
    "s": "SECOND", "m": "MINUTE", "h": "HOUR",
    "d": "DAY", "w": "WEEK", "mo": "MONTH", "y": "YEAR",
}
_DURATION_RE = re.compile(r"^(\d+)\s*(mo|[smhdwy])$", re.I)


class FeatureError(ValueError):
    """A feature could not be understood or does not make sense."""


def parse_duration(text: str) -> tuple[int, str]:
    """``"30d"`` -> ``(30, "DAY")``."""
    m = _DURATION_RE.match(text.strip())
    if not m:
        raise FeatureError(
            f"{text!r} is not a duration. Write a number and a unit, like "
            "'30d', '12h', '2w' (s/m/h/d/w/mo/y)."
        )
    return int(m.group(1)), _DURATION_UNITS[m.group(2).lower()]


class Window(BaseModel):
    """When a feature looks, relative to the row it describes."""

    kind: Literal["all", "trailing", "calendar"] = "all"
    # kind="trailing": how far back, e.g. "30d".
    duration: str | None = None
    # kind="calendar": which bucket the row falls in, e.g. "day" for "today".
    unit: CalendarUnit | None = None
    # Count the row itself. "How many logins in the last 30 days" normally means
    # including this one; set False for a strictly-past feature.
    include_current: bool = True

    def label(self) -> str:
        if self.kind == "trailing":
            return self.duration or ""
        if self.kind == "calendar":
            return f"per_{self.unit}"
        return ""

    def describe(self) -> str:
        if self.kind == "trailing":
            n, unit = parse_duration(self.duration or "")
            span = f"{n} {unit.lower()}{'s' if n != 1 else ''}"
            return (f"the {span} up to and including this event"
                    if self.include_current else f"the {span} before this event")
        if self.kind == "calendar":
            return f"the same {self.unit} as this event"
        return "the whole dataset, including events after this one"


class Feature(BaseModel):
    """One computed column."""

    fn: FeatureFn
    column: str | None = Field(default=None, description="Column to summarise")
    by: list[str] = Field(default_factory=list, description="Partition keys")
    window: Window = Field(default_factory=Window)
    name: str | None = Field(default=None, description="Output column name")

    @property
    def is_sequence(self) -> bool:
        return self.fn in SEQUENCE_FNS

    @property
    def sees_future(self) -> bool:
        """Whether this feature's value depends on rows that come after it.

        True for anything unwindowed, and for a calendar bucket -- "today"
        includes the rest of today. Not an error; several of these are exactly
        what an analyst wants. It is surfaced so the fact travels with the
        column instead of being rediscovered later.
        """
        if self.is_sequence:
            # All three read backwards from the row: lag, min, row_number, each
            # ordered by time. None can see forward.
            return False
        return self.window.kind in ("all", "calendar")

    def output_name(self) -> str:
        """A readable, deterministic column name.

        Deterministic matters: re-running the same feature set must produce the
        same columns, or a second run appends duplicates instead of replacing.
        """
        if self.name:
            return self.name
        stem = "n" if self.fn == "count" else self.fn
        parts = [stem]
        if self.column and self.fn not in NULLARY_FNS:
            parts.append(self.column)
        if self.by:
            parts.append("by_" + "_".join(self.by))
        label = self.window.label()
        if label:
            parts.append(label)
        return _sanitize("_".join(parts))

    def describe(self) -> str:
        """One sentence, used as the column's rationale in the UI and logs."""
        what = {
            "count": "number of events",
            "share": "share of all events",
            "count_distinct": f"distinct {self.column}",
            "days_since_last": "days since the previous event",
            "days_since_first": "days since the first event",
            "event_index": "position in the sequence",
        }.get(self.fn, f"{self.fn} of {self.column}")
        who = f" for the same {' + '.join(self.by)}" if self.by else ""
        if self.is_sequence:
            return f"{what}{who}".capitalize()
        return f"{what}{who}, over {self.window.describe()}".capitalize()


def _sanitize(name: str) -> str:
    """Make a string safe and pleasant as a column name."""
    cleaned = re.sub(r"[^0-9a-zA-Z_]+", "_", name).strip("_").lower()
    return cleaned or "feature"


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def validate(feature: Feature, columns: set[str], time_column: str | None) -> None:
    """Check a feature against the dataset it will run on.

    Every failure here names the offending part, because these are written by
    hand and a message like "invalid feature" would send the reader back to
    guessing.
    """
    if feature.fn not in NULLARY_FNS and not feature.column:
        raise FeatureError(f"{feature.fn}() needs a column, as in {feature.fn}(amount)")
    if feature.column and feature.fn in NULLARY_FNS:
        raise FeatureError(
            f"{feature.fn}() counts rows, so it takes no column "
            f"-- drop {feature.column!r}"
        )
    for name in [*feature.by, *([feature.column] if feature.column else [])]:
        if name not in columns:
            raise FeatureError(
                f"no column named {name!r}; available: {', '.join(sorted(columns)[:12])}"
            )
    if feature.window.kind == "trailing":
        if not feature.window.duration:
            raise FeatureError("a trailing window needs a duration, as in 'over 30d'")
        parse_duration(feature.window.duration)
    if feature.window.kind == "calendar" and not feature.window.unit:
        raise FeatureError("a calendar window needs a unit, as in 'in day'")
    if feature.is_sequence and feature.window.kind != "all":
        raise FeatureError(
            f"{feature.fn}() is about neighbouring events, not a span of time, "
            "so it cannot take a window"
        )
    needs_time = feature.is_sequence or feature.window.kind in ("trailing", "calendar")
    if needs_time and not time_column:
        raise FeatureError(
            f"{feature.output_name()} needs a time column, but the dataset has "
            "none. Parse one with normalize.timestamp first."
        )


# --------------------------------------------------------------------------- #
# SQL
# --------------------------------------------------------------------------- #
def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


_AGG_SQL: dict[str, str] = {
    "count": "count(*)",
    "count_distinct": "count(DISTINCT {c})",
    "sum": "sum({c})",
    "avg": "avg({c})",
    "min": "min({c})",
    "max": "max({c})",
    "stddev": "stddev({c})",
    "median": "median({c})",
}


def window_clause(feature: Feature, time_column: str | None) -> str:
    """The OVER (...) body. Features sharing this text share one sort.

    That is not a detail: DuckDB reuses a single sort for identical window
    definitions, and on 5M rows five features sharing a window cost 0.7s while
    six across three windows cost 6.3s. So this is generated deterministically
    -- same window, same text, byte for byte.
    """
    parts: list[str] = []
    partition = [quote(k) for k in feature.by]
    if feature.window.kind == "calendar" and time_column:
        partition.append(f"date_trunc('{feature.window.unit}', {quote(time_column)})")
    if partition:
        parts.append("PARTITION BY " + ", ".join(partition))

    if feature.window.kind == "trailing" and time_column:
        n, unit = parse_duration(feature.window.duration or "")
        parts.append(f"ORDER BY {quote(time_column)}")
        parts.append(
            f"RANGE BETWEEN INTERVAL {n} {unit} PRECEDING AND CURRENT ROW"
        )
        if not feature.window.include_current:
            # EXCLUDE CURRENT ROW drops the row itself but keeps its ties; that
            # is the right reading of "before this event" when two events share
            # a timestamp, since neither precedes the other.
            parts.append("EXCLUDE CURRENT ROW")
    return " ".join(parts)


def to_sql(feature: Feature, time_column: str | None) -> str:
    """The full SQL expression for one feature, evaluated per row."""
    col = quote(feature.column) if feature.column else None
    over = window_clause(feature, time_column)
    ts = quote(time_column) if time_column else None

    if feature.fn == "days_since_last":
        order = f"PARTITION BY {', '.join(quote(k) for k in feature.by)} " if feature.by else ""
        return (f"date_diff('day', lag({ts}) OVER ({order}ORDER BY {ts}), {ts})")
    if feature.fn == "days_since_first":
        part = f"PARTITION BY {', '.join(quote(k) for k in feature.by)}" if feature.by else ""
        return f"date_diff('day', min({ts}) OVER ({part}), {ts})"
    if feature.fn == "event_index":
        order = f"PARTITION BY {', '.join(quote(k) for k in feature.by)} " if feature.by else ""
        return f"row_number() OVER ({order}ORDER BY {ts})"

    if feature.fn == "share":
        # Share of what: the same window, but without the partition. So "share
        # of all events" rather than "share of this user's events" -- which is
        # the reading that makes 'how common is a login from France' work.
        whole = Feature(fn="count", by=[], window=feature.window)
        denominator = window_clause(whole, time_column)
        return (f"count(*) OVER ({over})::DOUBLE / "
                f"nullif(count(*) OVER ({denominator}), 0)")

    body = _AGG_SQL[feature.fn].format(c=col)
    return f"{body} OVER ({over})"


# --------------------------------------------------------------------------- #
# the shorthand
# --------------------------------------------------------------------------- #
_SHORTHAND_RE = re.compile(
    r"""^\s*
    (?P<fn>[a-z_]+)\s*\(\s*(?P<arg>[^)]*?)\s*\)      # count()  avg(amount)
    (?:\s+by\s+(?P<by>[^]]*?))?                      # by user, activity_type
    (?:\s+over\s+(?P<over>\S+))?                     # over 30d
    (?:\s+in\s+(?P<in>\w+))?                         # in day
    (?P<excl>\s+excluding\s+current)?                # excluding current
    (?:\s+as\s+(?P<as>\w+))?                         # as spend_7d
    \s*$""",
    re.I | re.X,
)


def parse(text: str) -> Feature:
    """Parse one shorthand feature expression.

    ``count() by user, activity_type over 30d as recent``
    """
    raw = text.strip().rstrip(";,")
    if not raw:
        raise FeatureError("empty feature")
    m = _SHORTHAND_RE.match(raw)
    if not m:
        raise FeatureError(
            f"could not parse {text!r}. Expected something like "
            "'count() by user, activity_type over 30d'."
        )

    fn = m.group("fn").lower()
    if fn == "n":
        fn = "count"
    if fn not in FeatureFn.__args__:  # type: ignore[attr-defined]
        raise FeatureError(
            f"unknown function {fn!r}. Available: "
            f"{', '.join(sorted(FeatureFn.__args__))}."  # type: ignore[attr-defined]
        )

    arg = (m.group("arg") or "").strip()
    by = [k.strip() for k in (m.group("by") or "").split(",") if k.strip()]

    over, unit = m.group("over"), m.group("in")
    if over and unit:
        raise FeatureError(
            f"{text!r} asks for both a trailing window ('over {over}') and a "
            f"calendar bucket ('in {unit}'); pick one."
        )
    if over:
        window = Window(kind="trailing", duration=over.lower(),
                        include_current=not m.group("excl"))
    elif unit:
        window = Window(kind="calendar", unit=unit.lower())  # type: ignore[arg-type]
    else:
        window = Window()

    return Feature(fn=fn, column=arg or None, by=by, window=window,
                   name=m.group("as"))


def parse_all(lines: list[str] | str) -> list[Feature]:
    """Parse a block of shorthand, one feature per line.

    Accepts a list (the API shape) or a newline-separated string (what the
    textarea sends). Blank lines and ``#`` comments are skipped so a feature set
    can be annotated.
    """
    if isinstance(lines, str):
        lines = lines.splitlines()
    out: list[Feature] = []
    for i, line in enumerate(lines, 1):
        text = line.split("#", 1)[0].strip()
        if not text:
            continue
        try:
            out.append(parse(text))
        except FeatureError as exc:
            raise FeatureError(f"line {i}: {exc}") from exc
    if not out:
        raise FeatureError("no features given")
    return out


def coerce(items: list) -> list[Feature]:
    """Accept either notation: shorthand strings or IR objects."""
    features: list[Feature] = []
    for item in items:
        if isinstance(item, Feature):
            features.append(item)
        elif isinstance(item, str):
            features.extend(parse_all([item]))
        elif isinstance(item, dict):
            features.append(Feature.model_validate(item))
        else:
            raise FeatureError(f"cannot read a feature from {type(item).__name__}")
    return features


def distinct_windows(features: list[Feature], time_column: str | None) -> int:
    """How many sorts this feature set will cost.

    The number that actually predicts the wait, so it is worth reporting before
    a run rather than leaving the user to wonder.
    """
    return len({window_clause(f, time_column) for f in features
                if not f.is_sequence} | {
        f"seq:{','.join(f.by)}" for f in features if f.is_sequence})
