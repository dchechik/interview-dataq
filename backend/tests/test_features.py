"""The feature IR, its shorthand, and the SQL it compiles to.

The parser is the part with the most ways to be subtly wrong, so it gets the
most cases. The SQL is checked by running it against a fixture small enough to
work out by hand -- five events whose every feature value is stated in the test
rather than computed by the thing under test.
"""

from __future__ import annotations

import duckdb
import pytest

from dataq.core.features import (
    Feature,
    FeatureError,
    Window,
    coerce,
    distinct_windows,
    parse,
    parse_all,
    parse_duration,
    to_sql,
    validate,
)

COLUMNS = {"user", "ts", "activity_type", "location", "amount"}


# --------------------------------------------------------------------------- #
# the shorthand
# --------------------------------------------------------------------------- #
def test_the_five_worked_examples_parse():
    """The features from the original question, verbatim."""
    feats = parse_all("""
        share()            by activity_type
        count()            by user, activity_type over 30d
        count()            by user, activity_type in day
        count()            by user, location      over 30d
        days_since_last()  by user, activity_type
    """)
    assert [f.fn for f in feats] == [
        "share", "count", "count", "count", "days_since_last"]
    assert [f.output_name() for f in feats] == [
        "share_by_activity_type",
        "n_by_user_activity_type_30d",
        "n_by_user_activity_type_per_day",
        "n_by_user_location_30d",
        "days_since_last_by_user_activity_type",
    ]


def test_a_column_argument():
    f = parse("avg(amount) by user over 7d")
    assert (f.fn, f.column, f.by) == ("avg", "amount", ["user"])
    assert f.window.kind == "trailing" and f.window.duration == "7d"


def test_an_explicit_name_wins():
    assert parse("avg(amount) by user over 7d as spend_7d").output_name() == "spend_7d"


def test_no_window_means_the_whole_dataset():
    assert parse("count() by user").window.kind == "all"


def test_excluding_current_is_understood():
    f = parse("count() by user over 30d excluding current")
    assert f.window.include_current is False
    assert parse("count() by user over 30d").window.include_current is True


def test_n_is_an_alias_for_count():
    assert parse("n() by user").fn == "count"


def test_comments_and_blank_lines_are_skipped():
    feats = parse_all("""
        # how common is each activity
        share() by activity_type

        count() by user  # this user's total
    """)
    assert len(feats) == 2


@pytest.mark.parametrize("text,unit", [
    ("30d", "DAY"), ("12h", "HOUR"), ("2w", "WEEK"),
    ("90m", "MINUTE"), ("6mo", "MONTH"), ("1y", "YEAR"),
])
def test_durations(text, unit):
    assert parse_duration(text)[1] == unit


# --------------------------------------------------------------------------- #
# errors name what is wrong
# --------------------------------------------------------------------------- #
def test_an_unparseable_line_quotes_it():
    with pytest.raises(FeatureError, match="could not parse"):
        parse("count by user over 30d")          # missing ()


def test_an_unknown_function_lists_the_real_ones():
    with pytest.raises(FeatureError, match="unknown function 'frequency'"):
        parse("frequency() by user")


def test_a_bad_duration_says_what_one_looks_like():
    with pytest.raises(FeatureError, match="not a duration"):
        parse_duration("30 fortnights")


def test_the_failing_line_number_is_reported():
    """A feature set is a block; 'invalid feature' would not locate it."""
    with pytest.raises(FeatureError, match="line 3"):
        parse_all(["count() by user", "share() by activity_type", "nonsense("])


def test_two_windows_at_once_is_refused():
    with pytest.raises(FeatureError, match="pick one"):
        parse("count() by user over 30d in day")


def test_an_unknown_column_is_caught_before_sql():
    with pytest.raises(FeatureError, match="no column named 'usr'"):
        validate(parse("count() by usr"), COLUMNS, "ts")


def test_an_aggregate_without_its_column():
    with pytest.raises(FeatureError, match="needs a column"):
        validate(parse("avg() by user"), COLUMNS, "ts")


