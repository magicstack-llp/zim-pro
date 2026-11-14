import logging
import os
import threading
from pathlib import Path
from typing import Any

from libzim.reader import Archive

from ..db import Database
from .archive_pool import ArchivePool
from .naming import category_of, filename_revision, split_filename

log = logging.getLogger(__name__)


def _meta(archive: Archive, key: str) -> str:
    try:
        return archive.get_metadata(key).decode("utf-8", "replace").strip()
    except Exception:
        return ""


def read_book(path: Path) -> dict[str, Any]:
    """Read the metadata of one ZIM file into a ``books`` row."""
    stat = path.stat()
    stem, file_date = split_filename(path.name)
    row: dict[str, Any] = {
        "id": stem,
        "path": str(path),
        "filename": path.name,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "name": stem,
        "flavour": "",
        "title": stem,
        "description": "",
        "language": "",
        "date": file_date,
        "uuid": "",
        "creator": "",
        "publisher": "",
        "tags": "",
        "article_count": 0,
        "media_count": 0,
        "has_ftindex": 0,
        "has_illustration": 0,
        "error": None,
    }
    try:
        archive = Archive(str(path))
        row.update(
            name=_meta(archive, "Name") or stem,
            flavour=_meta(archive, "Flavour").lstrip("_"),
            title=_meta(archive, "Title") or stem,
            description=_meta(archive, "Description"),
            language=_meta(archive, "Language"),
            date=_meta(archive, "Date") or file_date,
            uuid=str(archive.uuid),
            creator=_meta(archive, "Creator"),
            publisher=_meta(archive, "Publisher"),
            tags=_meta(archive, "Tags"),
            article_count=archive.article_count,
            media_count=archive.media_count,
            has_ftindex=int(archive.has_fulltext_index),
            has_illustration=int(archive.has_illustration(48)),
        )
    except Exception as exc:
        log.warning("cannot read %s: %s", path, exc)
        row["error"] = str(exc)
    return row


_COLUMNS = (
    "id", "path", "filename", "size", "mtime", "name", "flavour", "title", "description",
    "language", "date", "uuid", "creator", "publisher", "tags", "article_count",
    "media_count", "has_ftindex", "has_illustration", "error",
)  # fmt: skip


def file_under_categories(db: Database) -> None:
    """Set every book's category: the one chosen where it was made, or what it says itself."""
    chosen: dict[str, str] = {}
    for sql in (
        "SELECT book_id, category FROM links WHERE book_id != ''",
        "SELECT book_id, category FROM feeds WHERE book_id != ''",
        # A ZIM that was uploaded ready-made keeps what it says of itself.
        "SELECT book_id, category FROM uploads WHERE book_id != '' "
        "AND (kind != 'zim' OR category != '')",
    ):
        for row in db.all(sql):
            # Made here and not filed anywhere: "My ZIMs", whatever tags a capture gave it.
            chosen[row["book_id"]] = row["category"] or "custom"
    for row in db.all("SELECT id, tags, category FROM books"):
        category = chosen.get(row["id"]) or category_of(row["id"], row["tags"])
        if category != row["category"]:
            db.execute("UPDATE books SET category = ? WHERE id = ?", (category, row["id"]))


def _iter_zim_paths(data_dir: Path) -> tuple[list[Path], bool]:
    """Walk ``data_dir`` following directory symlinks, guarding against loops and I/O errors."""
    if not data_dir.is_dir():
        return [], True
    had_error = False
    visited: set[tuple[int, int]] = set()
    try:
        root_stat = data_dir.stat()
        visited.add((root_stat.st_dev, root_stat.st_ino))
    except OSError:
        return [], True

    def _on_error(exc: OSError) -> None:
        nonlocal had_error
        had_error = True
        log.warning("scan walk error: %s", exc)

    paths: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(data_dir, followlinks=True, onerror=_on_error):
        kept_dirs: list[str] = []
        for d in sorted(dirnames):
            if d.startswith("."):
                continue
            full_dir = os.path.join(dirpath, d)
            try:
                st = os.stat(full_dir)
            except OSError as exc:
                had_error = True
                log.warning("cannot stat directory %s: %s", full_dir, exc)
                continue
            key = (st.st_dev, st.st_ino)
            if key in visited:
                continue
            visited.add(key)
            kept_dirs.append(d)
        dirnames[:] = kept_dirs

        for name in sorted(filenames):
            if name.startswith(".") or not name.endswith(".zim"):
                continue
            paths.append(Path(dirpath) / name)
    paths.sort()
    return paths, had_error


