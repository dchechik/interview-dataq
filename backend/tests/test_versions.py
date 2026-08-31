"""Going back to an earlier version, and pruning the ones you no longer want.

The two halves are deliberately asymmetric. Revert only ever *appends* -- it
copies an old version's data forward as a new one -- so nothing it does is
destructive and reverting the revert is the same operation again. Deletion is
the destructive half, and it refuses the two cases where removing a version
would change what the dataset means rather than just reclaiming disk.

What the tests are really pinning down is the invariant those two share: a
version number is allocated once and never reused, because steps record the
number they wrote and reusing it would make old provenance describe new data.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dataq.api.app import create_app
from dataq.services.datasets import DeleteRefused, delete_version
from dataq.services.operations import OperationRequest, submit_operation
from dataq.storage.base import StoredRef

from .fixtures import write_auth_csv


@pytest.fixture
def auth(app_ctx, run_op, tmp_path):
    """An import, then a transform over it: v1 raw, v2 with a column added."""
    ds = run_op(op="import", uri=str(write_auth_csv(tmp_path / "a.csv", rows=400)),
                name="auth")
    run_op(op="transform", plugin_id="normalize.ip",
           inputs=[{"dataset_id": ds}], params={"column": "src_ip"})
    return ds


def revert(app_ctx, dataset_id: str, version: int):
    """Submit a revert and block until it finishes; return the job."""
    accepted = submit_operation(app_ctx, OperationRequest(
        op="revert", inputs=[{"dataset_id": dataset_id, "version": version}]))
    app_ctx.runner.wait(accepted.job_id, timeout=120)
    return app_ctx.catalog.get_job(accepted.job_id)


def columns_of(app_ctx, dataset_id: str, version: int | None = None) -> list[str]:
    profile = app_ctx.catalog.get_profile(dataset_id, version)
    return [c.name for c in profile.columns]


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
# revert
# --------------------------------------------------------------------------- #
def test_revert_brings_the_old_data_back(app_ctx, auth):
    before = columns_of(app_ctx, auth, 1)
    assert "src_ip_canon" in columns_of(app_ctx, auth, 2), "precondition"

    job = revert(app_ctx, auth, 1)
    assert job.status == "succeeded", job.error

    assert columns_of(app_ctx, auth) == before, \
        "the current version reads like v1 again"
    assert app_ctx.catalog.get_profile(auth).row_count == \
        app_ctx.catalog.get_profile(auth, 1).row_count


def test_revert_appends_rather_than_rewinding(app_ctx, auth):
    """The point of the design: history is append-only, so nothing is lost.

    A pointer moved backwards would leave v2 unreachable and hand the next
    write a number v2's own step already claims in its provenance.
    """
    revert(app_ctx, auth, 1)

    versions = [v.version for v in app_ctx.catalog.list_versions(auth)]
    assert versions == [3, 2, 1], "v2 is still there, and v3 is the copy of v1"
    assert app_ctx.catalog.get_dataset(auth).latest_version == 3


def test_the_revert_is_itself_revertible(app_ctx, auth):
    """Undoing an undo is the same operation, not a special case."""
    revert(app_ctx, auth, 1)                       # v3 == v1
    assert "src_ip_canon" not in columns_of(app_ctx, auth)

    job = revert(app_ctx, auth, 2)                 # v4 == v2
    assert job.status == "succeeded", job.error
    assert "src_ip_canon" in columns_of(app_ctx, auth)


def test_revert_keeps_the_column_metadata_of_the_version_it_restores(app_ctx, auth):
    """A pin is a decision somebody made; re-profiling would throw it away.

    The bytes are a copy, so the meaning is copied with them rather than
    rediscovered from sampled statistics.
    """
    v1 = app_ctx.catalog.get_version(auth, 1)
    app_ctx.catalog.pin_column_type(v1.id, "country", "geo.country", role="geo")

    revert(app_ctx, auth, 1)

    restored = {c.name: c for c in app_ctx.catalog.get_profile(auth).columns}
    assert restored["country"].semantic_type == "geo.country"
    assert restored["country"].role == "geo"
    assert restored["country"].pinned is True


def test_reverting_to_the_current_version_is_refused(app_ctx, auth):
    """It would spend a full copy of the data to produce no change."""
    job = revert(app_ctx, auth, 2)
    assert job.status == "failed"
    assert "already the current version" in job.error


def test_reverting_to_a_version_that_never_existed(app_ctx, auth):
    job = revert(app_ctx, auth, 99)
    assert job.status == "failed"
    assert "no version 99" in job.error


def test_revert_records_its_own_lineage(app_ctx, auth):
    revert(app_ctx, auth, 1)

    steps = app_ctx.catalog.lineage(auth)
    reverts = [s for s in steps if s.op == "revert"]
    assert len(reverts) == 1
    assert reverts[0].inputs == [{"dataset_id": auth, "version": 1}]
    assert reverts[0].outputs == [{"dataset_id": auth, "version": 3}]


def test_revert_is_not_a_derivation_edge(app_ctx, auth):
    """A revert produces a new version of one dataset, not a child of it.

    Counted as an edge it would make the dataset its own parent, and the
    derivation forest would stop being a tree.
    """
    from dataq.services.lineage import derivation_edges

    revert(app_ctx, auth, 1)
    assert auth not in derivation_edges(app_ctx.catalog)


def test_the_reverted_version_owns_its_own_bytes(app_ctx, auth):
    """Two version rows sharing one StoredRef would make deleting either one
    destroy the other's data."""
    revert(app_ctx, auth, 1)

    locations = [StoredRef(**v.stored_ref).location
                 for v in app_ctx.catalog.list_versions(auth)]
    assert len(set(locations)) == 3, "each version materialised separately"
    assert all(exists(app_ctx, loc) for loc in locations)


