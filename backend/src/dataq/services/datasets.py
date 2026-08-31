"""Deleting datasets.

Deletion is the one operation that cannot be undone by re-running a step, so it
is the one that has to be careful. Three things it must get right, none of which
the catalog can do on its own:

* **Free the disk.** The catalog only knows about rows; the parquet parts and
  DuckDB tables live behind the storage backend. Removing the metadata and
  leaving the bytes is the failure mode where a lake grows forever and the
  dataset list says nothing is there.

* **Not strand a derivation tree.** An aggregate built from a dataset stays
  perfectly valid after its parent goes -- it was materialised independently --
  but its provenance does not. Deleting a parent from under three aggregates is
  rarely what someone means, so it takes an explicit cascade.

* **Not race a running job.** A job writing a new version of a dataset that is
  being deleted underneath it produces a version row pointing at files that no
  longer exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..storage.base import StoredRef
from .context import AppContext
from .lineage import derivation_edges


class DeleteRefused(ValueError):
    """The delete would have destroyed or corrupted something unasked."""


@dataclass
class DeletedVersion:
    """What deleting one version removed."""

    dataset_id: str
    version: int
    bytes_freed: int = 0


@dataclass
class Deleted:
    """What a delete actually removed."""

    datasets: list[dict] = field(default_factory=list)
    versions: int = 0
    bytes_freed: int = 0

    @property
    def ids(self) -> list[str]:
        return [d["id"] for d in self.datasets]


def descendants(ctx: AppContext, dataset_id: str) -> list[str]:
    """Every dataset derived from this one, deepest last.

    Follows *all* parents, not just the primary one, so a join nested under its
    left input is still recognised as depending on its right input.
    """
    children: dict[str, list[str]] = {}
    for child, edges in derivation_edges(ctx.catalog).items():
        for edge in edges:
            children.setdefault(edge.parent_id, []).append(child)

    out: list[str] = []
    seen = {dataset_id}
    queue = [dataset_id]
    while queue:
        current = queue.pop(0)
        for child in children.get(current, []):
            if child in seen:
                continue  # a diamond in the DAG, or a cycle
            seen.add(child)
            # Steps outlive the datasets they produced -- deleting a dataset
            # leaves its lineage intact, which is the point of keeping it. So
            # an edge is not evidence the child is still there, and without
            # this check a parent stays undeletable forever after its children
            # have gone.
            if ctx.catalog.get_dataset(child) is None:
                continue
            out.append(child)
            queue.append(child)
    return out


def _active_jobs(ctx: AppContext, dataset_ids: set[str]) -> list[str]:
    """Titles of unfinished jobs that touch any of these datasets."""
    busy: list[str] = []
    for job in ctx.catalog.list_jobs(limit=200):
        if job.status not in ("queued", "running", "paused"):
            continue
        for step in ctx.catalog.list_steps(job.id):
            refs = {r.get("dataset_id") for r in (*step.inputs, *step.outputs)}
            if refs & dataset_ids:
                busy.append(job.title or job.id)
                break
    return busy


def delete_dataset(ctx: AppContext, dataset_id: str, cascade: bool = False) -> Deleted:
    """Delete a dataset, its versions, and the bytes behind them."""
    dataset = ctx.catalog.get_dataset(dataset_id)
    if dataset is None:
        raise KeyError(f"unknown dataset: {dataset_id}")

    derived = descendants(ctx, dataset_id)
    if derived and not cascade:
        names = ", ".join(
            (d.name if (d := ctx.catalog.get_dataset(i)) else i) for i in derived[:5]
        )
        more = f" and {len(derived) - 5} more" if len(derived) > 5 else ""
        raise DeleteRefused(
            f"{dataset.name} has {len(derived)} dataset(s) derived from it "
            f"({names}{more}). Delete those first, or pass cascade=true to "
            "remove them together."
        )

    targets = [*derived, dataset_id] if cascade else [dataset_id]
    busy = _active_jobs(ctx, set(targets))
    if busy:
        raise DeleteRefused(
            f"a job is still running against this data ({busy[0]}). Wait for it "
            "or cancel it first -- deleting now would leave it writing to files "
            "that no longer exist."
        )

    out = Deleted()
    # Deepest first, so a parent never disappears before its children.
    with ctx.warehouse.cur() as conn:
        for target in targets:
            row = ctx.catalog.get_dataset(target)
            if row is None:
                continue
            for version in ctx.catalog.list_versions(target):
                stored = StoredRef(**version.stored_ref)
                out.bytes_freed += stored.bytes
                out.versions += 1
                try:
                    with ctx.warehouse.ddl_lock:
                        ctx.storage.drop(stored, conn)
                except Exception as exc:  # noqa: BLE001
                    # Losing the bytes is bad; losing the catalog row *and* the
                    # bytes' whereabouts is worse, so the row stays and the
                    # failure is reported rather than swallowed.
                    raise DeleteRefused(
                        f"could not remove the stored data for {row.name} "
                        f"v{version.version}: {exc}"
                    ) from exc
            # Then everything else under that id, which catches data left by a
            # run that wrote files but never recorded the version.
            with ctx.warehouse.ddl_lock:
                ctx.storage.drop_dataset(target, conn)
            ctx.catalog.delete_dataset(target)
            out.datasets.append({"id": target, "name": row.name, "kind": row.kind})
    return out


def delete_version(ctx: AppContext, dataset_id: str, version: int) -> DeletedVersion:
    """Delete one version of a dataset, and the bytes behind it.

    Two versions are refused, and the reasons are different:

    * **The only one.** A dataset with no version cannot be queried, profiled or
      explained -- it is the ghost that :func:`_new_dataset` exists to prevent.
      Deleting the data means deleting the dataset, so it says so.
    * **The newest one.** It is what every query, chart and dashboard resolves
      to, so removing it silently changes what the dataset means. It is also the
      number ``next_version`` allocates from, and those numbers are never
      reused: dropping the newest would either strand the high-water mark above
      anything that exists, or hand the next write a number an existing step's
      provenance already refers to. Revert to the version you want first -- that
      writes a new newest -- and this one is then an ordinary older version.

    Together with revert, that is still complete: any state you can describe is
    reachable by reverting forward and pruning behind.
    """
    dataset = ctx.catalog.get_dataset(dataset_id)
    if dataset is None:
        raise KeyError(f"unknown dataset: {dataset_id}")

    versions = ctx.catalog.list_versions(dataset_id)  # newest first
    target = next((v for v in versions if v.version == version), None)
    if target is None:
        raise KeyError(f"{dataset.name} has no version {version}")

    if len(versions) == 1:
        raise DeleteRefused(
            f"v{version} is the only version of {dataset.name}, and a dataset "
            "with no data cannot be queried or explained. Delete the dataset "
            "itself instead."
        )
    if version == versions[0].version:
        raise DeleteRefused(
            f"v{version} is the current version of {dataset.name} -- every "
            "query, chart and dashboard reads it. Revert to the version you "
            "want first; that writes a new current version, and this one "
            "becomes an ordinary older version you can delete."
        )

    busy = _active_jobs(ctx, {dataset_id})
    if busy:
        raise DeleteRefused(
            f"a job is still running against this data ({busy[0]}). Wait for it "
            "or cancel it first -- deleting now would leave it writing to files "
            "that no longer exist."
        )

    stored = StoredRef(**target.stored_ref)
    with ctx.warehouse.cur() as conn:
        try:
            with ctx.warehouse.ddl_lock:
                ctx.storage.drop(stored, conn)
        except Exception as exc:  # noqa: BLE001
            # Same order as delete_dataset: the row outlives a failed drop, so
            # the bytes still have something pointing at them.
            raise DeleteRefused(
                f"could not remove the stored data for {dataset.name} "
                f"v{version}: {exc}"
            ) from exc
    ctx.catalog.delete_version(target.id)
    return DeletedVersion(dataset_id=dataset_id, version=version,
                          bytes_freed=stored.bytes)
