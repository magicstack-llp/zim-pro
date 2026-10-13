"""PDF to ZIM pages: one page per PDF page, as text to read and search.

With pictures on, each page is also kept as an image, so a scanned page or one whose layout
matters (tables, formulas, diagrams) loses nothing. A PDF with no text layer still converts,
but cannot be searched: there is no OCR here.
"""

import io
from pathlib import Path

import pypdfium2 as pdfium

from ..report import Reporter
from ..writer import BuildError, Stage, ZimMeta, slugify
from .common import BOOK_CSS, document, escape, front_page, keep_original, nav_bar

RENDER_SCALE = 1.6  # about 115 dpi: readable on a screen without bloating the ZIM
MIN_TEXT_PER_PAGE = 20  # fewer characters than this on average means "scanned"


def _paragraphs(text: str) -> list[str]:
    """Rejoin the lines of a PDF page into paragraphs."""
    paragraphs: list[str] = []
    current: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if not stripped:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        if current and current[-1].endswith("-") and stripped[:1].islower():
            current[-1] = current[-1][:-1] + stripped  # a word split across lines
        else:
            current.append(stripped)
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs


def convert(
    source: Path,
    stage: Stage,
    report: Reporter,
    filename: str,
    images: bool = True,
    title: str = "",
) -> ZimMeta:
    try:
        pdf = pdfium.PdfDocument(str(source))
    except pdfium.PdfiumError as exc:
        if "password" in str(exc).lower():
            raise BuildError("This PDF is password-protected and cannot be converted.") from exc
        raise BuildError(f"This is not a readable PDF file ({exc}).") from exc
    try:
        info = pdf.get_metadata_dict()
        book_title = title or (info.get("Title") or "").strip() or Path(filename).stem
        author = (info.get("Author") or "").strip()
        count = len(pdf)
        if count == 0:
            raise BuildError("This PDF has no pages.")
        width = len(str(count))
        paths = [f"page/{number:0{width}d}" for number in range(1, count + 1)]
        report.phase("converting", count)
        characters = 0
        for index in range(count):
            report.check()
            page = pdf[index]
            textpage = page.get_textpage()
            text = textpage.get_text_bounded()
            textpage.close()
            characters += len(text.strip())
            picture = ""
            if images:
                bitmap = page.render(scale=RENDER_SCALE)
                out = io.BytesIO()
                bitmap.to_pil().convert("RGB").save(out, "JPEG", quality=80, optimize=True)
                bitmap.close()
                picture = f"img/page-{index + 1:0{width}d}.jpg"
                stage.add_resource(picture, "image/jpeg", out.getvalue())
                if index == 0:
                    stage.icon = out.getvalue()
            page.close()
            label = f"Page {index + 1}"
            body = [f"<h1>{escape(book_title)} · {label}</h1>"]
            if picture:
                body.append(
                    f'<img class="zp-page-image" src="../{picture}" alt="{label} as printed">'
                )
            paragraphs = _paragraphs(text)
            if paragraphs and picture:
                body.append("<h2>Text of this page</h2>")
            body.extend(f"<p>{escape(p)}</p>" for p in paragraphs)
            if not paragraphs and not picture:
                body.append('<p class="zp-meta">This page has no text that can be read.</p>')
            before = paths[index - 1].split("/")[1] if index else ""
            after = paths[index + 1].split("/")[1] if index + 1 < count else ""
            body.append(nav_bar(before, "../index", after))
            head = f"<style>{BOOK_CSS}</style>"
            stage.add_page(
                paths[index],
                f"{book_title} · {label}",
                document(f"{book_title} · {label}", "".join(body), head=head),
                text,
            )
            report.progress(index + 1)

        # The PDF's own outline as the contents; without one, the pages.
        contents: list[tuple[str, str]] = []
        for bookmark in pdf.get_toc(max_depth=2):
            target = bookmark.get_dest()
            number = target.get_index() if target is not None else None
            heading = (bookmark.get_title() or "").strip()
            if number is not None and 0 <= number < count and heading:
                contents.append((paths[number], heading))
        if not contents:
            contents = [(path, f"Page {n}") for n, path in enumerate(paths, 1)]
    finally:
        pdf.close()

    notice = ""
    if characters < MIN_TEXT_PER_PAGE * count:
        notice = "This PDF is scanned pictures with no text layer: it can be read but not searched."
        report.note(notice)
    kept = keep_original(stage, source, filename, "application/pdf")
    cover = "img/page-" + "1".zfill(width) + ".jpg" if images else ""
    front_page(stage, book_title, author, "", contents, kept, cover=cover, notice=notice)
    return ZimMeta(
        name=f"custom_{slugify(book_title)}",
        title=book_title,
        description=f"{book_title}, {count} pages, converted from {filename}",
        creator=author,
        source=filename,
        tags="_category:custom;book",
    )
