"""Deleting datasets.

The interesting cases are all about what deletion must *not* quietly do: leave
the bytes on disk, strand a derivation tree, or race a job that is still
writing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dataq.api.app import create_app
from dataq.services.datasets import DeleteRefused, delete_dataset, descendants
from dataq.storage.base import StoredRef

from .fixtures import write_auth_csv


@pytest.fixture
def auth(app_ctx, run_op, tmp_path):
    return run_op(op="import", uri=str(write_auth_csv(tmp_path / "a.csv", rows=400)),
                  name="auth")


@pytest.fixture
def with_children(app_ctx, run_op, auth):
    """An import, an aggregate over it, and a join of the two."""
    freq = run_op(op="aggregate", plugin_id="agg.frequency",
                  inputs=[{"dataset_id": auth}], params={"column": "country"},
                  output_name="country_freq")
    joined = run_op(op="join", inputs=[{"dataset_id": auth}, {"dataset_id": freq}],
                    params={"left_column": "country", "right_column": "country"},
                    output_name="annotated")
    return {"auth": auth, "freq": freq, "joined": joined}


def stored_locations(app_ctx, dataset_id) -> list[str]:
    return [StoredRef(**v.stored_ref).location
            for v in app_ctx.catalog.list_versions(dataset_id)]


def exists(app_ctx, location: str) -> bool:
    """Is the stored data still there, in either backend?"""
    import pathlib

    if app_ctx.settings.storage == "parquet":
        return pathlib.Path(location).exists()
    with app_ctx.warehouse.cur() as conn:
        found = conn.execute(
            "SELECT count(*) FROM duckdb_tables() WHERE table_name = ?", [location]
        ).fetchone()[0]
    return bool(found)


# --------------------------------------------------------------------------- #
# the bytes
# --------------------------------------------------------------------------- #
def test_deleting_removes_the_stored_data(app_ctx, auth):
    """The bug this fixes: the catalog row went, the files stayed.

    A lake that only grows, while the dataset list says nothing is there.
    """
    locations = stored_locations(app_ctx, auth)
    assert locations and all(exists(app_ctx, loc) for loc in locations)

    result = delete_dataset(app_ctx, auth)

    assert app_ctx.catalog.get_dataset(auth) is None
    assert not any(exists(app_ctx, loc) for loc in locations)
    assert result.versions == len(locations)


def test_every_version_is_removed_not_just_the_latest(app_ctx, run_op, auth):
    run_op(op="transform", plugin_id="normalize.ip",
           inputs=[{"dataset_id": auth}], params={"column": "src_ip"})
    locations = stored_locations(app_ctx, auth)
    assert len(locations) == 2, "precondition: a transform added a version"

    delete_dataset(app_ctx, auth)
    assert not any(exists(app_ctx, loc) for loc in locations)


def test_it_reports_what_it_freed(app_ctx, auth):
    result = delete_dataset(app_ctx, auth)
    assert result.bytes_freed > 0
    assert [d["name"] for d in result.datasets] == ["auth"]


# --------------------------------------------------------------------------- #
# not stranding a derivation tree
# --------------------------------------------------------------------------- #
def test_descendants_follows_both_sides_of_a_join(app_ctx, with_children):
    """A join nests under its left input, but it depends on both."""
    from_left = descendants(app_ctx, with_children["auth"])
    assert set(from_left) == {with_children["freq"], with_children["joined"]}

    from_right = descendants(app_ctx, with_children["freq"])
    assert from_right == [with_children["joined"]], \
        "the join depends on its right input too"


def test_deleting_a_parent_is_refused(app_ctx, with_children):
    with pytest.raises(DeleteRefused, match="derived from it"):
        delete_dataset(app_ctx, with_children["auth"])
    assert app_ctx.catalog.get_dataset(with_children["auth"]) is not None


def test_the_refusal_names_what_depends_on_it(app_ctx, with_children):
    """'Cannot delete' sends the reader hunting; naming them does not."""
    with pytest.raises(DeleteRefused) as exc:
        delete_dataset(app_ctx, with_children["auth"])
    assert "country_freq" in str(exc.value) or "annotated" in str(exc.value)
    assert "cascade" in str(exc.value)


def test_cascade_removes_the_whole_subtree(app_ctx, with_children):
    locations = [loc for ds in with_children.values()
                 for loc in stored_locations(app_ctx, ds)]

    result = delete_dataset(app_ctx, with_children["auth"], cascade=True)

    assert set(result.ids) == set(with_children.values())
    for ds in with_children.values():
        assert app_ctx.catalog.get_dataset(ds) is None
    assert not any(exists(app_ctx, loc) for loc in locations)


def test_a_leaf_needs_no_cascade(app_ctx, with_children):
    """Deleting the join takes nothing else with it."""
    delete_dataset(app_ctx, with_children["joined"])
    assert app_ctx.catalog.get_dataset(with_children["auth"]) is not None
    assert app_ctx.catalog.get_dataset(with_children["freq"]) is not None


def test_deleting_a_child_then_its_parent(app_ctx, with_children):
    """Once nothing depends on it, the parent goes without a cascade."""
    delete_dataset(app_ctx, with_children["joined"])
    delete_dataset(app_ctx, with_children["freq"])
    delete_dataset(app_ctx, with_children["auth"])
    assert app_ctx.catalog.list_datasets() == []


def test_an_unknown_dataset(app_ctx):
    with pytest.raises(KeyError, match="unknown dataset"):
        delete_dataset(app_ctx, "nope")


# --------------------------------------------------------------------------- #
# not racing a job
# --------------------------------------------------------------------------- #
def test_a_running_job_blocks_deletion(app_ctx, auth):
    """Deleting under a live writer leaves a version row pointing at nothing."""
    job = app_ctx.catalog.create_job(title="transform: pretend")
    app_ctx.catalog.create_step(job_id=job.id, op="transform", plugin_id="x",
                                inputs=[{"dataset_id": auth}])
    app_ctx.catalog.update_job(job.id, status="running")

    with pytest.raises(DeleteRefused, match="still running"):
        delete_dataset(app_ctx, auth)

    app_ctx.catalog.update_job(job.id, status="succeeded")
    delete_dataset(app_ctx, auth)
    assert app_ctx.catalog.get_dataset(auth) is None


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


def test_the_route_deletes_and_reports(client, app_ctx, auth):
    r = client.delete(f"/api/datasets/{auth}")
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] == [auth]
    assert body["versions"] >= 1 and body["bytes_freed"] > 0
    assert client.get(f"/api/datasets/{auth}").status_code == 404


def test_the_route_refuses_a_parent_with_409(client, with_children):
    r = client.delete(f"/api/datasets/{with_children['auth']}")
    assert r.status_code == 409
    assert "cascade" in r.json()["detail"]


def test_the_route_cascades_when_asked(client, with_children):
    r = client.delete(f"/api/datasets/{with_children['auth']}?cascade=true")
    assert r.status_code == 200
    assert set(r.json()["deleted"]) == set(with_children.values())


def test_an_unknown_dataset_is_404(client):
    assert client.delete("/api/datasets/nope").status_code == 404


def test_dependents_lists_what_would_go(client, with_children):
    r = client.get(f"/api/datasets/{with_children['auth']}/dependents")
    assert r.status_code == 200
    assert {d["id"] for d in r.json()} == {
        with_children["freq"], with_children["joined"]}

    leaf = client.get(f"/api/datasets/{with_children['joined']}/dependents")
    assert leaf.json() == []


def test_no_empty_directory_is_left_behind(app_ctx, auth):
    """Zero bytes, but a lake listing directories for datasets that are gone is
    exactly the leftover state that makes storage hard to reason about."""
    if app_ctx.settings.storage != "parquet":
        pytest.skip("directory layout is specific to the parquet backend")
    import pathlib

    lake = pathlib.Path(app_ctx.settings.lake_dir)
    assert (lake / auth).exists()

    delete_dataset(app_ctx, auth)
    assert not (lake / auth).exists()
    assert [d.name for d in lake.iterdir()] == []


# --------------------------------------------------------------------------- #
# a failed operation leaves nothing behind
# --------------------------------------------------------------------------- #
def test_a_failed_aggregate_leaves_no_phantom_dataset(app_ctx, auth, monkeypatch):
    """The dataset row is created before the work that fills it.

    Anything failing in between left a ghost: it listed in the UI, reported zero
    rows, could not be queried, and could not be explained.
    """
    import dataq.plugins.builtin.aggregators as agg

    before = {d.id for d in app_ctx.catalog.list_datasets()}
    original = agg.FrequencyAggregate.plan

    def clipped(self, ctx):
        plan = original(self, ctx)
        plan.spec.limit = 2          # forces the truncation guard to fire
        return plan

    monkeypatch.setattr(agg.FrequencyAggregate, "plan", clipped)
    from dataq.services.operations import OperationRequest, submit_operation

    accepted = submit_operation(app_ctx, OperationRequest(
        op="aggregate", plugin_id="agg.frequency", inputs=[{"dataset_id": auth}],
        params={"column": "country"}))
    app_ctx.runner.wait(accepted.job_id, timeout=60)
    assert app_ctx.catalog.get_job(accepted.job_id).status == "failed"

    assert {d.id for d in app_ctx.catalog.list_datasets()} == before, \
        "a failed aggregate must leave the catalog as it found it"


def test_a_failed_import_leaves_no_phantom_dataset(app_ctx, tmp_path):
    from dataq.services.operations import OperationRequest, submit_operation

    before = {d.id for d in app_ctx.catalog.list_datasets()}
    bad = tmp_path / "broken.parquet"
    bad.write_text("this is not parquet")

    accepted = submit_operation(app_ctx, OperationRequest(
        op="import", uri=str(bad), name="broken"))
    app_ctx.runner.wait(accepted.job_id, timeout=60)
    assert app_ctx.catalog.get_job(accepted.job_id).status == "failed"
    assert {d.id for d in app_ctx.catalog.list_datasets()} == before


def test_data_with_no_version_row_is_still_removed(app_ctx, auth):
    """A run that wrote files but never recorded the version leaves data that
    nothing points at. Deleting version-by-version would walk straight past it.
    """
    locations = stored_locations(app_ctx, auth)
    # Forget the versions, keeping the dataset -- the shape a half-failed
    # operation leaves behind.
    from sqlmodel import Session, select

    from dataq.catalog.models import VersionRow

    with Session(app_ctx.catalog.engine) as s:
        for v in s.exec(select(VersionRow).where(VersionRow.dataset_id == auth)):
            s.delete(v)
        s.commit()
    assert app_ctx.catalog.list_versions(auth) == []
    assert all(exists(app_ctx, loc) for loc in locations), "precondition: data remains"

    delete_dataset(app_ctx, auth)
    assert not any(exists(app_ctx, loc) for loc in locations)
