"""The timeline view.

A timeline is a filtered slice of raw events in time order. The parts worth
testing are the ones that are easy to get subtly wrong: that the query stays
un-aggregated and sorted, that defaults come from the semantic layer rather than
column names, and that the abnormality rule finds the rarity column a join put
there.
"""

from __future__ import annotations

import pytest

from dataq.core.timeline import AbnormalityRule, EventAttribute, TimelineSpec
from dataq.services.chart import ChartError, resolve_timeline
from dataq.services.inspect import render_viz

from .fixtures import write_auth_csv, write_taxi_csv


@pytest.fixture
def auth(app_ctx, run_op, tmp_path):
    return run_op(op="import", uri=str(write_auth_csv(tmp_path / "a.csv", rows=800)),
                  name="auth")


@pytest.fixture
def annotated(app_ctx, run_op, auth):
    """Auth events joined to a country frequency table.

    This is the shape the abnormality rule exists for: every event carries how
    common its own country is.
    """
    freq = run_op(op="aggregate", plugin_id="agg.frequency",
                  inputs=[{"dataset_id": auth}], params={"column": "country"},
                  output_name="country_freq")
    return run_op(op="join", inputs=[{"dataset_id": auth}, {"dataset_id": freq}],
                  params={"left_column": "country", "right_column": "country"},
                  output_name="auth_annotated")


BASE = {"time_column": "ts", "title_column": "action"}


# --------------------------------------------------------------------------- #
# the query a timeline builds
# --------------------------------------------------------------------------- #
def test_the_query_is_not_aggregated(app_ctx, auth):
    """A timeline shows individual events; aggregating would defeat it."""
    out = render_viz(app_ctx, "viz.timeline", auth, BASE)
    assert not out.spec.query.is_aggregate
    assert out.spec.query.group_by == []
    assert out.spec.query.time_bucket is None


def test_events_come_back_in_time_order(app_ctx, auth):
    out = render_viz(app_ctx, "viz.timeline", auth, BASE)
    assert out.spec.query.order_by[0].column == "ts"
    assert out.spec.query.order_by[0].desc is True
    times = [r["ts"] for r in out.data]
    assert times == sorted(times, reverse=True)

    ascending = render_viz(app_ctx, "viz.timeline", auth, {**BASE, "descending": False})
    times = [r["ts"] for r in ascending.data]
    assert times == sorted(times)


def test_only_the_rendered_columns_are_selected(app_ctx, auth):
    """A timeline over a wide table should not ship every column per row."""
    out = render_viz(app_ctx, "viz.timeline", auth, BASE)
    selected = {s.column for s in out.spec.query.select}
    assert selected == set(out.spec.timeline.columns())
    assert "ts" in selected and "action" in selected


def test_filtering_to_a_subject(app_ctx, auth):
    """The core use case: everything for one user."""
    everything = render_viz(app_ctx, "viz.timeline", auth, {**BASE, "limit": 5000})
    subject = everything.data[0]["user_email"]

    out = render_viz(app_ctx, "viz.timeline", auth, {
        **BASE, "limit": 5000,
        "filters": [{"column": "user_email", "op": "=", "value": subject}],
    })
    assert out.row_count > 0
    assert {r["user_email"] for r in out.data} == {subject}
    assert out.row_count < everything.row_count
    assert subject in out.spec.title


def test_a_non_temporal_column_explains_itself(app_ctx, auth):
    with pytest.raises(ValueError, match="not a date or timestamp"):
        render_viz(app_ctx, "viz.timeline", auth, {"time_column": "action"})
    with pytest.raises(ValueError, match="no column named"):
        render_viz(app_ctx, "viz.timeline", auth, {"time_column": "nope"})


# --------------------------------------------------------------------------- #
# defaults from the semantic layer
# --------------------------------------------------------------------------- #
def test_subjects_are_highlighted_and_filterable(app_ctx, auth):
    out = render_viz(app_ctx, "viz.timeline", auth, BASE)
    by_column = {a.column: a for a in out.spec.timeline.attributes}

    # Identity-typed columns you would pivot on.
    for name in ("user_email", "src_ip", "country"):
        assert by_column[name].highlight, name
        assert by_column[name].filterable, name