def test_count_with_a_column_is_refused_rather_than_ignored():
    with pytest.raises(FeatureError, match="takes no column"):
        validate(parse("count(amount) by user"), COLUMNS, "ts")


def test_a_sequence_feature_cannot_take_a_window():
    """'Days since last' is about two adjacent events, not a span."""
    with pytest.raises(FeatureError, match="neighbouring events"):
        validate(parse("days_since_last() by user over 30d"), COLUMNS, "ts")


def test_a_windowed_feature_without_a_time_column_explains_itself():
    with pytest.raises(FeatureError, match="normalize.timestamp"):
        validate(parse("count() by user over 30d"), COLUMNS, None)


# --------------------------------------------------------------------------- #
# leakage labelling
# --------------------------------------------------------------------------- #
def test_which_features_see_the_future():
    """Not an error -- 'how common is this overall' is a real question. But it
    is recorded, so nobody has to rediscover it."""
    assert parse("share() by activity_type").sees_future is True
    assert parse("count() by user in day").sees_future is True, "today includes later today"
    assert parse("count() by user over 30d").sees_future is False
    assert parse("days_since_last() by user").sees_future is False


def test_the_description_says_what_the_window_covers():
    assert "including events after this one" in parse("share() by user").describe()
    assert "up to and including" in parse("count() by user over 30d").describe()
    assert "before this event" in parse(
        "count() by user over 30d excluding current").describe()


# --------------------------------------------------------------------------- #
# the SQL, against hand-computed answers
# --------------------------------------------------------------------------- #
@pytest.fixture
def conn():
    c = duckdb.connect()
    # Five events chosen so every answer is checkable by eye: two logins in one
    # day, a different activity, and one login 60 days later (outside a 30-day
    # window). 2016 is a leap year, which is why the gap is 60 and not 59.
    c.execute("""CREATE TABLE ev AS SELECT * FROM (VALUES
      ('u1', TIMESTAMP '2016-01-01 09:00', 'login', 'nyc', 10.0),
      ('u1', TIMESTAMP '2016-01-01 18:00', 'login', 'nyc', 20.0),
      ('u1', TIMESTAMP '2016-01-05 09:00', 'buy',   'sfo', 30.0),
      ('u1', TIMESTAMP '2016-03-01 09:00', 'login', 'nyc', 40.0),
      ('u2', TIMESTAMP '2016-01-02 09:00', 'login', 'lon', 50.0)
    ) t(user, ts, activity_type, location, amount)""")
    return c


def compute(conn, shorthand: str) -> list:
    """Run one feature over the fixture, in event order."""
    f = parse(shorthand)
    validate(f, COLUMNS, "ts")
    sql = (f'SELECT ({to_sql(f, "ts")}) AS v FROM ev '
           f'ORDER BY "user", ts')
    return [r[0] for r in conn.execute(sql).fetchall()]


def test_global_share(conn):
    """4 of 5 events are logins, 1 is a buy."""
    assert compute(conn, "share() by activity_type") == [0.8, 0.8, 0.2, 0.8, 0.8]


def test_trailing_window_per_entity(conn):
    """The March login is 60 days after the January ones, so it resets to 1."""
    assert compute(conn, "count() by user, activity_type over 30d") == [1, 2, 1, 1, 1]


def test_trailing_window_excluding_the_current_row(conn):
    assert compute(
        conn, "count() by user, activity_type over 30d excluding current"
    ) == [0, 1, 0, 0, 0]


def test_calendar_bucket_is_not_a_trailing_window(conn):
    """Both of u1's Jan-1 logins are in the same day, so both see 2 --
    including the 09:00 row, which counts the 18:00 one that follows it."""
    assert compute(conn, "count() by user, activity_type in day") == [2, 2, 1, 1, 1]


def test_days_since_last(conn):
    """None where there is no previous event of that kind."""
    assert compute(conn, "days_since_last() by user, activity_type") == [
        None, 0, None, 60, None]


