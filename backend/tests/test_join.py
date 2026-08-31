"""Joining two datasets on a key you choose.

The join op has always accepted a key, but only a suggester ever supplied one,
and a suggester only ever proposes a single pair of columns that share a meaning.
Letting a person pick the key reaches two cases the suggested path never did: a
key that needs more than one column, and a right side whose column names are
already taken on the left.
"""

from __future__ import annotations

import csv

import pytest

from dataq.services.join_plan import candidates, preview
from dataq.services.operations import JoinParams, OperationRequest, submit_operation

from .fixtures import write_auth_csv


@pytest.fixture
def auth(app_ctx, run_op, tmp_path):
    return run_op(op="import", uri=str(write_auth_csv(tmp_path / "auth.csv", rows=400)),
                  name="auth")


@pytest.fixture
def per_country_action(app_ctx, run_op, auth):
    """A table keyed on two columns: neither is unique, the pair is."""
    return run_op(op="aggregate", plugin_id="agg.features",
                  inputs=[{"dataset_id": auth}],
                  params={"by": ["country", "action"]}, output_name="pairs")


def run_failing(app_ctx, **kwargs):
    """Submit an operation expected to fail; return the job."""
    accepted = submit_operation(app_ctx, OperationRequest(**kwargs))
    app_ctx.runner.wait(accepted.job_id, timeout=120)
    return app_ctx.catalog.get_job(accepted.job_id)


def rows_of(app_ctx, dataset_id: int) -> int:
    return app_ctx.catalog.get_profile(dataset_id).row_count


def columns_of(app_ctx, dataset_id) -> list[str]:
    return [c.name for c in app_ctx.catalog.get_profile(dataset_id).columns]


# --------------------------------------------------------------------------- #
# the key
# --------------------------------------------------------------------------- #
def test_a_composite_key_annotates_what_one_column_multiplies(
    app_ctx, run_op, auth, per_country_action
):
    """The case the fan-out refusal was written for, now expressible.

    ``country`` repeats in the aggregate once per action and ``action`` once per
    country, so either alone matches several rows; the pair matches one. This is
    the whole reason the key is a list.
    """
    job = run_failing(app_ctx, op="join",
                      inputs=[{"dataset_id": auth}, {"dataset_id": per_country_action}],
                      params={"left_column": "country", "right_column": "country",
                              "prefix": "p_"})
    assert job.status == "failed" and "multiply" not in job.error
    assert "matched several" in job.error

    out = run_op(op="join",
                 inputs=[{"dataset_id": auth}, {"dataset_id": per_country_action}],
                 params={"on": [{"left": "country", "right": "country"},
                                {"left": "action", "right": "action"}],
                         "prefix": "p_"},
                 output_name="annotated")
    assert rows_of(app_ctx, out) == rows_of(app_ctx, auth), \
        "an annotation must not change the row count"
    assert "p_n" in columns_of(app_ctx, out)


def test_both_key_columns_are_kept_out_of_what_comes_across(
    app_ctx, run_op, auth, per_country_action
):
    """The left side already has them; bringing them back is how a join grows a
    second column of the same name."""
    out = run_op(op="join",
                 inputs=[{"dataset_id": auth}, {"dataset_id": per_country_action}],
                 params={"on": [{"left": "country", "right": "country"},
                                {"left": "action", "right": "action"}]},
                 output_name="annotated")
    cols = columns_of(app_ctx, out)
    assert cols.count("country") == 1 and cols.count("action") == 1


def test_the_one_pair_spelling_still_works(app_ctx, run_op, auth):
    """What a suggestion's action and the agent's create_join emit. A stored
    payload cannot be migrated, so the shorthand has to keep meaning what it
    meant."""
    freq = run_op(op="aggregate", plugin_id="agg.frequency",
                  inputs=[{"dataset_id": auth}], params={"column": "country"},
                  output_name="freq")
    out = run_op(op="join", inputs=[{"dataset_id": auth}, {"dataset_id": freq}],
                 params={"left_column": "country", "right_column": "country"},
                 output_name="annotated")
    assert rows_of(app_ctx, out) == rows_of(app_ctx, auth)
    assert "share" in columns_of(app_ctx, out)


