"""Date-format inference, and the parse checks that stop a bad guess silently.

Two things are being protected here. One is that DuckDB answers every parse
failure with NULL, so a transform with the wrong format succeeds and writes a
column of nothing; the checks make that loud. The other is that some date
columns cannot be read correctly without asking a human, and the system has to
know which those are rather than picking for them.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta

import pytest

from dataq.core.datefmt import _FORMATS, ambiguous, infer_epoch, infer_formats
from dataq.services.operations import OperationRequest, submit_operation


def write_dates(path, values, column="when"):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([column, "n"])
        for i, v in enumerate(values):
            w.writerow([v, i])
    return path


def dates(fmt: str, n: int = 40, start_day: int = 1):
    """n consecutive days rendered in `fmt`."""
    base = datetime(2016, 3, start_day, 14, 5, 6)
    return [(base + timedelta(days=i)).strftime(fmt) for i in range(n)]


# Slash dates where both fields stay <= 12, so no value can settle day-first
# against month-first. The time is not decoration: DuckDB's CSV sniffer turns a
# bare 03/04/2016 into a DATE -- silently choosing a reading -- while the same
# value with a time stays VARCHAR and reaches our own detection.
_AMBIGUOUS_PAIRS = ((3, 4), (5, 6), (7, 8), (1, 2), (9, 10), (11, 12))
# 12-hour, because DuckDB's sniffer converts the 24-hour form to TIMESTAMP on
# its own (choosing a reading as it goes -- see the sniffer tests below), while
# this shape stays VARCHAR and reaches DataQ's own detection.
AMBIGUOUS = [f"{m:02d}/{d:02d}/2016 02:05 PM" for m, d in _AMBIGUOUS_PAIRS]
# The same dates in the shape DuckDB will silently convert.
AMBIGUOUS_SNIFFED = [f"{m:02d}/{d:02d}/2016 14:05:06" for m, d in _AMBIGUOUS_PAIRS]


def count_nulls(app_ctx, dataset_id: str, column: str) -> int:
    src = app_ctx.resolve_source(dataset_id).sql
    with app_ctx.warehouse.cur() as conn:
        return conn.execute(
            f'SELECT count(*) FROM {src} WHERE "{column}" IS NULL'
        ).fetchone()[0]


def run_failing(app_ctx, **kwargs):
    """Submit an operation expected to fail; return the job."""
    accepted = submit_operation(app_ctx, OperationRequest(**kwargs))
    app_ctx.runner.wait(accepted.job_id, timeout=120)
    return app_ctx.catalog.get_job(accepted.job_id)


# --------------------------------------------------------------------------- #
# the assumption everything else rests on
# --------------------------------------------------------------------------- #
def test_every_format_means_the_same_to_python_and_duckdb():
    """Inference runs in Python; parsing runs in DuckDB.

    If the two disagreed about any format, DataQ would confidently recommend a
    format that then parses nothing -- the exact failure this work exists to
    prevent. So the format library is checked against a live DuckDB.
    """
    import duckdb

    conn = duckdb.connect()
    probe = datetime(2016, 3, 4, 14, 5, 6)
    for fmt, label, _ in _FORMATS:
        rendered = probe.strftime(fmt)
        py = datetime.strptime(rendered, fmt)
        duck = conn.execute("SELECT try_strptime(?, ?)", [rendered, fmt]).fetchone()[0]
        assert duck is not None, f"{label}: DuckDB cannot parse {rendered!r} as {fmt}"
        assert duck.replace(tzinfo=None) == py, f"{label}: {duck} != {py}"


# --------------------------------------------------------------------------- #
# inference
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fmt", [
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d.%m.%Y",
    "%b %d, %Y", "%d %b %Y", "%Y%m%d", "%m/%d/%Y %I:%M %p",
])
def test_the_format_that_wrote_the_values_is_the_one_inferred(fmt):
    best = infer_formats(dates(fmt))[0]
    assert best.format == fmt
    assert best.success_rate == 1.0


def test_a_column_of_words_infers_nothing():
    assert infer_formats(["red", "green", "blue"]) == []


def test_a_mostly_unparseable_column_infers_nothing():
    """A format explaining half a column is a coincidence, not a description."""
    values = dates("%Y-%m-%d", 10) + ["not a date"] * 10
    assert infer_formats(values) == []


def test_the_most_specific_format_wins_a_tie():
    """A column carrying times should not be read as bare dates."""
    best = infer_formats(dates("%Y-%m-%d %H:%M:%S"))[0]
    assert best.format == "%Y-%m-%d %H:%M:%S"


# --------------------------------------------------------------------------- #
# ambiguity -- the part that has to ask
# --------------------------------------------------------------------------- #
def test_day_first_and_month_first_are_ambiguous_when_nothing_separates_them():
    """Every day of the sample is <= 12, so both readings parse everything."""
    found = infer_formats(AMBIGUOUS)
    assert ambiguous(found)
    assert "reads as" in found[0].conflict


def test_a_day_past_the_twelfth_settles_it_by_itself():
    """No confirmation needed: only one reading parses 25/03/2016."""
    found = infer_formats(dates("%d/%m/%Y", n=40))
    assert not ambiguous(found)
    assert found[0].format == "%d/%m/%Y"


def test_the_conflict_names_a_value_and_both_readings():
    found = infer_formats(AMBIGUOUS)
    assert ambiguous(found)
    conflict = found[0].conflict
    assert "03/04/2016" in conflict
    assert "3 Apr 2016" in conflict and "4 Mar 2016" in conflict


def test_month_names_are_never_ambiguous():
    """The name fixes which field is which, so there is nothing to ask."""
    assert not ambiguous(infer_formats(dates("%b %d, %Y", start_day=2)))


# --------------------------------------------------------------------------- #
# epoch
# --------------------------------------------------------------------------- #
def test_epoch_ranges():
    assert infer_epoch(1_456_000_000, 1_457_000_000) == ("s", "epoch seconds")
    assert infer_epoch(1_456_000_000_000, 1_457_000_000_000)[0] == "ms"
    # A counter that happens to be a big number is not a timestamp.
    assert infer_epoch(1, 500) is None
    assert infer_epoch(None, None) is None


# --------------------------------------------------------------------------- #
# detection, end to end through import
# --------------------------------------------------------------------------- #
def test_a_non_iso_date_column_is_detected_as_temporal(app_ctx, run_op, tmp_path):
    """The case that motivated this: DuckDB types it VARCHAR and gives up."""
    ds = run_op(op="import", name="d",
                uri=str(write_dates(tmp_path / "d.csv", dates("%m/%d/%Y %H:%M:%S"))))
    col = app_ctx.catalog.get_profile(ds).column("when")
    assert col.semantic_type == "time.timestamp"
    assert col.candidates[0].formats[0].format == "%m/%d/%Y %H:%M:%S"


def test_an_epoch_column_is_detected(app_ctx, run_op, tmp_path):
    base = 1_456_000_000
    ds = run_op(op="import", name="e", uri=str(write_dates(
        tmp_path / "e.csv", [base + i * 3600 for i in range(40)], column="event_time")))
    col = app_ctx.catalog.get_profile(ds).column("event_time")
    assert col.semantic_type == "time.timestamp"
    assert col.candidates[0].formats[0].format == "epoch:s"


def test_a_plain_number_named_like_a_time_is_not_a_timestamp(app_ctx, run_op, tmp_path):
    ds = run_op(op="import", name="c", uri=str(write_dates(
        tmp_path / "c.csv", list(range(40)), column="response_time")))
    assert app_ctx.catalog.get_profile(ds).column("response_time").semantic_type \
        != "time.timestamp"


# --------------------------------------------------------------------------- #
# parsing: the format is taken from detection, not from the user
# --------------------------------------------------------------------------- #
def test_parsing_needs_no_format_when_detection_found_one(app_ctx, run_op, tmp_path):
    ds = run_op(op="import", name="d",
                uri=str(write_dates(tmp_path / "d.csv", dates("%d.%m.%Y"))))
    run_op(op="transform", plugin_id="normalize.timestamp",
           inputs=[{"dataset_id": ds}], params={"column": "when"})

    prof = app_ctx.catalog.get_profile(ds)
    assert prof.column("when_ts").physical_type.upper().startswith("TIMESTAMP")
    assert count_nulls(app_ctx, ds, "when_ts") == 0, "every row should have parsed"


def test_an_ambiguous_column_refuses_to_be_guessed_at(app_ctx, run_op, tmp_path):
    """Guessing would silently shift dates by up to twelve days.

    Both readings produce valid dates, so nothing downstream could ever notice.
    That is precisely why this has to stop and ask.
    """
    values = AMBIGUOUS * 5
    ds = run_op(op="import", name="amb",
                uri=str(write_dates(tmp_path / "amb.csv", values)))
    job = run_failing(app_ctx, op="transform", plugin_id="normalize.timestamp",
                      inputs=[{"dataset_id": ds}], params={"column": "when"})

    assert job.status == "failed"
    assert "ambiguous" in job.error
    # The error has to be actionable: it names both formats to choose between.
    assert "%m/%d/%Y %I:%M %p" in job.error and "%d/%m/%Y %I:%M %p" in job.error


def test_naming_the_format_resolves_the_ambiguity(app_ctx, run_op, tmp_path):
    values = AMBIGUOUS * 5
    ds = run_op(op="import", name="amb",
                uri=str(write_dates(tmp_path / "amb.csv", values)))
    run_op(op="transform", plugin_id="normalize.timestamp",
           inputs=[{"dataset_id": ds}],
           params={"column": "when", "format": "%d/%m/%Y %I:%M %p"})
    assert app_ctx.catalog.get_profile(ds).column("when_ts") is not None


# --------------------------------------------------------------------------- #
# the check itself
# --------------------------------------------------------------------------- #
def test_a_wrong_explicit_format_fails_loudly(app_ctx, run_op, tmp_path):
    """The original bug: this used to succeed and write a column of NULLs."""
    ds = run_op(op="import", name="d",
                uri=str(write_dates(tmp_path / "d.csv", dates("%Y-%m-%d %H:%M:%S"))))
    job = run_failing(app_ctx, op="transform", plugin_id="normalize.timestamp",
                      inputs=[{"dataset_id": ds}],
                      params={"column": "when", "format": "%d/%m/%Y"})

    assert job.status == "failed"
    assert "0 of" in job.error or "0.0%" in job.error
    # It says what did not parse, and what would have.
    assert "2016-03-01" in job.error
    # The column is already a TIMESTAMP, so the hint says that rather than
    # sending the reader off to hunt for a text format that does not exist.
    assert "already temporal" in job.error


def test_the_failed_transform_leaves_no_column_behind(app_ctx, run_op, tmp_path):
    """Failing after the write would leave a NULL column on the dataset."""
    ds = run_op(op="import", name="d",
                uri=str(write_dates(tmp_path / "d.csv", dates("%Y-%m-%d"))))
    run_failing(app_ctx, op="transform", plugin_id="normalize.timestamp",
                inputs=[{"dataset_id": ds}],
                params={"column": "when", "format": "%d/%m/%Y"})
    assert app_ctx.catalog.get_profile(ds).column("when_ts") is None


def test_parsing_words_as_numbers_fails_loudly(app_ctx, run_op, tmp_path):
    ds = run_op(op="import", name="w", uri=str(write_dates(
        tmp_path / "w.csv", ["alpha", "beta", "gamma"] * 10, column="label")))
    job = run_failing(app_ctx, op="transform", plugin_id="normalize.numeric",
                      inputs=[{"dataset_id": ds}], params={"column": "label"})
    assert job.status == "failed"
    assert "not a number" in job.error


def test_a_partially_messy_column_still_parses(app_ctx, run_op, tmp_path):
    """The check catches wrong formats, not imperfect data.

    Real columns have a few bad rows; failing the whole import for them would be
    worse than the silence this replaces.
    """
    values = dates("%Y-%m-%d", 36) + ["", "n/a", "unknown", "-"]
    ds = run_op(op="import", name="m",
                uri=str(write_dates(tmp_path / "m.csv", values)))
    run_op(op="transform", plugin_id="normalize.timestamp",
           inputs=[{"dataset_id": ds}],
           params={"column": "when", "format": "%Y-%m-%d"})
    assert app_ctx.catalog.get_profile(ds).column("when_ts") is not None


def test_an_all_null_column_is_not_blamed_on_the_transform(app_ctx, run_op, tmp_path):
    """Nothing to parse is the source's problem, not this transform's."""
    ds = run_op(op="import", name="n", uri=str(write_dates(
        tmp_path / "n.csv", [""] * 30)), params={"all_varchar": True})
    run_op(op="transform", plugin_id="normalize.timestamp",
           inputs=[{"dataset_id": ds}],
           params={"column": "when", "format": "%Y-%m-%d"})
    assert app_ctx.catalog.get_profile(ds).column("when_ts") is not None


# --------------------------------------------------------------------------- #
# what the CSV sniffer decided, and did not tell you
# --------------------------------------------------------------------------- #
def test_the_sniffers_silent_choice_is_reported(app_ctx, run_op, tmp_path):
    """The most dangerous case, because nothing downstream can detect it.

    DuckDB types 03/04/2016 14:05:06 as a TIMESTAMP and picks day-first or
    month-first from the sample, without recording which. Both readings are
    valid dates, so a wrong one is off by up to twelve days and looks perfectly
    healthy forever after. By profiling time the text is gone -- so the check
    happens at import, against the raw file.
    """
    ds = run_op(op="import", name="s",
                uri=str(write_dates(tmp_path / "s.csv", AMBIGUOUS_SNIFFED * 5)))
    col = app_ctx.catalog.get_profile(ds).column("when")

    assert col.physical_type.upper().startswith("TIMESTAMP"), \
        "precondition: DuckDB converted it without being asked"
    assert col.warning, "the silent choice must not stay silent"
    assert "Ambiguous date format" in col.warning
    # Actionable: names the rival reading and the parameter that pins it.
    assert "timestampformat=" in col.warning
    assert "%m/%d/%Y" in col.warning or "%d/%m/%Y" in col.warning


def test_the_warning_says_which_reading_was_taken(app_ctx, run_op, tmp_path):
    """'It might be wrong' is not useful; 'it read April, not March' is."""
    ds = run_op(op="import", name="s",
                uri=str(write_dates(tmp_path / "s.csv", AMBIGUOUS_SNIFFED * 5)))
    warning = app_ctx.catalog.get_profile(ds).column("when").warning
    assert "read as" in warning

    src = app_ctx.resolve_source(ds).sql
    with app_ctx.warehouse.cur() as conn:
        first = conn.execute(
            f'SELECT CAST("when" AS VARCHAR) FROM {src} ORDER BY n LIMIT 1'
        ).fetchone()[0]
    # Whichever it chose, the warning must describe that choice and not the other.
    took_day_first = first.startswith("2016-04-03")
    assert ("DD/MM" in warning) == took_day_first


def test_pinning_the_format_at_import_changes_the_reading(app_ctx, run_op, tmp_path):
    """The fix the warning recommends has to actually work."""
    path = write_dates(tmp_path / "s.csv", AMBIGUOUS_SNIFFED * 5)
    readings = {}
    for fmt in ("%m/%d/%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
        ds = run_op(op="import", name=f"pinned {fmt}", uri=str(path),
                    params={"timestampformat": fmt})
        src = app_ctx.resolve_source(ds).sql
        with app_ctx.warehouse.cur() as conn:
            readings[fmt] = conn.execute(
                f'SELECT CAST("when" AS VARCHAR) FROM {src} ORDER BY n LIMIT 1'
            ).fetchone()[0]

    assert readings["%m/%d/%Y %H:%M:%S"].startswith("2016-03-04")
    assert readings["%d/%m/%Y %H:%M:%S"].startswith("2016-04-03")


def test_an_unambiguous_date_column_raises_no_warning(app_ctx, run_op, tmp_path):
    """A warning on every date column would be noise, and quickly ignored."""
    ds = run_op(op="import", name="u",
                uri=str(write_dates(tmp_path / "u.csv", dates("%Y-%m-%d %H:%M:%S"))))
    assert app_ctx.catalog.get_profile(ds).column("when").warning is None


def test_dates_past_the_twelfth_need_no_warning(app_ctx, run_op, tmp_path):
    """The data settles it, so the sniffer had no choice to make."""
    ds = run_op(op="import", name="u",
                uri=str(write_dates(tmp_path / "u.csv", dates("%d/%m/%Y %H:%M:%S", n=40))))
    assert app_ctx.catalog.get_profile(ds).column("when").warning is None


def test_an_existing_catalog_gains_new_columns(tmp_path):
    """Adding a field to a model must not break databases that already exist.

    create_all() creates missing tables but never missing columns, so a new
    field works against every fresh test database and fails on the first real
    one. This reproduces that: build a catalog without the column, then open it
    with the current models.
    """
    from sqlalchemy import inspect, text

    from dataq.catalog.repo import make_engine
    from dataq.config import Settings

    settings = Settings(data_dir=tmp_path / "data", browse_roots=str(tmp_path))
    engine = make_engine(settings)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE columns DROP COLUMN warning"))
    assert "warning" not in {c["name"] for c in inspect(engine).get_columns("columns")}
    engine.dispose()

    engine = make_engine(settings)
    assert "warning" in {c["name"] for c in inspect(engine).get_columns("columns")}
    engine.dispose()


def test_a_warning_survives_later_transforms(app_ctx, run_op, tmp_path):
    """The raw text is gone after import, so the warning cannot be recomputed.

    A transform re-profiles the dataset, and without carrying warnings across,
    the first one silently clears them -- which is worse than never having
    warned, because the column looks checked.
    """
    ds = run_op(op="import", name="s",
                uri=str(write_dates(tmp_path / "s.csv", AMBIGUOUS_SNIFFED * 5)))
    assert app_ctx.catalog.get_profile(ds).column("when").warning

    run_op(op="transform", plugin_id="normalize.numeric",
           inputs=[{"dataset_id": ds}], params={"column": "n"})
    assert app_ctx.catalog.get_profile(ds).column("when").warning, \
        "the transform cleared a warning it knows nothing about"
