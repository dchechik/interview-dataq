"""Meanings people define themselves.

The built-in semantic types are the ones a detector can recognise from values
alone: an IP address looks like an IP address anywhere. Most of what a column
actually means does not survive that test. Nothing in a list of machine names,
cost centres, badge numbers or ticket queues distinguishes it from any other
short string, so every such column arrives with no meaning at all -- and a
column with no meaning satisfies no plugin's ``Accepts`` gate, joins to nothing,
and contributes nothing to suggestion.

The missing piece is not a cleverer detector. It is that the fact is only
knowable by a person: whether ``pc`` in the browsing log and ``device`` in the
asset inventory refer to the same machines is a question about how one
organisation names things. Once said, it is worth keeping -- and it is exactly
the kind of statement that pays off across datasets rather than within one,
because the join suggester matches on shared meaning.

So a custom type is an ordinary member of the hierarchy that happens to have
been written down by hand. Two rules keep it that way:

* **It must name a parent.** A rootless type would descend from nothing, and
  ``matches_any`` would then answer False for every gate a plugin declares --
  the column would be *less* usable than when it had no meaning at all. Parents
  are what make a new type immediately useful.
* **Built-in ids cannot be redefined**, because plugins are written against
  them.
"""

from __future__ import annotations

from typing import get_args

from ..catalog.models import SemanticTypeRow
from ..catalog.repo import Catalog
from ..core.semantic import (
    SEMANTIC_TYPES,
    SemanticType,
    SemanticTypeError,
    SemanticTypeRegistry,
)
from ..core.types import ColumnRole

ROLES: tuple[str, ...] = get_args(ColumnRole)

# The parent to offer for a new meaning on a column of a given storage type.
# Text defaults to ``categorical`` rather than ``text`` because categorical is
# both joinable and accepted by more plugins, while still descending from text
# -- so it satisfies every gate ``text`` would have, and several more.
PARENT_FOR_PHYSICAL: tuple[tuple[tuple[str, ...], str], ...] = (
    (("TIMESTAMP", "DATE", "TIME"), "temporal"),
    (("BIGINT", "INTEGER", "SMALLINT", "TINYINT", "HUGEINT", "DOUBLE", "FLOAT",
      "DECIMAL", "REAL"), "numeric"),
    (("BOOL",), "boolean"),
)
DEFAULT_PARENT = "categorical"


class TypeInUse(SemanticTypeError):
    """Refusing to delete a meaning something still depends on."""


def suggest_parent(physical_type: str) -> str:
    """Which existing type a new meaning for this column should sit under."""
    pt = (physical_type or "").upper()
    for prefixes, parent in PARENT_FOR_PHYSICAL:
        if pt.startswith(prefixes):
            return parent
    return DEFAULT_PARENT


def title_from_id(type_id: str) -> str:
    """``machine.name`` -> ``Machine name``. A label, not a translation."""
    words = type_id.replace(".", " ").replace("_", " ").split()
    return " ".join(words).capitalize() if words else type_id


def _to_type(row: SemanticTypeRow) -> SemanticType:
    return SemanticType(
        id=row.id,
        title=row.title or title_from_id(row.id),
        parent=row.parent,
        role=row.role,  # type: ignore[arg-type]
        joinable=row.joinable,
        description=row.description,
    )


def load_into_registry(
    catalog: Catalog, registry: SemanticTypeRegistry = SEMANTIC_TYPES
) -> list[str]:
    """Replace the registry's custom types with this catalog's.

    Replace, not merge. The registry is a process-wide singleton while the
    catalog is per data directory, so a merge would let one installation's
    vocabulary leak into the next -- which in practice means into the next test.

    Rows are loaded parents-first, since a type cannot be registered before the
    one it descends from. Anything still unresolvable after a full pass is
    skipped rather than raised on: a catalog that has somehow lost a parent
    should start with a smaller vocabulary, not fail to start.
    """
    registry.reset_custom()
    pending = list(catalog.list_semantic_types())
    loaded: list[str] = []
    while pending:
        deferred: list[SemanticTypeRow] = []
        for row in pending:
            if row.parent is not None and registry.get(row.parent) is None:
                deferred.append(row)
                continue
            try:
                registry.add_custom(_to_type(row))
            except SemanticTypeError:
                continue  # a stored row that no longer makes sense
            loaded.append(row.id)
        if len(deferred) == len(pending):
            break  # no progress: the rest are orphans
        pending = deferred
    return loaded


def define(
    catalog: Catalog,
    type_id: str,
    *,
    title: str | None = None,
    parent: str = DEFAULT_PARENT,
    role: str | None = None,
    joinable: bool = True,
    description: str = "",
    registry: SemanticTypeRegistry = SEMANTIC_TYPES,
) -> SemanticType:
    """Define a meaning, or edit one already defined.

    ``role`` defaults to the parent's, which is almost always right: a machine
    name under ``categorical`` is a dimension, a cost figure under ``numeric``
    is a measure. ``joinable`` defaults to True because linking datasets is the
    usual reason for defining a type at all -- a column nothing detects is only
    worth naming if the name is going to be used somewhere else.
    """
    type_id = (type_id or "").strip()
    parent_def = registry.get(parent)
    if parent_def is None:
        raise SemanticTypeError(
            f"unknown parent {parent!r}; a new meaning must sit under an "
            "existing one so that plugins written for the parent still accept it"
        )
    if role is not None and role not in ROLES:
        raise SemanticTypeError(
            f"unknown role {role!r}; one of {', '.join(ROLES)}")

    st = SemanticType(
        id=type_id,
        title=(title or "").strip() or title_from_id(type_id),
        parent=parent,
        role=role or parent_def.role,  # type: ignore[arg-type]
        joinable=joinable,
        description=description.strip(),
    )
    # Validated before it is stored, so a rejected definition leaves no trace.
    registry.validate_custom(st)
    catalog.upsert_semantic_type(SemanticTypeRow(
        id=st.id, title=st.title, parent=st.parent, role=st.role,
        joinable=st.joinable, description=st.description,
    ))
    registry.add_custom(st)
    return st


def usage(catalog: Catalog, type_id: str) -> list[tuple[str, str, str]]:
    """``(dataset_id, dataset_name, column)`` for every column carrying it."""
    return catalog.columns_using_type(type_id)


def remove(
    catalog: Catalog, type_id: str,
    registry: SemanticTypeRegistry = SEMANTIC_TYPES,
) -> None:
    """Delete a custom meaning, if nothing depends on it.

    Refused while columns still carry it, because the column metadata stores the
    id as text: deleting the type does not clear those columns, it leaves them
    describing themselves in a vocabulary nothing can look up. Refused too while
    another custom type descends from it, which would orphan that one at the
    next start-up.
    """
    if registry.is_builtin(type_id):
        raise SemanticTypeError(f"{type_id!r} is built in and cannot be deleted")

    columns = usage(catalog, type_id)
    if columns:
        where = ", ".join(f"{name}.{col}" for _, name, col in columns[:5])
        more = f" and {len(columns) - 5} more" if len(columns) > 5 else ""
        raise TypeInUse(
            f"{type_id!r} is still the meaning of {len(columns)} column"
            f"{'s' if len(columns) != 1 else ''}: {where}{more}. Change those "
            "first, then delete it."
        )
    children = [t.id for t in registry.custom() if t.parent == type_id]
    if children:
        raise TypeInUse(
            f"{type_id!r} is the parent of {', '.join(sorted(children))}. "
            "Delete or re-parent those first."
        )

    catalog.delete_semantic_type(type_id)
    registry.reset_custom()
    load_into_registry(catalog, registry)