def test_a_join_with_no_key_is_refused():
    """A cross join is not a thing to arrive at by leaving a field blank."""
    with pytest.raises(ValueError, match="needs a key"):
        JoinParams.model_validate({"how": "left"})


def test_the_fanout_error_names_the_whole_key(app_ctx, run_op, auth, per_country_action):
    job = run_failing(app_ctx, op="join",
                      inputs=[{"dataset_id": auth}, {"dataset_id": per_country_action}],
                      params={"on": [{"left": "action", "right": "action"}],
                              "prefix": "p_"})
    assert "joining on action" in job.error
    assert "Add the columns that make the key unique" in job.error


# --------------------------------------------------------------------------- #
# names that are already taken
# --------------------------------------------------------------------------- #
@pytest.fixture
def two_frequencies(app_ctx, run_op, tmp_path):
    """Two aggregates over different data, both with n / share / rarity."""
    a = run_op(op="import", uri=str(write_auth_csv(tmp_path / "a.csv", rows=300, seed=1)),
               name="a")
    b = run_op(op="import", uri=str(write_auth_csv(tmp_path / "b.csv", rows=300, seed=2)),
               name="b")
    return tuple(
        run_op(op="aggregate", plugin_id="agg.frequency", inputs=[{"dataset_id": d}],
               params={"column": "country"}, output_name=f"freq_{name}")
        for d, name in ((a, "a"), (b, "b"))
    )


def test_columns_that_would_appear_twice_are_refused(app_ctx, two_frequencies):
    """DuckDB returns two result columns of the same name without complaint, and
    the catalog stores one column per name per version. Caught before the write,
    where there is still a remedy to name."""
    left, right = two_frequencies
    job = run_failing(app_ctx, op="join",
                      inputs=[{"dataset_id": left}, {"dataset_id": right}],
                      params={"left_column": "country", "right_column": "country"})
    assert job.status == "failed"
    assert "duplicate columns" in job.error and "n" in job.error
    assert "prefix" in job.error, "it names a remedy"


def test_a_prefix_settles_it(app_ctx, run_op, two_frequencies):
    left, right = two_frequencies
    out = run_op(op="join", inputs=[{"dataset_id": left}, {"dataset_id": right}],
                 params={"left_column": "country", "right_column": "country",
                         "prefix": "b_"}, output_name="both")
    cols = columns_of(app_ctx, out)
    assert {"n", "share", "b_n", "b_share"} <= set(cols)


def test_naming_the_columns_to_bring_across_settles_it_too(app_ctx, run_op, two_frequencies):
    left, right = two_frequencies
    out = run_op(op="join", inputs=[{"dataset_id": left}, {"dataset_id": right}],
                 params={"left_column": "country", "right_column": "country",
                         "right_select": [], "prefix": "other_"}, output_name="both")
    assert "other_rarity" in columns_of(app_ctx, out)


# --------------------------------------------------------------------------- #
# the preview
# --------------------------------------------------------------------------- #
def test_the_preview_sees_the_fanout_without_writing_anything(
    app_ctx, auth, per_country_action
):
    """The op's own guard costs a full pass and arrives as a failed job. This is
    the same answer for the price of a GROUP BY."""
    before = len(app_ctx.catalog.list_datasets())
    out = preview(app_ctx, auth, per_country_action,
                  {"left_column": "country", "right_column": "country", "prefix": "p_"})
    assert out.fanout and out.duplicate_keys > 0
    assert "multiply rows" in " ".join(out.notes)
    assert len(app_ctx.catalog.list_datasets()) == before, "nothing was created"


def test_the_preview_clears_the_composite_key(app_ctx, auth, per_country_action):
    out = preview(app_ctx, auth, per_country_action,
                  {"on": [{"left": "country", "right": "country"},
                          {"left": "action", "right": "action"}], "prefix": "p_"})
    assert not out.fanout
    assert out.duplicate_keys == 0
    assert out.result_rows == out.left_rows == rows_of(app_ctx, auth)
    assert out.matched == out.sampled, "every row of the source is in its own rollup"
    assert "p_n" in out.columns_added


def test_the_preview_reports_a_key_that_matches_nothing(app_ctx, run_op, auth, tmp_path):
    path = tmp_path / "strangers.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["country", "note"])
        for i, c in enumerate(["ZZ", "YY", "XX"]):
            w.writerow([c, f"note{i}"])
    strangers = run_op(op="import", uri=str(path), name="strangers")

    out = preview(app_ctx, auth, strangers,
                  {"left_column": "country", "right_column": "country"})
    assert out.sampled > 0 and out.matched == 0
    assert "every attached column would be empty" in " ".join(out.notes)


