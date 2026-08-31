"""Meanings people define themselves.

The detectors can only recognise what looks the same everywhere. Whether ``pc``
in a browsing log and ``device`` in an asset inventory name the same machines is
a fact about one organisation, so somebody has to say it -- and the payoff is
across datasets, which is what most of these tests check.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dataq.api.app import create_app
from dataq.core.profile import ColumnProfile, DatasetProfile
from dataq.core.semantic import SEMANTIC_TYPES, SemanticType, SemanticTypeError
from dataq.services import semantic_types as service


@pytest.fixture
def client(app_ctx):
    import dataq.api.app as app_module

    app = create_app(ctx=app_ctx)
    with TestClient(app) as c:
        yield c
    app_module.CTX = None


def write_csv(path: Path, header: list[str], rows: list[list[object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return path


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #
def test_a_custom_type_is_an_ordinary_member_of_the_hierarchy(app_ctx):
    """The point of naming a parent: every rule written for the parent applies."""
    service.define(app_ctx.catalog, "machine.name", parent="categorical")

    assert SEMANTIC_TYPES.ancestry("machine.name") == [
        "machine.name", "categorical", "text"]
    # Which is what makes the column visible to plugins that were written years
    # before anybody thought of machine names.
    assert SEMANTIC_TYPES.matches_any("machine.name", ("categorical",))
    assert SEMANTIC_TYPES.matches_any("machine.name", ("text",))
    assert not SEMANTIC_TYPES.matches_any("machine.name", ("numeric",))


def test_the_title_is_derived_when_not_given(app_ctx):
    st = service.define(app_ctx.catalog, "cost_centre.code", parent="categorical")
    assert st.title == "Cost centre code"


def test_the_role_is_inherited_from_the_parent(app_ctx):
    """A machine name is a dimension; a machine's age is a measure. Neither is
    something the person defining the type should have to think about."""
    assert service.define(
        app_ctx.catalog, "machine.name", parent="categorical").role == "dimension"
    assert service.define(
        app_ctx.catalog, "machine.age_days", parent="numeric").role == "measure"


def test_an_explicit_role_wins(app_ctx):
    st = service.define(app_ctx.catalog, "machine.serial", parent="categorical",
                        role="key")
    assert st.role == "key"


def test_joinable_by_default(app_ctx):
    """Linking datasets is the usual reason to define a type at all."""
    assert service.define(app_ctx.catalog, "machine.name", parent="categorical").joinable
    assert SEMANTIC_TYPES.joinable_with("machine.name")
    assert not service.define(
        app_ctx.catalog, "ticket.body", parent="text", joinable=False).joinable


def test_defining_the_same_id_again_edits_it(app_ctx):
    """The id is the identity. Two people reaching for ``machine.name`` should
    land on the same type, not on a conflict."""
    service.define(app_ctx.catalog, "machine.name", parent="text", joinable=False)
    st = service.define(app_ctx.catalog, "machine.name", parent="categorical",
                        joinable=True, description="a host in the fleet")
    assert (st.parent, st.joinable, st.description) == (
        "categorical", True, "a host in the fleet")
    assert len(app_ctx.catalog.list_semantic_types()) == 1


# --------------------------------------------------------------------------- #
# what is refused, and why
# --------------------------------------------------------------------------- #
def test_a_built_in_type_cannot_be_redefined(app_ctx):
    """Plugins are written against these ids; redefining one would change what
    every ``Accepts`` clause in the codebase means."""
    with pytest.raises(SemanticTypeError, match="built-in"):
        service.define(app_ctx.catalog, "net.ip", parent="text")


def test_an_unknown_parent_says_why_a_parent_is_needed(app_ctx):
    with pytest.raises(SemanticTypeError, match="still accept it"):
        service.define(app_ctx.catalog, "machine.name", parent="hardware")


def test_a_type_cannot_be_its_own_ancestor(app_ctx):
    service.define(app_ctx.catalog, "machine.name", parent="categorical")
    service.define(app_ctx.catalog, "machine.host", parent="machine.name")
    with pytest.raises(SemanticTypeError, match="own ancestor"):
        service.define(app_ctx.catalog, "machine.name", parent="machine.host")


@pytest.mark.parametrize("bad", [
    "Machine.Name",      # uppercase
    "machine name",      # space
    "machine..name",     # empty segment
    "1machine",          # leading digit
    "machine-name",      # hyphen
    "",
])
def test_unusable_ids_are_refused_with_an_example(app_ctx, bad):
    with pytest.raises(SemanticTypeError, match="machine.name"):
        service.define(app_ctx.catalog, bad, parent="categorical")


def test_an_unknown_role_lists_the_real_ones(app_ctx):
    with pytest.raises(SemanticTypeError, match="unknown role"):
        service.define(app_ctx.catalog, "machine.name", parent="categorical",
                       role="entity")


def test_a_rejected_definition_leaves_nothing_behind(app_ctx):
    """Validation happens before the write, so a refused type is not half-made."""
    with pytest.raises(SemanticTypeError):
        service.define(app_ctx.catalog, "Machine.Name", parent="categorical")
    assert app_ctx.catalog.list_semantic_types() == []
    assert SEMANTIC_TYPES.custom() == []


# --------------------------------------------------------------------------- #
# persistence and isolation
# --------------------------------------------------------------------------- #
def test_a_definition_survives_a_restart(settings, app_ctx):
    """The registry is a process singleton; the vocabulary is not. A new context
    over the same data directory is what a restart looks like."""
    service.define(app_ctx.catalog, "machine.name", parent="categorical",
                   description="a host in the fleet")
    SEMANTIC_TYPES.reset_custom()
    assert SEMANTIC_TYPES.get("machine.name") is None

    from dataq.services.context import build_context

    fresh = build_context(settings)
    try:
        st = SEMANTIC_TYPES.get("machine.name")
        assert st is not None
        assert st.description == "a host in the fleet"
        assert SEMANTIC_TYPES.matches_any("machine.name", ("categorical",))
    finally:
        fresh.warehouse.close()


def test_another_catalog_does_not_inherit_the_vocabulary(settings, app_ctx, tmp_path):
    """Loading replaces rather than merges, so one data directory's types cannot
    leak into the next -- which in practice means into the next test."""
    service.define(app_ctx.catalog, "machine.name", parent="categorical")

    from dataq.config import Settings
    from dataq.services.context import build_context

    other = Settings(_env_file=None, data_dir=tmp_path / "elsewhere",
                     storage=settings.storage, duckdb_threads=2,
                     browse_roots=str(tmp_path))
    ctx = build_context(other)
    try:
        assert SEMANTIC_TYPES.get("machine.name") is None
    finally:
        ctx.warehouse.close()


def test_children_load_after_their_parents(settings, app_ctx):
    """Rows come back sorted by id, so a child can precede its parent."""
    service.define(app_ctx.catalog, "zeta", parent="categorical")
    service.define(app_ctx.catalog, "alpha", parent="zeta")
    SEMANTIC_TYPES.reset_custom()

    loaded = service.load_into_registry(app_ctx.catalog)
    assert set(loaded) == {"alpha", "zeta"}
    assert SEMANTIC_TYPES.ancestry("alpha") == ["alpha", "zeta", "categorical", "text"]


# --------------------------------------------------------------------------- #
# deletion
# --------------------------------------------------------------------------- #
def test_an_unused_type_can_be_deleted(app_ctx):
    service.define(app_ctx.catalog, "machine.name", parent="categorical")
    service.remove(app_ctx.catalog, "machine.name")
    assert SEMANTIC_TYPES.get("machine.name") is None
    assert app_ctx.catalog.list_semantic_types() == []


def test_deleting_a_type_in_use_is_refused_and_names_the_columns(app_ctx, tmp_path):
    """Column metadata stores the id as text. Deleting the type would not clear
    those columns; it would leave them speaking a language nothing can read."""
    path = write_csv(tmp_path / "hosts.csv", ["pc", "n"],
                     [[f"PC-{i:03d}", i] for i in range(20)])
    from dataq.services.operations import OperationRequest, submit_operation

    accepted = submit_operation(app_ctx, OperationRequest(
        op="import", uri=str(path), name="hosts"))
    app_ctx.runner.wait(accepted.job_id, timeout=120)
    dataset_id = app_ctx.catalog.get_step(accepted.step_id).outputs[0]["dataset_id"]

    service.define(app_ctx.catalog, "machine.name", parent="categorical")
    version = app_ctx.catalog.get_version(dataset_id)
    app_ctx.catalog.pin_column_type(version.id, "pc", "machine.name")

    with pytest.raises(SemanticTypeError, match="hosts.pc"):
        service.remove(app_ctx.catalog, "machine.name")
    assert SEMANTIC_TYPES.get("machine.name") is not None


def test_deleting_a_parent_of_another_type_is_refused(app_ctx):
    service.define(app_ctx.catalog, "machine.name", parent="categorical")
    service.define(app_ctx.catalog, "machine.host", parent="machine.name")
    with pytest.raises(SemanticTypeError, match="machine.host"):
        service.remove(app_ctx.catalog, "machine.name")


def test_a_built_in_type_cannot_be_deleted(app_ctx):
    with pytest.raises(SemanticTypeError, match="built in"):
        service.remove(app_ctx.catalog, "net.ip")


# --------------------------------------------------------------------------- #
# the payoff: two datasets, different column names, one meaning
# --------------------------------------------------------------------------- #
def test_a_shared_custom_meaning_makes_two_datasets_joinable(app_ctx):
    """The whole reason to define a type. Nothing detects a machine name, so
    without this the two datasets have no column in common at all -- and they
    do not even share a column *name* to fall back on."""
    from dataq.plugins.builtin.suggesters import JoinSuggester
    from dataq.plugins.kinds import SuggestCtx

    service.define(app_ctx.catalog, "machine.name", parent="categorical")

    browsing = DatasetProfile(
        dataset_id="d1", version=1, row_count=10_000,
        columns=[
            ColumnProfile(name="pc", physical_type="VARCHAR",
                          semantic_type="machine.name"),
            ColumnProfile(name="url", physical_type="VARCHAR",
                          semantic_type="identity.url"),
        ],
    )
    inventory = DatasetProfile(
        dataset_id="d2", version=1, row_count=300,
        columns=[
            ColumnProfile(name="device", physical_type="VARCHAR",
                          semantic_type="machine.name"),
            ColumnProfile(name="owner", physical_type="VARCHAR",
                          semantic_type="identity.email"),
        ],
    )

    out = JoinSuggester().suggest(SuggestCtx(profile=browsing, params=None, peers=[inventory]))
    assert [(s.action["params"]["left_column"], s.action["params"]["right_column"])
            for s in out] == [("pc", "device")]
    assert "machine.name" in out[0].rationale
    # A small peer is an annotation, which is the more useful kind of join.
    assert out[0].score >= 0.85


def test_a_non_joinable_custom_type_suggests_nothing(app_ctx):
    from dataq.plugins.builtin.suggesters import JoinSuggester
    from dataq.plugins.kinds import SuggestCtx

    service.define(app_ctx.catalog, "ticket.body", parent="text", joinable=False)
    left = DatasetProfile(dataset_id="d1", version=1, row_count=100, columns=[
        ColumnProfile(name="body", physical_type="VARCHAR",
                      semantic_type="ticket.body")])
    right = DatasetProfile(dataset_id="d2", version=1, row_count=100, columns=[
        ColumnProfile(name="text", physical_type="VARCHAR",
                      semantic_type="ticket.body")])
    assert JoinSuggester().suggest(SuggestCtx(profile=left, params=None, peers=[right])) == []


def test_a_custom_type_satisfies_the_plugin_gate(app_ctx):
    """The failure this is really about: a column nothing detects satisfies no
    ``Accepts`` clause, because ``matches_any(None, ...)`` is always false. So
    the two most useful columns in a log can be the two DataQ ignores."""
    from dataq.plugins.builtin.aggregators import FrequencyAggregate

    unnamed = ColumnProfile(name="pc", physical_type="VARCHAR")
    assert FrequencyAggregate.accepts.matching_columns([unnamed]) == []

    service.define(app_ctx.catalog, "machine.name", parent="categorical")
    named = ColumnProfile(name="pc", physical_type="VARCHAR",
                          semantic_type="machine.name")
    assert FrequencyAggregate.accepts.matching_columns([named]) == ["pc"]


# --------------------------------------------------------------------------- #
# parent defaulting
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("physical,parent", [
    ("VARCHAR", "categorical"),
    ("TIMESTAMP", "temporal"),
    ("DATE", "temporal"),
    ("BIGINT", "numeric"),
    ("DOUBLE", "numeric"),
    ("BOOLEAN", "boolean"),
    ("", "categorical"),
])
def test_the_offered_parent_follows_the_storage(physical, parent):
    """Text defaults to ``categorical`` rather than ``text``: it is joinable,
    more plugins accept it, and it descends from text anyway -- so it satisfies
    every gate ``text`` would, and several more."""
    assert service.suggest_parent(physical) == parent


# --------------------------------------------------------------------------- #
# over HTTP
# --------------------------------------------------------------------------- #
def test_the_api_round_trip(client):
    created = client.post("/api/semantic-types",
                          json={"id": "machine.name", "parent": "categorical"})
    assert created.status_code == 200, created.text
    assert created.json()["custom"] is True
    assert created.json()["title"] == "Machine name"

    listed = {t["id"]: t for t in client.get("/api/semantic-types").json()}
    assert listed["machine.name"]["custom"] is True
    assert listed["machine.name"]["in_use"] == 0
    assert listed["net.ip"]["custom"] is False

    assert client.delete("/api/semantic-types/machine.name").status_code == 200
    assert "machine.name" not in {
        t["id"] for t in client.get("/api/semantic-types").json()}


def test_the_api_refuses_a_bad_definition_with_a_readable_reason(client):
    r = client.post("/api/semantic-types",
                    json={"id": "Machine Name", "parent": "categorical"})
    assert r.status_code == 400
    assert "machine.name" in r.json()["detail"]

    r = client.post("/api/semantic-types", json={"id": "net.ip", "parent": "text"})
    assert r.status_code == 400 and "built-in" in r.json()["detail"]


def test_the_api_refuses_to_delete_a_built_in(client):
    assert client.delete("/api/semantic-types/net.ip").status_code == 409


# --------------------------------------------------------------------------- #
# at import time
# --------------------------------------------------------------------------- #
def test_a_custom_meaning_can_be_assigned_during_import(client, app_ctx, tmp_path):
    """The reported flow, end to end: define the meaning while looking at the
    column plan, then import with it already applied."""
    path = write_csv(tmp_path / "browsing.csv", ["pc", "url"],
                     [[f"PC-{i % 20:03d}", f"http://site{i % 7}.example"]
                      for i in range(200)])

    assert client.post("/api/semantic-types",
                       json={"id": "machine.name", "parent": "categorical"}
                       ).status_code == 200

    accepted = client.post("/api/operations", json={
        "op": "import", "uri": str(path), "name": "browsing",
        "params": {"columns": [
            {"name": "pc", "semantic_type": "machine.name", "pinned": True}]},
    })
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["job_id"]
    app_ctx.runner.wait(job_id, timeout=120)
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "succeeded", job

    dataset_id = job["steps"][0]["outputs"][0]["dataset_id"]
    columns = {c["name"]: c for c in
               client.get(f"/api/datasets/{dataset_id}/profile").json()["columns"]}
    assert columns["pc"]["semantic_type"] == "machine.name"
    assert columns["pc"]["pinned"] is True
    # And the role came from the parent, not from a guess about the name.
    assert columns["pc"]["role"] == "dimension"

    # It now counts as in use, which is what blocks the delete.
    listed = {t["id"]: t for t in client.get("/api/semantic-types").json()}
    assert listed["machine.name"]["in_use"] == 1
    assert client.delete("/api/semantic-types/machine.name").status_code == 409


def test_an_undefined_meaning_fails_the_import_and_says_how_to_fix_it(
    client, app_ctx, tmp_path
):
    """The plan is validated inside the job, like every other import decision,
    so this surfaces as a failed job rather than a rejected request. What
    matters is that nothing is written and the message names the remedy."""
    path = write_csv(tmp_path / "browsing.csv", ["pc"], [["PC-001"]] * 20)
    r = client.post("/api/operations", json={
        "op": "import", "uri": str(path), "name": "browsing",
        "params": {"columns": [{"name": "pc", "semantic_type": "machine.name"}]},
    })
    assert r.status_code == 202
    app_ctx.runner.wait(r.json()["job_id"], timeout=120)
    job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "failed"
    assert "Define it first" in job["steps"][0]["error"]


def test_a_pinned_custom_meaning_survives_reprofiling(app_ctx, tmp_path):
    """Re-detection must not overwrite a human's word -- and the role has to be
    resolved through the custom type, which no detector will ever return."""
    from dataq.services.profiler import profile_columns

    service.define(app_ctx.catalog, "machine.name", parent="categorical", role="key")
    previous = [ColumnProfile(name="pc", physical_type="VARCHAR",
                              semantic_type="machine.name", role="key", pinned=True)]

    from dataq.core.profile import ColumnStats

    stats = [ColumnStats(name="pc", physical_type="VARCHAR", row_count=100,
                         distinct_count=20)]
    out = profile_columns(stats, previous=previous)
    assert out[0].semantic_type == "machine.name"
    assert out[0].role == "key"
    assert out[0].pinned is True


def test_a_custom_type_appears_in_the_registry_listing(app_ctx):
    service.define(app_ctx.catalog, "machine.name", parent="categorical")
    ids = {t.id for t in SEMANTIC_TYPES.all()}
    assert "machine.name" in ids and "net.ip" in ids
    assert [t.id for t in SEMANTIC_TYPES.custom()] == ["machine.name"]
    assert SEMANTIC_TYPES.is_builtin("net.ip")
    assert not SEMANTIC_TYPES.is_builtin("machine.name")


def test_registering_directly_still_refuses_a_duplicate_builtin():
    """``register`` is the plugin-facing path and stays strict; ``add_custom``
    is the editable one."""
    with pytest.raises(ValueError, match="already registered"):
        SEMANTIC_TYPES.register(SemanticType(id="net.ip", title="x", parent="text"))
