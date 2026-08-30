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
def test_the_built_in_account_does_not_satisfy_require_auth(tmp_path):
    """Its hash is committed, so every clone shares the password.

    Good enough to get started on a laptop; not a secret, and so not something a
    deployment should be able to go live on without noticing.
    """
    from dataq.api.auth import MisconfiguredAuth, check_settings
    from dataq.config import Settings

    with pytest.raises(MisconfiguredAuth, match="not a secret"):
        check_settings(Settings(data_dir=tmp_path, require_auth=True,
                                browse_roots=str(tmp_path)))

    # An explicit user list does satisfy it.
    check_settings(Settings(data_dir=tmp_path, require_auth=True,
                            users="alice:scrypt$abc$def", browse_roots=str(tmp_path)))


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


# --------------------------------------------------------------------------- #
# usernames and passwords
# --------------------------------------------------------------------------- #
def test_a_password_round_trips_but_a_wrong_one_does_not():
    from dataq.api.users import hash_password, verify_password

    stored = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", stored)
    assert not verify_password("Correct horse battery staple", stored)
    assert not verify_password("", stored)


def test_the_same_password_hashes_differently_each_time():
    """Per-password salt: identical passwords must not be identifiable as such."""
    from dataq.api.users import hash_password

    assert hash_password("hunter2") != hash_password("hunter2")


def test_a_malformed_hash_fails_rather_than_raises():
    """This runs on an unauthenticated path, so a bad entry in the user list
    must not become a way to crash the login endpoint."""
    from dataq.api.users import verify_password

    for junk in ("", "nonsense", "md5$x$y", "scrypt$notbase64$$$"):
        assert verify_password("anything", junk) is False


def test_an_unknown_user_and_a_wrong_password_are_indistinguishable():
    from dataq.api.users import authenticate, hash_password

    users = {"alice": hash_password("secret")}
    assert authenticate("alice", "secret", users)
    assert not authenticate("alice", "wrong", users)
    assert not authenticate("mallory", "secret", users)


def test_parsing_a_user_list():
    from dataq.api.users import parse_users

    parsed = parse_users("alice:scrypt$a$b, bob:scrypt$c$d\n# a comment\n")
    assert parsed == {"alice": "scrypt$a$b", "bob": "scrypt$c$d"}
    assert parse_users(None) == {} and parse_users("  ") == {}


def test_a_session_names_its_user_and_expires(tmp_path):
    from dataq.api.users import issue_session, read_session

    secret = b"a" * 32
    assert read_session(issue_session("alice", secret), secret) == "alice"
    assert read_session(issue_session("alice", secret, hours=-1), secret) is None


def test_a_session_cannot_be_forged(tmp_path):
    """Signature before claims: an unsigned token's own expiry is not evidence."""
    from dataq.api.users import issue_session, read_session

    secret, other = b"a" * 32, b"b" * 32
    token = issue_session("alice", secret)
    assert read_session(token, other) is None

    prefix, body, sig = token.split(".", 2)
    forged = f"{prefix}.{body.replace(body[5], 'x', 1)}.{sig}"
    assert read_session(forged, secret) is None
    assert read_session("nonsense", secret) is None


def test_the_signing_key_survives_a_restart(tmp_path):
    """Sessions outliving a restart is the difference between a deploy being
    invisible and everybody being logged out by it."""
    from dataq.api.users import session_secret

    first = session_secret(tmp_path)
    assert session_secret(tmp_path) == first
    assert (tmp_path / "session_secret").exists()


# --------------------------------------------------------------------------- #
# logging in, over HTTP
# --------------------------------------------------------------------------- #
@pytest.fixture
def accounts(tmp_path):
    """An instance with one real account and no shared token."""
    from dataq.api.users import hash_password
    from dataq.config import Settings

    return Settings(data_dir=tmp_path / "data", browse_roots=str(tmp_path),
                    users=f"alice:{hash_password('open sesame')}")


def logged_in_client(settings):
    from dataq.jobs.runner import ThreadPoolJobRunner
    from dataq.services.context import build_context

    ctx = build_context(settings)
    ctx.runner = ThreadPoolJobRunner(ctx.catalog, workers=1)
    return ctx


def test_login_exchanges_a_password_for_a_session(accounts):
    ctx = logged_in_client(accounts)
    try:
        app = create_app(ctx=ctx)
        with TestClient(app) as client:
            assert client.get("/api/datasets").status_code == 401

            bad = client.post("/api/auth/login",
                              json={"username": "alice", "password": "wrong"})
            assert bad.status_code == 401
            assert "wrong username or password" in bad.json()["detail"]

            ok = client.post("/api/auth/login",
                             json={"username": "alice", "password": "open sesame"})
            assert ok.status_code == 200
            token = ok.json()["token"]

            headers = {"Authorization": f"Bearer {token}"}
            assert client.get("/api/datasets", headers=headers).status_code == 200
            assert client.get("/api/auth/me", headers=headers).json()["username"] == "alice"
    finally:
        _teardown(ctx)


def test_the_login_route_itself_is_reachable_without_a_session(accounts):
    """Requiring a credential to reach the place you get one is a closed loop."""
    ctx = logged_in_client(accounts)
    try:
        with TestClient(create_app(ctx=ctx)) as client:
            assert client.post("/api/auth/login",
                               json={"username": "x", "password": "y"}).status_code == 401
            assert client.get("/api/health").status_code == 200
    finally:
        _teardown(ctx)


def test_a_shared_token_still_works_alongside_accounts(accounts):
    """A person logs in; a script, a probe or a curl in the deploy docs carries
    the token. Either is sufficient."""
    accounts.auth_token = "s3cret-token"
    ctx = logged_in_client(accounts)
    try:
        with TestClient(create_app(ctx=ctx)) as client:
            assert client.get(
                "/api/datasets",
                headers={"Authorization": "Bearer s3cret-token"}).status_code == 200
            assert client.get(
                "/api/datasets",
                headers={"Authorization": "Bearer nope"}).status_code == 401
    finally:
        _teardown(ctx)


def _teardown(ctx):
    import dataq.api.app as app_module

    ctx.runner.shutdown()
    ctx.warehouse.close()
    app_module.CTX = None
