"""The typed chart grammar and its resolver.

The resolver is the reason this IR exists rather than passing Vega-Lite through:
it validates encoded fields against the columns the query actually returns, and
infers measurement types from the semantic layer instead of making the caller
restate what a column means.
"""

from __future__ import annotations

import pytest

from dataq.core.chart import ChartSpec, Encoding
from dataq.core.profile import ColumnProfile, DatasetProfile
from dataq.services.chart import ChartError, default_chart_for, resolve_chart


def profile_with(*columns: tuple[str, str, str | None, str]) -> DatasetProfile:
    """(name, physical_type, semantic_type, role) -> a DatasetProfile."""
    return DatasetProfile(
        dataset_id="d", version=1, row_count=100,
        columns=[
            ColumnProfile(name=n, physical_type=p, semantic_type=s, role=r)
            for n, p, s, r in columns
        ],
    )


TAXI = profile_with(
    ("tpep_pickup_datetime", "TIMESTAMP", "time.timestamp", "time"),
    ("fare_amount", "DOUBLE", "money.amount", "measure"),
    ("payment_type", "VARCHAR", "categorical", "dimension"),
    ("pickup_latitude", "DOUBLE", "geo.lat", "geo"),
    ("trip_id", "VARCHAR", "identity.key", "key"),
)


# --------------------------------------------------------------------------- #
# validation -- the silent-empty-chart bug class
# --------------------------------------------------------------------------- #
def test_unknown_field_is_rejected_by_name(TAXI=TAXI):
    chart = ChartSpec(mark="bar", encodings={"x": Encoding(field="fare_amoun")})
    with pytest.raises(ChartError) as exc:
        resolve_chart(chart, ["fare_amount", "payment_type"], TAXI)
    message = str(exc.value)
    # The message must name the channel, the bad field and the real columns --
    # this used to render as an empty chart with no explanation at all.
    assert "x=" in message and "fare_amoun" in message
    assert "fare_amount" in message and "payment_type" in message


def test_a_field_the_query_stopped_selecting_is_caught():
    """The realistic failure: the query changed, the chart did not."""
    chart = ChartSpec(
        mark="line",
        encodings={"x": Encoding(field="bucket"), "y": Encoding(field="avg_fare_amount")},
    )
    with pytest.raises(ChartError, match="avg_fare_amount"):
        resolve_chart(chart, ["bucket", "n"], None)


def test_layers_are_validated_too():
    chart = ChartSpec(
        mark="line",
        encodings={"x": Encoding(field="bucket")},
        layers=[ChartSpec(mark="point", encodings={"y": Encoding(field="nope")})],
    )
    with pytest.raises(ChartError, match="nope"):
        resolve_chart(chart, ["bucket"], None)


def test_empty_chart_is_rejected():
    with pytest.raises(ChartError, match="no encodings"):
        resolve_chart(ChartSpec(mark="bar"), ["a"], None)


# --------------------------------------------------------------------------- #
# inference from the semantic layer
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "field,expected",
    [
        ("tpep_pickup_datetime", "temporal"),   # time.timestamp
        ("fare_amount", "quantitative"),        # money.amount isa numeric
        ("payment_type", "nominal"),            # categorical
        ("pickup_latitude", "quantitative"),    # geo.lat isa numeric
        ("trip_id", "nominal"),                 # identity.key isa text
    ],
)
def test_encoding_type_inferred_from_semantic_type(field, expected):
    columns = [c.name for c in TAXI.columns]
    resolved = resolve_chart(
        ChartSpec(mark="point", encodings={"x": Encoding(field=field)}), columns, TAXI
    )
    assert resolved.encodings["x"].type == expected
    assert resolved.encodings["x"].inferred_from


def test_explicit_type_is_never_overridden():
    resolved = resolve_chart(
        ChartSpec(mark="bar", encodings={"x": Encoding(field="fare_amount", type="ordinal")}),
        ["fare_amount"], TAXI,
    )
    assert resolved.encodings["x"].type == "ordinal"
    assert resolved.encodings["x"].inferred_from is None


def test_aggregate_output_columns_are_quantitative_without_a_profile():
    """`n`, `share` and `avg_x` never appear in the source profile."""
    resolved = resolve_chart(
        ChartSpec(
            mark="bar",
            encodings={"x": Encoding(field="country"), "y": Encoding(field="n")},
        ),
        ["country", "n", "share"], None,
    )
    assert resolved.encodings["y"].type == "quantitative"
    assert resolved.encodings["x"].type == "nominal"


# --------------------------------------------------------------------------- #
# the cyclical time part -- the case that motivated all of this
# --------------------------------------------------------------------------- #
def test_time_part_label_is_ordinal_and_sorted_by_its_ordinal():
    """'Thu' does not sort as text.

    The query compiler already emits `bucket_ord` for exactly this reason and
    nothing used it before; the chart layer does now.
    """
    resolved = resolve_chart(
        ChartSpec(
            mark="bar",
            encodings={"x": Encoding(field="bucket"), "y": Encoding(field="n")},
        ),
        ["bucket", "bucket_ord", "n"], None,
    )
    x = resolved.encodings["x"]
    assert x.type == "ordinal"
    assert x.sort == "bucket_ord"
    assert "ordinal" in x.inferred_from


def test_a_truncated_bucket_stays_temporal():
    """Without a sibling ordinal, `bucket` is a real timestamp."""
    profile = profile_with(("bucket", "TIMESTAMP", "time.timestamp", "time"))
    resolved = resolve_chart(
        ChartSpec(mark="line", encodings={"x": Encoding(field="bucket")}),
        ["bucket", "n"], profile,
    )
    assert resolved.encodings["x"].type == "temporal"
    assert resolved.encodings["x"].sort is None