def _split_distinct_books(
    groups: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Separate files that share a filename stem but have different (name, flavour) metadata."""
    result: dict[str, list[dict[str, Any]]] = {}
    for stem, rows in groups.items():
        identities = {
            (r["name"], r.get("flavour", ""))
            for r in rows
            if "name" in r and r.get("error") is None
        }
        if len(identities) <= 1:
            result[stem] = rows
            continue
        by_ident: dict[tuple[str, str], list[dict[str, Any]]] = {}
        unknown: list[dict[str, Any]] = []
        for r in rows:
            if "name" in r and r.get("error") is None:
                by_ident.setdefault((r["name"], r.get("flavour", "")), []).append(r)
            else:
                unknown.append(r)
        # Keep the group with the matching stem (or largest/first) under `stem`.
        ordered = sorted(by_ident.items(), key=lambda kv: (kv[0][0] != stem, kv[0]))
        for idx, ((name, flavour), subrows) in enumerate(ordered):
            if idx == 0:
                book_id = stem
                subrows.extend(unknown)
            else:
                fallback = flavour or subrows[0].get("uuid", "")[:8] or str(idx)
                suffix = name if name and name != stem else fallback
                book_id = f"{stem}_{suffix}"
                while book_id in result:
                    book_id = f"{book_id}_{idx}"
            for r in subrows:
                r["id"] = book_id
            result[book_id] = subrows
    return result


class Scanner:
    """Keeps the ``books`` table in step with the ZIM files on disk."""

    def __init__(self, db: Database, pool: ArchivePool, data_dir: Path):
        self.db = db
        self.pool = pool
        self.data_dir = data_dir
        self._lock = threading.Lock()

    def scan(self) -> dict[str, int]:
        with self._lock:
            return self._scan()

    def _scan(self) -> dict[str, int]:
        known_rows = self.db.all("SELECT * FROM books")
        known = {r["path"]: r for r in known_rows}
        known_ids = {r["id"] for r in known_rows}
        shadowed = {r["path"]: r for r in self.db.all("SELECT * FROM shadowed")}
        default_policy = self.db.get_setting("default_policy")

        # Group files by stem; the newest release of each book is the one served.
        groups: dict[str, list[dict[str, Any]]] = {}
        read = 0
        paths, had_walk_error = _iter_zim_paths(self.data_dir)
        to_invalidate: list[str] = []

        for path in paths:
            try:
                stat = path.stat()
                if not path.is_file():
                    continue
            except OSError:
                if (existing := known.get(str(path))) is not None:
                    groups.setdefault(existing["id"], []).append(existing)
                continue
            row = known.get(str(path))
            if (
                row is None
                or row["error"] is not None
                or row["size"] != stat.st_size
                or row["mtime"] != stat.st_mtime
            ):
                stem, file_date = split_filename(path.name)
                old = shadowed.get(str(path))
                if old and old["size"] == stat.st_size:
                    row = {
                        "id": stem,
                        "path": str(path),
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                        "date": old["date"],
                    }
                else:
                    try:
                        row = read_book(path)
                    except OSError as exc:
                        log.warning("cannot stat/read %s: %s", path, exc)
                        if (existing := known.get(str(path))) is not None:
                            groups.setdefault(existing["id"], []).append(existing)
                        continue
                    read += 1
                    to_invalidate.append(str(path))
            groups.setdefault(row["id"], []).append(row)

        groups = _split_distinct_books(groups)

        # Resolve best candidate for every group before opening the DB write transaction.
        resolved: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        preserved_ids: set[str] = set()
        for book_id, rows in groups.items():
            rows.sort(
                key=lambda r: (
                    r.get("error") is None,
                    r["date"],
                    filename_revision(r["path"]),
                    r.get("mtime", 0),
                ),
                reverse=True,
            )
            best: dict[str, Any] | None = None
            others: list[dict[str, Any]] = []
            for idx, candidate in enumerate(rows):
                if "filename" in candidate:
                    best = candidate
                    others = rows[idx + 1 :]
                    break
                try:
                    best = read_book(Path(candidate["path"]))
                    best["id"] = book_id
                    read += 1
                    others = rows[idx + 1 :]
                    break
                except OSError as exc:
                    log.warning("cannot read shadowed candidate %s: %s", candidate["path"], exc)
            if best is None:
                preserved_ids.add(book_id)
                continue
            resolved[book_id] = (best, others)

        seen = set(resolved) | preserved_ids
        vanished_by_uuid = {
            r["uuid"]: r for r in known_rows if r["id"] not in seen and r.get("uuid")
        }

        with self.db.transaction():
            self.db.execute("DELETE FROM shadowed")
            for book_id, (best, others) in resolved.items():
                previous = self.db.one("SELECT path FROM books WHERE id = ?", (book_id,))
                if previous and previous["path"] != best["path"]:
                    to_invalidate.append(previous["path"])
                # A different file may previously have held this id or path.
                self.db.execute(
                    "DELETE FROM books WHERE path = ? AND id != ?", (best["path"], book_id)
                )
                policy = default_policy
                if book_id not in known_ids and best.get("uuid") in vanished_by_uuid:
                    policy = vanished_by_uuid[best["uuid"]]["policy"]
                self.db.execute(
                    f"INSERT INTO books ({', '.join(_COLUMNS)}, policy) "
                    f"VALUES ({', '.join(':' + c for c in _COLUMNS)}, :policy) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    + ", ".join(f"{c} = excluded.{c}" for c in _COLUMNS if c != "id"),
                    {**{c: best.get(c) for c in _COLUMNS}, "policy": policy},
                )
                for other in others:
                    self.db.execute(
                        "INSERT OR REPLACE INTO shadowed (path, book_id, size, date) "
                        "VALUES (?, ?, ?, ?)",
                        (other["path"], book_id, other["size"], other["date"]),
                    )

            # Derived from the id and tags, so recompute for every book rather than only new files.
            file_under_categories(self.db)

            removed = 0
            if not had_walk_error:
                for row in known_rows:
                    if row["id"] not in seen:
                        self.db.execute("DELETE FROM books WHERE id = ?", (row["id"],))
                        to_invalidate.append(row["path"])
                        removed += 1

        for p in to_invalidate:
            self.pool.invalidate(p)
        return {"books": len(seen), "read": read, "removed": removed}

    def leftovers(self) -> list[dict[str, Any]]:
        """Files in the data directory that are not part of the library."""
        active = {
            r["filename"] + suffix
            for r in self.db.all(
                "SELECT filename FROM downloads WHERE state NOT IN ('done', 'cancelled')"
            )
            for suffix in (".part", ".meta4")
        }
        result = []
        if self.data_dir.is_dir():
            for path in sorted(self.data_dir.rglob("*")):
                if any(part.startswith(".") for part in path.relative_to(self.data_dir).parts):
                    continue
                if not path.is_file() or path.suffix in (".zim", ".meta4") or path.name in active:
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                result.append({"path": str(path), "name": path.name, "size": size})
        for row in self.db.all("SELECT * FROM shadowed"):
            result.append(
                {"path": row["path"], "name": os.path.basename(row["path"]), "size": row["size"],
                 "superseded_by": row["book_id"]}
            )  # fmt: skip
        return result
