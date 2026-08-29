"""Deciding column types before the import rather than after.

A column's physical type is settled by the reader and can never be changed
again, so these tests are mostly about the two ways that goes wrong: a type
nobody chose, and a reading nobody recorded.
"""

from __future__ import annotations

import csv

import pytest
from fastapi.testclient import TestClient

from dataq.api.app import create_app
from dataq.services.import_plan import (
    ColumnPlan,
    PlanError,
    build_plan,
    cast_projection,
    text_columns,
    validate_plan,
)
from dataq.services.operations import OperationRequest, submit_operation

# MM/DD/YYYY text dates, the shape of the export that prompted this.
LOGON = [("u1", "03/07/2011 08:07:29", "Logon"),
         ("u2", "11/22/2010 14:03:11", "Logoff"),
         ("u3", "07/19/2010 09:30:00", "Logon")]


def write_csv(path, header, rows):
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return path


@pytest.fixture
def logon(tmp_path):
    """Enough rows, with days past the 12th, to settle the reading."""
    rows = [(f"u{i % 5}", f"{i % 12 + 1:02d}/{i % 27 + 1:02d}/2011 08:07:29",
             ["Logon", "Logoff"][i % 2]) for i in range(200)]
    return write_csv(tmp_path / "logon.csv", ["user", "date", "activity"], rows)


@pytest.fixture
def ambiguous_file(tmp_path):
    """Every day <= 12 in the whole file, so nothing can settle the reading."""
    rows = [(f"{m:02d}/{d:02d}/2016 09:00:00", 1)
            for m, d in ((3, 4), (5, 6), (7, 8), (1, 2), (9, 10), (11, 12)) * 20]
    return write_csv(tmp_path / "amb.csv", ["when", "n"], rows)


def plan_for(app_ctx, path):
    from dataq.plugins.builtin.readers import CsvReader

    with app_ctx.warehouse.cur() as conn:
        return build_plan(conn, CsvReader, str(path), {})


def run_failing(app_ctx, **kwargs):
    accepted = submit_operation(app_ctx, OperationRequest(**kwargs))
    app_ctx.runner.wait(accepted.job_id, timeout=120)
    return app_ctx.catalog.get_job(accepted.job_id)


# --------------------------------------------------------------------------- #
# what the plan proposes
# --------------------------------------------------------------------------- #
def test_a_text_date_column_is_proposed_as_a_timestamp(app_ctx, logon):
    """The reported case: the reader leaves it VARCHAR and nothing asks."""
    plan = plan_for(app_ctx, logon)
    date = next(c for c in plan.columns if c.name == "date")

    assert date.source_type == "VARCHAR"
    assert date.proposed.target_type == "TIMESTAMP"
    assert date.proposed.format == "%m/%d/%Y %H:%M:%S"
    assert date.proposed.role == "time"
    assert date.parse_rate == 1.0
    assert "usable as a time axis" in date.rationale


def test_columns_needing_nothing_are_proposed_untouched(app_ctx, logon):
    plan = plan_for(app_ctx, logon)
    user = next(c for c in plan.columns if c.name == "user")
    assert user.proposed.target_type is None
    assert not user.decision_required


def test_the_plan_carries_evidence_not_just_a_verdict(app_ctx, logon):
    """Someone has to be able to see why, or confirming means nothing."""
    plan = plan_for(app_ctx, logon)
    date = next(c for c in plan.columns if c.name == "date")
    assert date.sample_values, "the values it read"
    assert date.formats and date.formats[0].example_input
    assert plan.rows, "and rows to look at"


def test_the_proposal_matches_what_the_import_will_do(app_ctx, run_op, logon):
    """The preview is only worth confirming if it is not a second opinion."""
    plan = plan_for(app_ctx, logon)
    proposed = {c.name: c.proposed for c in plan.columns}

    ds = run_op(op="import", uri=str(logon), name="logon",
                params={"columns": [p.model_dump() for p in proposed.values()]})
    profile = app_ctx.catalog.get_profile(ds)
    for name, plan_for_column in proposed.items():
        column = profile.column(name)
        if plan_for_column.target_type:
            assert column.physical_type.upper().startswith(
                plan_for_column.target_type), name
        if plan_for_column.role:
            assert column.role == plan_for_column.role, name


# --------------------------------------------------------------------------- #
# ambiguity: settled where possible, asked where not
# --------------------------------------------------------------------------- #
def test_a_prefix_sample_does_not_get_to_decide(app_ctx, logon):
    """The first rows of a time-sorted export span a narrow window, in which
    every day can be <= 12 and both readings fit. Counting real failures over a
    wider slice settles it; sampling harder does not."""
    plan = plan_for(app_ctx, logon)
    assert plan.undecided == [], "this file's later rows rule out day-first"


