"""The two feature steps, end to end.

Built around the dataset shape the capability was asked for --
``(user, timestamp, activity_type, location)`` -- with a fixture small enough
that every expected value below is worked out by hand rather than by the code
under test.
"""

from __future__ import annotations

import csv

import pytest

from dataq.services.operations import OperationRequest, submit_operation

# u1 logs in twice on Jan 1, buys on Jan 5, and logs in again on Mar 1 -- 60 days
# later, which is outside a 30-day window. u2 and u3 give the global shares
# something to divide by. 2016 is a leap year; that is why the gap is 60.
EVENTS = [
    ("u1", "2016-01-01 09:00:00", "login", "nyc", 10.0),
    ("u1", "2016-01-01 18:00:00", "login", "nyc", 20.0),
    ("u1", "2016-01-05 09:00:00", "buy",   "sfo", 30.0),
    ("u1", "2016-03-01 09:00:00", "login", "nyc", 40.0),
    ("u2", "2016-01-02 09:00:00", "login", "lon", 50.0),
    ("u3", "2016-01-03 09:00:00", "buy",   "nyc", 60.0),
]


@pytest.fixture
def events(app_ctx, run_op, tmp_path):
    path = tmp_path / "events.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["user", "ts", "activity_type", "location", "amount"])
        w.writerows(EVENTS)
    return run_op(op="import", uri=str(path), name="events")


def rows(app_ctx, dataset_id, columns):
    """Every row, in event order, for the named columns."""
    src = app_ctx.resolve_source(dataset_id).sql
    select = ", ".join(f'"{c}"' for c in columns)
    with app_ctx.warehouse.cur() as conn:
        return conn.execute(
            f'SELECT {select} FROM {src} ORDER BY "user", "ts"'
        ).fetchall()


def run_failing(app_ctx, **kwargs):
    accepted = submit_operation(app_ctx, OperationRequest(**kwargs))
    app_ctx.runner.wait(accepted.job_id, timeout=120)
    return app_ctx.catalog.get_job(accepted.job_id)


# --------------------------------------------------------------------------- #
# step 2 alone: the per-row features
# --------------------------------------------------------------------------- #
def test_the_five_features_from_the_original_question(app_ctx, run_op, events):
    """All five, in one pass, with hand-computed answers."""
    run_op(op="transform", plugin_id="enrich.features",
           inputs=[{"dataset_id": events}], params={"features": [
               "share() by activity_type",
               "count() by user, activity_type over 30d",
               "count() by user, activity_type in day",
               "count() by user, location over 30d",
               "days_since_last() by user, activity_type",
           ]})

    got = rows(app_ctx, events, [
        "share_by_activity_type",
        "n_by_user_activity_type_30d",
        "n_by_user_activity_type_per_day",
        "n_by_user_location_30d",
        "days_since_last_by_user_activity_type",
    ])
    # 4 of 6 events are logins (0.667), 2 are buys (0.333).
    assert [round(r[0], 3) for r in got] == [0.667, 0.667, 0.333, 0.667, 0.667, 0.333]
    # u1's March login is 60 days after the January ones, so it starts over.
    assert [r[1] for r in got] == [1, 2, 1, 1, 1, 1]
    # Both Jan-1 logins fall in the same calendar day.
    assert [r[2] for r in got] == [2, 2, 1, 1, 1, 1]
    assert [r[3] for r in got] == [1, 2, 1, 1, 1, 1]
    assert [r[4] for r in got] == [None, 0, None, 60, None, None]


def test_the_row_count_is_unchanged(app_ctx, run_op, events):
    before = app_ctx.catalog.get_profile(events).row_count
    run_op(op="transform", plugin_id="enrich.features",
           inputs=[{"dataset_id": events}],
           params={"features": ["count() by user over 30d"]})
    assert app_ctx.catalog.get_profile(events).row_count == before


def test_features_land_as_a_new_version_not_a_new_dataset(app_ctx, run_op, events):
    """An annotation belongs to the dataset's history, not to its offspring."""
    datasets_before = len(app_ctx.catalog.list_datasets())
    run_op(op="transform", plugin_id="enrich.features",
           inputs=[{"dataset_id": events}],
           params={"features": ["count() by user"]})
    assert len(app_ctx.catalog.list_datasets()) == datasets_before
    assert app_ctx.catalog.get_profile(events).version == 2


