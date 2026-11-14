"""Finding a site's own list of its pages: sitemaps, wherever they are kept.

``robots.txt`` names them when the site is well kept; otherwise they sit at one of a few
usual addresses. A sitemap is XML (a list of pages, or an index of more sitemaps), plain
text with one address per line, or either of those gzipped.
"""

import gzip
import io
import re
from xml.etree import ElementTree

MAX_FILES = 50  # sitemap files read per host
MAX_BYTES = 60 * 1024 * 1024  # the protocol's own limit is 50 MB unpacked
MAX_URLS = 50_000  # cap per sitemap file so hostile documents cannot exhaust memory/time
# Where a sitemap is usually found when robots.txt does not say. Most likely first.
USUAL_PATHS = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/sitemapindex.xml",
    "/wp-sitemap.xml",
    "/sitemap.xml.gz",
    "/sitemap/sitemap.xml",
    "/sitemap/index.xml",
    "/sitemaps/sitemap.xml",
    "/sitemap/",
    "/sitemaps.xml",
    "/sitemap1.xml",
    "/sitemap.txt",
    "/post-sitemap.xml",
    "/page-sitemap.xml",
)


def _iter_locs(data: bytes) -> list[str]:
    """Stream ``<loc>`` text values from XML without building a DOM or backtracking."""
    locs: list[str] = []
    try:
        for _, elem in ElementTree.iterparse(io.BytesIO(data), events=("end",)):
            tag = elem.tag.rsplit("}", 1)[-1].lower()
            if tag == "loc":
                text = (elem.text or "").strip()
                if text:
                    locs.append(text)
                    if len(locs) >= MAX_URLS:
                        break
            if tag in ("url", "sitemap", "loc"):
                elem.clear()
    except ElementTree.ParseError:
        pass
    return locs


def listed_in_robots(text: str) -> list[str]:
    """The ``Sitemap:`` lines of a robots.txt, in order, each once."""
    found: list[str] = []
    for line in text.splitlines():
        name, _, value = line.partition(":")
        value = value.split("#")[0].strip()
        if name.strip().lower() == "sitemap" and value and value not in found:
            found.append(value)
    return found


def _unpack(data: bytes) -> bytes:
    if data[:2] != b"\x1f\x8b":
        return data
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as packed:
            return packed.read(MAX_BYTES)
    except (OSError, EOFError):
        return b""


def parse(data: bytes) -> tuple[list[str], list[str]]:
    """What a sitemap file holds: (more sitemaps to read, pages). Empty if it is not one."""
    data = _unpack(data)[:MAX_BYTES]
    head = data[:2000].lower()
    if b"<html" in head or b"<!doctype html" in head:
        return [], []  # an error page served in place of the file

    if b"<sitemapindex" in data[:5000].lower():
        return _iter_locs(data), []
    if b"<urlset" in data[:5000].lower() or b"<loc" in head:
        return [], _iter_locs(data)
    if b"<" in head:
        return [], []  # some other XML, such as a feed
    lines = (line.strip() for line in data.decode("utf-8", "replace").splitlines())
    return [], [line for line in lines if re.match(r"https?://\S+$", line)][:MAX_URLS]
