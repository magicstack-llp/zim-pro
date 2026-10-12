import logging
import os
import threading
from pathlib import Path
from typing import Any

from libzim.reader import Archive

from ..db import Database
from .archive_pool import ArchivePool
from .naming import split_filename

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
            flavour=_meta(archive, "Flavour"),
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
        known = {r["path"]: r for r in self.db.all("SELECT * FROM books")}
        shadowed = {r["path"]: r for r in self.db.all("SELECT * FROM shadowed")}
        default_policy = self.db.get_setting("default_policy")

        # Group files by stem; the newest release of each book is the one served.
        groups: dict[str, list[dict[str, Any]]] = {}
        read = 0
        for path in sorted(self.data_dir.rglob("*.zim")):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            row = known.get(str(path))
            if row is None or row["size"] != stat.st_size or row["mtime"] != stat.st_mtime:
                stem, file_date = split_filename(path.name)
                old = shadowed.get(str(path))
                if old and old["size"] == stat.st_size:
                    row = {"id": stem, "path": str(path), "size": stat.st_size, "date": old["date"]}
                else:
                    row = read_book(path)
                    read += 1
                    self.pool.invalidate(str(path))
            groups.setdefault(row["id"], []).append(row)

        self.db.execute("DELETE FROM shadowed")
        seen: set[str] = set()
        for book_id, rows in groups.items():
            rows.sort(key=lambda r: (r.get("error") is None, r["date"]), reverse=True)
            best = rows[0]
            if "filename" not in best:  # a previously shadowed file is now the newest
                best = read_book(Path(best["path"]))
                read += 1
            seen.add(book_id)
            previous = self.db.one("SELECT path FROM books WHERE id = ?", (book_id,))
            if previous and previous["path"] != best["path"]:
                self.pool.invalidate(previous["path"])
            # A different file may previously have held this id or path.
            self.db.execute("DELETE FROM books WHERE path = ? AND id != ?", (best["path"], book_id))
            self.db.execute(
                f"INSERT INTO books ({', '.join(_COLUMNS)}, policy) "
                f"VALUES ({', '.join(':' + c for c in _COLUMNS)}, :policy) "
                "ON CONFLICT (id) DO UPDATE SET "
                + ", ".join(f"{c} = excluded.{c}" for c in _COLUMNS if c != "id"),
                {**{c: best.get(c) for c in _COLUMNS}, "policy": default_policy},
            )
            for other in rows[1:]:
                self.db.execute(
                    "INSERT OR REPLACE INTO shadowed (path, book_id, size, date) "
                    "VALUES (?, ?, ?, ?)",
                    (other["path"], book_id, other["size"], other["date"]),
                )

        removed = 0
        for row in known.values():
            if row["id"] not in seen:
                self.db.execute("DELETE FROM books WHERE id = ?", (row["id"],))
                self.pool.invalidate(row["path"])
                removed += 1
        return {"books": len(seen), "read": read, "removed": removed}

    def leftovers(self) -> list[dict[str, Any]]:
        """Files in the data directory that are not part of the library."""
        active = {
            r["filename"] + ".part"
            for r in self.db.all(
                "SELECT filename FROM downloads WHERE state NOT IN ('done', 'cancelled')"
            )
        }
        result = []
        for path in sorted(self.data_dir.rglob("*")):
            if not path.is_file() or path.suffix == ".zim" or path.name in active:
                continue
            result.append({"path": str(path), "name": path.name, "size": path.stat().st_size})
        for row in self.db.all("SELECT * FROM shadowed"):
            result.append(
                {"path": row["path"], "name": os.path.basename(row["path"]), "size": row["size"],
                 "superseded_by": row["book_id"]}
            )  # fmt: skip
        return result