def test_a_named_feature_uses_that_name(app_ctx, run_op, events):
    run_op(op="transform", plugin_id="enrich.features",
           inputs=[{"dataset_id": events}],
           params={"features": ["avg(amount) by user over 30d as spend_30d"]})
    assert app_ctx.catalog.get_profile(events).column("spend_30d") is not None


def test_a_bad_feature_fails_before_anything_is_written(app_ctx, run_op, events):
    job = run_failing(app_ctx, op="transform", plugin_id="enrich.features",
                      inputs=[{"dataset_id": events}],
                      params={"features": ["count() by nonexistent"]})
    assert job.status == "failed"
    assert "no column named 'nonexistent'" in job.error
    assert app_ctx.catalog.get_profile(events).version == 1, "no version written"


def test_two_features_with_the_same_name_are_refused(app_ctx, run_op, events):
    """Silently keeping one of them would lose a column the user asked for."""
    job = run_failing(app_ctx, op="transform", plugin_id="enrich.features",
                      inputs=[{"dataset_id": events}],
                      params={"features": ["count() by user", "count() by user"]})
    assert job.status == "failed"
    assert "both be called" in job.error


# --------------------------------------------------------------------------- #
# step 1: the feature table, which is a dataset in its own right
# --------------------------------------------------------------------------- #
def test_the_feature_table_is_analysable_on_its_own(app_ctx, run_op, events):
    table = run_op(op="aggregate", plugin_id="agg.features",
                   inputs=[{"dataset_id": events}],
                   params={"by": ["user", "activity_type"]},
                   output_name="user_activity_features")

    profile = app_ctx.catalog.get_profile(table)
    assert {c.name for c in profile.columns} >= {
        "user", "activity_type", "n", "share", "rarity", "first_seen", "last_seen"}
    # u1 has three logins and one buy; four entities in total.
    assert profile.row_count == 4

    src = app_ctx.resolve_source(table).sql
    with app_ctx.warehouse.cur() as conn:
        got = dict(conn.execute(
            f'SELECT "user" || \'/\' || activity_type, n FROM {src}').fetchall())
    assert got == {"u1/login": 3, "u1/buy": 1, "u2/login": 1, "u3/buy": 1}


def test_the_feature_table_carries_semantic_types_across(app_ctx, run_op, events):
    """Which is what lets it be joined back, and charted."""
    table = run_op(op="aggregate", plugin_id="agg.features",
                   inputs=[{"dataset_id": events}], params={"by": ["activity_type"]})
    source_type = app_ctx.catalog.get_profile(events).column("activity_type").semantic_type
    assert app_ctx.catalog.get_profile(table).column(
        "activity_type").semantic_type == source_type


def test_a_bucketed_table_has_one_row_per_entity_per_bucket(app_ctx, run_op, events):
    table = run_op(op="aggregate", plugin_id="agg.features",
                   inputs=[{"dataset_id": events}],
                   params={"by": ["user", "activity_type"], "grain": "day"})
    profile = app_ctx.catalog.get_profile(table)
    assert profile.column("bucket") is not None
    # u1: login on Jan 1, buy on Jan 5, login on Mar 1 = 3 rows. Plus u2, u3.
    assert profile.row_count == 5


def test_rolling_totals_need_a_grain(app_ctx, run_op, events):
    job = run_failing(app_ctx, op="aggregate", plugin_id="agg.features",
                      inputs=[{"dataset_id": events}],
                      params={"by": ["user"], "windows": ["30d"]})
    assert job.status == "failed"
    assert "grain" in job.error


def test_a_rolling_total_cannot_see_the_current_bucket(app_ctx, run_op, events):
    """The reason the frame stops one bucket short.

    A day-bucket holds the whole day, including events after the one being
    described. So 'n_30d' means the 30 days *before* today -- which for u1's
    first day is nothing at all, not the two logins in it.
    """
    table = run_op(op="aggregate", plugin_id="agg.features",
                   inputs=[{"dataset_id": events}],
                   params={"by": ["user"], "grain": "day", "windows": ["30d"]})
    src = app_ctx.resolve_source(table).sql
    with app_ctx.warehouse.cur() as conn:
        got = conn.execute(
            f'SELECT bucket, n, n_30d FROM {src} WHERE "user" = \'u1\' ORDER BY bucket'
        ).fetchall()
    assert [r[1] for r in got] == [2, 1, 1], "events in each bucket"
    assert got[0][2] is None, "nothing precedes the first day"
    assert got[1][2] == 2, "Jan 5 sees the two Jan 1 logins, not its own"


