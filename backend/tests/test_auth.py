"""Access control, and the file-read holes it sits in front of.

The app is safe on a laptop because nobody else can reach it. These are the
tests that it is also safe when it is reachable.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dataq.api.app import create_app
from dataq.api.auth import MisconfiguredAuth

from .fixtures import write_auth_csv

TOKEN = "s3cret-deploy-token"


@pytest.fixture
def client(app_ctx):
    """An instance with auth switched on."""
    import dataq.api.app as app_module

    app_ctx.settings.auth_token = TOKEN
    app = create_app(ctx=app_ctx)
    with TestClient(app) as c:
        yield c
    app_module.CTX = None


def auth(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# fail closed
# --------------------------------------------------------------------------- #
def test_requiring_auth_without_a_token_refuses_to_start(app_ctx):
    """A deployment must not come up unprotected because a variable was missed."""
    app_ctx.settings.require_auth = True
    app_ctx.settings.auth_token = None
    with pytest.raises(MisconfiguredAuth, match="DATAQ_AUTH_TOKEN"):
        create_app(ctx=app_ctx)


def test_requiring_auth_with_a_token_starts(app_ctx):
    import dataq.api.app as app_module

    app_ctx.settings.require_auth = True
    app_ctx.settings.auth_token = TOKEN
    try:
        assert create_app(ctx=app_ctx) is not None
    finally:
        app_module.CTX = None


def test_no_token_configured_leaves_the_api_open(app_ctx):
    """Local dev must not need ceremony."""
    import dataq.api.app as app_module

    app_ctx.settings.auth_token = None
    app = create_app(ctx=app_ctx)
    try:
        with TestClient(app) as c:
            assert c.get("/api/datasets").status_code == 200
    finally:
        app_module.CTX = None


# --------------------------------------------------------------------------- #
# the check itself
# --------------------------------------------------------------------------- #
def test_health_stays_public(client):
    """The platform probes this to decide whether the deploy is live."""
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/datasets"),
        ("get", "/api/plugins"),
        ("get", "/api/sources/browse"),
        ("post", "/api/query/sql"),
        ("post", "/api/agent/estimate"),
        ("post", "/api/operations"),
    ],
)
def test_every_other_route_needs_the_token(client, method, path):
    kwargs = {"json": {}} if method == "post" else {}
    r = getattr(client, method)(path, **kwargs)
    assert r.status_code == 401, path
    assert r.headers.get("www-authenticate") == "Bearer"


def test_a_valid_token_gets_through(client):
    assert client.get("/api/datasets", headers=auth()).status_code == 200


def test_a_wrong_token_is_rejected(client):
    assert client.get("/api/datasets", headers=auth("nope")).status_code == 401
    # A prefix of the real token must not pass either.
    assert client.get("/api/datasets", headers=auth(TOKEN[:-1])).status_code == 401


def test_a_malformed_header_is_rejected(client):
    for header in ({"Authorization": TOKEN}, {"Authorization": "Basic " + TOKEN}, {}):
        assert client.get("/api/datasets", headers=header).status_code == 401


def test_the_query_parameter_works_for_event_streams(client):
    """EventSource cannot set headers, and job progress is an SSE endpoint."""
    assert client.get(f"/api/datasets?token={TOKEN}").status_code == 200
    assert client.get("/api/datasets?token=wrong").status_code == 401


def test_the_spa_itself_is_not_behind_the_token(app_ctx, tmp_path):
    """The bundle is not the secret; the data behind /api is."""
    import dataq.api.app as app_module

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>DataQ</title>")
    app_ctx.settings.auth_token = TOKEN
    app_ctx.settings.static_dir = dist
    app = create_app(ctx=app_ctx)
    try:
        with TestClient(app) as c:
            assert c.get("/").status_code == 200
            assert c.get("/datasets").status_code == 200
            assert c.get("/api/datasets").status_code == 401
    finally:
        app_module.CTX = None


# --------------------------------------------------------------------------- #
# what auth is protecting: arbitrary server-side file reads
# --------------------------------------------------------------------------- #
def test_preview_refuses_a_path_outside_the_browse_roots(client):
    """This used to read any file the server could open."""
    r = client.post("/api/sources/preview", json={"uri": "/etc/passwd"}, headers=auth())
    assert r.status_code == 403
    assert "outside" in r.json()["detail"]


def test_import_refuses_a_path_outside_the_browse_roots(client, app_ctx):
    accepted = client.post(
        "/api/operations", json={"op": "import", "uri": "/etc/passwd", "name": "leak"},
        headers=auth(),
    )
    assert accepted.status_code == 202
    app_ctx.runner.wait(accepted.json()["job_id"], timeout=30)
    job = app_ctx.catalog.get_job(accepted.json()["job_id"])
    assert job.status == "failed"
    assert "outside" in job.error


def test_remote_uris_are_off_by_default(client):
    """Fetching a URL makes the server issue outbound requests for a caller."""
    r = client.post(
        "/api/sources/preview", json={"uri": "https://example.com/x.csv"}, headers=auth()
    )
    assert r.status_code == 403
    assert "remote URIs are disabled" in r.json()["detail"]


def test_an_allowed_path_still_works(client, app_ctx, tmp_path):
    """Containment must not break the normal case."""
    path = write_auth_csv(tmp_path / "ok.csv", rows=20)
    r = client.post("/api/sources/preview", json={"uri": str(path), "limit": 3},
                    headers=auth())
    assert r.status_code == 200
    assert "src_ip" in r.json()["columns"]
