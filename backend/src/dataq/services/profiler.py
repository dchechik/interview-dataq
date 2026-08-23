"""Profiling: compute column statistics, then resolve semantic types via detectors."""

from __future__ import annotations

from typing import Any

from ..core.profile import ColumnProfile, ColumnStats, SemanticGuess
from ..core.semantic import SEMANTIC_TYPES
from ..plugins.base import REGISTRY
from ..plugins.kinds import Detector
from ..query.compiler import quote_ident

# Below this, a guess is recorded as a candidate but not applied.
CONFIDENCE_THRESHOLD = 0.5
MAX_SAMPLE_VALUES = 30
MAX_TOP_VALUES = 20


def _jsonable(value: Any) -> Any:
    """Stats are persisted into a JSON column, so values must be JSON-safe.

    Temporals and decimals become strings; detectors already compare on ``str(v)``,
    so this loses nothing they rely on.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def compute_stats(
    conn, source_sql: str, columns: list[tuple[str, str]], sample_rows: int
) -> list[ColumnStats]:
    """One aggregate pass over a bounded sample, plus a top-values pass per column.

    Sampling keeps profiling ~constant-time regardless of whether the dataset is
    10MB or 10GB.
    """
    conn.execute(
        f"CREATE OR REPLACE TEMP VIEW _dq_sample AS "
        f"SELECT * FROM {source_sql} USING SAMPLE reservoir({int(sample_rows)} ROWS)"
    )
    total = conn.execute(f"SELECT count(*) FROM {source_sql}").fetchone()[0]
    n = conn.execute("SELECT count(*) FROM _dq_sample").fetchone()[0]

    if n == 0:
        return [ColumnStats(name=c, physical_type=t, row_count=0) for c, t in columns]

    # Aggregate every column in a single scan of the sample.
    parts: list[str] = []
    for name, _ in columns:
        q = quote_ident(name)
        parts += [
            f"count({q})",
            f"approx_count_distinct({q})",
            f"CAST(min({q}) AS VARCHAR)",
            f"CAST(max({q}) AS VARCHAR)",
        ]
    row = conn.execute(f"SELECT {', '.join(parts)} FROM _dq_sample").fetchone()

    out: list[ColumnStats] = []
    for i, (name, ptype) in enumerate(columns):
        non_null, ndv, cmin, cmax = row[i * 4 : i * 4 + 4]
        q = quote_ident(name)
        samples = [
            _jsonable(r[0])
            for r in conn.execute(
                f"SELECT DISTINCT {q} FROM _dq_sample WHERE {q} IS NOT NULL "
                f"LIMIT {MAX_SAMPLE_VALUES}"
            ).fetchall()
        ]
        top: list[tuple[Any, int]] = []
        if 0 < (ndv or 0) <= 200:
            top = [
                (_jsonable(r[0]), int(r[1]))
                for r in conn.execute(
                    f"SELECT {q}, count(*) c FROM _dq_sample WHERE {q} IS NOT NULL "
                    f"GROUP BY 1 ORDER BY c DESC LIMIT {MAX_TOP_VALUES}"
                ).fetchall()
            ]
        out.append(
            ColumnStats(
                name=name,
                physical_type=ptype,
                # Report against the sample so distinct_frac is meaningful, but
                # carry the true total separately on the profile.
                row_count=int(n),
                null_count=int(n) - int(non_null or 0),
                # approx_count_distinct is HyperLogLog and can overshoot the true
                # count; clamp so distinct_frac never exceeds 1.
                distinct_count=min(int(ndv or 0), int(n)),
                min=cmin,
                max=cmax,
                sample_values=samples,
                top_values=top,
            )
        )
    conn.execute("DROP VIEW IF EXISTS _dq_sample")
    _ = total
    return out


def run_detectors(stats: ColumnStats) -> list[SemanticGuess]:
    guesses: list[SemanticGuess] = []
    for cls in REGISTRY.list(kind="detector"):
        detector: Detector = cls()  # type: ignore[assignment]
        for g in detector.detect(stats):
            guesses.append(g.model_copy(update={"detector_id": cls.id}))
    return sorted(guesses, key=lambda g: g.confidence, reverse=True)


def profile_columns(
    stats: list[ColumnStats], previous: list[ColumnProfile] | None = None
) -> list[ColumnProfile]:
    """Resolve each column to a semantic type, preserving human-pinned edits."""
    pinned = {p.name: p for p in (previous or []) if p.pinned}
    out: list[ColumnProfile] = []
    for st in stats:
        guesses = run_detectors(st)
        prof = ColumnProfile(
            name=st.name, physical_type=st.physical_type, stats=st, candidates=guesses
        )
        if st.name in pinned:
            # A human said what this column is; never overwrite that.
            prev = pinned[st.name]
            prof.semantic_type = prev.semantic_type
            prof.confidence = 1.0
            prof.role = prev.role
            prof.pinned = True
        elif guesses and guesses[0].confidence >= CONFIDENCE_THRESHOLD:
            best = guesses[0]
            prof.semantic_type = best.semantic_type
            prof.confidence = best.confidence
            st_def = SEMANTIC_TYPES.get(best.semantic_type)
            prof.role = st_def.role if st_def else "dimension"
        else:
            prof.role = _fallback_role(st)
        out.append(prof)
    return out


def _fallback_role(st: ColumnStats) -> str:
    pt = st.physical_type.upper()
    if pt.startswith(("TIMESTAMP", "DATE", "TIME")):
        return "time"
    if pt.startswith(("BIGINT", "INTEGER", "DOUBLE", "FLOAT", "DECIMAL", "HUGEINT",
                      "SMALLINT", "TINYINT", "REAL")):
        return "measure"
    return "dimension"