def test_a_per_row_key_is_not_offered_as_a_subject(app_ctx, auth):
    """event_id is identity-typed but unique per row.

    Filtering by it yields a timeline of one event, which is not a timeline --
    so it stays a plain attribute rather than a pivot.
    """
    out = render_viz(app_ctx, "viz.timeline", auth, BASE)
    event_id = next(a for a in out.spec.timeline.attributes if a.column == "event_id")
    assert not event_id.filterable
    assert not event_id.highlight


def test_the_time_and_title_columns_are_not_repeated_as_attributes(app_ctx, auth):
    out = render_viz(app_ctx, "viz.timeline", auth, BASE)
    columns = {a.column for a in out.spec.timeline.attributes}
    assert "ts" not in columns
    assert "action" not in columns


def test_explicit_attributes_win_over_the_defaults(app_ctx, auth):
    out = render_viz(app_ctx, "viz.timeline", auth, {
        **BASE, "attributes": ["country", "success"], "highlight": ["country"],
    })
    attributes = out.spec.timeline.attributes
    assert [a.column for a in attributes] == ["country", "success"]
    assert attributes[0].highlight and not attributes[1].highlight


# --------------------------------------------------------------------------- #
# abnormality -- the payoff of aggregate-then-join
# --------------------------------------------------------------------------- #
def test_no_abnormality_rule_without_a_rarity_column(app_ctx, auth):
    assert render_viz(app_ctx, "viz.timeline", auth, BASE).spec.timeline.abnormality is None


def test_a_joined_share_column_becomes_the_abnormality_rule(app_ctx, annotated):
    out = render_viz(app_ctx, "viz.timeline", annotated, BASE)
    rule = out.spec.timeline.abnormality
    assert rule is not None
    assert rule.column == "share"
    assert rule.op == "<"
    assert rule.rationale, "a highlight the user cannot explain looks arbitrary"
    # And the column it reads is actually fetched.
    assert "share" in {s.column for s in out.spec.query.select}


def test_the_rule_actually_separates_rare_from_common(app_ctx, annotated):
    """The fixture is deliberately skewed: US is ~70%, NG ~1%.

    The threshold is derived from the data rather than hardcoded, so this tests
    the mechanism instead of whether the 1% default happens to land between two
    countries at this particular row count.
    """
    out = render_viz(app_ctx, "viz.timeline", annotated, {**BASE, "limit": 5000})
    shares = {r["country"]: r["share"] for r in out.data}
    rarest = min(shares, key=lambda c: shares[c])
    threshold = shares[rarest] * 1.5

    tuned = render_viz(app_ctx, "viz.timeline", annotated, {
        **BASE, "limit": 5000, "abnormality_value": threshold,
    })
    rule = tuned.spec.timeline.abnormality
    flagged = {r["country"] for r in tuned.data if r[rule.column] < rule.value}
    common = {r["country"] for r in tuned.data if r[rule.column] >= rule.value}

    assert rarest in flagged, "the rarest value must trip the rule"
    assert "US" in common, "the dominant value must not"
    assert 0 < len(flagged) < len(shares)


def test_an_explicit_rule_overrides_the_inferred_one(app_ctx, annotated):
    out = render_viz(app_ctx, "viz.timeline", annotated, {
        **BASE, "abnormality_column": "share", "abnormality_op": ">",
        "abnormality_value": 0.5, "abnormality_label": "very common",
    })
    rule = out.spec.timeline.abnormality
    assert (rule.op, rule.value, rule.label) == (">", 0.5, "very common")


# --------------------------------------------------------------------------- #
# resolution against the query's real output
# --------------------------------------------------------------------------- #
def test_a_missing_time_column_is_an_error_not_a_blank_list():
    spec = TimelineSpec(time_column="ts")
    with pytest.raises(ChartError, match="ordered by"):
        resolve_timeline(spec, ["action", "country"], None)


def test_a_missing_abnormality_column_is_an_error():
    spec = TimelineSpec(
        time_column="ts",
        abnormality=AbnormalityRule(column="share", op="<", value=0.01),
    )
    with pytest.raises(ChartError, match="abnormality rule"):
        resolve_timeline(spec, ["ts", "action"], None)


