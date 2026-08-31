"""The semantic type registry -- DataQ's metadata layer.

A semantic type is what a column *means* ("this is an IPv4 address"), as opposed to
how it is stored ("VARCHAR"). Semantic types form a hierarchy, so a rule written for
``categorical`` automatically applies to ``geo.country_iso2``.

This registry is what makes join suggestion, chart suggestion and normalization
automatic rather than hand-wired: plugins declare which semantic types they accept
and produce, and the suggesters match columns across datasets by type.

The built-in types below are the ones DataQ can *detect*. They are not meant to be
the whole vocabulary: no detector will ever recognise a fleet's machine names or an
internal cost-centre code, and a dataset full of columns nothing recognises is a
dataset nothing can suggest anything about. So the registry also holds *custom*
types, defined by a person and persisted in the catalog -- see
``services.semantic_types``. Saying that ``pc`` here and ``device`` there both mean
``machine.name`` is what makes the two joinable, and it is a claim only a person can
make.

Custom types are ordinary members of the hierarchy: they name a parent, inherit
matching from it, and are indistinguishable to every consumer. Only two things
separate them -- they can be edited and deleted, and they are never produced by a
detector.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .types import ColumnRole

# A custom type's id. Dotted segments are a naming convention rather than a
# structure -- ``geo.lat`` has parent ``numeric``, not ``geo`` -- but they read
# well and keep a vocabulary sorted into families.
TYPE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
MAX_TYPE_ID_LENGTH = 64


@dataclass(frozen=True)
class SemanticType:
    id: str
    title: str
    parent: str | None = None
    # Default role for a column of this type, used when suggesting charts/queries.
    role: ColumnRole = "dimension"
    # Whether matching columns of this type across datasets is a meaningful join.
    # High-cardinality identifiers and shared vocabularies are; free text is not.
    joinable: bool = False
    description: str = ""
    # Physical types this can plausibly sit on top of; used to skip detectors early.
    physical: tuple[str, ...] = field(default_factory=tuple)


class SemanticTypeError(ValueError):
    """A custom type cannot mean what it says."""


class SemanticTypeRegistry:
    def __init__(self) -> None:
        self._types: dict[str, SemanticType] = {}
        self._builtins: frozenset[str] = frozenset()

    def register(self, st: SemanticType) -> SemanticType:
        if st.id in self._types:
            raise ValueError(f"semantic type already registered: {st.id}")
        if st.parent is not None and st.parent not in self._types:
            raise ValueError(f"unknown parent {st.parent!r} for semantic type {st.id!r}")
        self._types[st.id] = st
        return st

    # --- built-in vs custom ------------------------------------------------
    def seal_builtins(self) -> None:
        """Everything registered so far is built in; anything later is custom.

        Called once at the bottom of this module. The distinction matters in
        exactly two places: a person may edit or delete their own types but not
        the ones plugins are written against, and only custom types need loading
        from the catalog at start-up.
        """
        self._builtins = frozenset(self._types)

    def is_builtin(self, type_id: str) -> bool:
        return type_id in self._builtins

    def custom(self) -> list[SemanticType]:
        return [t for t in self._types.values() if t.id not in self._builtins]

    def add_custom(self, st: SemanticType) -> SemanticType:
        """Register a person-defined type, replacing one of the same id.

        Replacement rather than refusal because this is an edit surface: a type
        defined with the wrong parent should be correctable without first being
        deleted out from under the columns using it.
        """
        self.validate_custom(st)
        self._types[st.id] = st
        return st

    def validate_custom(self, st: SemanticType) -> None:
        if st.id in self._builtins:
            raise SemanticTypeError(
                f"{st.id!r} is a built-in type and cannot be redefined")
        if len(st.id) > MAX_TYPE_ID_LENGTH:
            raise SemanticTypeError(
                f"{st.id!r} is too long; {MAX_TYPE_ID_LENGTH} characters at most")
        if not TYPE_ID_RE.match(st.id):
            raise SemanticTypeError(
                f"{st.id!r} is not a usable id. Use lowercase words separated by "
                "dots or underscores, starting with a letter -- for example "
                "'machine.name' or 'cost_centre'."
            )
        if st.parent is not None:
            if st.parent not in self._types:
                raise SemanticTypeError(
                    f"unknown parent {st.parent!r} for {st.id!r}; "
                    f"known types: {', '.join(sorted(self._types)[:12])}"
                )
            # Walking up from the parent must terminate somewhere other than
            # here, or matching this type would depend on itself.
            if st.id in self.ancestry(st.parent):
                raise SemanticTypeError(
                    f"{st.parent!r} already descends from {st.id!r}; "
                    "a type cannot be its own ancestor"
                )

    def reset_custom(self) -> None:
        """Forget every custom type. The registry is a process-wide singleton,
        so this is how a new catalog -- a different data directory, or the next
        test -- avoids inheriting the last one's vocabulary."""
        for type_id in list(self._types):
            if type_id not in self._builtins:
                del self._types[type_id]

    def get(self, type_id: str) -> SemanticType | None:
        return self._types.get(type_id)

    def require(self, type_id: str) -> SemanticType:
        st = self._types.get(type_id)
        if st is None:
            raise KeyError(f"unknown semantic type: {type_id}")
        return st

    def all(self) -> list[SemanticType]:
        return list(self._types.values())

    def ancestry(self, type_id: str) -> list[str]:
        """``geo.country_iso2`` -> ``['geo.country_iso2', 'categorical', 'text']``."""
        chain: list[str] = []
        cur: str | None = type_id
        seen: set[str] = set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            st = self._types.get(cur)
            cur = st.parent if st else None
        return chain

    def is_a(self, type_id: str, ancestor: str) -> bool:
        return ancestor in self.ancestry(type_id)

    def matches_any(self, type_id: str | None, accepted: tuple[str, ...]) -> bool:
        """True if ``type_id`` is, or descends from, any of ``accepted``.

        An empty ``accepted`` means "no constraint".
        """
        if not accepted:
            return True
        if type_id is None:
            return False
        return any(self.is_a(type_id, a) for a in accepted)

    def joinable_with(self, type_id: str | None) -> bool:
        if type_id is None:
            return False
        st = self._types.get(type_id)
        return bool(st and st.joinable)


