"""Answering "what would this join do?" before it is run.

The join op is honest about two failure modes but only after the fact: it writes
the whole result, counts the rows, and drops it again if the count grew. That is
the right guard to keep -- it is the last one before a wrong dataset exists --
but it is a poor way to find out you picked the wrong key, because the answer
costs a full pass over the data and arrives as a failed job.

Everything the guard checks can be checked first, and cheaply:

* whether the right side is unique on the key, which is a ``GROUP BY`` over one
  column set and is what decides annotation from multiplication;
* how much of the left side actually matches, over a bounded sample, because a
  left join answers "no match" with NULL and a key that matches nothing still
  reports success;
* whether the names coming across collide with names already there, which is
  pure metadata.

So this module is what the join form asks as it is being filled in. It shares its
two data-touching checks with the transform-side join (``jobs.executor``) rather
than restating them, because two copies of a rule about correctness is how the
second one comes to disagree with the first.

``candidates`` is the other half: which datasets are worth joining to at all,
answered from the semantic layer alone. Same rule as ``JoinSuggester`` -- two
datasets are joinable when they share a *meaning* -- but it returns every pair
rather than a ranked handful, because these are a form's options and not advice.
"""

from __future__ import annotations

from pydantic import BaseModel, ValidationError

from ..core.profile import DatasetProfile
from ..core.semantic import SEMANTIC_TYPES
from ..jobs.executor import duplicate_key_rows, match_rate, name_collisions
from ..query.compiler import quote_ident
from .context import AppContext
from .operations import JoinParams

# A key that matches less of the left side than this is probably the wrong key,
# though it is not necessarily -- an annotation table covering only the countries
# you care about is a real thing. Reported, never refused.
LOW_MATCH = 0.5


class JoinKeyCandidate(BaseModel):
    """A column pair that could be part of a key, and why it could."""

    left: str
    right: str
    semantic_type: str = ""
    reason: str = ""


class JoinCandidate(BaseModel):
    """A dataset worth joining to, with the pairs that make it joinable."""

    dataset_id: str
    name: str
    kind: str = ""
    row_count: int = 0
    keys: list[JoinKeyCandidate] = []


class JoinPreview(BaseModel):
    """What the join would do, measured rather than predicted.

    ``result_rows`` is exact when the right side is unique on the key -- a left
    join onto unique keys returns the left row count, which is the whole point --
    and an estimate otherwise, which is when it is worth looking at.
    """

    left_rows: int = 0
    right_rows: int = 0
    sampled: int = 0
    matched: int = 0
    duplicate_keys: int = 0
    result_rows: int = 0
    exact: bool = True
    columns_added: list[str] = []
    collisions: list[str] = []
    fanout: bool = False
    notes: list[str] = []

    @property
    def match_fraction(self) -> float:
        return self.matched / self.sampled if self.sampled else 0.0


def _pairs(left: DatasetProfile, right: DatasetProfile) -> list[JoinKeyCandidate]:
    """Column pairs that mean the same thing on both sides.

    Asked of the semantic layer, not of the names: ``pc`` and ``device`` are the
    same key when both are pinned to the same meaning, and two columns both
    called ``id`` are usually not.
    """
    out: list[JoinKeyCandidate] = []
    for lc in left.columns:
        if not SEMANTIC_TYPES.joinable_with(lc.semantic_type):
            continue
        for rc in right.columns:
            if rc.semantic_type != lc.semantic_type:
                continue
            same_name = lc.name == rc.name
            out.append(JoinKeyCandidate(
                left=lc.name, right=rc.name, semantic_type=lc.semantic_type or "",
                reason=(f"both are {lc.semantic_type}"
                        + (" and share a name" if same_name else "")),
            ))
    # A pair that agrees on the name as well is the one to offer first: the
    # meaning says the join is possible, the name says it was intended.
    out.sort(key=lambda k: (k.left != k.right, k.left, k.right))
    return out


def candidates(ctx: AppContext, dataset_id: str) -> list[JoinCandidate]:
    """Every other dataset this one could be joined to, best first.

    Profile-only -- no data is read. The ordering puts small right sides first,
    because a join onto a small table is an annotation, which is the kind that
    preserves cardinality and is almost always what was wanted.
    """
    left = ctx.catalog.get_profile(dataset_id)
    if left is None:
        raise KeyError(f"dataset {dataset_id} has no profile")

    out: list[JoinCandidate] = []
    for ds in ctx.catalog.list_datasets():
        if ds.id == dataset_id:
            continue
        right = ctx.catalog.get_profile(ds.id)
        if right is None:
            continue
        keys = _pairs(left, right)
        if not keys:
            continue
        out.append(JoinCandidate(dataset_id=ds.id, name=ds.name, kind=ds.kind,
                                 row_count=right.row_count, keys=keys))

    out.sort(key=lambda c: (c.row_count or 0, c.name))
    return out