def test_a_missing_attribute_is_dropped_rather_than_fatal():
    """Losing a chip is recoverable; losing the timeline is not."""
    spec = TimelineSpec(
        time_column="ts",
        title_column="gone",
        attributes=[EventAttribute(column="country"), EventAttribute(column="missing")],
    )
    resolved = resolve_timeline(spec, ["ts", "country"], None)
    assert [a.column for a in resolved.attributes] == ["country"]
    assert resolved.title_column is None


# --------------------------------------------------------------------------- #
# it works on any dataset with a time column, not just auth logs
# --------------------------------------------------------------------------- #
def test_a_timeline_of_taxi_trips(app_ctx, run_op, tmp_path):
    ds = run_op(op="import", uri=str(write_taxi_csv(tmp_path / "t.csv", rows=300)),
                name="taxi")
    run_op(op="transform", plugin_id="normalize.timestamp",
           inputs=[{"dataset_id": ds}], params={"column": "tpep_pickup_datetime"})

    out = render_viz(app_ctx, "viz.timeline", ds, {
        "time_column": "tpep_pickup_datetime", "title_column": "payment_type",
    })
    assert out.row_count > 0
    assert out.spec.renderer == "timeline"
    assert out.spec.timeline.time_column == "tpep_pickup_datetime"


# --------------------------------------------------------------------------- #
# discoverability
# --------------------------------------------------------------------------- #
def test_a_timeline_is_suggested_for_any_dataset_with_a_time_column(app_ctx, auth):
    from dataq.services.inspect import suggest

    timelines = [s for s in suggest(app_ctx, auth, kinds=("viz",))
                 if s.action.get("plugin_id") == "viz.timeline"]
    assert timelines, "a dataset of timestamped rows should offer a timeline"
    assert timelines[0].action["params"]["time_column"] == "ts"


def test_an_annotated_dataset_ranks_the_timeline_above_the_charts(app_ctx, annotated):
    """When rarity is present the timeline can point at what stands out."""
    from dataq.services.inspect import suggest

    viz = suggest(app_ctx, annotated, kinds=("viz",))
    top = viz[0]
    assert top.action["plugin_id"] == "viz.timeline"
    assert "unusual" in top.title
    assert "how common" in top.rationale


def test_replaying_the_suggestion_renders(app_ctx, annotated):
    """A suggestion is an executable payload, so it must actually run."""
    from dataq.services.inspect import suggest

    action = next(s.action for s in suggest(app_ctx, annotated, kinds=("viz",))
                  if s.action.get("plugin_id") == "viz.timeline")
    out = render_viz(app_ctx, action["plugin_id"], action["dataset_id"], action["params"])
    assert out.spec.renderer == "timeline"
    assert out.row_count > 0
    assert out.spec.timeline.abnormality is not None


def test_the_threshold_control_actually_moves_the_threshold(app_ctx, annotated):
    """An explicit value used to be honoured only if the column was named too.

    The UI's threshold input sends the value alone, so dragging it did nothing
    at all -- which is worse than having no control, because it looks like an
    answer to the question it silently ignores.
    """
    default = render_viz(app_ctx, "viz.timeline", annotated, BASE).spec.timeline
    assert default.abnormality.value == 0.01

    loosened = render_viz(app_ctx, "viz.timeline", annotated,
                          {**BASE, "abnormality_value": 0.05}).spec.timeline
    assert loosened.abnormality.value == 0.05
    assert loosened.abnormality.column == default.abnormality.column
    assert loosened.abnormality.op == default.abnormality.op
    # And the explanation moves with it, rather than still claiming 1%.
    assert "5%" in loosened.abnormality.rationale


def test_a_looser_threshold_flags_more_events(app_ctx, annotated):
    """The check that matters: the number highlighted actually changes."""
    def flagged(value):
        out = render_viz(app_ctx, "viz.timeline", annotated,
                         {**BASE, "limit": 5000, "abnormality_value": value})
        rule = out.spec.timeline.abnormality
        return sum(1 for r in out.data if r[rule.column] < rule.value)

    assert flagged(0.5) > flagged(0.01)
