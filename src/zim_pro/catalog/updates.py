import re
from typing import Any

from ..db import Database

ACTIVE_STATES = ("queued", "downloading", "verifying", "paused")
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})(?:-(\d{2}))?([a-z](?![\d:]))?")


def parse_release_date(value: str) -> tuple[int, int, int, str]:
    """Parse ``YYYY-MM``, ``YYYY-MMa``, or ``YYYY-MM-DD...`` into a comparable tuple."""
    m = _DATE_RE.match((value or "").strip().lower())
    if not m:
        return (0, 0, 0, "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0), m.group(4) or "")


def compare_release_dates(a: str, b: str) -> int:
    """Compare two release date strings; returns -1, 0, or 1."""
    pa, pb = parse_release_date(a), parse_release_date(b)
    if pa[0] == 0 or pb[0] == 0:
        return 0
    if (pa[0], pa[1]) != (pb[0], pb[1]):
        return -1 if (pa[0], pa[1]) < (pb[0], pb[1]) else 1
    if pa[2] > 0 and pb[2] > 0:
        if (pa[2], pa[3]) != (pb[2], pb[3]):
            return -1 if (pa[2], pa[3]) < (pb[2], pb[3]) else 1
        return 0
    if pa[3] != pb[3]:
        return -1 if pa[3] < pb[3] else 1
    return 0


def is_newer_release(
    candidate_issued: str, candidate_uuid: str, local_date: str, local_uuid: str
) -> bool:
    """Return True if the catalog candidate is a newer release than the local book."""
    if not candidate_uuid or candidate_uuid == local_uuid:
        return False
    cmp = compare_release_dates(candidate_issued, local_date)
    if cmp != 0:
        return cmp > 0
    pa, pb = parse_release_date(candidate_issued), parse_release_date(local_date)
    # Same-day repack (both have full YYYY-MM-DD and different UUIDs).
    return pa[0] > 0 and pb[0] > 0 and pa[2] > 0 and pb[2] > 0


def _latest(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    return max(
        candidates,
        key=lambda c: (parse_release_date(c["issued"]), c["issued"], c["uuid"]),
        default=None,
    )


def book_updates(db: Database) -> list[dict[str, Any]]:
    """Compare every local book with the newest catalog release of the same book.

    Each row gets a ``status``: ``update`` (a newer release exists), ``current``,
    ``unpublished`` (the catalog no longer carries this book), or ``unknown``
    (no catalog has been loaded yet).
    """
    by_stem: dict[str, list[dict[str, Any]]] = {}
    by_name: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry in db.all("SELECT * FROM catalog"):
        by_stem.setdefault(entry["stem"], []).append(entry)
        by_name.setdefault((entry["name"], entry["flavour"]), []).append(entry)
    catalog_loaded = bool(by_stem or by_name)

    downloads = {
        d["book_id"]: d
        for d in db.all(
            "SELECT id, book_id, state, bytes_done, size, error FROM downloads "
            f"WHERE state IN ({', '.join('?' * len(ACTIVE_STATES))}) ORDER BY id",
            ACTIVE_STATES,
        )
    }

    # Made here, whatever category they were filed under: updated from Create and Feeds.
    made_here = {
        row["book_id"]
        for sql in (
            "SELECT book_id FROM links WHERE book_id != ''",
            "SELECT book_id FROM feeds WHERE book_id != ''",
            "SELECT book_id FROM uploads WHERE book_id != '' AND kind != 'zim'",
        )
        for row in db.all(sql)
    }
    result = []
    for book in db.all("SELECT * FROM books ORDER BY title COLLATE NOCASE"):
        if book["category"] == "custom" or book["id"] in made_here:
            continue  # made here, not published by Kiwix: updated from the Create page
        latest = _latest(by_stem.get(book["id"], [])) or _latest(
            by_name.get((book["name"], book["flavour"]), [])
        )
        if not catalog_loaded:
            status = "unknown"
        elif latest is None:
            status = "unpublished"
        elif is_newer_release(latest["issued"], latest["uuid"], book["date"], book["uuid"]):
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