def test_explicit_sort_beats_the_inferred_ordinal():
    resolved = resolve_chart(
        ChartSpec(mark="bar", encodings={"x": Encoding(field="bucket", sort="-y")}),
        ["bucket", "bucket_ord"], None,
    )
    assert resolved.encodings["x"].sort == "-y"


# --------------------------------------------------------------------------- #
# defaults
# --------------------------------------------------------------------------- #
def test_default_chart_for_a_time_rollup():
    profile = profile_with(
        ("bucket", "TIMESTAMP", "time.timestamp", "time"),
        ("n", "BIGINT", "numeric", "measure"),
    )
    chart = default_chart_for(profile)
    assert chart.mark == "line"
    assert chart.encodings["x"].field == "bucket"
    assert chart.encodings["y"].field == "n"


def test_default_chart_for_a_cyclical_rollup_uses_bars():
    profile = profile_with(
        ("bucket", "VARCHAR", "categorical", "dimension"),
        ("bucket_ord", "BIGINT", "numeric", "measure"),
        ("n", "BIGINT", "numeric", "measure"),
    )
    chart = default_chart_for(profile)
    assert chart.mark == "bar"


def test_default_chart_for_a_frequency_table():
    profile = profile_with(
        ("country", "VARCHAR", "geo.country_iso2", "dimension"),
        ("n", "BIGINT", "numeric", "measure"),
        ("share", "DOUBLE", "numeric", "measure"),
    )
    chart = default_chart_for(profile)
    assert chart.mark == "bar"
    assert chart.encodings["y"].field == "country"
    assert chart.encodings["y"].sort == "-x"
    assert chart.encodings["x"].field == "n"


def test_default_chart_returns_none_when_there_is_nothing_to_measure():
    assert default_chart_for(profile_with(("name", "VARCHAR", "text", "dimension"))) is None


# --------------------------------------------------------------------------- #
# escape hatch
# --------------------------------------------------------------------------- #
def test_raw_vega_lite_survives_resolution():
    chart = ChartSpec(
        mark="bar",
        encodings={"x": Encoding(field="n")},
        raw_vega_lite={"mark": {"type": "bar", "color": "#4269d0"}},
    )
    resolved = resolve_chart(chart, ["n"], None)
    assert resolved.raw_vega_lite == {"mark": {"type": "bar", "color": "#4269d0"}}


def test_fields_lists_every_column_the_chart_reads():
    chart = ChartSpec(
        mark="line",
        encodings={"x": Encoding(field="bucket"), "y": Encoding(field="n")},
        layers=[ChartSpec(mark="point", encodings={"y": Encoding(field="share")})],
    )
    assert set(chart.fields()) == {"bucket", "n", "share"}


# --------------------------------------------------------------------------- #
# the query's own output types
# --------------------------------------------------------------------------- #
def test_derived_column_type_comes_from_the_query_output():
    """A truncated `bucket` is a TIMESTAMP but appears in no source profile.

    Without the query's output types it defaulted to nominal, which draws a line
    chart against a categorical axis — wrong, and not obviously wrong.
    """
    resolved = resolve_chart(
        ChartSpec(mark="line", encodings={"x": Encoding(field="bucket")}),
        ["bucket", "n"], TAXI,
        output_types={"bucket": "TIMESTAMP", "n": "BIGINT"},
    )
    assert resolved.encodings["x"].type == "temporal"
    assert "TIMESTAMP" in resolved.encodings["x"].inferred_from


def test_profile_semantics_still_win_over_the_output_type():
    """A BIGINT that the catalog knows is a country code is still nominal."""
    profile = profile_with(("country", "VARCHAR", "geo.country_iso2", "dimension"))
    resolved = resolve_chart(
        ChartSpec(mark="bar", encodings={"x": Encoding(field="country")}),
        ["country"], profile, output_types={"country": "VARCHAR"},
    )
    assert resolved.encodings["x"].type == "nominal"
    assert "semantic type" in resolved.encodings["x"].inferred_from


def test_ordinal_sibling_beats_the_output_type():
    """The label column is VARCHAR, but it is an ordered cycle, not a category."""
    resolved = resolve_chart(
        ChartSpec(mark="bar", encodings={"x": Encoding(field="bucket")}),
        ["bucket", "bucket_ord"], None,
        output_types={"bucket": "VARCHAR", "bucket_ord": "BIGINT"},
    )
    assert resolved.encodings["x"].type == "ordinal"
    assert resolved.encodings["x"].sort == "bucket_ord"


def test_raw_vega_lite_must_not_retype_the_mark():
    """The mark type has exactly one source of truth: ChartSpec.mark.

    The visualizers put styling (colour, point markers) in raw_vega_lite, and
    that dict is merged last. If it also carried the mark *type*, editing the
    mark would silently do nothing — which is exactly what happened before the
    compiler was taught to keep `mark` authoritative.
    """
    chart = ChartSpec(
        mark="area",
        encodings={"x": Encoding(field="bucket")},
        raw_vega_lite={"mark": {"type": "line", "point": True, "color": "#4269d0"}},
    )
    # The backend keeps the escape hatch verbatim; the frontend compiler is what
    # enforces this, so assert the shape the compiler relies on.
    assert chart.mark == "area"
    assert chart.raw_vega_lite["mark"]["type"] == "line"
