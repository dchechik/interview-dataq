"""Proposing a feature set from the shape of a table.

Knowing the feature language exists is not the same as knowing what to write, so
the editor opens with a draft. What matters here is that the draft is sensible
-- above all that it picks the right thing to call an actor, since every other
expression hangs off that choice.
"""

from __future__ import annotations

import csv

import pytest

from dataq.core import features as F
from dataq.services.feature_plan import propose


def write(path, header, rows):
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return path


@pytest.fixture
def mail(app_ctx, run_op, tmp_path):
    """The shape that prompted this: a few recipients, many senders.

    150 recipients getting 20 emails each, against 1,400 senders sending two.
    Both are email addresses, so semantics cannot tell them apart, and the
    sender has far more distinct values -- which is exactly the trap.
    """
    rows = [(f"r{i % 150:03d}@corp.example",
             f"2016-01-{i % 28 + 1:02d} 09:00:00",
             f"s{i % 1400}@out.example",
             ["US", "GB", "DE", "NL", "RU"][i % 5],
             ["mail.com", "post.net", "mx.org", "send.io", "relay.co"][i % 5],
             i % 7)
            for i in range(3000)]
    path = write(tmp_path / "mail.csv",
                 ["recipient_id", "ts", "sender_email", "country", "domain",
                  "urls"], rows)
    return run_op(op="import", uri=str(path), name="mail", params={"columns": [
        {"name": "ts", "target_type": "TIMESTAMP", "format": "%Y-%m-%d %H:%M:%S"}]})


def plan(app_ctx, dataset, **kw):
    return propose(app_ctx.catalog.get_profile(dataset), **kw)


# --------------------------------------------------------------------------- #
# choosing the actor
# --------------------------------------------------------------------------- #
def test_the_actor_is_the_one_with_a_history(app_ctx, mail):
    """Not the one with the most values.

    sender_email has seventeen times more distinct values than recipient_id and
    is the wrong answer: two events each is not a history to compare against.
    An actor needs both many of them and many events each, and the weaker of
    those is what limits it.
    """
    assert plan(app_ctx, mail).actor == "recipient_id"


def test_the_alternatives_come_with_it(app_ctx, mail):
    """The table cannot settle whether behaviour is per recipient or per sender,
    so the choice is offered rather than assumed."""
    options = {o.column for o in plan(app_ctx, mail).actor_options}
    assert {"recipient_id", "sender_email", "country"} <= options


def test_naming_a_different_actor_rewrites_the_per_actor_features(app_ctx, mail):
    """The population-wide ones do not mention an actor and should not change:
    how common a country is across everyone is the same question either way."""
    proposal = plan(app_ctx, mail, actor="sender_email")
    assert proposal.actor == "sender_email"

    # In a per-actor feature the actor is the first key, so that is what has to
    # change. Everything after it is the value being asked about.
    per_actor = [f.expression for f in proposal.features
                 if f.expression.startswith(("count()", "days_since_last()"))]
    assert per_actor
    assert all(e.split(" by ")[1].startswith("sender_email") for e in per_actor)
    assert "percentile(urls) by sender_email" in proposal.text

    # And recipient_id is now a *category* rather than the actor: "how often has
    # this sender written to this recipient" is a real question, and the right
    # one once the sender is who you are studying.
    assert "count() by sender_email, recipient_id" in proposal.text
    # while `share() by country` is unchanged -- how common a country is across
    # everyone does not depend on who you grouped the rest by.
    assert "share() by country" in proposal.text


def test_an_unusable_actor_is_refused_by_name(app_ctx, mail):
    with pytest.raises(ValueError, match="not a usable actor"):
        plan(app_ctx, mail, actor="urls")


# --------------------------------------------------------------------------- #
# what it proposes
# --------------------------------------------------------------------------- #
def test_every_categorical_gets_the_same_three_questions(app_ctx, mail):
    """Recent frequency for this actor, recency for this actor, and how common
    the value is across everyone. The last is a different claim from the first
    two, and together they are what 'unusual' usually means."""
    text = plan(app_ctx, mail).text
    assert "count() by recipient_id, country over 30d" in text
    assert "days_since_last() by recipient_id, country" in text
    assert "share() by country" in text


def test_numeric_columns_get_a_percentile_both_ways(app_ctx, mail):
    text = plan(app_ctx, mail).text
    assert "percentile(urls)" in text
    assert "percentile(urls) by recipient_id" in text


def test_the_window_is_adjustable(app_ctx, mail):
    assert "over 7d" in plan(app_ctx, mail, window="7d").text


def test_every_proposed_expression_actually_parses(app_ctx, mail):
    """A draft that does not run is worse than an empty box."""
    proposal = plan(app_ctx, mail)
    columns = {c.name for c in app_ctx.catalog.get_profile(mail).columns}
    for feature in F.parse_all([f.expression for f in proposal.features]):
        F.validate(feature, columns, proposal.time_column)


def test_it_reports_what_the_set_will_cost(app_ctx, mail):
    """Sorts, not features: that is the number that predicts the wait."""
    proposal = plan(app_ctx, mail)
    # Sorts are shared between features that agree on partition and order, so
    # the count is at least one and never more than the number of features.
    assert 0 < proposal.distinct_windows <= len(proposal.features)


def test_every_feature_explains_itself(app_ctx, mail):
    assert all(f.explains for f in plan(app_ctx, mail).features)


