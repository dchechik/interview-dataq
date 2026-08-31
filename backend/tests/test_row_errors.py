"""Rows a CSV reader cannot parse, and what the user is allowed to do about them.

DuckDB's default is to stop on the first bad row, which is right: a file that
will not parse is usually a file being read with the wrong settings, and quietly
dropping part of it hides that. What it is not is the *only* reasonable answer,
and the error saying so named three parameters (``strict_mode``,
``null_padding``, ``ignore_errors``) that nothing in the app could send.

So there are three things to pin down here. That each mode does what its label
claims -- and they differ, which is the whole reason there are three. That
skipping rows is counted rather than silent, because a dataset that looks
complete and is not is worse than an import that failed. And that the error a
person actually sees is the one sentence they can act on.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dataq.api.app import create_app
from dataq.plugins.builtin.readers import CsvParams, CsvReader, rejected_rows
from dataq.services.import_plan import explain_read_error
from dataq.services.operations import OperationRequest, submit_operation

# Past the 20,480-row sniffer sample on purpose. Inside it, DuckDB simply widens
# its guess and nothing fails -- the error only exists once the schema is
# settled, which is also why this is the shape that surprises people.
GOOD_ROWS = 21_000


@pytest.fixture
def extra_column(tmp_path):
    """A file whose last row has one value too many."""
    path = tmp_path / "extra.csv"
    path.write_text(
        "id,name\n"
        + "".join(f"{i},n{i}\n" for i in range(GOOD_ROWS))
        + "99999,bob,EXTRA\n"
    )
    return path


@pytest.fixture
def missing_column(tmp_path):
    """A file whose last row has one value too few."""
    path = tmp_path / "missing.csv"
    path.write_text(
        "id,name,amount\n"
        + "".join(f"{i},n{i},{i}\n" for i in range(GOOD_ROWS))
        + "99999,bob\n"
    )
    return path


KEEP = {"strict_mode": False, "null_padding": True}
SKIP = {"ignore_errors": True}


def read(conn, path, **params):
    return CsvReader().to_relation(conn, str(path), CsvParams(**params)).fetchall()


def run_import(app_ctx, path, **params):
    """Submit an import and block; return the job."""
    accepted = submit_operation(app_ctx, OperationRequest(
        op="import", uri=str(path), name="rows", params=params))
    app_ctx.runner.wait(accepted.job_id, timeout=120)
    return app_ctx.catalog.get_job(accepted.job_id)


# --------------------------------------------------------------------------- #
# the three modes
# --------------------------------------------------------------------------- #
def test_by_default_a_bad_row_stops_the_read(warehouse, extra_column):
    with warehouse.cur() as conn, pytest.raises(Exception, match="CSV Error on Line"):
        read(conn, extra_column)


def test_keep_admits_a_row_with_extra_values(warehouse, extra_column):
    with warehouse.cur() as conn:
        rows = read(conn, extra_column, **KEEP)
    assert len(rows) == GOOD_ROWS + 1, "nothing was dropped"
    assert rows[-1] == (99999, "bob"), "the value with no column is not kept"


def test_keep_admits_a_row_with_missing_values(warehouse, missing_column):
    with warehouse.cur() as conn:
        rows = read(conn, missing_column, **KEEP)
    assert len(rows) == GOOD_ROWS + 1
    assert rows[-1] == (99999, "bob", None), "the absent value becomes empty"


@pytest.mark.parametrize("flags", [{"strict_mode": False}, {"null_padding": True}])
def test_neither_flag_alone_covers_both_shapes(warehouse, extra_column,
                                               missing_column, flags):
    """Why "keep" sets both.

    strict_mode handles extra values and still stops on missing ones;
    null_padding handles missing values and still stops on extra ones. A
    row-handling choice that set only one would keep working right up until the
    file was broken the other way.
    """
    failures = 0
    for path in (extra_column, missing_column):
        with warehouse.cur() as conn:
            try:
                read(conn, path, **flags)
            except Exception:  # noqa: BLE001
                failures += 1
    assert failures == 1, "each flag covers exactly one of the two shapes"


def test_skip_drops_the_row_and_keeps_the_rest(warehouse, extra_column):
    with warehouse.cur() as conn:
        rows = read(conn, extra_column, **SKIP)
    assert len(rows) == GOOD_ROWS


def test_skip_and_keep_are_not_the_same_answer(warehouse, missing_column):
    """One reaches a decision about the row; the other throws it away."""
    with warehouse.cur() as conn:
        kept = read(conn, missing_column, **KEEP)
        skipped = read(conn, missing_column, **SKIP)
    assert len(kept) == len(skipped) + 1


# --------------------------------------------------------------------------- #
# skipping is counted, never silent
# --------------------------------------------------------------------------- #
def test_a_skipped_row_is_counted(warehouse, extra_column):
    with warehouse.cur() as conn:
        read(conn, extra_column, **SKIP)
        rejected = rejected_rows(conn)
    assert rejected.rows == 1
    assert bool(rejected) is True
    assert "line 21002" in rejected.describe()


def test_one_bad_line_counts_once_however_many_columns_it_breaks(warehouse, tmp_path):
    """DuckDB records a reject row per bad *column*, so a three-column file
    reports one broken line three times. Reported as three, "skipped 3 rows"
    would be a made-up number in a message whose whole job is to be exact."""
    path = tmp_path / "wide.csv"
    path.write_text(
        "id,name,amount\n"
        + "".join(f"{i},n{i},{i}\n" for i in range(GOOD_ROWS))
        + "99999,bob,7,EXTRA,MORE\n"
    )
    with warehouse.cur() as conn:
        read(conn, path, **SKIP)
        assert rejected_rows(conn).rows == 1


def test_rescanning_the_same_file_does_not_double_count(warehouse, extra_column):
    """An import scans the file more than once -- the sniffer check, the cast
    measurement, the write -- and each pass rejects the same line again."""
    with warehouse.cur() as conn:
        read(conn, extra_column, **SKIP)
        read(conn, extra_column, **SKIP)
        assert rejected_rows(conn).rows == 1


def test_nothing_skipped_reports_nothing(warehouse, tmp_path):
    clean = tmp_path / "clean.csv"
    clean.write_text("id,name\n1,a\n2,b\n")
    with warehouse.cur() as conn:
        read(conn, clean, **SKIP)
        rejected = rejected_rows(conn)
    assert not rejected and rejected.rows == 0


def test_counting_a_connection_that_never_skipped_anything(warehouse):
    """The reject tables are TEMP and only exist after such a read. Their
    absence means nothing was lost, not that something went wrong."""
    with warehouse.cur() as conn:
        assert not rejected_rows(conn)


# --------------------------------------------------------------------------- #
# the import
# --------------------------------------------------------------------------- #
def test_an_import_of_a_bad_file_fails_by_default(app_ctx, extra_column):
    job = run_import(app_ctx, extra_column)
    assert job.status == "failed"


def test_the_failure_says_what_is_wrong_and_what_to_do(app_ctx, extra_column):
    """Not DuckDB's message. That one ends with a list of parameter names the
    person reading it in a browser has no way to set."""
    job = run_import(app_ctx, extra_column)
    assert "Line 21002 does not match the columns" in job.error
    assert "Expected Number of Columns: 2 Found: 3" in job.error
    assert "keep them, or skip them" in job.error
    assert "strict_mode=false" not in job.error, "the flag names are not the advice"
    assert "Auto-Detected" not in job.error, "nor is the settings dump"


def test_importing_with_skip_succeeds_and_says_what_it_lost(app_ctx, extra_column):
    job = run_import(app_ctx, extra_column, **SKIP)
    assert job.status == "succeeded", job.error

    warnings = [line for line in job.logs if "WARNING" in line and "skipped" in line]
    assert len(warnings) == 1, job.logs
    assert "skipped 1 row(s)" in warnings[0]
    assert "line 21002" in warnings[0]


def test_importing_with_keep_loses_nothing(app_ctx, extra_column):
    job = run_import(app_ctx, extra_column, **KEEP)
    assert job.status == "succeeded", job.error
    assert not [line for line in job.logs if "skipped" in line]

    step = app_ctx.catalog.list_steps(job.id)[0]
    dataset = step.outputs[0]["dataset_id"]
    assert app_ctx.catalog.get_profile(dataset).row_count == GOOD_ROWS + 1


def test_a_skipped_import_stores_only_the_rows_it_kept(app_ctx, extra_column):
    job = run_import(app_ctx, extra_column, **SKIP)
    step = app_ctx.catalog.list_steps(job.id)[0]
    dataset = step.outputs[0]["dataset_id"]
    assert app_ctx.catalog.get_profile(dataset).row_count == GOOD_ROWS


def test_a_clean_import_is_unaffected(app_ctx, tmp_path):
    """The default has to stay the default: this changes what happens to a
    broken file, not to a good one."""
    clean = tmp_path / "clean.csv"
    clean.write_text("id,name\n1,a\n2,b\n")
    job = run_import(app_ctx, clean)
    assert job.status == "succeeded", job.error
    assert not [line for line in job.logs if "skipped" in line]


# --------------------------------------------------------------------------- #
# explaining the error
# --------------------------------------------------------------------------- #
def test_an_unrelated_failure_is_left_alone(app_ctx):
    """Only a row-parsing failure is rewritten. Anything else keeps its own type
    and message, so a real bug is not disguised as a CSV problem."""
    assert explain_read_error(ValueError("no such file")) is None
    assert explain_read_error(RuntimeError("out of memory")) is None


def test_the_explanation_quotes_the_offending_line(app_ctx, extra_column):
    job = run_import(app_ctx, extra_column)
    assert '"99999,bob,EXTRA"' in job.error


def test_a_very_long_line_is_truncated(app_ctx, tmp_path):
    """The offending line is the user's data and can be any width; a 40,000
    character error message is not a message."""
    path = tmp_path / "long.csv"
    path.write_text(
        "id,name\n"
        + "".join(f"{i},n{i}\n" for i in range(GOOD_ROWS))
        + f"99999,{'x' * 5_000},EXTRA\n"
    )
    job = run_import(app_ctx, path)
    assert job.status == "failed"
    assert len(job.error) < 500
    assert "..." in job.error


# --------------------------------------------------------------------------- #
# over HTTP
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(app_ctx):
    import dataq.api.app as app_module

    app = create_app(ctx=app_ctx)
    with TestClient(app) as c:
        yield c
    app_module.CTX = None


def test_the_plan_cannot_see_a_bad_row_past_its_sample(client, app_ctx, extra_column):
    """Worth stating, because it is the thing that surprises people.

    The proposal reads a 2,000-row prefix, and the error only exists once the
    sniffer has settled the schema from its own 20,480-row sample -- so a row
    that breaks it is always beyond what the preview looked at. The preview is
    honest about the columns and cannot promise the file parses to the end.
    That is why the row-handling control sits on the import panel next to the
    file, rather than appearing only after a plan comes back unhappy.
    """
    assert client.post("/api/sources/plan",
                       json={"uri": str(extra_column)}).status_code == 200
    assert run_import(app_ctx, extra_column).status == "failed"


def test_the_plan_route_passes_the_choice_to_the_reader(client, extra_column):
    """The plan is a preview of the read the import will do, so it has to be
    made with the same settings -- otherwise it previews a stricter read."""
    for params in (KEEP, SKIP):
        r = client.post("/api/sources/plan",
                        json={"uri": str(extra_column), "params": params})
        assert r.status_code == 200, r.json()
        assert [c["name"] for c in r.json()["columns"]] == ["id", "name"]


def test_a_route_that_does_reach_a_bad_row_explains_it(client, tmp_path):
    """Whichever scan hits the row first reports it the same way.

    The plan's format-settling pass reads far more of the file than the sample
    does, so this path is reachable from the preview as well as the import; the
    explanation lives with the reader rather than with either caller.
    """
    import duckdb

    from dataq.services.import_plan import explain_read_error

    path = tmp_path / "reached.csv"
    path.write_text("id,name\n" + "".join(f"{i},n{i}\n" for i in range(GOOD_ROWS))
                    + "99999,bob,EXTRA\n")
    with duckdb.connect() as conn, pytest.raises(Exception) as caught:
        conn.execute(f"SELECT * FROM read_csv('{path}', sample_size=20480)").fetchall()

    detail = explain_read_error(caught.value)
    assert "Line 21002 does not match the columns" in detail
    assert "read the file again" in detail
    assert "strict_mode" not in detail and "Auto-Detected" not in detail