# --------------------------------------------------------------------------- #
# deleting a version
# --------------------------------------------------------------------------- #
def test_deleting_an_old_version_frees_its_bytes(app_ctx, auth):
    v1 = app_ctx.catalog.get_version(auth, 1)
    location = StoredRef(**v1.stored_ref).location
    assert exists(app_ctx, location), "precondition"

    result = delete_version(app_ctx, auth, 1)

    assert not exists(app_ctx, location)
    assert app_ctx.catalog.get_version(auth, 1) is None
    assert result.bytes_freed > 0


def test_deleting_an_old_version_leaves_the_current_one_alone(app_ctx, auth):
    before = columns_of(app_ctx, auth)
    delete_version(app_ctx, auth, 1)

    assert columns_of(app_ctx, auth) == before
    assert [v.version for v in app_ctx.catalog.list_versions(auth)] == [2]


def test_deleting_the_current_version_is_refused(app_ctx, auth):
    with pytest.raises(DeleteRefused, match="current version"):
        delete_version(app_ctx, auth, 2)
    assert app_ctx.catalog.get_version(auth, 2) is not None


def test_the_refusal_says_what_to_do_instead(app_ctx, auth):
    with pytest.raises(DeleteRefused, match="[Rr]evert"):
        delete_version(app_ctx, auth, 2)


def test_revert_then_delete_reaches_the_state_the_refusal_blocked(app_ctx, auth):
    """The two halves compose: the refusal costs a step, not the outcome."""
    revert(app_ctx, auth, 1)          # v3 == v1, now current
    delete_version(app_ctx, auth, 2)  # the transform's output, no longer current

    assert [v.version for v in app_ctx.catalog.list_versions(auth)] == [3, 1]
    assert "src_ip_canon" not in columns_of(app_ctx, auth)


def test_deleting_the_only_version_is_refused(app_ctx, run_op, tmp_path):
    """A dataset with no data lists in the UI, reports zero rows, and cannot be
    queried or explained -- the ghost the operations layer works to avoid."""
    ds = run_op(op="import", uri=str(write_auth_csv(tmp_path / "b.csv", rows=50)),
                name="single")
    with pytest.raises(DeleteRefused, match="only version"):
        delete_version(app_ctx, ds, 1)
    assert "Delete the dataset itself" in _refusal(app_ctx, ds, 1)


def _refusal(app_ctx, dataset_id, version) -> str:
    try:
        delete_version(app_ctx, dataset_id, version)
    except DeleteRefused as exc:
        return str(exc)
    raise AssertionError("expected a refusal")


