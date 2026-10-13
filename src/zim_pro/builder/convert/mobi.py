"""MOBI and AZW3 to ZIM pages, by unpacking the Kindle file to the EPUB or HTML inside it."""

import html
import re
import shutil
from pathlib import Path

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..extract import serialise
from ..report import Reporter
from ..writer import BuildError, Stage, ZimMeta, slugify
from . import epub
from .common import BOOK_CSS, front_page, keep_original


def convert(
    source: Path,
    stage: Stage,
    report: Reporter,
    filename: str,
    images: bool = True,
    title: str = "",
) -> ZimMeta:
    import mobi  # slow to import, and only needed for these files

    report.phase("converting", 1)
    try:
        folder, unpacked = mobi.extract(str(source))
    except Exception as exc:
        reason = "it is protected by DRM" if "drm" in str(exc).lower() else str(exc)
        raise BuildError(f"This Kindle file cannot be converted: {reason}.") from exc
    try:
        result = Path(unpacked)
        if result.suffix.lower() == ".epub":
            return epub.convert(result, stage, report, filename, images, title, original=source)
        if result.suffix.lower() not in (".html", ".htm"):
            raise BuildError(
                f"This Kindle file holds a {result.suffix} book, which is not supported."
            )
        # An older MOBI: one long HTML file with its pictures beside it.
        tree = HTMLParser(result.read_bytes())
        for node in tree.css("script"):
            node.decompose()
        for node in tree.css("img"):
            name = (node.attributes.get("src") or "").replace("\\", "/")
            file = (result.parent / name).resolve()
            if images and name and file.is_file() and result.parent.resolve() in file.parents:
                stage.add_resource(f"book/{name}", "image/jpeg", file)
            else:
                node.decompose()
        body = tree.body
        text = " ".join(body.text(separator=" ", strip=True).split()) if body else ""
        if not text:
            raise BuildError("This Kindle file has no text that can be read.")
        # The unpacked book's own record gives the title and author when the HTML does not.
        record = result.parent / "content.opf"
        found = (
            re.findall(r"<dc:(title|creator)[^>]*>([^<]+)<", record.read_text("utf-8", "replace"))
            if record.is_file()
            else []
        )
        details = {key: html.unescape(value).strip() for key, value in reversed(found)}
        named = tree.css_first("title")
        book_title = title or details.get("title") or (named.text().strip() if named else "")
        book_title = book_title[:120] or Path(filename).stem
        author = details.get("creator", "")
        covers = sorted((result.parent / "Images").glob("cover*")) if images else []
        cover = ""
        if covers:
            cover = f"book/Images/{covers[0].name}"
            stage.add_resource(cover, "image/jpeg", covers[0])
            stage.icon = covers[0].read_bytes()
        style = HTMLParser(f"<style>{BOOK_CSS}</style>").css_first("style")
        if tree.head is not None:
            tree.head.insert_child(style)
        stage.add_page("book/book.html", book_title, serialise(tree), text)
        kept = keep_original(stage, source, filename, "application/x-mobipocket-ebook")
        front_page(
            stage, book_title, author, "", [("book/book.html", "Read the book")], kept, cover=cover
        )
        report.progress(1)
        return ZimMeta(
            name=f"custom_{slugify(book_title)}",
            title=book_title,
            description=f"{book_title}, converted from {filename}",
            creator=author,
            source=filename,
            tags="_category:custom;book",
        )
    finally:
        shutil.rmtree(folder, ignore_errors=True)