def test_the_preview_reports_collisions(app_ctx, two_frequencies):
    left, right = two_frequencies
    out = preview(app_ctx, left, right,
                  {"left_column": "country", "right_column": "country"})
    assert "n" in out.collisions and "share" in out.collisions
    assert "two columns of each name" in " ".join(out.notes)

    clean = preview(app_ctx, left, right,
                    {"left_column": "country", "right_column": "country",
                     "prefix": "b_"})
    assert clean.collisions == []


def test_an_inner_join_previews_a_smaller_result(app_ctx, run_op, auth, tmp_path):
    """A left join keeps every row; an inner one keeps the matched ones, which is
    a different number and worth seeing first."""
    path = tmp_path / "some.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["country", "note"])
        w.writerow(["US", "the big one"])
    some = run_op(op="import", uri=str(path), name="some")

    left = preview(app_ctx, auth, some,
                   {"left_column": "country", "right_column": "country"})
    inner = preview(app_ctx, auth, some,
                    {"left_column": "country", "right_column": "country",
                     "how": "inner"})
    assert left.result_rows == left.left_rows
    assert 0 < inner.result_rows < inner.left_rows


def test_a_key_column_that_does_not_exist_is_named(app_ctx, auth, per_country_action):
    with pytest.raises(ValueError, match="'nope' not found"):
        preview(app_ctx, auth, per_country_action,
                {"left_column": "nope", "right_column": "country"})


# --------------------------------------------------------------------------- #
# candidates
# --------------------------------------------------------------------------- #
def test_candidates_propose_the_pairs_the_semantic_layer_knows(app_ctx, run_op, auth):
    freq = run_op(op="aggregate", plugin_id="agg.frequency",
                  inputs=[{"dataset_id": auth}], params={"column": "country"},
                  output_name="freq")
    found = candidates(app_ctx, auth)
    by_id = {c.dataset_id: c for c in found}
    assert freq in by_id
    assert ("country", "country") in [(k.left, k.right) for k in by_id[freq].keys]
    assert "geo.country_iso2" in by_id[freq].keys[0].reason


def test_candidates_match_on_meaning_not_on_name(app_ctx, run_op, auth, tmp_path):
    """Two columns called the same thing are often unrelated, and two columns
    called different things are often the same key. The pairing asks the
    semantic layer, which is what makes the join suggestable at all."""
    path = tmp_path / "iso.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["nation", "continent"])
        for c in ["US", "GB", "DE", "FR", "BR", "IN", "JP"]:
            w.writerow([c, "somewhere"])
    iso = run_op(op="import", uri=str(path), name="iso")

    found = {c.dataset_id: c for c in candidates(app_ctx, auth)}
    assert iso in found, "different names, same meaning"
    assert ("country", "nation") in [(k.left, k.right) for k in found[iso].keys]


def test_a_dataset_shares_no_meaning_and_is_not_proposed(app_ctx, run_op, auth, tmp_path):
    """Measures are not keys. Nothing in this table means what anything in the
    auth log means, so it is not offered -- which is the difference between a
    dataset picker and a list of datasets."""
    path = tmp_path / "prices.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["fare_amount", "trip_distance"])
        for i in range(20):
            w.writerow([i * 1.5, i * 0.25])
    prices = run_op(op="import", uri=str(path), name="prices")
    assert prices not in {c.dataset_id for c in candidates(app_ctx, auth)}


def test_candidates_put_the_small_side_first(app_ctx, run_op, auth, tmp_path):
    """A join onto a small table is an annotation, which is the kind that keeps
    the row count and is almost always what was wanted."""
    other = run_op(op="import",
                   uri=str(write_auth_csv(tmp_path / "big.csv", rows=900, seed=7)),
                   name="big")
    freq = run_op(op="aggregate", plugin_id="agg.frequency",
                  inputs=[{"dataset_id": auth}], params={"column": "country"},
                  output_name="freq")
    order = [c.dataset_id for c in candidates(app_ctx, auth)]
    assert order.index(freq) < order.index(other)
