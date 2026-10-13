"""Finding a site's own list of its pages: sitemaps, wherever they are kept.

``robots.txt`` names them when the site is well kept; otherwise they sit at one of a few
usual addresses. A sitemap is XML (a list of pages, or an index of more sitemaps), plain
text with one address per line, or either of those gzipped.
"""

import gzip
import html
import io
import re

MAX_FILES = 50  # sitemap files read per host
MAX_BYTES = 60 * 1024 * 1024  # the protocol's own limit is 50 MB unpacked
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
_LOC = re.compile(
    rb"<(?:\w+:)?loc\b[^>]*>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</(?:\w+:)?loc>", re.S | re.I
)
_INDEX_ENTRY = re.compile(rb"<(?:\w+:)?sitemap\b[^>]*>(.*?)</(?:\w+:)?sitemap>", re.S | re.I)


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

    def text(raw: bytes) -> str:
        return html.unescape(raw.decode("utf-8", "replace")).strip()

    if b"<sitemapindex" in data[:5000].lower():
        children = [m.group(1) for entry in _INDEX_ENTRY.findall(data) if (m := _LOC.search(entry))]
        return [text(c) for c in children], []
    if b"<urlset" in data[:5000].lower() or b"<loc" in head:
        return [], [text(m) for m in _LOC.findall(data)]
    if b"<" in head:
        return [], []  # some other XML, such as a feed
    lines = (line.strip() for line in data.decode("utf-8", "replace").splitlines())
    return [], [line for line in lines if re.match(r"https?://\S+$", line)]