# --------------------------------------------------------------------------- #
# the two steps together
# --------------------------------------------------------------------------- #
def test_attaching_a_feature_table_to_the_events(app_ctx, run_op, events):
    """The composite-key join the standalone join op cannot express."""
    table = run_op(op="aggregate", plugin_id="agg.features",
                   inputs=[{"dataset_id": events}],
                   params={"by": ["user", "activity_type"]},
                   output_name="uaf")

    run_op(op="transform", plugin_id="enrich.features",
           inputs=[{"dataset_id": events}],
           params={"from_dataset": table, "prefix": "ua_"})

    profile = app_ctx.catalog.get_profile(events)
    assert profile.row_count == len(EVENTS), "an annotation must not multiply rows"
    got = rows(app_ctx, events, ["ua_n", "ua_share"])
    # Each row now carries how often that user did that activity.
    assert [r[0] for r in got] == [3, 3, 1, 3, 1, 1]
    assert round(got[0][1], 3) == 0.5, "u1/login is 3 of 6 events"


def test_attaching_a_bucketed_table_matches_the_right_day(app_ctx, run_op, events):
    """The events side has to be truncated to the table's grain to match it,
    and the grain is worked out from the buckets rather than restated."""
    table = run_op(op="aggregate", plugin_id="agg.features",
                   inputs=[{"dataset_id": events}],
                   params={"by": ["user"], "grain": "day"},
                   output_name="daily")

    run_op(op="transform", plugin_id="enrich.features",
           inputs=[{"dataset_id": events}],
           params={"from_dataset": table, "join_on": ["user"], "prefix": "day_"})

    got = rows(app_ctx, events, ["day_n"])
    # u1 has two events on Jan 1 and one on each of Jan 5 and Mar 1.
    assert [r[0] for r in got] == [2, 2, 1, 1, 1, 1]
    assert app_ctx.catalog.get_profile(events).row_count == len(EVENTS)


def test_a_feature_table_with_duplicate_keys_is_refused(app_ctx, run_op, events):
    """A left join onto duplicated keys multiplies rows, and every total
    downstream would then be wrong with nothing to say so."""
    table = run_op(op="aggregate", plugin_id="agg.features",
                   inputs=[{"dataset_id": events}],
                   params={"by": ["user", "activity_type"]},
                   output_name="uaf")

    # Join on user alone: u1 has two rows in the table, so the key is not unique.
    job = run_failing(app_ctx, op="transform", plugin_id="enrich.features",
                      inputs=[{"dataset_id": events}],
                      params={"from_dataset": table, "join_on": ["user"],
                              "prefix": "x_"})
    assert job.status == "failed"
    assert "multiply rows" in job.error
    assert app_ctx.catalog.get_profile(events).row_count == len(EVENTS)


def test_colliding_column_names_are_refused(app_ctx, run_op, events):
    """Attaching the same table twice under one prefix would land it twice.

    DuckDB tolerates duplicate output names, and the catalog's name-keyed column
    map would then silently keep one of them -- so this has to be caught here.
    """
    table = run_op(op="aggregate", plugin_id="agg.features",
                   inputs=[{"dataset_id": events}],
                   params={"by": ["user", "activity_type"]}, output_name="uaf")
    attach = {"from_dataset": table, "prefix": "ua_"}
    run_op(op="transform", plugin_id="enrich.features",
           inputs=[{"dataset_id": events}], params=attach)

    job = run_failing(app_ctx, op="transform", plugin_id="enrich.features",
                      inputs=[{"dataset_id": events}], params=attach)
    assert job.status == "failed"
    assert "duplicate columns" in job.error and "ua_n" in job.error