def preview(ctx: AppContext, left_id: str, right_id: str, params: dict,
            left_version: int | None = None,
            right_version: int | None = None) -> JoinPreview:
    """Measure the join the given params describe, without writing anything.

    Takes the operation's own ``params`` rather than a second shape of its own,
    so what is previewed is what would run -- including the defaults, which is
    where the surprises are: ``right_select`` empty means every non-key column
    comes across, and that is the set the collision check has to be run against.
    """
    try:
        p = JoinParams.model_validate(params)
    except ValidationError as exc:
        # This one goes straight into a form, where pydantic's full report --
        # the model name, the input, a link to the docs -- is noise around the
        # one sentence that says what to do.
        raise ValueError("; ".join(e["msg"].removeprefix("Value error, ")
                                   for e in exc.errors())) from exc
    lsrc = ctx.resolve_source(left_id, left_version)
    rsrc = ctx.resolve_source(right_id, right_version)

    for k in p.on:
        if k.left not in lsrc.columns:
            raise ValueError(f"left column {k.left!r} not found")
        if k.right not in rsrc.columns:
            raise ValueError(f"right column {k.right!r} not found")

    key_columns = {k.right for k in p.on}
    right_cols = p.right_select or [c for c in rsrc.columns if c not in key_columns]
    missing = [c for c in right_cols if c not in rsrc.columns]
    if missing:
        raise ValueError(f"right columns not found: {missing}")

    prefix = p.prefix or ""
    added = [prefix + c for c in right_cols]
    collisions = name_collisions(list(lsrc.columns), added)

    out = JoinPreview(columns_added=added, collisions=collisions)
    if collisions:
        out.notes.append(
            f"{', '.join(collisions)} already exist here, so the result would "
            "have two columns of each name. Set a prefix, or choose which "
            "columns to bring across."
        )
    if not right_cols:
        out.notes.append("the key is the only column on the right, so the join "
                         "would add nothing")

    with ctx.warehouse.cur() as conn:
        out.left_rows = conn.execute(f"SELECT count(*) FROM {lsrc.sql}").fetchone()[0]
        out.right_rows = conn.execute(f"SELECT count(*) FROM {rsrc.sql}").fetchone()[0]
        out.duplicate_keys = duplicate_key_rows(conn, rsrc.sql, [k.right for k in p.on])

        on = " AND ".join(
            f"l.{quote_ident(k.left)} = r.{quote_ident(k.right)}" for k in p.on
        )
        # Probe on the key rather than on a brought-across column: the key is
        # never NULL on the right side of a match, whereas a data column may be,
        # which would read as "did not match" and understate the rate.
        probe = "__dataq_matched"
        joined = (
            f"(SELECT r.{quote_ident(p.on[0].right)} AS {quote_ident(probe)} "
            f"FROM {lsrc.sql} l LEFT JOIN {rsrc.sql} r ON {on})"
        )
        out.matched, out.sampled = match_rate(conn, joined, probe)

    out.fanout = out.duplicate_keys > 0
    if out.fanout:
        # Only the left row count is known for certain; the result is larger by
        # however many duplicates the matched rows happen to hit, which is not
        # a number a sample can be trusted for.
        out.exact = False
        out.result_rows = out.left_rows
        out.notes.append(
            f"the right side has {out.duplicate_keys:,} repeated values of "
            f"({', '.join(k.right for k in p.on)}), so each matching row would "
            "match several and the join would multiply rows rather than annotate "
            "them. Add a column to the key, or allow it deliberately."
        )
    elif p.how == "inner":
        out.result_rows = round(out.left_rows * out.match_fraction)
        out.exact = out.sampled >= out.left_rows
        if not out.exact:
            out.notes.append(
                f"an inner join drops unmatched rows, so the row count is "
                f"estimated from the first {out.sampled:,} sampled"
            )
    else:
        out.result_rows = out.left_rows

    if out.sampled and out.matched == 0:
        out.notes.append(
            f"none of {out.sampled:,} sampled rows found a match, so every "
            "attached column would be empty. Check the key."
        )
    elif out.sampled and out.match_fraction < LOW_MATCH:
        out.notes.append(
            f"only {out.matched:,} of {out.sampled:,} sampled rows match "
            f"({out.match_fraction:.0%}), so most rows would come back empty"
        )
    return out
