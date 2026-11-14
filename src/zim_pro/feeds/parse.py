"""Reading feeds: RSS, Atom and RDF, and finding the feeds a web page advertises."""

import calendar
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import format_datetime

import feedparser
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..builder import extract

_FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/rdf+xml")


def _safe_normalise(url: str, base: str = "") -> str:
    try:
        return extract.normalise(url, base)
    except ValueError:
        return ""


@dataclass
class Entry:
    guid: str  # what makes this article this article: its address, or the feed's own id
    url: str
    title: str
    author: str = ""
    published: float = 0  # seconds since 1970; 0 when the feed does not say
    html: str = ""  # the article as the feed carries it: full text, or only a summary


@dataclass
class Feed:
    title: str = ""
    site: str = ""  # the website the feed belongs to
    language: str = ""
    entries: list[Entry] = field(default_factory=list)
    skipped: int = 0


class NotAFeed(Exception):
    pass


def parse(data: bytes, url: str) -> Feed:
    """Read a feed; raises :class:`NotAFeed` when ``data`` is something else."""
    parsed = feedparser.parse(data)
    if not parsed.get("version") and not parsed.entries:
        raise NotAFeed("this address does not hold an RSS or Atom feed")
    info = parsed.feed
    feed = Feed(
        title=" ".join(str(info.get("title", "")).split())[:200],
        site=_safe_normalise(str(info.get("link", "")), url),
        language=str(info.get("language", ""))[:10],
    )
    seen: set[str] = set()
    for item in parsed.entries:
        raw_link = str(item.get("link", "")).strip()
        link = _safe_normalise(raw_link, url) if raw_link else ""
        guid = (link or str(item.get("id", "")).strip())[:1000]
        if not guid:
            feed.skipped += 1
            continue
        if guid in seen:
            continue
        seen.add(guid)
        body = ""
        for part in item.get("content") or []:
            if len(part.get("value", "")) > len(body):
                body = part["value"]
        summary = str(item.get("summary", ""))
        when = item.get("published_parsed") or item.get("updated_parsed")
        feed.entries.append(
            Entry(
                guid=guid,
                url=link,
                title=" ".join(str(item.get("title", "")).split())[:300] or link or "Untitled",
                author=str(item.get("author", ""))[:200],
                published=float(calendar.timegm(when)) if when else 0,
                html=body if len(body) >= len(summary) else summary,
            )
        )
    return feed


def discover(html: str, url: str) -> list[str]:
    """The feeds a web page says it has, in the order it lists them."""
    found: list[str] = []
    for node in HTMLParser(html).css("link[rel~=alternate][href]"):
        if (node.attributes.get("type") or "").lower() in _FEED_TYPES:
            address = _safe_normalise(node.attributes.get("href") or "", url)
            if address and address not in found:
                found.append(address)
    return found


def read_opml(text: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """An OPML file as folders of (title, feed address). Feeds outside a folder come first."""
    try:
        root = ElementTree.fromstring(text.strip())
    except ElementTree.ParseError as exc:
        raise NotAFeed(f"this is not an OPML file ({exc})") from exc
    for element in root.iter():
        if "}" in element.tag:
            element.tag = element.tag.split("}", 1)[1]
    body = root.find("body")
    loose: list[tuple[str, str]] = []
    folders: list[tuple[str, list[tuple[str, str]]]] = []

    def walk(node: ElementTree.Element, folder_name: str = "") -> None:
        direct: list[tuple[str, str]] = []
        children: list[tuple[ElementTree.Element, str]] = []
        for child in node:
            if child.tag != "outline":
                continue
            address = _safe_normalise(child.get("xmlUrl") or "")
            if address:
                direct.append((child.get("title") or child.get("text") or "", address))
            elif not child.get("xmlUrl"):
                children.append((child, child.get("title") or child.get("text") or "Feeds"))
        if direct:
            if folder_name:
                folders.append((folder_name, direct))
            else:
                loose.extend(direct)
        for child, sub_name in children:
            walk(child, sub_name)

    if body is not None:
        walk(body)
    return ([("", loose)] if loose else []) + folders


def write_opml(folders: list[tuple[str, list[tuple[str, str, str]]]]) -> str:
    """Folders of (title, feed address, site address) as an OPML file."""
    root = ElementTree.Element("opml", version="2.0")
    head = ElementTree.SubElement(root, "head")
    ElementTree.SubElement(head, "title").text = "Zim Pro feeds"
    ElementTree.SubElement(head, "dateCreated").text = format_datetime(
        datetime.now(UTC), usegmt=True
    )
    body = ElementTree.SubElement(root, "body")
    for name, feeds in folders:
        folder = ElementTree.SubElement(body, "outline", text=name, title=name)
        for title, address, site in feeds:
            ElementTree.SubElement(
                folder, "outline", type="rss", text=title, title=title, xmlUrl=address, htmlUrl=site
            )
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ElementTree.tostring(root, "unicode")
