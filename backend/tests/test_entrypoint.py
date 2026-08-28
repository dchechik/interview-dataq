"""The container entrypoint's restore-on-boot behaviour.

This is how data reaches a hosted instance, and it runs before the app opens
SQLite or DuckDB -- which is the whole point, since replacing those files under
a live process risks corrupting them. Worth testing directly rather than only
discovering it against a real deployment.

The script is exercised as a script, with a stub `uvicorn` on PATH so the final
`exec` returns instead of starting a server.
"""

from __future__ import annotations

import os
import subprocess
import tarfile
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[2] / "docker" / "entrypoint.sh"


@pytest.fixture
def stub_bin(tmp_path) -> Path:
    """A fake uvicorn that records its arguments and exits."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "uvicorn"
    stub.write_text('#!/bin/sh\necho "uvicorn $*"\n')
    stub.chmod(0o755)
    return bin_dir


def make_bundle(path: Path, files: dict[str, str]) -> Path:
    """A .tgz laid out like a real data dir."""
    staging = path.parent / "staging"
    staging.mkdir(exist_ok=True)
    for name, content in files.items():
        target = staging / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    with tarfile.open(path, "w:gz") as tar:
        for name in files:
            tar.add(staging / name, arcname=name)
    return path


def run(data_dir: Path, stub_bin: Path, **env) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(ENTRYPOINT)],
        capture_output=True, text=True, timeout=30,
        env={
            **os.environ,
            "PATH": f"{stub_bin}:{os.environ['PATH']}",
            "DATAQ_DATA_DIR": str(data_dir),
            **env,
        },
    )


@pytest.fixture
def data_dir(tmp_path) -> Path:
    d = tmp_path / "data"
    (d / "_inbox").mkdir(parents=True)
    return d


def test_starts_the_server_when_there_is_nothing_to_restore(data_dir, stub_bin):
    result = run(data_dir, stub_bin)
    assert result.returncode == 0, result.stderr
    assert "uvicorn dataq.api.app:app" in result.stdout


def test_binds_the_injected_port(data_dir, stub_bin):
    """Railway injects PORT; ignoring it means the healthcheck never passes."""
    assert "--port 8080" in run(data_dir, stub_bin, PORT="8080").stdout
    # And falls back for a plain `docker run`.
    assert "--port 8000" in run(data_dir, stub_bin).stdout


def test_a_bundle_is_unpacked_before_the_server_starts(data_dir, stub_bin):
    make_bundle(
        data_dir / "_inbox" / "data.tgz",
        {"catalog.sqlite": "db", "lake/ds/v1/part-00000.parquet": "cols"},
    )
    result = run(data_dir, stub_bin)
    assert result.returncode == 0, result.stderr

    assert (data_dir / "catalog.sqlite").read_text() == "db"
    assert (data_dir / "lake/ds/v1/part-00000.parquet").read_text() == "cols"
    # Restore happens first, then the server starts.
    assert result.stdout.index("restore complete") < result.stdout.index("uvicorn")


def test_the_bundle_is_renamed_so_a_restart_does_not_redo_it(data_dir, stub_bin):
    make_bundle(data_dir / "_inbox" / "data.tgz", {"catalog.sqlite": "first"})
    run(data_dir, stub_bin)
    assert not (data_dir / "_inbox" / "data.tgz").exists()
    assert list((data_dir / "_inbox").glob("data.tgz.applied-*"))

    # A later restart must leave the (now possibly changed) data alone.
    (data_dir / "catalog.sqlite").write_text("changed since")
    result = run(data_dir, stub_bin)
    assert "restoring" not in result.stdout
    assert (data_dir / "catalog.sqlite").read_text() == "changed since"


def test_merge_mode_leaves_existing_data_in_place(data_dir, stub_bin):
    (data_dir / "keep.txt").write_text("still here")
    make_bundle(data_dir / "_inbox" / "data.tgz", {"catalog.sqlite": "new"})
    run(data_dir, stub_bin)
    assert (data_dir / "keep.txt").exists()
    assert (data_dir / "catalog.sqlite").read_text() == "new"


def test_replace_mode_is_a_reset(data_dir, stub_bin):
    """The 'reset the deployed data' path."""
    (data_dir / "stale.txt").write_text("from a previous life")
    (data_dir / "lake").mkdir()
    (data_dir / "lake" / "old.parquet").write_text("old")
    make_bundle(data_dir / "_inbox" / "data.tgz", {"catalog.sqlite": "fresh"})

    run(data_dir, stub_bin, DATAQ_RESTORE_MODE="replace")

    assert not (data_dir / "stale.txt").exists()
    assert not (data_dir / "lake" / "old.parquet").exists()
    assert (data_dir / "catalog.sqlite").read_text() == "fresh"
    # The inbox itself must survive -- it holds the bundle being read.
    assert (data_dir / "_inbox").is_dir()


def test_a_missing_data_dir_is_created(tmp_path, stub_bin):
    fresh = tmp_path / "never-existed"
    result = run(fresh, stub_bin)
    assert result.returncode == 0, result.stderr
    assert (fresh / "_inbox").is_dir()


def test_tar_gz_extension_is_also_accepted(data_dir, stub_bin):
    make_bundle(data_dir / "_inbox" / "data.tar.gz", {"catalog.sqlite": "db"})
    run(data_dir, stub_bin)
    assert (data_dir / "catalog.sqlite").exists()


def test_a_replace_bundle_resets_by_its_filename(data_dir, stub_bin):
    """Intent travels with the file, so a stale env var cannot cause a wipe."""
    (data_dir / "stale.txt").write_text("from a previous life")
    make_bundle(data_dir / "_inbox" / "data.replace.tgz", {"catalog.sqlite": "fresh"})

    result = run(data_dir, stub_bin)  # no DATAQ_RESTORE_MODE set

    assert "mode=replace" in result.stdout
    assert not (data_dir / "stale.txt").exists()
    assert (data_dir / "catalog.sqlite").read_text() == "fresh"


def test_a_plain_bundle_does_not_reset_even_next_to_a_replace_name(data_dir, stub_bin):
    (data_dir / "keep.txt").write_text("still here")
    make_bundle(data_dir / "_inbox" / "data.tgz", {"catalog.sqlite": "new"})
    result = run(data_dir, stub_bin)
    assert "mode=merge" in result.stdout
    assert (data_dir / "keep.txt").exists()