# --------------------------------------------------------------------------- #
# when there is nothing to propose
# --------------------------------------------------------------------------- #
def test_a_table_with_no_usable_clock_says_so(app_ctx, run_op, tmp_path):
    """Imported without parsing the dates, so the column is still text.

    A 12-hour clock, because DuckDB's sniffer converts a bare MM/DD/YYYY to a
    DATE by itself and there would be nothing to demonstrate.
    """
    rows = [(f"r{i % 20}", f"{i % 12 + 1:02d}/{i % 27 + 1:02d}/2016 02:05 PM",
             ["US", "GB", "DE"][i % 3]) for i in range(200)]
    ds = run_op(op="import", name="untimed",
                uri=str(write(tmp_path / "u.csv", ["who", "when", "country"], rows)))

    proposal = plan(app_ctx, ds)
    assert proposal.features == []
    assert "time column" in proposal.blocked
    assert "clean-up" in proposal.blocked, "and points at the fix"


def test_a_table_with_nothing_to_summarise_says_so(app_ctx, run_op, tmp_path):
    rows = [(f"r{i % 20}", f"2016-01-{i % 28 + 1:02d} 09:00:00") for i in range(200)]
    ds = run_op(op="import", name="bare", uri=str(write(
        tmp_path / "b.csv", ["who", "ts"], rows)))
    proposal = plan(app_ctx, ds)
    assert proposal.features == []
    assert "recur" in proposal.blocked


# --------------------------------------------------------------------------- #
# the draft runs
# --------------------------------------------------------------------------- #
def test_the_proposal_can_be_run_as_given(app_ctx, run_op, mail):
    """The whole point: open the editor, press Run, get columns."""
    proposal = plan(app_ctx, mail)
    run_op(op="transform", plugin_id="enrich.features",
           inputs=[{"dataset_id": mail}],
           params={"features": [f.expression for f in proposal.features]})

    after = app_ctx.catalog.get_profile(mail)
    assert after.row_count == 3000, "features annotate, they do not reshape"
    assert after.column("share_by_country") is not None
    assert after.column("percentile_urls_by_recipient_id") is not None


# --------------------------------------------------------------------------- #
# a browsing log: many values, all of them recurring
# --------------------------------------------------------------------------- #
def _profile(rows: int, columns: dict[str, tuple[int, str, str, str | None]]):
    """Build a profile straight from counts, so a real schema can be tested
    without carrying the millions of rows it came from."""
    from dataq.core.profile import ColumnProfile, ColumnStats, DatasetProfile

    return DatasetProfile(
        dataset_id="x", version=1, row_count=rows,
        columns=[
            ColumnProfile(
                name=name, physical_type=ptype, semantic_type=sem, role=role,
                stats=ColumnStats(name=name, physical_type=ptype, row_count=rows,
                                  distinct_count=distinct),
            )
            for name, (distinct, ptype, role, sem) in columns.items()
        ],
    )


HTTP_LOG = _profile(3_451_665, {
    "id":   (3_451_665, "VARCHAR",   "key",       "identity.key"),
    "date": (1_598_640, "TIMESTAMP", "time",      "time.timestamp"),
    "user": (    1_064, "VARCHAR",   "dimension", None),
    "pc":   (    1_161, "VARCHAR",   "dimension", None),
    "url":  (  110_491, "VARCHAR",   "dimension", "identity.url"),
})


def test_a_hundred_thousand_urls_is_still_a_category():
    """Each URL appears about thirty times, so "has this user been here before"
    is a real question. An absolute cap on distinct values called it an
    identifier and proposed nothing at all."""
    text = propose(HTTP_LOG).text
    assert "count() by user, url over 30d" in text
    assert "days_since_last() by user, url" in text
    assert "share() by url" in text


def test_a_column_with_a_value_per_row_is_not_a_category():
    """id is unique: its share is 1/N everywhere and nobody has seen the same
    one twice."""
    assert "id" not in propose(HTTP_LOG).text


def test_the_actor_is_the_one_named_like_one():
    """user and pc have a thousand values each and a few thousand events each;
    they differ by nine percent, which is noise. The name is the only remaining
    evidence, and it is the right evidence."""
    proposal = propose(HTTP_LOG)
    assert proposal.actor == "user"
    assert "count() by user, pc over 30d" in proposal.text, \
        "and the machine becomes a category, which is the interesting pairing"


def test_the_name_nudge_does_not_override_the_numbers():
    """A column that is genuinely a better actor still wins without the name."""
    log = _profile(100_000, {
        "ts":       (50_000, "TIMESTAMP", "time",      "time.timestamp"),
        "user":     (     3, "VARCHAR",   "dimension", None),
        "device_id": (2_000, "VARCHAR",   "dimension", None),
    })
    assert propose(log).actor == "device_id"


def test_nothing_recurs_is_reported_as_that(app_ctx):
    """The message has to name the actual reason, not a number nobody set."""
    log = _profile(500, {
        "ts":     (500, "TIMESTAMP", "time",      "time.timestamp"),
        "who":    ( 50, "VARCHAR",   "dimension", None),
        "ref":    (498, "VARCHAR",   "dimension", None),
    })
    proposal = propose(log)
    assert proposal.actor == "who"
    assert proposal.features == []
    assert "recur" in proposal.blocked
