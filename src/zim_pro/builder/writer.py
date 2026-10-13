"""Writing a ZIM file from staged pages and resources, and checking the result.

A build first collects everything on disk in a :class:`Stage`. Only then is the ZIM written,
by one thread, because libzim's creator must not be fed from several threads at once (its
compression and indexing still run on libzim's own worker threads). The finished file is
opened again and compared with what was staged before it is allowed into the library, so a
build cannot silently lose pages.
"""

import hashlib
import io
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from libzim.reader import Archive
from libzim.writer import (
    Compression,
    Creator,
    FileProvider,
    Hint,
    IndexData,
    Item,
)
from PIL import Image, ImageDraw

SCRAPER = "zim-pro"
# Formats that are already compressed: packing them again costs time and saves nothing.
_COMPRESSED = re.compile(
    r"^(image/(?!svg|bmp|x-icon)|video/|audio/|font/woff|application/"
    r"(pdf|zip|epub|gzip|x-mobipocket|vnd\.amazon|octet-stream))"
)
# ISO 639-1 codes people type, as the ISO 639-3 codes the ZIM format wants.
LANGUAGES = {
    "en": "eng", "hi": "hin", "mr": "mar", "es": "spa", "fr": "fra", "de": "deu", "pt": "por",
    "it": "ita", "ru": "rus", "zh": "zho", "ja": "jpn", "ko": "kor", "ar": "ara", "bn": "ben",
    "ta": "tam", "te": "tel", "gu": "guj", "kn": "kan", "ml": "mal", "pa": "pan", "ur": "urd",
    "nl": "nld", "sv": "swe", "pl": "pol", "tr": "tur", "id": "ind", "vi": "vie", "uk": "ukr",
}  # fmt: skip


def language_code(value: str) -> str:
    """A three-letter language code from whatever a page or a person declared."""
    code = (value or "").strip().lower().replace("_", "-").split("-")[0]
    if len(code) == 3 and code.isalpha():
        return code
    return LANGUAGES.get(code, "eng")


def slugify(text: str, limit: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].strip("-") or "untitled"


class BuildError(Exception):
    """A build cannot continue; the message is shown to the user as is."""


@dataclass
class ZimMeta:
    name: str  # stable across rebuilds: the book's id in the library
    title: str
    description: str = ""
    language: str = "eng"
    creator: str = ""
    source: str = ""
    tags: str = "_category:custom"
    date: str = field(default_factory=lambda: date.today().isoformat())

    @property
    def filename(self) -> str:
        return f"{self.name}_{self.date[:7]}.zim"


@dataclass
class _Entry:
    path: str
    title: str
    mimetype: str
    file: Path
    text_file: Path | None = None  # set for pages: the text to index