SEMANTIC_TYPES = SemanticTypeRegistry()


def _r(*args, **kwargs) -> SemanticType:
    return SEMANTIC_TYPES.register(SemanticType(*args, **kwargs))


# --- roots -------------------------------------------------------------------
_r("numeric", "Number", role="measure", physical=("BIGINT", "INTEGER", "DOUBLE", "DECIMAL"))
_r("text", "Text", role="dimension", physical=("VARCHAR",))
_r("temporal", "Date/Time", role="time", physical=("TIMESTAMP", "DATE", "TIME"))
_r("boolean", "Boolean", role="dimension", physical=("BOOLEAN",))

# A low-cardinality value set. Joinable because a shared vocabulary (status codes,
# categories, country names) is exactly what you join two datasets on.
_r("categorical", "Category", parent="text", role="dimension", joinable=True)

# --- geo ---------------------------------------------------------------------
_r("geo.lat", "Latitude", parent="numeric", role="geo",
   description="Decimal degrees, -90..90")
_r("geo.lng", "Longitude", parent="numeric", role="geo",
   description="Decimal degrees, -180..180")
_r("geo.country_iso2", "Country (ISO-3166 alpha-2)", parent="categorical", role="dimension",
   joinable=True, description="Two-letter uppercase country code")

# --- time --------------------------------------------------------------------
_r("time.timestamp", "Timestamp", parent="temporal", role="time")
_r("time.date", "Date", parent="temporal", role="time")

# --- network -----------------------------------------------------------------
_r("net.ip", "IP address", parent="text", role="dimension", joinable=True,
   description="IPv4 or IPv6 address")

# --- identity ----------------------------------------------------------------
_r("identity.email", "Email address", parent="text", role="dimension", joinable=True)
_r("identity.url", "URL", parent="text", role="dimension")
_r("identity.key", "Identifier", parent="text", role="key", joinable=True,
   description="High-cardinality identifier; a natural join key")

# --- money -------------------------------------------------------------------
_r("money.amount", "Monetary amount", parent="numeric", role="measure")

# How common something is. Worth its own type because several things downstream
# want to find such a column without knowing which plugin produced it: the
# timeline highlights rows whose value is unusually low, and chart suggestion
# ranks a dataset carrying one differently. Matching on the *meaning* rather
# than on the literal names "share"/"rarity" is what lets a computed feature
# like n_by_user_activity_30d take part.
_r("numeric.share", "Share", parent="numeric", role="measure",
   description="Fraction of rows holding this value; small means rare")
_r("numeric.rarity", "Rarity", parent="numeric", role="measure",
   description="Inverse of share; large means rare")



# Everything above is built in and immutable; everything registered from here on
# is somebody's own vocabulary, loaded from the catalog.
SEMANTIC_TYPES.seal_builtins()
