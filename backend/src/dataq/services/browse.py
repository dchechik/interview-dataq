"""Server-side file browsing, so importing a dataset does not mean typing a path.

Files are read *in place* by DuckDB, so what the picker must return is a path the
server can open -- not the contents of a file the browser happens to hold. A
browser's own file input cannot supply that (it gives contents, not a path), which
is why this lists the server's filesystem instead.

Browsing is confined to ``settings.browse_roots``. On a laptop the server is your
own machine and the default roots are your home and working directories; on a
hosted deployment the roots are whatever the operator allowed, and nothing outside
them is listable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..plugins.builtin.readers import pick_reader

# Extensions DuckDB can read via a registered reader, plus the compressed forms.
DATA_SUFFIXES = {
    ".csv", ".tsv", ".txt", ".parquet", ".pq", ".json", ".ndjson", ".jsonl",
}
MAX_ENTRIES = 500


class BrowseError(PermissionError):
    """Raised when a path is outside every configured root, or unreadable."""


@dataclass
class Entry:
    name: str
    path: str
    is_dir: bool
    size: int | None
    importable: bool
    reader_id: str | None


def _is_data_file(path: Path) -> bool:
    name = path.name.lower()
    if name.endswith(".gz"):
        name = name[:-3]
    return any(name.endswith(s) for s in DATA_SUFFIXES)


def _within_roots(path: Path, roots: list[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def resolve_within_roots(raw: str | None, settings: Settings) -> Path:
    """Resolve a requested path, refusing anything outside the configured roots.

    ``resolve()`` collapses ``..`` and follows symlinks *before* the check, so
    neither traversal nor a symlink pointing outside a root gets through.
    """
    roots = settings.resolved_browse_roots()
    if not roots:
        raise BrowseError("no browsable directories are configured")
    if not raw:
        return roots[0]
    try:
        candidate = Path(raw).expanduser().resolve()
    except OSError as exc:
        raise BrowseError(f"cannot resolve path: {exc}") from exc
    if not _within_roots(candidate, roots):
        raise BrowseError(f"path is outside the browsable directories: {raw}")
    return candidate


def list_directory(raw: str | None, settings: Settings, show_hidden: bool = False) -> dict:
    """List one directory: subdirectories first, then importable data files."""
    roots = settings.resolved_browse_roots()
    target = resolve_within_roots(raw, settings)
    if not target.is_dir():
        # Selecting a file in the UI navigates to its directory.
        target = target.parent

    entries: list[Entry] = []
    truncated = False
    try:
        for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            if not show_hidden and child.name.startswith("."):
                continue
            if len(entries) >= MAX_ENTRIES:
                truncated = True
                break
            try:
                is_dir = child.is_dir()
                size = None if is_dir else child.stat().st_size
            except OSError:
                continue  # a broken symlink or an unreadable entry
            if not is_dir and not _is_data_file(child):
                continue
            reader = None if is_dir else pick_reader(str(child))
            entries.append(Entry(
                name=child.name, path=str(child), is_dir=is_dir, size=size,
                importable=not is_dir and reader is not None,
                reader_id=reader.id if reader else None,
            ))
    except PermissionError as exc:
        raise BrowseError(f"cannot read {target}: permission denied") from exc

    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))

    # Offer the parent only while it stays inside a root.
    parent = target.parent
    parent_path = str(parent) if parent != target and _within_roots(parent, roots) else None

    return {
        "path": str(target),
        "parent": parent_path,
        "roots": [{"path": str(r), "name": r.name or str(r)} for r in roots],
        "entries": [e.__dict__ for e in entries],
        "truncated": truncated,
    }


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_upload_path(filename: str, settings: Settings) -> Path:
    """Where an uploaded file lands.

    The client-supplied name is reduced to safe characters and stripped of any
    directory component, so an upload cannot write outside the upload directory.
    """
    base = Path(filename or "upload").name
    cleaned = _SAFE_NAME.sub("_", base).lstrip(".") or "upload"
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    target = settings.upload_dir / cleaned
    if target.exists():
        stem, suffix = target.stem, target.suffix
        for n in range(1, 1000):
            candidate = settings.upload_dir / f"{stem}-{n}{suffix}"
            if not candidate.exists():
                return candidate
    return target