def test_a_genuinely_ambiguous_column_asks(app_ctx, ambiguous_file):
    plan = plan_for(app_ctx, ambiguous_file)
    when = next(c for c in plan.columns if c.name == "when")

    assert when.decision_required
    assert plan.undecided == ["when"]
    assert {f.format for f in when.formats} >= {
        "%m/%d/%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S"}
    # A worked example, because "ambiguous" is not a question anyone can answer.
    assert "reads as" in when.conflict


def test_each_reading_produces_the_dates_it_should(app_ctx, run_op, ambiguous_file):
    """Whichever the user picks must actually be the one applied."""
    got = {}
    for fmt, label in (("%m/%d/%Y %H:%M:%S", "month"), ("%d/%m/%Y %H:%M:%S", "day")):
        ds = run_op(op="import", uri=str(ambiguous_file), name=f"as {label}",
                    params={"columns": [{"name": "when", "target_type": "TIMESTAMP",
                                         "format": fmt}]})
        src = app_ctx.resolve_source(ds).sql
        with app_ctx.warehouse.cur() as conn:
            got[label] = str(conn.execute(
                f'SELECT min("when") FROM {src}').fetchone()[0])

    assert got["month"].startswith("2016-01-02"), got
    assert got["day"].startswith("2016-02-01"), got


def test_a_reader_converted_column_can_still_be_re_read(app_ctx, run_op, tmp_path):
    """Given 03/04/2016 the sniffer returns a DATE and does not record which
    reading it took. The text has to be held back for the choice to exist."""
    rows = [(f"{m:02d}/{d:02d}/2016", 1)
            for m, d in ((3, 4), (5, 6), (7, 8), (1, 2)) * 20]
    path = write_csv(tmp_path / "conv.csv", ["when", "n"], rows)

    plan = plan_for(app_ctx, path)
    when = next(c for c in plan.columns if c.name == "when")
    assert when.source_type.upper().startswith("DATE"), "precondition: it converted"
    assert when.decision_required, "and it guessed which reading"

    ds = run_op(op="import", uri=str(path), name="conv",
                params={"columns": [{"name": "when", "target_type": "DATE",
                                     "format": "%d/%m/%Y"}]})
    src = app_ctx.resolve_source(ds).sql
    with app_ctx.warehouse.cur() as conn:
        earliest = str(conn.execute(f'SELECT min("when") FROM {src}').fetchone()[0])
    assert earliest.startswith("2016-02-01"), "the chosen reading, not the guess"


# --------------------------------------------------------------------------- #
# bad values cost values, not rows
# --------------------------------------------------------------------------- #
def test_an_unparseable_value_costs_one_value(app_ctx, run_op, tmp_path):
    """Forcing the type at read time aborts the whole import on one bad row,
    and ignore_errors answers that by dropping rows. Casting after the read
    cannot do either."""
    rows = [(f"{i % 12 + 1:02d}/{i % 27 + 1:02d}/2011 08:07:29",) for i in range(60)]
    rows += [("n/a",), ("",)]
    path = write_csv(tmp_path / "messy.csv", ["date"], rows)

    ds = run_op(op="import", uri=str(path), name="messy",
                params={"columns": [{"name": "date", "target_type": "TIMESTAMP",
                                     "format": "%m/%d/%Y %H:%M:%S"}]})
    profile = app_ctx.catalog.get_profile(ds)
    assert profile.row_count == 62, "every row survives"
    assert profile.column("date").physical_type.upper().startswith("TIMESTAMP")

    warning = profile.column("date").warning
    assert warning and "1 of 61" in warning
    assert "no rows were dropped" in warning


def test_a_clean_cast_leaves_no_warning(app_ctx, run_op, logon):
    ds = run_op(op="import", uri=str(logon), name="logon",
                params={"columns": [{"name": "date", "target_type": "TIMESTAMP",
                                     "format": "%m/%d/%Y %H:%M:%S"}]})
    assert app_ctx.catalog.get_profile(ds).column("date").warning is None


# --------------------------------------------------------------------------- #
# the plan is validated before it is trusted
# --------------------------------------------------------------------------- #
def test_time_role_on_a_text_column_is_refused():
    """profile_columns treats a pin as final, so this would recreate the exact
    VARCHAR - INTERVAL failure the plan exists to prevent."""
    with pytest.raises(PlanError, match="needs a date or timestamp"):
        validate_plan([ColumnPlan(name="date", role="time")], {"date"})


def test_unknown_semantic_types_and_columns_are_refused():
    with pytest.raises(PlanError, match="unknown semantic type"):
        validate_plan([ColumnPlan(name="a", semantic_type="not.a.type")], {"a"})
    with pytest.raises(PlanError, match="no column named"):
        validate_plan([ColumnPlan(name="nope")], {"a"})


