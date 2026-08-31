"""What to store each column as, decided before the import rather than after.

A column's physical type is settled by DuckDB's sniffer the moment the file is
read, is copied into every record of the dataset, and cannot be changed
afterwards -- storage is immutable per version and nothing but a transform that
*adds* a column can produce a different type. Semantic type and role, by
contrast, are derived later, from data already written, and stay editable
forever.

That asymmetry is the problem this module exists to fix: the one decision that
cannot be revisited is the one nobody is shown. So the decision is proposed
first, with its evidence, and the user gets to change it before it is made.

The proposal is built by running the *real* profiler over a sample rather than
by reimplementing it. That is deliberate, and it is the property that makes the
preview worth trusting: what it shows is what the import will do, because it is
the same code.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..core.datefmt import FormatCandidate, ambiguous, infer_formats
from ..core.profile import ColumnProfile, is_temporal
from ..core.semantic import SEMANTIC_TYPES
from ..core.types import ColumnRole
from ..query.compiler import quote_ident

# Types a column may be stored as. Deliberately short: these are the ones a
# reader can plausibly get wrong and a person can meaningfully choose between.
TargetType = Literal["VARCHAR", "BIGINT", "DOUBLE", "BOOLEAN", "DATE", "TIMESTAMP"]
TARGET_TYPES: tuple[str, ...] = (
    "VARCHAR", "BIGINT", "DOUBLE", "BOOLEAN", "DATE", "TIMESTAMP")

# Rows read to build the proposal. Enough to answer the question rather than
# punt it: at 200 sampled values a MM/DD/YYYY column reads as ambiguous, because
# every day in the first 200 rows of a real export can be <= 12. At 1,000 it
# resolves, in 89ms. The whole proposal costs ~70ms on a 5-column file and
# ~200ms on a 19-column one.
PLAN_SAMPLE_ROWS = 2_000

# Preview rows returned alongside the proposal.
PREVIEW_ROWS = 8

# Rows scanned to settle a format the sample could not. A prefix sample is a
# poor witness for day-first versus month-first: the first 2,000 rows of a
# time-sorted export span a single day, so every day in them is <= 12 and both
# readings fit. Asking the file how often each reading actually fails settles
# it -- 109ms over 200,000 rows of the reported file, which found day-first
# failing 114,076 times. Bounded rather than whole-file so the cost does not
# grow with the data.
SETTLE_ROWS = 200_000


class PlanError(ValueError):
    """The plan cannot be carried out as written."""


_CSV_ERROR_LINE = re.compile(r"CSV Error on Line:\s*(\d+)")


def explain_read_error(exc: Exception) -> str | None:
    """A malformed-row failure, said in a sentence. None if it is not one.

    DuckDB's own message is accurate and unusable: the useful three lines are
    followed by a list of flag names to set (``strict_mode=false``,
    ``null_padding=true``, ``ignore_errors=true``) and then a dump of every
    setting the sniffer auto-detected. Someone reading that in a browser has
    been handed a fix they have no way to apply -- the flags are reader
    parameters, and nothing in the UI ever sent one.

    So the flags come out and the choice goes in. What is left is where the
    problem is, what it is, and the fact that there is now a control for it.
    """
    text = str(exc)
    match = _CSV_ERROR_LINE.search(text)
    if match is None:
        return None
    # Everything before DuckDB's flag suggestions: the offending text, then
    # what was wrong with it. The first line is the one the regex matched.
    detail = [ln.strip() for ln in
              text.split("Possible fixes:", 1)[0].splitlines()[1:] if ln.strip()]
    original = next((ln[len("Original Line:"):].strip() for ln in detail
                     if ln.startswith("Original Line:")), "")
    reason = next((ln for ln in detail if not ln.startswith("Original Line:")), "")

    parts = [f"Line {match.group(1)} does not match the columns the rest of the "
             f"file uses"]
    if reason:
        parts.append(f" ({reason.rstrip('.')})")
    parts.append(".")
    if original:
        shown = original if len(original) <= 120 else original[:117] + "..."
        parts.append(f' The line reads: "{shown}".')
    parts.append(" Choose what to do with rows like this -- keep them, or skip "
                 "them -- and read the file again.")
    return "".join(parts)


class ColumnPlan(BaseModel):
    """What to do with one column. The editable part of a proposal."""

    name: str
    # None means "leave it as the reader produced it".
    target_type: TargetType | None = None
    # How to read the text, when the target is temporal and the source is not.
    # A strptime format, or epoch:s / epoch:ms / epoch:us.
    format: str | None = None
    semantic_type: str | None = None
    role: ColumnRole | None = None
    # Set when a person changed this from what was proposed. Only those are
    # frozen against re-detection: accepting a proposal is not an override, and
    # marking every column as one would make "pinned" meaningless.
    pinned: bool = False


class ColumnProposal(BaseModel):
    """A proposed plan for one column, with the evidence behind it."""

    name: str
    source_type: str = Field(description="What the reader would produce, untouched")
    proposed: ColumnPlan
    sample_values: list[Any] = []
    # Why this is proposed, in a sentence, for the person deciding.
    rationale: str = ""
    # Temporal candidates, best first. Offered as choices when more than one.
    formats: list[FormatCandidate] = []
    # Share of sampled non-null values the proposed cast would keep, when it
    # casts at all. Reported rather than enforced: a stray 'n/a' should cost one
    # value, not the import.
    parse_rate: float | None = None
    # True when the data genuinely cannot settle the question and a person must.
    decision_required: bool = False
    conflict: str | None = None


class ImportPlan(BaseModel):
    reader: str
    sampled_rows: int
    columns: list[ColumnProposal]
    rows: list[list[Any]] = []

    @property
    def undecided(self) -> list[str]:
        return [c.name for c in self.columns if c.decision_required]


# --------------------------------------------------------------------------- #
# SQL
# --------------------------------------------------------------------------- #
def _epoch_expr(col: str, unit: str) -> str:
    divisor = {"s": "1", "ms": "1000.0", "us": "1000000.0"}[unit]
    return f"to_timestamp(CAST({col} AS BIGINT) / {divisor})"


def cast_expr(name: str, plan: ColumnPlan, source_type: str) -> str | None:
    """SQL for one column, or None when it should be passed through untouched."""
    if plan.target_type is None or plan.target_type == source_type.upper():
        return None
    col = quote_ident(name)

    if plan.target_type in ("TIMESTAMP", "DATE") and plan.format:
        if plan.format.startswith("epoch:"):
            expr = _epoch_expr(col, plan.format.split(":", 1)[1])
        else:
            literal = "'" + plan.format.replace("'", "''") + "'"
            expr = f"try_strptime(CAST({col} AS VARCHAR), {literal})"
        # try_strptime yields a TIMESTAMP; a DATE target needs the narrowing.
        return f"CAST({expr} AS DATE)" if plan.target_type == "DATE" else expr

    # try_cast rather than cast: a value that will not convert becomes NULL
    # instead of ending the import. Which values those were is measured and
    # reported, so the failure is loud without being fatal.
    return f"try_cast({col} AS {plan.target_type})"


def cast_projection(plans: dict[str, ColumnPlan], columns: list[tuple[str, str]]) -> str:
    """The SELECT list that materialises the version.

    Casting here rather than at read time is what keeps the import survivable.
    Asking DuckDB for `types={'date':'TIMESTAMP'}` aborts the whole read on the
    first unparseable row -- one 'n/a' in 850,000 -- and `ignore_errors` answers
    that by silently dropping rows, which was measured at 18 lost from 43. A
    projection over the already-read relation cannot drop anything.
    """
    parts: list[str] = []
    for name, source_type in columns:
        plan = plans.get(name)
        expr = cast_expr(name, plan, source_type) if plan else None
        parts.append(f"({expr}) AS {quote_ident(name)}" if expr else quote_ident(name))
    return ", ".join(parts) if parts else "*"


def text_columns(plans: dict[str, ColumnPlan], columns: list[tuple[str, str]]) -> list[str]:
    """Columns the reader must hand back as text for the plan to be applied.

    Given 03/04/2016 the sniffer returns a DATE and does not record that it
    chose between March and April. Once it has converted, the evidence is gone
    and the other reading is unreachable -- so a column whose reading is the
    user's to make has to arrive unconverted.
    """
    return [
        name for name, source_type in columns
        # A format only needs preserving when the reader would otherwise have
        # applied its own. A column that is already text arrives intact.
        if (plan := plans.get(name)) and plan.format and is_temporal(source_type)
    ]


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def validate_plan(plans: list[ColumnPlan], known: set[str]) -> None:
    """Reject a plan that cannot mean what it says.

    ``pin_column_type`` validates neither semantic types nor roles, so a plan
    arriving over HTTP is the first place either is checked.
    """
    seen: set[str] = set()
    for plan in plans:
        if plan.name not in known:
            raise PlanError(
                f"no column named {plan.name!r}; the file has: "
                f"{', '.join(sorted(known)[:12])}"
            )
        if plan.name in seen:
            raise PlanError(f"two plans for the same column: {plan.name!r}")
        seen.add(plan.name)

        if plan.semantic_type and SEMANTIC_TYPES.get(plan.semantic_type) is None:
            raise PlanError(
                f"{plan.name}: unknown semantic type {plan.semantic_type!r}. "
                "Define it first (POST /api/semantic-types) if it is a meaning "
                "of your own -- it needs a parent so that plugins written for "
                "the parent still accept the column."
            )
        if plan.role and plan.role == "time":
            # profile_columns copies a pinned role verbatim, so a pinned
            # role="time" on a text column recreates the VARCHAR - INTERVAL
            # failure this whole feature exists to prevent. Refused at the door.
            target = plan.target_type or ""
            if not is_temporal(target):
                as_what = target or "whatever the reader produces"
                raise PlanError(
                    f"{plan.name}: role 'time' needs a date or timestamp column, "
                    f"but it is being imported as {as_what}. Set the type to "
                    "TIMESTAMP, or pick another role."
                )
        if plan.format and plan.target_type not in ("TIMESTAMP", "DATE"):
            raise PlanError(
                f"{plan.name}: a format only applies when importing as a "
                f"TIMESTAMP or DATE, not {plan.target_type or 'the reader default'}"
            )


# --------------------------------------------------------------------------- #
# building the proposal
# --------------------------------------------------------------------------- #
def settle_formats(conn, raw_sql: str, column: str,
                   formats: list[FormatCandidate]) -> list[FormatCandidate]:
    """Re-rank candidate formats by how often each actually fails.

    Only called when the sample could not choose. Counting real failures over a
    wider slice is both cheaper and more honest than widening the sample and
    hoping: one pass, two aggregates, and an answer that comes from the data
    rather than from the first rows of it.
    """
    col = quote_ident(column)
    parts = []
    for i, f in enumerate(formats):
        if f.format.startswith("epoch:"):
            parts.append(f"0 AS f{i}")
            continue
        literal = "'" + f.format.replace("'", "''") + "'"
        parts.append(
            f"count(*) FILTER (WHERE {col} IS NOT NULL AND "
            f"try_strptime(CAST({col} AS VARCHAR), {literal}) IS NULL) AS f{i}")
    try:
        row = conn.execute(
            f"SELECT count({col}), {', '.join(parts)} "
            f"FROM (SELECT {col} FROM ({raw_sql}) LIMIT {SETTLE_ROWS})"
        ).fetchone()
    except Exception:  # noqa: BLE001 -- fall back to the sample's own verdict
        return formats

    considered = int(row[0] or 0)
    if not considered:
        return formats
    rescored = [
        f.model_copy(update={"success_rate": 1.0 - int(row[i + 1] or 0) / considered})
        for i, f in enumerate(formats)
    ]
    rescored.sort(key=lambda f: -f.success_rate)
    best = rescored[0].success_rate
    # A strict winner ends the argument; the conflict was an artefact of a
    # sample too narrow to contain a counter-example.
    if len(rescored) == 1 or rescored[1].success_rate < best:
        rescored = [rescored[0].model_copy(update={"conflict": None})] + rescored[1:]
    return rescored


def _temporal_target(formats: list[FormatCandidate]) -> str:
    """TIMESTAMP when the format carries a time, DATE when it is a day."""
    best = formats[0]
    if best.format.startswith("epoch:"):
        return "TIMESTAMP"
    return "TIMESTAMP" if any(c in best.format for c in ("%H", "%I")) else "DATE"


def _propose(profile: ColumnProfile, formats: list[FormatCandidate]) -> ColumnProposal:
    """Turn one profiled column into a proposal."""
    source_type = profile.physical_type
    proposal = ColumnProposal(
        name=profile.name,
        source_type=source_type,
        proposed=ColumnPlan(
            name=profile.name,
            semantic_type=profile.semantic_type,
            role=profile.role,
        ),
        sample_values=(profile.stats.sample_values[:6] if profile.stats else []),
    )

    temporal_meaning = SEMANTIC_TYPES.matches_any(profile.semantic_type, ("temporal",))

    # The column reads as dates but is not stored as one -- the reported case.
    if temporal_meaning and not is_temporal(source_type) and formats:
        proposal.formats = formats
        proposal.proposed.target_type = _temporal_target(formats)  # type: ignore[assignment]
        proposal.proposed.format = formats[0].format
        proposal.proposed.role = "time"
        proposal.parse_rate = formats[0].success_rate
        proposal.rationale = (
            f"holds dates as text ({formats[0].label}); importing it as "
            f"{proposal.proposed.target_type} makes it usable as a time axis"
        )

    # The reader already converted it -- but from text that read two ways, so
    # which one it chose was a guess nobody recorded.
    elif is_temporal(source_type) and formats and ambiguous(formats):
        proposal.formats = formats
        proposal.proposed.target_type = source_type.upper()  # type: ignore[assignment]
        proposal.proposed.format = formats[0].format
        proposal.rationale = (
            "the reader converted this itself, choosing between two readings "
            "that both fit"
        )

    elif is_temporal(source_type):
        proposal.rationale = "already a date or timestamp"
    elif profile.semantic_type:
        proposal.rationale = f"detected as {profile.semantic_type}"

    if formats and ambiguous(formats):
        proposal.decision_required = True
        proposal.conflict = formats[0].conflict
    return proposal


def build_plan(conn, reader_cls, uri: str, params: dict[str, Any]) -> ImportPlan:
    """Propose how every column should be imported, with the evidence.

    Runs the same profiler the import runs, over a sample, so the preview and
    the outcome cannot disagree.
    """
    from .profiler import compute_stats, profile_columns

    reader = reader_cls()
    rel = reader.to_relation(conn, uri, reader_cls.parse_params(params))
    columns = list(zip(rel.columns, [str(t) for t in rel.types], strict=True))
    if not columns:
        raise PlanError(f"{uri} has no columns")

    view = "_dq_plan_sample"
    conn.execute(f"CREATE OR REPLACE TEMP VIEW {view} AS "
                 f"SELECT * FROM ({rel.sql_query()}) LIMIT {PLAN_SAMPLE_ROWS}")
    sampled = conn.execute(f"SELECT count(*) FROM {view}").fetchone()[0]

    stats = compute_stats(conn, view, columns, PLAN_SAMPLE_ROWS)
    profiles = profile_columns(stats)
    raw_sql, raw = _raw_text_sample(conn, reader_cls, uri, params, columns)

    proposals = []
    for profile in profiles:
        formats = infer_formats(raw[profile.name]) if raw.get(profile.name) else []
        if raw_sql and ambiguous(formats):
            formats = settle_formats(conn, raw_sql, profile.name, formats)
        proposals.append(_propose(profile, formats))
    rows = [list(r) for r in
            conn.execute(f"SELECT * FROM {view} LIMIT {PREVIEW_ROWS}").fetchall()]
    conn.execute(f"DROP VIEW IF EXISTS {view}")

    return ImportPlan(reader=reader_cls.id, sampled_rows=int(sampled),
                      columns=proposals, rows=rows)


def _raw_text_sample(
    conn, reader_cls, uri: str, params: dict[str, Any],
    columns: list[tuple[str, str]],
) -> tuple[str | None, dict[str, list[Any]]]:
    """The columns as unconverted text, for format inference.

    Needed because a column the reader already turned into a DATE has lost the
    evidence of how it was written -- and that evidence is the only way to tell
    whether the reading was forced or guessed. Readers that cannot hand back
    text (parquet, JSON) get the empty dict, which honestly says the question
    was not asked.
    """
    if "all_varchar" not in reader_cls.Params.model_fields:
        return None, {}
    try:
        raw = reader_cls().to_relation(
            conn, uri, reader_cls.parse_params({**params, "all_varchar": True}))
        select = ", ".join(quote_ident(n) for n, _ in columns)
        rows = conn.execute(
            f"SELECT {select} FROM ({raw.sql_query()}) LIMIT {PLAN_SAMPLE_ROWS}"
        ).fetchall()
    except Exception:  # noqa: BLE001 -- a diagnostic must not break the preview
        return None, {}
    out: dict[str, list[Any]] = {}
    for i, (name, _) in enumerate(columns):
        seen: list[Any] = []
        distinct: set[Any] = set()
        for row in rows:
            v = row[i]
            if v is None or v in distinct:
                continue
            distinct.add(v)
            seen.append(v)
        out[name] = seen
    return raw.sql_query(), out
