"""EPUB to ZIM pages: one page per chapter, in reading order, with its pictures and styles.

An EPUB is a zip of XHTML chapters. Each file is stored under ``book/`` at the path it has
inside the EPUB, so the links and pictures between chapters keep working untouched.
"""

import posixpath
import zipfile
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..extract import relative, serialise
from ..report import Reporter
from ..writer import BuildError, Stage, ZimMeta, language_code, slugify
from .common import BOOK_CSS, front_page, keep_original, nav_bar

_NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
}
_PAGES = ("application/xhtml+xml", "text/html")


def _text(element: ElementTree.Element | None) -> str:
    return " ".join("".join(element.itertext()).split()) if element is not None else ""


def _labels(book: zipfile.ZipFile, root: ElementTree.Element, folder: str) -> dict[str, str]:
    """Chapter titles from the book's own table of contents (EPUB 2's NCX), by file."""
    labels: dict[str, str] = {}
    for item in root.findall("opf:manifest/opf:item", _NS):
        if item.get("media-type") != "application/x-dtbncx+xml":
            continue
        try:
            ncx = ElementTree.fromstring(book.read(posixpath.join(folder, item.get("href", ""))))
        except (KeyError, ElementTree.ParseError):
            continue
        base = posixpath.dirname(posixpath.join(folder, item.get("href", "")))
        for point in ncx.iter(f"{{{_NS['ncx']}}}navPoint"):
            content = point.find("ncx:content", _NS)
            label = _text(point.find("ncx:navLabel/ncx:text", _NS))
            if content is not None and label:
                target = unquote(content.get("src", "").split("#")[0])
                labels.setdefault(posixpath.normpath(posixpath.join(base, target)), label)
    return labels


def convert(
    source: Path,
    stage: Stage,
    report: Reporter,
    filename: str,
    images: bool = True,
    title: str = "",
    original: Path | None = None,
) -> ZimMeta:
    try:
        book = zipfile.ZipFile(source)
        container = ElementTree.fromstring(book.read("META-INF/container.xml"))
        rootfile = container.find("c:rootfiles/c:rootfile", _NS)
        opf_path = rootfile.get("full-path") if rootfile is not None else None
        root = ElementTree.fromstring(book.read(opf_path or ""))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
        raise BuildError(f"This is not a readable EPUB file ({exc}).") from exc
    if "META-INF/encryption.xml" in book.namelist() and b"EncryptedData" in book.read(
        "META-INF/encryption.xml"
    ):
        report.note("Parts of this book are encrypted (DRM) and may be unreadable.")
    folder = posixpath.dirname(opf_path or "")
    meta = root.find("opf:metadata", _NS)
    find = (lambda tag: _text(meta.find(f"dc:{tag}", _NS))) if meta is not None else (lambda _: "")
    book_title = title or find("title") or Path(filename).stem
    language = find("language") or "en"

    manifest: dict[str, dict[str, str]] = {}
    for item in root.findall("opf:manifest/opf:item", _NS):
        href = posixpath.normpath(posixpath.join(folder, unquote(item.get("href", ""))))
        manifest[item.get("id", "")] = {
            "href": href,
            "type": item.get("media-type", ""),
            "properties": item.get("properties", ""),
        }
    spine = [
        manifest[ref.get("idref", "")]["href"]
        for ref in root.findall("opf:spine/opf:itemref", _NS)
        if ref.get("idref", "") in manifest and manifest[ref.get("idref", "")]["type"] in _PAGES
    ]
    if not spine:
        raise BuildError("This EPUB has no chapters to read.")
    labels = _labels(book, root, folder)
    names = set(book.namelist())

    # Pictures, styles and fonts first, as they are; chapters are rewritten below.
    cover = ""
    cover_id = ""
    if meta is not None:
        for tag in meta.findall("opf:meta", _NS):
            if tag.get("name") == "cover":
                cover_id = tag.get("content", "")
    for item_id, item in manifest.items():
        if item["type"] in _PAGES or item["href"] not in names:
            continue
        picture = item["type"].startswith("image/")
        if picture and not images:
            continue
        stage.add_resource(f"book/{item['href']}", item["type"], book.read(item["href"]))
        if picture and ("cover-image" in item["properties"] or item_id == cover_id):
            cover = f"book/{item['href']}"
            stage.icon = book.read(item["href"])

    report.phase("converting", len(spine))
    contents: list[tuple[str, str]] = []
    paths = [f"book/{href}" for href in spine]
    for number, href in enumerate(spine):
        report.check()
        if href not in names:
            report.failed(href, "listed in the book but missing from the file")
            continue
        tree = HTMLParser(book.read(href))
        for node in tree.css("script, link[rel~=preload]"):
            node.decompose()
        if not images:
            for node in tree.css("img, image, svg"):
                alt = (node.attributes.get("alt") or "").strip()
                if alt:
                    node.replace_with(f"[{alt}]")
                else:
                    node.decompose()
        body = tree.body
        if body is None:
            report.skipped(href, "an empty chapter")
            continue
        text = " ".join(body.text(separator=" ", strip=True).split())
        heading = tree.css_first("h1, h2, h3")
        heading_text = " ".join(heading.text(strip=True).split()) if heading else ""
        chapter = labels.get(href) or heading_text or f"Section {number + 1}"
        path = paths[number]
        before = relative(path, paths[number - 1]) if number else ""
        after = relative(path, paths[number + 1]) if number + 1 < len(paths) else ""
        bar = HTMLParser(nav_bar(before, relative(path, "index"), after)).css_first("nav")
        style = HTMLParser(f"<style>{BOOK_CSS}</style>").css_first("style")
        body.insert_child(bar)
        if tree.head is not None:
            # The book's own styles come later in the head, so they still win.
            first = tree.head.child
            if first is not None:
                first.insert_before(style)
            else:
                tree.head.insert_child(style)
        stage.add_page(path, f"{chapter}", serialise(tree), text)
        if text or images:
            contents.append((path, chapter))
        report.progress(number + 1)

    kept = keep_original(stage, original or source, filename, "application/epub+zip")
    front_page(
        stage, book_title, find("creator"), find("description"), contents, kept, language, cover
    )
    return ZimMeta(
        name=f"custom_{slugify(book_title)}",
        title=book_title,
        description=find("description") or f"{book_title}, converted from {filename}",
        language=language_code(language),
        creator=find("creator"),
        source=filename,
        tags="_category:custom;book",
    )
