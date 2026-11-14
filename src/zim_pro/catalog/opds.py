import asyncio
import logging
import time
import uuid as uuid_mod
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urljoin

import httpx

from ..db import Database
from ..downloads.engine import USER_AGENT
from ..library.naming import split_filename

log = logging.getLogger(__name__)

ATOM = "{http://www.w3.org/2005/Atom}"
DC = "{http://purl.org/dc/terms/}"
OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"
ACQUISITION = "http://opds-spec.org/acquisition/open-access"
THUMBNAIL = "http://opds-spec.org/image/thumbnail"
_REFRESH_LOCK = asyncio.Lock()

_COLUMNS = (
    "uuid", "stem", "name", "flavour", "title", "summary", "language", "category", "tags",
    "author", "publisher", "article_count", "media_count", "issued", "url", "size",
    "illustration",
)  # fmt: skip


def _int(text: str | None) -> int:
    try:
        return max(0, int(text or 0))
    except ValueError:
        return 0


def _is_zim_acquisition(link: ET.Element, href: str) -> bool:
    ltype = (link.get("type") or "").lower()
    if "bittorrent" in ltype or href.lower().endswith(".torrent"):
        return False
    return (
        href.lower().endswith(".meta4")
        or href.lower().endswith(".zim")
        or "application/x-zim" in ltype
        or "metalink" in ltype
        or not ltype
    )


def parse_feed(xml: bytes, base_url: str) -> list[dict[str, Any]]:
    """Parse a Kiwix OPDS acquisition feed into ``catalog`` rows."""
    rows: list[dict[str, Any]] = []
    seen_uuids: set[str] = set()
    root = ET.fromstring(xml)
    for entry in root.iter(f"{ATOM}entry"):

        def text(tag: str, ns: str = ATOM, entry: ET.Element = entry) -> str:
            return (entry.findtext(f"{ns}{tag}") or "").strip()

        url, size, illustration = "", 0, ""
        for link in entry.iter(f"{ATOM}link"):
            rel = link.get("rel", "")
            href = (link.get("href") or "").strip()
            if not href:
                continue
            if rel == ACQUISITION and _is_zim_acquisition(link, href):
                resolved = urljoin(base_url, href)
                if not url:
                    url, size = resolved, _int(link.get("length"))
                elif resolved.endswith(".meta4") and not url.endswith(".meta4"):
                    url, size = resolved, _int(link.get("length"))
                elif resolved != url:
                    log.debug(
                        "multiple acquisition links for %s: ignoring %s", text("title"), resolved
                    )
            elif rel == THUMBNAIL and not illustration:
                illustration = urljoin(base_url, href)
        if not url:
            continue
        entry_uuid = text("id").removeprefix("urn:uuid:")
        if not entry_uuid:
            entry_uuid = str(uuid_mod.uuid5(uuid_mod.NAMESPACE_URL, url))
        if entry_uuid in seen_uuids:
            log.warning("duplicate catalog uuid %s skipped", entry_uuid)
            continue
        seen_uuids.add(entry_uuid)
        stem, _ = split_filename(url)
        rows.append(
            {
                "uuid": entry_uuid,
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
                "issued": text("issued", DC),
                "url": url,
                "size": size,
                "illustration": illustration,
            }
        )
    return rows


def store(db: Database, rows: list[dict[str, Any]]) -> None:
    with db.transaction():
        db.execute("DELETE FROM catalog")
        assert db._conn is not None
        db._conn.executemany(
            f"INSERT OR REPLACE INTO catalog ({', '.join(_COLUMNS)}) "
            f"VALUES ({', '.join(':' + c for c in _COLUMNS)})",
            rows,
        )
    db.set_meta("catalog_refreshed", time.time())


def _total_results(xml: bytes) -> int:
    try:
        root = ET.fromstring(xml)
        return _int(root.findtext(f"{OPENSEARCH}totalResults"))
    except ET.ParseError:
        return 0


async def refresh(db: Database, catalog_url: str, *, proxy: str | None = None) -> int:
    """Download the whole Kiwix catalog and replace the cached copy."""
    async with _REFRESH_LOCK:
        headers: dict[str, str] = {"User-Agent": USER_AGENT}
        if etag := db.get_meta("catalog_etag"):
            headers["If-None-Match"] = str(etag)
        if last_mod := db.get_meta("catalog_last_modified"):
            headers["If-Modified-Since"] = str(last_mod)

        response: httpx.Response | None = None
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(30, read=180),
            headers={"User-Agent": USER_AGENT},
            proxy=proxy or None,
        ) as client:
            for attempt in range(3):
                try:
                    response = await client.get(catalog_url, params={"count": -1}, headers=headers)
                    if response.status_code == 304:
                        db.set_meta("catalog_refreshed", time.time())
                        return int(db.scalar("SELECT COUNT(*) FROM catalog") or 0)
                    if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                        wait = _int(response.headers.get("retry-after")) or (2**attempt)
                        await asyncio.sleep(min(30, wait))
                        continue
                    response.raise_for_status()
                    break
                except httpx.TransportError:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(2**attempt)

        assert response is not None
        rows = parse_feed(response.content, str(response.url))
        if not rows:
            raise RuntimeError("the catalog feed contained no entries")
        expected_total = _total_results(response.content)
        if expected_total > 0 and len(rows) < expected_total * 0.5:
            raise RuntimeError(
                f"catalog feed looks truncated ({len(rows)} of {expected_total} entries)"
            )
        existing_count = int(db.scalar("SELECT COUNT(*) FROM catalog") or 0)
        if existing_count > 100 and len(rows) < existing_count * 0.25:
            raise RuntimeError(
                f"catalog feed shrank unexpectedly ({len(rows)} vs {existing_count} cached)"
            )
        store(db, rows)
        if new_etag := response.headers.get("etag"):
            db.set_meta("catalog_etag", new_etag)
        if new_mod := response.headers.get("last-modified"):
            db.set_meta("catalog_last_modified", new_mod)
        log.info("catalog refreshed: %d entries", len(rows))
        return len(rows)