def test_a_format_without_a_temporal_target_is_refused():
    with pytest.raises(PlanError, match="only applies when importing as"):
        validate_plan([ColumnPlan(name="a", target_type="BIGINT", format="%Y")], {"a"})


def test_two_plans_for_one_column_are_refused():
    with pytest.raises(PlanError, match="two plans"):
        validate_plan([ColumnPlan(name="a"), ColumnPlan(name="a")], {"a"})


def test_a_bad_plan_fails_the_import_before_writing(app_ctx, logon):
    job = run_failing(app_ctx, op="import", uri=str(logon), name="logon",
                      params={"columns": [{"name": "date", "role": "time"}]})
    assert job.status == "failed"
    assert "needs a date or timestamp" in job.error
    assert app_ctx.catalog.list_datasets() == [], "and leaves nothing behind"


# --------------------------------------------------------------------------- #
# the SQL the plan compiles to
# --------------------------------------------------------------------------- #
def test_only_planned_columns_are_touched():
    plans = {"b": ColumnPlan(name="b", target_type="TIMESTAMP", format="%Y-%m-%d")}
    sql = cast_projection(plans, [("a", "VARCHAR"), ("b", "VARCHAR")])
    assert sql.startswith('"a", ')
    assert "try_strptime" in sql and 'AS "b"' in sql


def test_a_target_matching_the_source_is_a_no_op():
    plans = {"a": ColumnPlan(name="a", target_type="VARCHAR")}
    assert cast_projection(plans, [("a", "VARCHAR")]) == '"a"'


def test_only_already_converted_columns_are_held_back_as_text():
    """A column that is already text arrives intact; one the reader converted
    has to be re-read or the other reading is unreachable."""
    plans = {"a": ColumnPlan(name="a", target_type="TIMESTAMP", format="%Y"),
             "b": ColumnPlan(name="b", target_type="TIMESTAMP", format="%Y")}
    assert text_columns(plans, [("a", "VARCHAR"), ("b", "DATE")]) == ["b"]


# --------------------------------------------------------------------------- #
# nothing changes for an import that does not send a plan
# --------------------------------------------------------------------------- #
def test_an_import_without_a_plan_behaves_as_before(app_ctx, run_op, logon):
    ds = run_op(op="import", uri=str(logon), name="logon")
    column = app_ctx.catalog.get_profile(ds).column("date")
    assert column.physical_type == "VARCHAR"
    assert column.semantic_type == "time.timestamp"
    assert column.role == "dimension", "text, so not a time axis"


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


def test_the_plan_route(client, logon):
    r = client.post("/api/sources/plan", json={"uri": str(logon)})
    assert r.status_code == 200
    body = r.json()
    assert body["reader"] == "read.csv"
    date = next(c for c in body["columns"] if c["name"] == "date")
    assert date["proposed"]["target_type"] == "TIMESTAMP"
    assert date["proposed"]["format"] == "%m/%d/%Y %H:%M:%S"


def test_the_plan_route_refuses_a_path_outside_the_roots(client):
    r = client.post("/api/sources/plan", json={"uri": "/etc/passwd"})
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# accepting a proposal is not an override
# --------------------------------------------------------------------------- #
def test_accepting_the_proposal_does_not_pin_everything(app_ctx, run_op, logon):
    """Pinned means a human overrode this. Marking every column as overridden
    because somebody looked at the plan would make the marker meaningless, and
    would stop re-detection on a dataset nobody had corrected."""
    plan = plan_for(app_ctx, logon)
    ds = run_op(op="import", uri=str(logon), name="logon",
                params={"columns": [c.proposed.model_dump() for c in plan.columns]})

    profile = app_ctx.catalog.get_profile(ds)
    assert not any(c.pinned for c in profile.columns)
    # The types still landed; only the freezing is withheld.
    assert profile.column("date").physical_type.upper().startswith("TIMESTAMP")
    assert profile.column("date").role == "time"


def test_an_edited_column_is_pinned(app_ctx, run_op, logon):
    ds = run_op(op="import", uri=str(logon), name="logon", params={"columns": [
        {"name": "activity", "semantic_type": "identity.key", "pinned": True}]})
    profile = app_ctx.catalog.get_profile(ds)
    assert profile.column("activity").pinned
    assert profile.column("activity").semantic_type == "identity.key"
    assert not profile.column("user").pinned


def test_an_explicitly_chosen_format_is_not_reported_as_a_guess(
        app_ctx, run_op, ambiguous_file):
    """The sniffer warning exists to report a reading the reader chose without
    saying so. When the reading was given to it, there was no guess to report."""
    ds = run_op(op="import", uri=str(ambiguous_file), name="amb",
                params={"columns": [{"name": "when", "target_type": "TIMESTAMP",
                                     "format": "%d/%m/%Y %H:%M:%S"}]})
    assert app_ctx.catalog.get_profile(ds).column("when").warning is None
