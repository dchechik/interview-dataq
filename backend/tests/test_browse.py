"""File browsing.

The security-relevant behaviour is path confinement: a hosted deployment must not
be walkable from the UI. Traversal, absolute escapes and symlinks out are all
checked here, since each is a distinct way to leave a root.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dataq.api.app import create_app
from dataq.config import Settings
from dataq.services.browse import (
    BrowseError,
    list_directory,
    resolve_within_roots,
    safe_upload_path,
)

from .fixtures import write_auth_csv, write_taxi_csv


@pytest.fixture
def tree(tmp_path):
    """A small directory tree, plus a sibling the browser must never reach."""
    root = tmp_path / "workspace"
    (root / "nested").mkdir(parents=True)
    write_taxi_csv(root / "taxi.csv", rows=20)
    write_auth_csv(root / "nested" / "auth.csv", rows=20)
    (root / "notes.md").write_text("not a data file")
    (root / ".hidden.csv").write_text("a,b\n1,2\n")

    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "private.csv").write_text("ssn\n1\n")
    return {"root": root, "secret": secret}


@pytest.fixture
def browse_settings(tmp_path, tree):
    return Settings(data_dir=tmp_path / "data", browse_roots=str(tree["root"]))


def test_lists_directories_and_data_files(browse_settings, tree):
    out = list_directory(str(tree["root"]), browse_settings)
    names = [e["name"] for e in out["entries"]]

    # Directories sort first, then data files.
    assert names[0] == "nested"
    assert "taxi.csv" in names
    # Files no reader can handle are not offered.
    assert "notes.md" not in names
    # Dotfiles are hidden by default.
    assert ".hidden.csv" not in names

    taxi = next(e for e in out["entries"] if e["name"] == "taxi.csv")
    assert taxi["importable"] is True
    assert taxi["reader_id"] == "read.csv"
    assert taxi["size"] > 0
    assert next(e for e in out["entries"] if e["name"] == "nested")["is_dir"] is True


def test_show_hidden_opt_in(browse_settings, tree):
    out = list_directory(str(tree["root"]), browse_settings, show_hidden=True)
    assert ".hidden.csv" in [e["name"] for e in out["entries"]]


def test_parent_is_offered_only_inside_a_root(browse_settings, tree):
    nested = list_directory(str(tree["root"] / "nested"), browse_settings)
    assert nested["parent"] == str(tree["root"])

    # At the root itself there is nowhere up to go -- otherwise the UI would
    # offer a link that walks straight out of the sandbox.
    top = list_directory(str(tree["root"]), browse_settings)
    assert top["parent"] is None


def test_defaults_to_the_first_root(browse_settings, tree):
    assert list_directory(None, browse_settings)["path"] == str(tree["root"])


@pytest.mark.parametrize("attempt", ["..", "../secret", "../../etc", "nested/../../secret"])
def test_traversal_is_refused(browse_settings, tree, attempt):
    with pytest.raises(BrowseError, match="outside"):
        resolve_within_roots(str(tree["root"] / attempt), browse_settings)


def test_absolute_path_outside_roots_is_refused(browse_settings, tree):
    with pytest.raises(BrowseError, match="outside"):
        resolve_within_roots(str(tree["secret"]), browse_settings)
    with pytest.raises(BrowseError, match="outside"):
        resolve_within_roots("/etc", browse_settings)


def test_symlink_out_of_a_root_is_refused(browse_settings, tree):
    """resolve() follows the link before the check, so the target is what counts."""
    link = tree["root"] / "escape"
    link.symlink_to(tree["secret"])
    with pytest.raises(BrowseError, match="outside"):
        resolve_within_roots(str(link), browse_settings)


def test_a_file_path_resolves_to_its_directory(browse_settings, tree):
    out = list_directory(str(tree["root"] / "taxi.csv"), browse_settings)
    assert out["path"] == str(tree["root"])


def test_upload_dir_is_always_browsable(tmp_path, tree):
    """Otherwise an uploaded file could not be picked afterwards."""
    settings = Settings(data_dir=tmp_path / "data", browse_roots=str(tree["root"]))
    roots = settings.resolved_browse_roots()
    assert settings.upload_dir.resolve() in roots


def test_upload_path_is_sanitised(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    # A directory component in the client-supplied name must not escape: only the
    # basename survives, so these land in the upload dir like anything else.
    assert safe_upload_path("../../etc/passwd", settings).parent == settings.upload_dir
    assert safe_upload_path("../../etc/passwd", settings).name == "passwd"
    assert safe_upload_path("/abs/path/data.csv", settings).name == "data.csv"
    # Shell metacharacters in the remaining name are replaced.
    assert safe_upload_path("a b;rm -rf.csv", settings).name == "a_b_rm_-rf.csv"
    assert safe_upload_path("", settings).name == "upload"


def test_upload_path_does_not_clobber(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_dirs()
    first = safe_upload_path("data.csv", settings)
    first.write_text("x")
    second = safe_upload_path("data.csv", settings)
    assert second.name == "data-1.csv"


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(app_ctx, tree):
    import dataq.api.app as app_module

    app_ctx.settings.browse_roots = str(tree["root"])
    app = create_app(ctx=app_ctx)
    with TestClient(app) as c:
        yield c
    app_module.CTX = None


def test_browse_endpoint(client, tree):
    body = client.get("/api/sources/browse", params={"path": str(tree["root"])}).json()
    assert [e["name"] for e in body["entries"]][0] == "nested"
    assert body["roots"]


def test_browse_endpoint_refuses_escape(client, tree):
    r = client.get("/api/sources/browse", params={"path": str(tree["secret"])})
    assert r.status_code == 403
    assert "outside" in r.json()["detail"]


def test_upload_then_import_round_trip(client, app_ctx, tree):
    """The whole point: an uploaded file yields a path the server can import."""
    payload = (tree["root"] / "taxi.csv").read_bytes()
    up = client.post("/api/sources/upload", files={"file": ("taxi.csv", payload, "text/csv")})
    assert up.status_code == 200, up.text
    uri = up.json()["uri"]
    assert up.json()["bytes"] == len(payload)

    preview = client.post("/api/sources/preview", json={"uri": uri, "limit": 3}).json()
    assert "pickup_latitude" in preview["columns"]

    accepted = client.post("/api/operations", json={"op": "import", "uri": uri, "name": "up"})
    assert accepted.status_code == 202
    app_ctx.runner.wait(accepted.json()["job_id"], timeout=60)
    job = client.get(f"/api/jobs/{accepted.json()['job_id']}").json()
    assert job["status"] == "succeeded", job


def test_upload_over_the_limit_is_rejected(client, app_ctx):
    app_ctx.settings.max_upload_mb = 0  # anything non-empty exceeds it
    r = client.post("/api/sources/upload", files={"file": ("big.csv", b"a,b\n1,2\n", "text/csv")})
    assert r.status_code == 413
    assert "Browse" in r.json()["detail"]
    # The partial file must not be left behind.
    assert list(app_ctx.settings.upload_dir.glob("big*")) == []