def test_joining_and_computing_in_one_pass(app_ctx, run_op, events):
    """Both halves of the same question, one scan: the table supplies what a
    group-by can answer, the window expressions supply what it cannot."""
    table = run_op(op="aggregate", plugin_id="agg.features",
                   inputs=[{"dataset_id": events}],
                   params={"by": ["user", "activity_type"]}, output_name="uaf")

    run_op(op="transform", plugin_id="enrich.features",
           inputs=[{"dataset_id": events}],
           params={"from_dataset": table, "prefix": "ua_",
                   "features": ["days_since_last() by user, activity_type"]})

    got = rows(app_ctx, events, ["ua_n", "days_since_last_by_user_activity_type"])
    assert [r[0] for r in got] == [3, 3, 1, 3, 1, 1]
    assert [r[1] for r in got] == [None, 0, None, 60, None, None]


@pytest.mark.parametrize("grain,expected", [("day", 6), ("month", 6), ("week", 6)])
def test_every_grain_matches_the_right_bucket(app_ctx, run_op, events, grain, expected):
    """Grain is read from the buckets' alignment, not their spacing.

    Spacing is the tempting answer and it is wrong: these six events form five
    *daily* buckets averaging fifteen days apart, which a spacing rule reads as
    weekly and then matches nothing at all.
    """
    table = run_op(op="aggregate", plugin_id="agg.features",
                   inputs=[{"dataset_id": events}],
                   params={"by": ["user"], "grain": grain},
                   output_name=f"by_{grain}")
    run_op(op="transform", plugin_id="enrich.features",
           inputs=[{"dataset_id": events}],
           params={"from_dataset": table, "join_on": ["user"], "prefix": "b_"})

    got = [r[0] for r in rows(app_ctx, events, ["b_n"])]
    assert len(got) == expected
    assert all(v is not None for v in got), f"{grain} buckets did not match"


# --------------------------------------------------------------------------- #
# discoverability, and what the features connect to once they exist
# --------------------------------------------------------------------------- #
def test_features_are_suggested_for_an_event_shaped_dataset(app_ctx, events):
    """A time column plus something to call an actor is a log of behaviour."""
    from dataq.services.inspect import suggest

    found = [s for s in suggest(app_ctx, events)
             if s.action.get("plugin_id") in ("enrich.features", "agg.features")]
    assert found, "an event log should offer behavioural features"

    enrich = next(s for s in found if s.action["plugin_id"] == "enrich.features")
    assert "user" in enrich.title
    # The suggestion is an executable payload, so its features must be real.
    assert any("days_since_last" in f for f in enrich.action["params"]["features"])


def test_replaying_the_feature_suggestion_works(app_ctx, run_op, events):
    """A suggestion nobody can run is advice, which is what these exist not to be."""
    from dataq.services.inspect import suggest

    action = next(s.action for s in suggest(app_ctx, events)
                  if s.action.get("plugin_id") == "enrich.features")
    run_op(op="transform", plugin_id=action["plugin_id"],
           inputs=action["inputs"], params=action["params"])
    assert app_ctx.catalog.get_profile(events).version == 2


def test_nothing_is_suggested_without_a_time_column(app_ctx, run_op, tmp_path):
    from dataq.services.inspect import suggest

    path = tmp_path / "flat.csv"
    path.write_text("user,activity_type\nu1,login\nu2,buy\n")
    ds = run_op(op="import", uri=str(path), name="flat")
    assert not [s for s in suggest(app_ctx, ds)
                if s.action.get("plugin_id") == "enrich.features"]


def test_a_share_feature_is_typed_as_one(app_ctx, run_op, events):
    """Typed rather than recognised by name, which is what lets the timeline
    highlight rows using a computed feature."""
    run_op(op="transform", plugin_id="enrich.features",
           inputs=[{"dataset_id": events}],
           params={"features": ["share() by activity_type"]})
    column = app_ctx.catalog.get_profile(events).column("share_by_activity_type")
    assert column.semantic_type == "numeric.share"


