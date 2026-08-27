"""Derivation tree: which datasets came out of which.

The catalog already records a DAG in ``Step.inputs``/``Step.outputs``. This turns
the part of it that creates *new datasets* into a tree the UI can nest.

Two things make it a tree rather than a straight rendering of the DAG:

  * A ``transform`` is not an edge. It produces a new version of the dataset it was
    given, so it belongs to that dataset's history, not to its offspring.
  * A ``join`` has two parents but a tree node has one. It nests under its **left**
    input and names the other parent, so the second edge is visible rather than
    silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..catalog.repo import Catalog


@dataclass
class Edge:
    """How a dataset came to exist."""

    parent_id: str
    op: str
    plugin_id: str
    # "primary" is the parent a node nests under; "joined" is the other side of a join.
    role: str


@dataclass
class Node:
    dataset: Any
    edge: Edge | None = None
    others: list[Edge] = field(default_factory=list)
    children: list[Node] = field(default_factory=list)

    def descendants(self) -> int:
        return len(self.children) + sum(c.descendants() for c in self.children)


def derivation_edges(catalog: Catalog) -> dict[str, list[Edge]]:
    """child dataset id -> the edges that produced it, primary first."""
    edges: dict[str, list[Edge]] = {}
    for step in catalog.derivation_steps():
        child = step.outputs[0].get("dataset_id")
        if not child:
            continue
        parents = [i.get("dataset_id") for i in step.inputs if i.get("dataset_id")]
        if not parents:
            continue
        edges[child] = [
            Edge(parent_id=p, op=step.op, plugin_id=step.plugin_id,
                 role="primary" if n == 0 else "joined")
            for n, p in enumerate(parents)
        ]
    return edges


def build_forest(catalog: Catalog) -> list[Node]:
    """Nest every dataset under the one it was derived from.

    A dataset whose parent no longer exists surfaces as a root rather than
    disappearing, and a cycle (which the operations layer should never create)
    is broken rather than recursed into.
    """
    datasets = catalog.list_datasets()
    by_id = {d.id: d for d in datasets}
    edges = derivation_edges(catalog)

    nodes = {d.id: Node(dataset=d) for d in datasets}
    roots: list[Node] = []

    for dataset_id, node in nodes.items():
        found = edges.get(dataset_id, [])
        primary = next((e for e in found if e.role == "primary"), None)
        node.others = [e for e in found if e.role != "primary"]

        if primary is None or primary.parent_id not in by_id:
            roots.append(node)
            continue
        if _would_cycle(dataset_id, primary.parent_id, edges):
            roots.append(node)
            continue
        node.edge = primary
        nodes[primary.parent_id].children.append(node)

    def sort_key(n: Node) -> tuple:
        # Sources first, then derived datasets, newest first within each group --
        # the same ordering the flat list used.
        return (n.dataset.kind != "source", -n.dataset.created_at.timestamp())

    def sort_tree(items: list[Node]) -> None:
        items.sort(key=sort_key)
        for item in items:
            sort_tree(item.children)

    sort_tree(roots)
    return roots


def _would_cycle(child_id: str, parent_id: str, edges: dict[str, list[Edge]]) -> bool:
    """True if ``parent_id`` already descends from ``child_id``."""
    seen: set[str] = set()
    cursor: str | None = parent_id
    while cursor and cursor not in seen:
        if cursor == child_id:
            return True
        seen.add(cursor)
        found = edges.get(cursor, [])
        primary = next((e for e in found if e.role == "primary"), None)
        cursor = primary.parent_id if primary else None
    return False


def related(catalog: Catalog, dataset_id: str) -> dict:
    """Immediate parents and children of one dataset, for its detail page."""
    by_id = {d.id: d for d in catalog.list_datasets()}
    if dataset_id not in by_id:
        raise KeyError(f"unknown dataset: {dataset_id}")
    edges = derivation_edges(catalog)

    parents = [
        {"id": e.parent_id, "name": by_id[e.parent_id].name,
         "kind": by_id[e.parent_id].kind, "op": e.op,
         "plugin_id": e.plugin_id, "role": e.role}
        for e in edges.get(dataset_id, [])
        if e.parent_id in by_id
    ]

    children = []
    for child_id, found in edges.items():
        if child_id not in by_id:
            continue
        for e in found:
            if e.parent_id != dataset_id:
                continue
            child = by_id[child_id]
            children.append({
                "id": child.id, "name": child.name, "kind": child.kind,
                "op": e.op, "plugin_id": e.plugin_id, "role": e.role,
                "row_count": _row_count(catalog, child.id),
            })
    children.sort(key=lambda c: c["name"])
    return {"parents": parents, "children": children}


def _row_count(catalog: Catalog, dataset_id: str) -> int:
    version = catalog.get_version(dataset_id)
    return version.row_count if version else 0
