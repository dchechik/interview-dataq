"""Storage backends must be behaviourally identical, including under interruption."""

from __future__ import annotations

import pyarrow as pa

from dataq.storage import VersionRef


def _batches(n_parts: int, rows_per: int) -> list[list[pa.RecordBatch]]:
    out = []
    for p in range(n_parts):
        base = p * rows_per
        out.append([
            pa.RecordBatch.from_pydict(
                {
                    "id": list(range(base, base + rows_per)),
                    "name": [f"n{i}" for i in range(base, base + rows_per)],
                }
            )
        ])
    return out


SCHEMA = pa.schema([("id", pa.int64()), ("name", pa.string())])


def test_write_relation_round_trip(storage, warehouse):
    ref = VersionRef(dataset_id="ds1", version=1)
    with warehouse.cur() as conn:
        stored = storage.write_relation(
            ref, "SELECT i AS id, 'n' || i AS name FROM range(100) t(i)", conn
        )
        assert stored.rows == 100
        rows = conn.execute(
            f"SELECT count(*), min(id), max(id) FROM {storage.sql_source(stored)}"
        ).fetchone()
    assert rows == (100, 0, 99)


def test_part_writer_round_trip(storage, warehouse):
    ref = VersionRef(dataset_id="ds2", version=1)
    with warehouse.cur() as conn:
        w = storage.open_writer(ref, SCHEMA, conn)
        for i, part in enumerate(_batches(4, 25)):
            w.write_part(i, part)
        stored = w.finalize()
        assert stored.rows == 100
        got = conn.execute(
            f"SELECT id, name FROM {storage.sql_source(stored)} ORDER BY id"
        ).fetchall()
    assert got[0] == (0, "n0") and got[-1] == (99, "n99")
    assert len(got) == 100


def test_resume_from_watermark_matches_uninterrupted_run(storage, warehouse):
    """The core resumability guarantee: a job killed mid-run and resumed produces
    exactly the same rows as one that ran straight through."""
    parts = _batches(4, 25)

    with warehouse.cur() as conn:
        # Uninterrupted.
        ref_a = VersionRef(dataset_id="clean", version=1)
        wa = storage.open_writer(ref_a, SCHEMA, conn)
        for i, p in enumerate(parts):
            wa.write_part(i, p)
        clean = wa.finalize()
        expected = conn.execute(
            f"SELECT id, name FROM {storage.sql_source(clean)} ORDER BY id"
        ).fetchall()

        # Interrupted after 2 parts, then resumed by a fresh writer.
        ref_b = VersionRef(dataset_id="resumed", version=1)
        wb = storage.open_writer(ref_b, SCHEMA, conn)
        wb.write_part(0, parts[0])
        wb.write_part(1, parts[1])
        assert wb.committed_parts() == 2

        wb2 = storage.open_writer(ref_b, SCHEMA, conn)
        watermark = wb2.committed_parts()
        assert watermark == 2
        wb2.discard_from(watermark)  # no-op here, but must be safe
        for i in range(watermark, len(parts)):
            wb2.write_part(i, parts[i])
        resumed = wb2.finalize()
        actual = conn.execute(
            f"SELECT id, name FROM {storage.sql_source(resumed)} ORDER BY id"
        ).fetchall()

    assert actual == expected
    assert len(actual) == 100


def test_discard_from_removes_torn_parts(storage, warehouse):
    parts = _batches(3, 10)
    ref = VersionRef(dataset_id="torn", version=1)
    with warehouse.cur() as conn:
        w = storage.open_writer(ref, SCHEMA, conn)
        for i, p in enumerate(parts):
            w.write_part(i, p)
        assert w.committed_parts() == 3
        w.discard_from(1)
        assert w.committed_parts() == 1
        stored = w.finalize()
    assert stored.rows == 10


def test_drop(storage, warehouse):
    ref = VersionRef(dataset_id="gone", version=1)
    with warehouse.cur() as conn:
        stored = storage.write_relation(ref, "SELECT 1 AS id, 'x' AS name", conn)
        storage.drop(stored, conn)
