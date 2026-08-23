"""Semantic detection must be right on the two v0 target datasets."""

from __future__ import annotations

import pytest

import dataq.plugins.builtin  # noqa: F401  (registers plugins)
from dataq.plugins.builtin.readers import CsvParams, CsvReader, pick_reader
from dataq.services.profiler import compute_stats, profile_columns

from .fixtures import write_auth_csv, write_taxi_csv


def _profile(conn, path):
    rel = CsvReader().to_relation(conn, str(path), CsvParams())
    rel.create_view("src", replace=True)
    cols = list(zip(rel.columns, [str(t) for t in rel.types]))
    stats = compute_stats(conn, "src", cols, sample_rows=5000)
    return {p.name: p for p in profile_columns(stats)}


@pytest.fixture
def conn(warehouse):
    return warehouse.cursor()


def test_taxi_semantic_types(conn, tmp_path):
    p = _profile(conn, write_taxi_csv(tmp_path / "taxi.csv"))
    assert p["pickup_latitude"].semantic_type == "geo.lat"
    assert p["pickup_longitude"].semantic_type == "geo.lng"
    assert p["tpep_pickup_datetime"].semantic_type == "time.timestamp"
    assert p["fare_amount"].semantic_type == "money.amount"
    assert p["payment_type"].semantic_type == "categorical"
    assert p["trip_id"].semantic_type == "identity.key"
    # Roles follow from semantic types and drive chart suggestion.
    assert p["pickup_latitude"].role == "geo"
    assert p["tpep_pickup_datetime"].role == "time"
    assert p["fare_amount"].role == "measure"


def test_auth_log_semantic_types(conn, tmp_path):
    p = _profile(conn, write_auth_csv(tmp_path / "auth.csv"))
    assert p["src_ip"].semantic_type == "net.ip"
    assert p["country"].semantic_type == "geo.country_iso2"
    assert p["user_email"].semantic_type == "identity.email"
    assert p["ts"].semantic_type == "time.timestamp"
    assert p["action"].semantic_type == "categorical"


def test_candidates_are_recorded_even_when_not_applied(conn, tmp_path):
    p = _profile(conn, write_auth_csv(tmp_path / "auth.csv"))
    country = p["country"]
    # The cardinality detector also fires; it must be outranked but still visible
    # so the UI can offer it as an alternative.
    types = {c.semantic_type for c in country.candidates}
    assert "geo.country_iso2" in types and "categorical" in types
    assert country.candidates[0].semantic_type == "geo.country_iso2"


def test_pinned_types_survive_reprofiling(conn, tmp_path):
    path = write_auth_csv(tmp_path / "auth.csv")
    first = list(_profile(conn, path).values())
    for c in first:
        if c.name == "action":
            c.semantic_type, c.pinned, c.role = "identity.key", True, "key"

    rel = CsvReader().to_relation(conn, str(path), CsvParams())
    rel.create_view("src2", replace=True)
    stats = compute_stats(conn, "src2", list(zip(rel.columns, [str(t) for t in rel.types])), 5000)
    again = {p.name: p for p in profile_columns(stats, previous=first)}
    assert again["action"].semantic_type == "identity.key"
    assert again["action"].pinned is True
    # Unpinned columns still re-detect normally.
    assert again["src_ip"].semantic_type == "net.ip"


def test_pick_reader_by_extension():
    assert pick_reader("/x/a.csv").id == "read.csv"
    assert pick_reader("/x/a.parquet").id == "read.parquet"
    assert pick_reader("s3://b/a.ndjson").id == "read.json"
    assert pick_reader("/x/a.unknown") is None