class Stage:
    """Everything that will go into a ZIM, kept on disk until it is written."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self.entries: dict[str, _Entry] = {}
        self.redirects: dict[str, tuple[str, str]] = {}
        self.main = ""
        self.icon: bytes | None = None
        self.size_estimate = 0  # bytes of files staged so far, kept by whoever adds them
        self._count = 0

    def _file(self, suffix: str = "") -> Path:
        self._count += 1
        return self.folder / f"{self._count:07d}{suffix}"

    def has(self, path: str) -> bool:
        return path in self.entries or path in self.redirects

    def add_page(self, path: str, title: str, html: str, text: str) -> None:
        """Stage a page; ``text`` is what full-text search will match."""
        entry = self.entries.get(path)
        file = entry.file if entry else self._file(".html")
        file.write_text(html, "utf-8")
        text_file = (entry.text_file if entry else None) or file.with_suffix(".txt")
        text_file.write_text(text, "utf-8")
        self.entries[path] = _Entry(path, title, "text/html", file, text_file)

    def add_resource(self, path: str, mimetype: str, data: bytes | Path) -> None:
        file = self._file()
        if isinstance(data, Path):
            shutil.copyfile(data, file)
        else:
            file.write_bytes(data)
        self.entries[path] = _Entry(path, "", mimetype or "application/octet-stream", file)

    def add_redirect(self, path: str, title: str, target: str) -> None:
        if path != target and path not in self.entries:
            self.redirects[path] = (title, target)

    def page_html(self, path: str) -> str:
        return self.entries[path].file.read_text("utf-8")

    @property
    def pages(self) -> list[str]:
        return [e.path for e in self.entries.values() if e.text_file is not None]

    @property
    def size(self) -> int:
        return sum(e.file.stat().st_size for e in self.entries.values())

    def clear(self) -> None:
        shutil.rmtree(self.folder, ignore_errors=True)


class _Index(IndexData):
    def __init__(self, title: str, text: str):
        super().__init__()
        self.title, self.text = title, text

    def has_indexdata(self) -> bool:
        return bool(self.text.strip() or self.title.strip())

    def get_title(self) -> str:
        return self.title

    def get_content(self) -> str:
        return self.text

    def get_keywords(self) -> str:
        return ""

    def get_wordcount(self) -> int:
        return len(self.text.split())


class _Item(Item):
    def __init__(self, entry: _Entry):
        super().__init__()
        self.entry = entry

    def get_path(self) -> str:
        return self.entry.path

    def get_title(self) -> str:
        return self.entry.title

    def get_mimetype(self) -> str:
        return self.entry.mimetype

    def get_contentprovider(self):
        return FileProvider(str(self.entry.file))

    def get_hints(self) -> dict[Hint, bool]:
        page = self.entry.text_file is not None
        return {
            # Only pages are articles: search, suggestions and "random" never land on a
            # picture or a stylesheet.
            Hint.FRONT_ARTICLE: page,
            Hint.COMPRESS: not _COMPRESSED.match(self.entry.mimetype),
        }

    def get_indexdata(self) -> IndexData | None:
        # The cleaned text, not the markup: search then matches what a reader sees.
        if self.entry.text_file is None:
            return None
        return _Index(self.entry.title, self.entry.text_file.read_text("utf-8"))


def make_icon(image: bytes | None, title: str) -> bytes:
    """A 48x48 PNG for the library: the given picture, or a tile with the title's initial."""
    if image:
        try:
            picture = Image.open(io.BytesIO(image))
            picture = picture.convert("RGBA")
            # Fill the tile whatever the picture's size: a 16-pixel favicon is drawn larger
            # rather than left as a speck in the middle.
            scale = 48 / max(picture.size)
            size = (max(1, round(picture.width * scale)), max(1, round(picture.height * scale)))
            picture = picture.resize(size, Image.LANCZOS)
            tile = Image.new("RGBA", (48, 48), (255, 255, 255, 255))
            offset = ((48 - picture.width) // 2, (48 - picture.height) // 2)
            tile.paste(picture, offset, picture)
            return _png(tile.convert("RGB"))
        except Exception:
            pass  # not a picture Pillow can read: fall through to the lettered tile
    hue = int(hashlib.sha1(title.encode()).hexdigest()[:2], 16)
    tile = Image.new("RGB", (48, 48), (20 + hue % 60, 110 + hue % 70, 150 + hue % 60))
    letter = (title.strip()[:1] or "Z").upper()
    draw = ImageDraw.Draw(tile)
    try:
        box = draw.textbbox((0, 0), letter, font_size=30)
        position = ((48 - box[2] - box[0]) / 2, (48 - box[3] - box[1]) / 2)
        draw.text(position, letter, fill="white", font_size=30)
    except Exception:
        draw.text((18, 16), letter, fill="white")
    return _png(tile)


def _png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def write_zim(
    stage: Stage, meta: ZimMeta, target: Path, workers: int = 4, on_progress=None
) -> dict[str, Any]:
    """Write the staged content to ``target`` and check that nothing went missing.

    Returns what the finished file holds. Raises :class:`BuildError`, leaving no file
    behind, if it cannot be written or does not match what was staged.
    """
    if not stage.pages:
        raise BuildError("There is nothing to put in the ZIM: no page could be read.")
    main = stage.main if stage.has(stage.main) else stage.pages[0]
    temporary = target.with_name(target.name + ".tmp")
    if not os.access(target.parent, os.W_OK | os.X_OK):
        # libzim only says "unable to write"; say whose folder it is, and how to mend it.
        raise BuildError(
            f"The library folder ({target.parent}) cannot be written to by the user Zim Pro "
            f"runs as (id {os.getuid()}). Give that user the folder, for example with "
            f"`chown {os.getuid()}:{os.getgid()}` on it; with Docker, on the folder mounted "
            "there. Docker creates a missing folder as root, which causes this."
        )
    temporary.unlink(missing_ok=True)
    seen: dict[str, str] = {}  # content hash -> the path that already holds it
    written = aliased = 0
    try:
        creator = Creator(str(temporary))
        creator.config_indexing(True, meta.language)
        creator.config_compression(Compression.zstd)
        creator.config_nbworkers(max(1, workers))
        with creator:
            creator.set_mainpath(main)
            total = len(stage.entries)
            for number, entry in enumerate(stage.entries.values(), 1):
                digest = ""
                if entry.text_file is None and entry.file.stat().st_size < 64 * 1024 * 1024:
                    digest = hashlib.sha1(entry.file.read_bytes()).hexdigest() + entry.mimetype
                if digest and digest in seen:
                    # The same bytes under another address: stored once, reachable twice.
                    creator.add_alias(entry.path, entry.title, seen[digest], {})
                    aliased += 1
                else:
                    creator.add_item(_Item(entry))
                    written += 1
                    if digest:
                        seen[digest] = entry.path
                if on_progress and (number % 25 == 0 or number == total):
                    on_progress(number, total)
            for path, (title, to) in stage.redirects.items():
                if stage.has(to) and to not in stage.redirects:
                    creator.add_redirection(path, title, to, {Hint.FRONT_ARTICLE: False})
            creator.add_illustration(48, make_icon(stage.icon, meta.title))
            fields = {
                "Name": meta.name,
                "Title": meta.title[:80] or meta.name,
                "Description": (meta.description or meta.title)[:80],
                "LongDescription": meta.description[:4000] if len(meta.description) > 80 else "",
                "Language": meta.language,
                "Creator": meta.creator or "Unknown",
                "Publisher": "Zim Pro",
                "Date": meta.date,
                "Tags": meta.tags,
                "Scraper": SCRAPER,
                "Source": meta.source,
            }
            for key, value in fields.items():
                if value:
                    creator.add_metadata(key, value)
        report = verify(temporary, stage, main)
    except BuildError:
        temporary.unlink(missing_ok=True)
        raise
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        raise BuildError(f"The ZIM could not be written: {exc}") from exc
    target.unlink(missing_ok=True)
    temporary.replace(target)
    report.update(size=target.stat().st_size, deduplicated=aliased)
    return report


def verify(path: Path, stage: Stage, main: str) -> dict[str, Any]:
    """Open a freshly written ZIM and compare it with what was staged."""
    try:
        archive = Archive(str(path))
    except Exception as exc:
        raise BuildError(f"The ZIM that was written cannot be opened: {exc}") from exc
    pages = stage.pages
    if archive.article_count != len(pages):
        raise BuildError(
            f"The ZIM holds {archive.article_count} pages but {len(pages)} were prepared."
        )
    missing = [p for p in stage.entries if not archive.has_entry_by_path(p)]
    if missing:
        raise BuildError(f"{len(missing)} files are missing from the ZIM, e.g. {missing[0]}")
    # A sample of pages, byte for byte, including the first and the last.
    step = max(1, len(pages) // 20)
    for page in {*pages[::step], pages[-1], main}:
        if page not in stage.entries:
            continue
        stored = bytes(archive.get_entry_by_path(page).get_item().content)
        if stored != stage.entries[page].file.read_bytes():
            raise BuildError(f"The page {page} was not stored as prepared.")
    if not archive.has_main_entry:
        raise BuildError("The ZIM has no main page.")
    return {
        "pages": archive.article_count,
        "files": len(stage.entries) - len(pages),
        "searchable": bool(archive.has_fulltext_index),
        "uuid": str(archive.uuid),
    }


def describe(report: dict[str, Any]) -> str:
    return json.dumps(report, sort_keys=True)
