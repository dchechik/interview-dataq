"""The derivation tree.

The interesting cases are the ones where a DAG does not map cleanly onto a tree:
a join has two parents, a parent can be deleted, and a transform must *not* count
as producing a child.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dataq.api.app import create_app
from dataq.services.lineage import build_forest, derivation_edges, related

from .fixtures import write_auth_csv, write_taxi_csv


@pytest.fixture
def built(app_ctx, run_op, tmp_path):
    """auth ─ country_freq ─ annotated(join), plus an unrelated taxi source."""
    auth = run_op(op="import", uri=str(write_auth_csv(tmp_path / "a.csv", rows=300)),
                  name="auth")
    taxi = run_op(op="import", uri=str(write_taxi_csv(tmp_path / "t.csv", rows=200)),
                  name="taxi")
    # A transform: a new *version* of auth, never a child.
    run_op(op="transform", plugin_id="normalize.ip", inputs=[{"dataset_id": auth}],
           params={"column": "src_ip"})
    freq = run_op(op="aggregate", plugin_id="agg.frequency",
                  inputs=[{"dataset_id": auth}], params={"column": "country"},
                  output_name="country_freq")
    annotated = run_op(op="join", inputs=[{"dataset_id": auth}, {"dataset_id": freq}],
                       params={"left_column": "country", "right_column": "country"},
                       output_name="annotated")
    return {"auth": auth, "taxi": taxi, "freq": freq, "annotated": annotated,
            "ctx": app_ctx}


def find(nodes, dataset_id):
    for n in nodes:
        if n.dataset.id == dataset_id:
            return n
        hit = find(n.children, dataset_id)
        if hit:
            return hit
    return None


def test_transform_does_not_create_a_child(built):
    """A transform makes a new version, so auth must not be its own child."""
    edges = derivation_edges(built["ctx"].catalog)
    assert built["auth"] not in edges
    forest = build_forest(built["ctx"].catalog)
    auth = find(forest, built["auth"])
    assert built["auth"] not in [c.dataset.id for c in auth.children]


def test_aggregate_nests_under_its_source(built):
    forest = build_forest(built["ctx"].catalog)
    # Only the two imported datasets are roots.
    assert {n.dataset.id for n in forest} == {built["auth"], built["taxi"]}

    auth = find(forest, built["auth"])
    child_ids = {c.dataset.id for c in auth.children}
    assert built["freq"] in child_ids

    freq = find(forest, built["freq"])
    assert freq.edge.op == "aggregate"
    assert freq.edge.plugin_id == "agg.frequency"


def test_join_nests_under_its_left_input_and_names_the_other(built):
    """A tree node has one parent, so the second edge must stay visible."""
    forest = build_forest(built["ctx"].catalog)
    auth = find(forest, built["auth"])
    assert built["annotated"] in {c.dataset.id for c in auth.children}

    annotated = find(forest, built["annotated"])
    assert annotated.edge.op == "join"
    assert annotated.edge.parent_id == built["auth"]
    # The right-hand side is not dropped.
    assert [e.parent_id for e in annotated.others] == [built["freq"]]


def test_descendant_count(built):
    forest = build_forest(built["ctx"].catalog)
    assert find(forest, built["auth"]).descendants() == 2
    assert find(forest, built["taxi"]).descendants() == 0


def test_unrelated_source_stays_a_root_with_no_children(built):
    forest = build_forest(built["ctx"].catalog)
    assert find(forest, built["taxi"]).children == []


def test_orphan_surfaces_as_a_root(built):
    """Deleting a parent must not hide its children."""
    built["ctx"].catalog.delete_dataset(built["auth"])
    forest = build_forest(built["ctx"].catalog)
    roots = {n.dataset.id for n in forest}
    assert built["freq"] in roots
    assert built["annotated"] in roots


def test_related_reports_parents_and_children(built):
    ctx = built["ctx"]
    auth = related(ctx.catalog, built["auth"])
    assert auth["parents"] == []
    by_id = {c["id"]: c for c in auth["children"]}
    assert set(by_id) == {built["freq"], built["annotated"]}
    assert by_id[built["freq"]]["op"] == "aggregate"
    assert by_id[built["freq"]]["row_count"] > 0

    freq = related(ctx.catalog, built["freq"])
    assert [p["id"] for p in freq["parents"]] == [built["auth"]]
    # The join lists freq as a parent too, so freq has the join as a child.
    assert [c["id"] for c in freq["children"]] == [built["annotated"]]

    annotated = related(ctx.catalog, built["annotated"])
    assert [p["id"] for p in annotated["parents"]] == [built["auth"], built["freq"]]
    assert [p["role"] for p in annotated["parents"]] == ["primary", "joined"]


def test_related_unknown_dataset(built):
    with pytest.raises(KeyError):
        related(built["ctx"].catalog, "nope")


def test_cycle_is_broken_rather_than_recursed(app_ctx):
    """Defensive: operations should never build one, but a cycle must not hang."""
    from dataq.services.lineage import Edge, _would_cycle

    edges = {
        "b": [Edge(parent_id="a", op="aggregate", plugin_id="x", role="primary")],
        "a": [Edge(parent_id="b", op="aggregate", plugin_id="x", role="primary")],
    }
    assert _would_cycle("a", "b", edges) is True
    assert _would_cycle("b", "a", edges) is True


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(app_ctx):
    import dataq.api.app as app_module

    app = create_app(ctx=app_ctx)
    with TestClient(app) as c:
        yield c
    app_module.CTX = None


def test_tree_endpoint(client, built):
    tree = client.get("/api/datasets/tree").json()
    assert {n["id"] for n in tree} == {built["auth"], built["taxi"]}

    auth = next(n for n in tree if n["id"] == built["auth"])
    assert auth["descendants"] == 2
    assert auth["derived_via"] is None

    freq = next(c for c in auth["children"] if c["id"] == built["freq"])
    assert freq["derived_via"] == {"op": "aggregate", "plugin_id": "agg.frequency"}
    assert freq["kind"] == "aggregate"
    assert freq["row_count"] > 0

    annotated = next(c for c in auth["children"] if c["id"] == built["annotated"])
    assert annotated["joined_with"][0]["name"] == "country_freq"


def test_tree_route_is_not_shadowed_by_the_id_route(client, built):
    """/api/datasets/tree must not be read as a dataset called "tree"."""
    r = client.get("/api/datasets/tree")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_related_endpoint(client, built):
    body = client.get(f"/api/datasets/{built['freq']}/related").json()
    assert [p["name"] for p in body["parents"]] == ["auth"]
    assert [c["name"] for c in body["children"]] == ["annotated"]

    assert client.get("/api/datasets/nope/related").status_code == 404