def test_the_timeline_highlights_using_a_computed_feature(app_ctx, run_op, events):
    """The payoff of typing it: rarity no longer has to come from a join, and
    no longer has to be called 'share'."""
    from dataq.services.inspect import render_viz

    run_op(op="transform", plugin_id="enrich.features",
           inputs=[{"dataset_id": events}],
           params={"features": ["share() by activity_type"]})
    out = render_viz(app_ctx, "viz.timeline", events,
                     {"time_column": "ts", "title_column": "activity_type"})
    rule = out.spec.timeline.abnormality
    assert rule is not None
    assert rule.column == "share_by_activity_type"
    assert rule.op == "<"


def test_a_yes_no_flag_is_not_an_actor(app_ctx, run_op, tmp_path):
    """The NYC taxi data has no driver or medallion column -- only a Y/N flag.

    Grouping behaviour by a two-value flag is not behavioural analysis, and a
    confidently wrong suggestion is worse than none, so nothing is offered.
    """
    from dataq.services.inspect import suggest

    path = tmp_path / "flags.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "store_and_fwd_flag", "fare"])
        for i in range(60):
            w.writerow([f"2016-01-{i % 28 + 1:02d} 09:00:00", "YN"[i % 2], i])
    ds = run_op(op="import", uri=str(path), name="flags")

    assert not [s for s in suggest(app_ctx, ds)
                if s.action.get("plugin_id") == "enrich.features"]


def test_the_actor_is_the_high_cardinality_column(app_ctx, run_op, tmp_path):
    """Cardinality picks the actor, not whether the column has a semantic type.

    Detection gives up on high-cardinality columns, so a real user id comes back
    untyped while the three-value activity_type beside it is a confident
    `categorical`. Preferring typed columns therefore picks the category over
    the actor -- which is what this dataset would do.
    """
    from dataq.core.profile import entity_columns
    from dataq.services.inspect import suggest

    path = tmp_path / "many.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["user", "ts", "activity_type"])
        for i in range(2000):
            w.writerow([f"u{i % 400}", f"2016-01-{i % 28 + 1:02d} 09:00:00",
                        ["login", "click", "buy"][i % 3]])
    ds = run_op(op="import", uri=str(path), name="many")

    profile = app_ctx.catalog.get_profile(ds)
    assert profile.column("activity_type").semantic_type == "categorical"
    assert [c.name for c in entity_columns(profile)][0] == "user"

    enrich = next(s for s in suggest(app_ctx, ds)
                  if s.action.get("plugin_id") == "enrich.features")
    assert "user" in enrich.title


def test_a_truncated_feature_table_is_refused(app_ctx, run_op, tmp_path, monkeypatch):
    """A feature table that stops at a round number is a wrong answer.

    Keyed by (user, activity, day) a real one runs to tens of millions of rows.
    Truncated, it looks complete and then annotates only part of the events it
    is joined back to -- which is how 60% of five million rows once came back
    NULL with the job reporting success.
    """
    import dataq.plugins.builtin.features as feat

    path = tmp_path / "many.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["user", "ts", "activity_type"])
        for i in range(300):
            w.writerow([f"u{i}", f"2016-01-{i % 28 + 1:02d} 09:00:00", "login"])
    ds = run_op(op="import", uri=str(path), name="many")

    # Force a limit low enough to truncate, standing in for the 1,000,000 the
    # IR allows at most.
    original = feat.FeatureTableAggregate.plan

    def clipped(self, ctx):
        plan = original(self, ctx)
        plan.spec.limit = 10
        return plan

    monkeypatch.setattr(feat.FeatureTableAggregate, "plan", clipped)
    job = run_failing(app_ctx, op="aggregate", plugin_id="agg.features",
                      inputs=[{"dataset_id": ds}], params={"by": ["user"]})
    assert job.status == "failed"
    assert "truncated" in job.error


def test_a_join_that_matches_nothing_is_refused(app_ctx, run_op, events, tmp_path):
    """Every attached column would be NULL, and the job would report success."""
    path = tmp_path / "strangers.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["user", "score"])
        for i in range(5):
            w.writerow([f"nobody{i}", i])
    strangers = run_op(op="import", uri=str(path), name="strangers")

    job = run_failing(app_ctx, op="transform", plugin_id="enrich.features",
                      inputs=[{"dataset_id": events}],
                      params={"from_dataset": strangers, "join_on": ["user"],
                              "prefix": "o_"})
    assert job.status == "failed"
    assert "none of" in job.error and "found a match" in job.error