def test_days_since_first(conn):
    assert compute(conn, "days_since_first() by user") == [0, 0, 4, 60, 0]


def test_event_index(conn):
    assert compute(conn, "event_index() by user") == [1, 2, 3, 4, 1]


def test_a_measure_over_a_window(conn):
    assert compute(conn, "sum(amount) by user, activity_type over 30d") == [
        10.0, 30.0, 30.0, 40.0, 50.0]


def test_count_distinct_over_a_window(conn):
    """u1 visits nyc then sfo; the trailing window sees both by Jan 5."""
    assert compute(conn, "count_distinct(location) by user over 30d") == [1, 1, 2, 1, 1]


def test_no_partition_means_the_whole_dataset(conn):
    assert compute(conn, "count()") == [5, 5, 5, 5, 5]


def test_a_null_partition_value_groups_together(conn):
    """Worth pinning: rows with an unknown entity are counted as one entity,
    not as one each. Surprising, but it is what SQL does and what the docs say."""
    conn.execute("INSERT INTO ev VALUES (NULL, TIMESTAMP '2016-01-01', 'login', 'x', 1.0),"
                 "(NULL, TIMESTAMP '2016-01-02', 'login', 'x', 1.0)")
    values = compute(conn, "count() by user")
    assert values[-2:] == [2, 2]


# --------------------------------------------------------------------------- #
# cost
# --------------------------------------------------------------------------- #
def test_features_sharing_a_window_share_a_sort():
    """The number that predicts the wait: five features over one window cost
    one sort, because the generated OVER text is identical."""
    feats = parse_all([
        "count() by user over 30d", "sum(amount) by user over 30d",
        "avg(amount) by user over 30d", "max(amount) by user over 30d",
    ])
    assert distinct_windows(feats, "ts") == 1

    spread = parse_all([
        "count() by user over 30d", "count() by location over 30d",
        "count() by activity_type",
    ])
    assert distinct_windows(spread, "ts") == 3


# --------------------------------------------------------------------------- #
# both notations
# --------------------------------------------------------------------------- #
def test_the_ir_and_the_shorthand_mean_the_same_thing():
    """The shorthand is for people; the IR is what the agent and API exchange."""
    typed = Feature(fn="count", by=["user", "activity_type"],
                    window=Window(kind="trailing", duration="30d"))
    written = parse("count() by user, activity_type over 30d")
    assert typed == written
    assert to_sql(typed, "ts") == to_sql(written, "ts")


def test_coerce_accepts_strings_dicts_and_models():
    feats = coerce([
        "count() by user",
        {"fn": "share", "by": ["activity_type"]},
        Feature(fn="event_index", by=["user"]),
    ])
    assert [f.fn for f in feats] == ["count", "share", "event_index"]


# --------------------------------------------------------------------------- #
# percentile
# --------------------------------------------------------------------------- #
def test_percentile_places_a_value_in_its_distribution(conn):
    """Ties share a value, and it reads as 'the fraction at or below this'."""
    assert compute(conn, "percentile(amount)") == [0.2, 0.4, 0.6, 0.8, 1.0]


def test_percentile_within_an_entity(conn):
    """u1's four amounts rank against each other, not against u2's."""
    assert compute(conn, "percentile(amount) by user") == [0.25, 0.5, 0.75, 1.0, 1.0]


def test_a_missing_value_has_no_percentile(conn):
    """NULL sorts last, so cume_dist alone would report a missing value as the
    most extreme thing in the column."""
    conn.execute("INSERT INTO ev VALUES ('u3', TIMESTAMP '2016-01-09', 'login', 'x', NULL)")
    assert compute(conn, "percentile(amount)")[-1] is None


def test_percentile_cannot_take_a_window():
    with pytest.raises(FeatureError, match="whole column"):
        validate(parse("percentile(amount) over 30d"), COLUMNS, "ts")


def test_percentile_needs_a_column():
    with pytest.raises(FeatureError, match="needs a column"):
        validate(parse("percentile()"), COLUMNS, "ts")
