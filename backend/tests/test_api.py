"""API surface: contracts the frontend and the agent both depend on."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dataq.api.app import create_app

from .fixtures import write_auth_csv, write_taxi_csv


@pytest.fixture
def client(app_ctx):
    import dataq.api.app as app_module

    app = create_app(ctx=app_ctx)
    with TestClient(app) as c:
        yield c
    app_module.CTX = None


def _import(client, app_ctx, path, name):
    r = client.post("/api/operations", json={"op": "import", "uri": str(path), "name": name})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    app_ctx.runner.wait(job_id, timeout=120)
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "succeeded", job
    return job["steps"][0]["outputs"][0]["dataset_id"]


def test_health_and_plugin_catalogue(client):
    assert client.get("/api/health").json()["status"] == "ok"

    plugins = client.get("/api/plugins").json()
    assert len(plugins) >= 20
    by_id = {p["id"]: p for p in plugins}
    # Every descriptor must carry a JSON Schema: the UI form renderer and the
    # agent tool generator both depend on it.
    for p in plugins:
        assert "params_schema" in p and p["params_schema"]["type"] == "object"
    assert by_id["normalize.ip"]["mode"] == "pushdown"
    assert by_id["transform.ip_class"]["mode"] == "batch"
    assert by_id["extract.entities"]["mode"] == "external"
    assert by_id["extract.entities"]["cost_class"] == "expensive"
    assert by_id["viz.map_points"]["kind"] == "visualizer"


def test_filter_plugins_by_kind_and_mode(client):
    assert all(p["kind"] == "reader" for p in
               client.get("/api/plugins?kind=reader").json())
    assert all(p["mode"] == "inspect" for p in
               client.get("/api/plugins?mode=inspect").json())


def test_preview_before_import(client, tmp_path):
    path = write_auth_csv(tmp_path / "auth.csv", rows=40)
    r = client.post("/api/sources/preview", json={"uri": str(path), "limit": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["reader"] == "read.csv"
    assert "src_ip" in body["columns"]
    assert len(body["rows"]) == 5


def test_import_profile_and_applicable_plugins(client, app_ctx, tmp_path):
    ds = _import(client, app_ctx, write_auth_csv(tmp_path / "a.csv", rows=200), "auth")

    profile = client.get(f"/api/datasets/{ds}/profile").json()
    types = {c["name"]: c["semantic_type"] for c in profile["columns"]}
    assert types["src_ip"] == "net.ip"
    assert types["country"] == "geo.country_iso2"

    applicable = client.get(f"/api/plugins?applicable_to={ds}").json()
    ids = {p["id"] for p in applicable}
    # An IP column makes the IP plugins applicable...
    assert {"normalize.ip", "transform.ip_class"} <= ids
    # ...and the absence of coordinates keeps the map plugin out.
    assert "viz.map_points" not in ids


def test_applicable_plugins_include_map_for_geo_data(client, app_ctx, tmp_path):
    ds = _import(client, app_ctx, write_taxi_csv(tmp_path / "t.csv", rows=200), "taxi")
    ids = {p["id"] for p in client.get(f"/api/plugins?applicable_to={ds}").json()}
    assert "viz.map_points" in ids
    assert "normalize.ip" not in ids


def test_suggestions_are_executable_actions(client, app_ctx, tmp_path):
    ds = _import(client, app_ctx, write_taxi_csv(tmp_path / "t.csv", rows=300), "taxi")
    suggestions = client.get(f"/api/datasets/{ds}/suggestions").json()
    assert suggestions

    # A suggestion must be actionable, not prose.
    viz = [s for s in suggestions if s["kind"] == "viz"]
    assert viz and all(s["action"].get("plugin_id") for s in viz)
    assert any("map" in s["action"]["plugin_id"] for s in viz)

    # Replaying a suggestion's action verbatim must work.
    action = next(s["action"] for s in viz if s["action"]["plugin_id"] == "viz.map_points")
    r = client.post("/api/inspect", json={
        "plugin_id": action["plugin_id"], "dataset_id": action["dataset_id"],
        "params": action["params"],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["spec"]["renderer"] == "maplibre"
    assert body["row_count"] > 0
    assert {"lat", "lng"} <= set(body["data"][0])


def test_aggregate_suggestion_round_trip(client, app_ctx, tmp_path):
    ds = _import(client, app_ctx, write_auth_csv(tmp_path / "a.csv", rows=400), "auth")
    suggestions = client.get(f"/api/datasets/{ds}/suggestions?kind=aggregate").json()
    freq = next(s for s in suggestions if s["action"]["plugin_id"] == "agg.frequency"
                and s["action"]["params"]["column"] == "country")

    r = client.post("/api/operations", json=freq["action"])
    assert r.status_code == 202
    app_ctx.runner.wait(r.json()["job_id"], timeout=60)
    job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "succeeded", job

    agg_id = job["steps"][0]["outputs"][0]["dataset_id"]
    rows = client.post("/api/query", json={"dataset": agg_id, "limit": 20}).json()
    cols = set(rows["columns"])
    assert {"country", "n", "share", "rarity"} <= cols
    shares = [r[rows["columns"].index("share")] for r in rows["rows"]]
    assert abs(sum(shares) - 1.0) < 1e-6


def test_query_validation_errors_are_400(client, app_ctx, tmp_path):
    ds = _import(client, app_ctx, write_auth_csv(tmp_path / "a.csv", rows=50), "auth")
    r = client.post("/api/query", json={"dataset": ds, "group_by": ["nope"]})
    assert r.status_code == 400
    assert "unknown column" in r.json()["detail"]


def test_sql_endpoint_rejects_writes(client, app_ctx, tmp_path):
    _import(client, app_ctx, write_auth_csv(tmp_path / "a.csv", rows=50), "auth")
    assert client.post("/api/query/sql", json={"sql": "SELECT 42 AS x"}).json()["rows"] == [[42]]
    r = client.post("/api/query/sql", json={"sql": "CREATE TABLE evil AS SELECT 1"})
    assert r.status_code == 400
    assert "SELECT" in r.json()["detail"]


def test_pin_column_type_survives_and_is_reported(client, app_ctx, tmp_path):
    ds = _import(client, app_ctx, write_auth_csv(tmp_path / "a.csv", rows=100), "auth")
    r = client.post(f"/api/datasets/{ds}/columns/action/type?semantic_type=identity.key&role=key")
    assert r.status_code == 200
    profile = client.get(f"/api/datasets/{ds}/profile").json()
    col = next(c for c in profile["columns"] if c["name"] == "action")
    assert col["semantic_type"] == "identity.key"
    assert col["pinned"] is True


def test_lineage_endpoint(client, app_ctx, tmp_path):
    ds = _import(client, app_ctx, write_auth_csv(tmp_path / "a.csv", rows=100), "auth")
    r = client.post("/api/operations", json={
        "op": "transform", "plugin_id": "normalize.ip",
        "inputs": [{"dataset_id": ds}], "params": {"column": "src_ip"}})
    app_ctx.runner.wait(r.json()["job_id"], timeout=60)

    steps = client.get(f"/api/datasets/{ds}/lineage").json()
    assert [s["op"] for s in steps] == ["import", "transform"]
    assert steps[1]["plugin_id"] == "normalize.ip"


def test_dashboard_round_trip(client, app_ctx, tmp_path):
    ds = _import(client, app_ctx, write_taxi_csv(tmp_path / "t.csv", rows=100), "taxi")
    rendered = client.post("/api/inspect", json={
        "plugin_id": "viz.histogram", "dataset_id": ds,
        "params": {"column": "fare_amount"}}).json()

    saved = client.post("/api/dashboards", json={
        "name": "Fares", "panels": [rendered["spec"]]}).json()
    got = client.get(f"/api/dashboards/{saved['id']}").json()
    assert got["name"] == "Fares"
    assert got["panels"][0]["renderer"] == "vega-lite"


def test_unknown_dataset_is_404(client):
    assert client.get("/api/datasets/nope").status_code == 404
    assert client.get("/api/datasets/nope/profile").status_code == 404
    assert client.get("/api/plugins?applicable_to=nope").status_code == 404


def test_spa_deep_links_fall_back_to_index(app_ctx, tmp_path):
    """A client-side route must survive a refresh or a shared link.

    StaticFiles(html=True) only serves index.html for *directory* paths, so
    without a fallback /datasets/abc/explore 404s: in-app navigation works but
    reloading the page does not.
    """
    from fastapi.testclient import TestClient

    import dataq.api.app as app_module

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>DataQ</title>")
    (dist / "assets").mkdir()
    (dist / "assets" / "app.js").write_text("console.log(1)")

    app_ctx.settings.static_dir = dist
    app = create_app(ctx=app_ctx)
    try:
        with TestClient(app) as client:
            assert client.get("/").status_code == 200
            # Client-side routes resolve to the shell.
            for route in ("/datasets", "/datasets/abc123/explore", "/dashboards", "/ask"):
                r = client.get(route)
                assert r.status_code == 200, route
                assert "DataQ" in r.text, route
            # Real assets are still served as themselves.
            assert client.get("/assets/app.js").text == "console.log(1)"
            # An unknown API path stays a 404 rather than becoming an HTML page,
            # which would surface at the caller as a JSON parse error.
            missing = client.get("/api/does-not-exist")
            assert missing.status_code == 404
            assert "<!doctype html>" not in missing.text.lower()
            # Nor does a missing *file*. Serving index.html with a 200 for a
            # missing chunk cost hours once: the map's web worker fetched HTML,
            # died parsing it as JavaScript inside the worker, and the only
            # symptom was a map that drew no points -- no console error, no
            # failed request. A 404 would have named the problem immediately.
            for asset in ("/assets/gone.js", "/assets/gone.mjs", "/favicon.png"):
                r = client.get(asset)
                assert r.status_code == 404, asset
                assert "<!doctype html>" not in r.text.lower(), asset
    finally:
        app_module.CTX = None


# --------------------------------------------------------------------------- #
# the join form's two questions
# --------------------------------------------------------------------------- #
def test_join_candidates_and_preview(client, app_ctx, tmp_path):
    """What the join panel asks as it is being filled in: which datasets are
    joinable, and what would happen if this key were used."""
    auth = _import(client, app_ctx, write_auth_csv(tmp_path / "auth.csv", rows=400), "auth")
    r = client.post("/api/operations", json={
        "op": "aggregate", "plugin_id": "agg.frequency",
        "inputs": [{"dataset_id": auth}], "params": {"column": "country"},
        "output_name": "freq"})
    app_ctx.runner.wait(r.json()["job_id"], timeout=120)
    freq = client.get(f"/api/jobs/{r.json()['job_id']}").json()[
        "steps"][0]["outputs"][0]["dataset_id"]

    found = client.get(f"/api/datasets/{auth}/join-candidates").json()
    assert freq in [c["dataset_id"] for c in found]
    keys = next(c for c in found if c["dataset_id"] == freq)["keys"]
    assert {"left", "right", "semantic_type", "reason"} <= set(keys[0])

    preview = client.post(f"/api/datasets/{auth}/join-preview", json={
        "right_dataset_id": freq,
        "params": {"left_column": "country", "right_column": "country"}}).json()
    assert preview["fanout"] is False
    assert preview["result_rows"] == preview["left_rows"] == 400
    assert "share" in preview["columns_added"]


def test_join_preview_reports_a_bad_key_as_400(client, app_ctx, tmp_path):
    auth = _import(client, app_ctx, write_auth_csv(tmp_path / "auth.csv", rows=100), "auth")
    r = client.post(f"/api/datasets/{auth}/join-preview", json={
        "right_dataset_id": auth, "params": {}})
    assert r.status_code == 400
    # The message goes into a form, so it says what to do and nothing else.
    assert r.json()["detail"].startswith("a join needs a key")


def test_join_candidates_for_an_unknown_dataset_is_404(client):
    assert client.get("/api/datasets/nope/join-candidates").status_code == 404
