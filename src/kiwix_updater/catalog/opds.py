import logging
import time
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urljoin

import httpx

from ..db import Database
from ..library.naming import split_filename

log = logging.getLogger(__name__)

ATOM = "{http://www.w3.org/2005/Atom}"
DC = "{http://purl.org/dc/terms/}"
ACQUISITION = "http://opds-spec.org/acquisition/open-access"
THUMBNAIL = "http://opds-spec.org/image/thumbnail"

_COLUMNS = (
    "uuid", "stem", "name", "flavour", "title", "summary", "language", "category", "tags",
    "author", "publisher", "article_count", "media_count", "issued", "url", "size",
    "illustration",
)  # fmt: skip


def _int(text: str | None) -> int:
    try:
        return int(text or 0)
    except ValueError:
        return 0


def parse_feed(xml: bytes, base_url: str) -> list[dict[str, Any]]:
    """Parse a Kiwix OPDS acquisition feed into ``catalog`` rows."""
    rows = []
    root = ET.fromstring(xml)
    for entry in root.iter(f"{ATOM}entry"):

        def text(tag: str, ns: str = ATOM, entry: ET.Element = entry) -> str:
            return (entry.findtext(f"{ns}{tag}") or "").strip()

        url, size, illustration = "", 0, ""
        for link in entry.iter(f"{ATOM}link"):
            rel = link.get("rel", "")
            if rel == ACQUISITION:
                url, size = link.get("href", ""), _int(link.get("length"))
            elif rel == THUMBNAIL:
                illustration = urljoin(base_url, link.get("href", ""))
        if not url:
            continue
        stem, _ = split_filename(url)
        rows.append(
            {
                "uuid": text("id").removeprefix("urn:uuid:"),
                "stem": stem,
                "name": text("name"),
                "flavour": text("flavour"),
                "title": text("title"),
                "summary": text("summary"),
                "language": text("language"),
                "category": text("category"),
                "tags": text("tags"),
                "author": (entry.findtext(f"{ATOM}author/{ATOM}name") or "").strip(),
                "publisher": (entry.findtext(f"{ATOM}publisher/{ATOM}name") or "").strip(),
                "article_count": _int(text("articleCount")),
                "media_count": _int(text("mediaCount")),
                "issued": text("issued", DC) or text("updated"),
                "url": url,
                "size": size,
                "illustration": illustration,
            }
        )
    return rows


def store(db: Database, rows: list[dict[str, Any]]) -> None:
    with db._lock:
        db.execute("BEGIN")
        try:
            db.execute("DELETE FROM catalog")
            db._conn.executemany(
                f"INSERT OR REPLACE INTO catalog ({', '.join(_COLUMNS)}) "
                f"VALUES ({', '.join(':' + c for c in _COLUMNS)})",
                rows,
            )
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    db.set_meta("catalog_refreshed", time.time())


async def refresh(db: Database, catalog_url: str) -> int:
    """Download the whole Kiwix catalog and replace the cached copy."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=180) as client:
        response = await client.get(catalog_url, params={"count": -1})
        response.raise_for_status()
    rows = parse_feed(response.content, str(response.url))
    if not rows:
        raise RuntimeError("the catalog feed contained no entries")
    store(db, rows)
    log.info("catalog refreshed: %d entries", len(rows))
    return len(rows)
