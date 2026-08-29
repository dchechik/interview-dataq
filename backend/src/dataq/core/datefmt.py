"""Working out how a text column encodes its dates.

DuckDB's CSV sniffer types a column as VARCHAR whenever the dates in it are not
ISO-8601, which is most of the time in real data: `03/04/2016`, `Jan 2, 2016`,
`20160102`, epoch seconds. The column is then a string as far as the rest of the
system is concerned, so it cannot be bucketed, charted over time, or laid out on
a timeline until somebody works out the format by hand.

This module works it out instead, by trying a library of formats against sampled
values and ranking them by how many parse.

The one thing it must not do is guess when guessing is unsafe. `03/04/2016` is
the 4th of March in most of the world and the 3rd of April in the US, and no
amount of statistics settles which -- so the ambiguity is detected, reported
with a worked example, and handed to the user to resolve. It resolves itself
whenever the sample contains a day past the 12th, because then only one reading
parses everything.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel

# Formats to try, most specific first. Every one of these must mean the same
# thing to Python's strptime and to DuckDB's, because inference runs here and
# parsing runs there; test_datefmt.py checks that against a live DuckDB.
#
# The `group` is what makes two formats rivals rather than alternatives: within
# a group the field order is the only difference, so a sample that both parse is
# genuinely ambiguous. Formats in different groups are distinguishable by shape.
_FORMATS: tuple[tuple[str, str, str | None], ...] = (
    # ISO 8601 and its near neighbours.
    ("%Y-%m-%dT%H:%M:%S.%f", "ISO 8601 with fractional seconds", None),
    ("%Y-%m-%dT%H:%M:%S", "ISO 8601", None),
    ("%Y-%m-%dT%H:%M", "ISO 8601, minutes", None),
    ("%Y-%m-%d %H:%M:%S.%f", "YYYY-MM-DD HH:MM:SS.ffffff", None),
    ("%Y-%m-%d %H:%M:%S", "YYYY-MM-DD HH:MM:SS", None),
    ("%Y-%m-%d %H:%M", "YYYY-MM-DD HH:MM", None),
    ("%Y-%m-%d", "YYYY-MM-DD", None),
    ("%Y/%m/%d %H:%M:%S", "YYYY/MM/DD HH:MM:SS", None),
    ("%Y/%m/%d", "YYYY/MM/DD", None),
    # Slash-separated, day/month order unknowable from shape alone.
    ("%m/%d/%Y %I:%M:%S %p", "MM/DD/YYYY, 12-hour clock", "slash4"),
    ("%d/%m/%Y %I:%M:%S %p", "DD/MM/YYYY, 12-hour clock", "slash4"),
    ("%m/%d/%Y %H:%M:%S", "MM/DD/YYYY HH:MM:SS", "slash4"),
    ("%d/%m/%Y %H:%M:%S", "DD/MM/YYYY HH:MM:SS", "slash4"),
    ("%m/%d/%Y %I:%M %p", "MM/DD/YYYY HH:MM am/pm", "slash4"),
    ("%d/%m/%Y %I:%M %p", "DD/MM/YYYY HH:MM am/pm", "slash4"),
    ("%m/%d/%Y %H:%M", "MM/DD/YYYY HH:MM", "slash4"),
    ("%d/%m/%Y %H:%M", "DD/MM/YYYY HH:MM", "slash4"),
    ("%m/%d/%Y", "MM/DD/YYYY", "slash4"),
    ("%d/%m/%Y", "DD/MM/YYYY", "slash4"),
    ("%m/%d/%y", "MM/DD/YY", "slash2"),
    ("%d/%m/%y", "DD/MM/YY", "slash2"),
    # Dash- and dot-separated.
    ("%d-%m-%Y %H:%M:%S", "DD-MM-YYYY HH:MM:SS", "dash4"),
    ("%m-%d-%Y %H:%M:%S", "MM-DD-YYYY HH:MM:SS", "dash4"),
    ("%d-%m-%Y", "DD-MM-YYYY", "dash4"),
    ("%m-%d-%Y", "MM-DD-YYYY", "dash4"),
    ("%d.%m.%Y %H:%M:%S", "DD.MM.YYYY HH:MM:SS", "dot4"),
    ("%d.%m.%Y", "DD.MM.YYYY", "dot4"),
    # Month by name: unambiguous, because the name fixes which field is which.
    ("%d %b %Y %H:%M:%S", "DD Mon YYYY HH:MM:SS", None),
    ("%b %d, %Y %H:%M:%S", "Mon DD, YYYY HH:MM:SS", None),
    ("%b %d, %Y", "Mon DD, YYYY", None),
    ("%B %d, %Y", "Month DD, YYYY", None),
    ("%d %b %Y", "DD Mon YYYY", None),
    ("%d %B %Y", "DD Month YYYY", None),
    # Compact.
    ("%Y%m%d%H%M%S", "YYYYMMDDHHMMSS", None),
    ("%Y%m%d", "YYYYMMDD", None),
)

# Epoch timestamps, as a numeric column. The ranges are the point: 1e9..2e9 in
# seconds is 2001..2033, which is where real data sits, and the same instants in
# milliseconds are 1e12..2e12. Anything outside is some other quantity that
# happens to be a big number, so it is left alone.
EPOCH_UNITS: tuple[tuple[str, float, float], ...] = (
    ("s", 1e9, 2e9),
    ("ms", 1e12, 2e12),
    ("us", 1e15, 2e15),
)


class FormatCandidate(BaseModel):
    """One way of reading a column, and how well it worked on the sample."""

    format: str
    label: str
    success_rate: float
    # A value from the sample and what this format turns it into. The example is
    # what makes an ambiguity legible: two candidates differ in the abstract, but
    # "03/04/2016 -> March 4th" versus "-> April 3rd" is a question anyone can
    # answer.
    example_input: str = ""
    example_output: str = ""
    # Set when another candidate parses the sample equally well and disagrees
    # about what it means. Carries the human-readable disagreement.
    conflict: str | None = None


def _clean(values: Sequence) -> list[str]:
    return [s for s in (str(v).strip() for v in values if v is not None) if s]


def _rate(values: list[str], fmt: str) -> tuple[float, str, str]:
    """Fraction of values that parse, plus one worked example."""
    ok = 0
    example_in = example_out = ""
    for v in values:
        try:
            parsed = datetime.strptime(v, fmt)
        except (ValueError, TypeError):
            continue
        ok += 1
        if not example_in:
            example_in, example_out = v, parsed.isoformat(sep=" ")
    return ok / len(values), example_in, example_out


def infer_formats(
    values: Sequence, *, min_rate: float = 0.9, limit: int = 4
) -> list[FormatCandidate]:
    """Rank the formats that explain these values, best first.

    Only formats reaching ``min_rate`` are returned: a format that parses half a
    column is not a description of that column, it is a coincidence.
    """
    vals = _clean(values)
    if not vals:
        return []

    scored: list[tuple[float, str, str, str, str, str | None]] = []
    for fmt, label, group in _FORMATS:
        rate, ex_in, ex_out = _rate(vals, fmt)
        if rate >= min_rate:
            scored.append((rate, fmt, label, ex_in, ex_out, group))
    if not scored:
        return []

    # Rank by success rate; ties keep the order of _FORMATS, which is most
    # specific first, so "YYYY-MM-DD HH:MM:SS" beats "YYYY-MM-DD" on a column
    # that carries times.
    scored.sort(key=lambda s: -s[0])
    best_rate = scored[0][0]

    out: list[FormatCandidate] = []
    for rate, fmt, label, ex_in, ex_out, group in scored[:limit]:
        conflict = None
        if group is not None and rate >= best_rate:
            # A rival is another format in the same group that does equally well
            # and reads at least one sampled value differently.
            for other_rate, other_fmt, other_label, _, _, other_group in scored:
                if other_group != group or other_fmt == fmt or other_rate < best_rate:
                    continue
                clash = _first_disagreement(vals, fmt, other_fmt)
                if clash:
                    value, mine, theirs = clash
                    conflict = (
                        f"{value} reads as {mine} under {label}, "
                        f"but {theirs} under {other_label}"
                    )
                    break
        out.append(FormatCandidate(
            format=fmt, label=label, success_rate=rate,
            example_input=ex_in, example_output=ex_out, conflict=conflict,
        ))
    return out


def _first_disagreement(
    values: list[str], a: str, b: str
) -> tuple[str, str, str] | None:
    """The first sampled value that two formats read differently."""
    for v in values:
        try:
            pa, pb = datetime.strptime(v, a), datetime.strptime(v, b)
        except (ValueError, TypeError):
            continue
        if pa != pb:
            return v, pa.strftime("%-d %b %Y"), pb.strftime("%-d %b %Y")
    return None


def ambiguous(candidates: Sequence[FormatCandidate]) -> bool:
    """True when the best reading is not uniquely determined by the data.

    Deliberately narrow: only a conflict on the *winning* candidate counts. A
    lower-ranked format that disagrees is not ambiguity, it is simply wrong.
    """
    return bool(candidates) and candidates[0].conflict is not None


def infer_epoch(minimum, maximum) -> tuple[str, str] | None:
    """Identify a numeric column as epoch time. Returns (unit, label)."""
    try:
        lo, hi = float(minimum), float(maximum)
    except (TypeError, ValueError):
        return None
    for unit, low, high in EPOCH_UNITS:
        if low <= lo and hi <= high:
            names = {"s": "seconds", "ms": "milliseconds", "us": "microseconds"}
            return unit, f"epoch {names[unit]}"
    return None