def test_version_numbers_are_never_reused(app_ctx, auth):
    """The invariant behind both refusals.

    A new v2 after the old one was deleted would make the first transform's
    step -- which still records ``version: 2`` -- describe data it never wrote.
    """
    revert(app_ctx, auth, 1)
    delete_version(app_ctx, auth, 2)
    assert app_ctx.catalog.next_version(auth) == 4, "not 2, and not 3"

    revert(app_ctx, auth, 1)
    assert [v.version for v in app_ctx.catalog.list_versions(auth)] == [4, 3, 1]


def test_deleting_a_version_of_an_unknown_dataset(app_ctx):
    with pytest.raises(KeyError, match="unknown dataset"):
        delete_version(app_ctx, "nope", 1)


def test_deleting_a_version_that_does_not_exist(app_ctx, auth):
    with pytest.raises(KeyError, match="no version 7"):
        delete_version(app_ctx, auth, 7)


def test_a_running_job_blocks_a_version_delete(app_ctx, auth):
    """Same hazard as deleting the dataset: the writer is left pointing at
    files that no longer exist."""
    job = app_ctx.catalog.create_job(title="transform: pretend")
    app_ctx.catalog.create_step(job_id=job.id, op="transform", plugin_id="x",
                                inputs=[{"dataset_id": auth}])
    app_ctx.catalog.update_job(job.id, status="running")

    with pytest.raises(DeleteRefused, match="still running"):
        delete_version(app_ctx, auth, 1)

    app_ctx.catalog.update_job(job.id, status="succeeded")
    delete_version(app_ctx, auth, 1)
    assert app_ctx.catalog.get_version(auth, 1) is None


def test_deleting_a_version_leaves_the_others_queryable(app_ctx, auth):
    """The parquet backend removes a directory per version; the DuckDB one drops
    a table. Either way the neighbours must survive it."""
    delete_version(app_ctx, auth, 1)
    source = app_ctx.resolve_source(auth)
    with app_ctx.warehouse.cur() as conn:
        rows = conn.execute(f"SELECT count(*) FROM {source.sql}").fetchone()[0]
    assert rows == 400


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


def test_the_versions_route_marks_the_current_one(client, auth):
    rows = client.get(f"/api/datasets/{auth}/versions").json()
    assert [r["version"] for r in rows] == [2, 1]
    assert [r["is_current"] for r in rows] == [True, False]
    assert rows[0]["bytes"] > 0


def test_the_revert_route_accepts_and_runs(client, app_ctx, auth):
    r = client.post(f"/api/datasets/{auth}/revert", json={"version": 1})
    assert r.status_code == 202
    app_ctx.runner.wait(r.json()["job_id"], timeout=120)

    assert app_ctx.catalog.get_job(r.json()["job_id"]).status == "succeeded"
    assert [v.version for v in app_ctx.catalog.list_versions(auth)] == [3, 2, 1]


def test_the_revert_job_is_titled_usefully(client, app_ctx, auth):
    """"revert:" with nothing after it is what the generic title would produce."""
    r = client.post(f"/api/datasets/{auth}/revert", json={"version": 1})
    job = app_ctx.catalog.get_job(r.json()["job_id"])
    assert job.title == "revert: auth to v1"


def test_the_revert_route_404s_on_an_unknown_version(client, auth):
    assert client.post(f"/api/datasets/{auth}/revert",
                       json={"version": 42}).status_code == 404
    assert client.post("/api/datasets/nope/revert",
                       json={"version": 1}).status_code == 404


def test_the_delete_version_route_reports_what_it_freed(client, app_ctx, auth):
    r = client.delete(f"/api/datasets/{auth}/versions/1")
    assert r.status_code == 200
    assert r.json()["version"] == 1 and r.json()["bytes_freed"] > 0
    assert app_ctx.catalog.get_version(auth, 1) is None


def test_the_delete_version_route_refuses_the_current_one_with_409(client, auth):
    r = client.delete(f"/api/datasets/{auth}/versions/2")
    assert r.status_code == 409
    assert "current version" in r.json()["detail"]


def test_the_delete_version_route_404s_on_an_unknown_version(client, auth):
    assert client.delete(f"/api/datasets/{auth}/versions/9").status_code == 404
    assert client.delete("/api/datasets/nope/versions/1").status_code == 404
