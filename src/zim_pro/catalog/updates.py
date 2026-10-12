from typing import Any

from ..db import Database

ACTIVE_STATES = ("queued", "downloading", "verifying", "paused", "failed")


def _latest(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    return max(candidates, key=lambda c: c["issued"], default=None)


def book_updates(db: Database) -> list[dict[str, Any]]:
    """Compare every local book with the newest catalog release of the same book.

    Each row gets a ``status``: ``update`` (a newer release exists), ``current``,
    or ``unpublished`` (the catalog no longer carries this book).
    """
    by_stem: dict[str, list[dict[str, Any]]] = {}
    by_name: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry in db.all("SELECT * FROM catalog"):
        by_stem.setdefault(entry["stem"], []).append(entry)
        by_name.setdefault((entry["name"], entry["flavour"]), []).append(entry)

    downloads = {
        d["book_id"]: d
        for d in db.all(
            "SELECT id, book_id, state, bytes_done, size, error FROM downloads "
            f"WHERE state IN ({', '.join('?' * len(ACTIVE_STATES))}) ORDER BY id",
            ACTIVE_STATES,
        )
    }

    result = []
    for book in db.all("SELECT * FROM books ORDER BY title COLLATE NOCASE"):
        latest = _latest(by_stem.get(book["id"], [])) or _latest(
            by_name.get((book["name"], book["flavour"]), [])
        )
        if latest is None:
            status = "unpublished"
        elif latest["uuid"] != book["uuid"] and latest["issued"][:10] > book["date"][:10]:
            status = "update"
        else:
            status = "current"
        result.append(
            {
                "book": book,
                "status": status,
                "latest": latest if status == "update" else None,
                "download": downloads.get(book["id"]),
            }
        )
    return result
