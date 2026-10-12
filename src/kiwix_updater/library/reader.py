import re
from dataclasses import dataclass
from urllib.parse import unquote

from libzim.reader import Archive, Entry
from selectolax.lexbor import LexborHTMLParser as HTMLParser

MAX_REDIRECTS = 8


def find_entry(archive: Archive, path: str, query: str = "") -> Entry | None:
    """Look up an entry by URL path, tolerating the encodings browsers send."""
    candidates = []
    if query:
        # Zimit archives keep the query string as part of the entry path.
        candidates += [f"{path}?{query}", f"{path}?{unquote(query)}"]
    candidates += [path, unquote(path)]
    if not archive.has_new_namespace_scheme and not re.match(r"^[A-Za-z-]/", path):
        candidates += [f"A/{path}", f"I/{path}", f"-/{path}"]
    for candidate in candidates:
        if archive.has_entry_by_path(candidate):
            return archive.get_entry_by_path(candidate)
    return None


def main_path(archive: Archive) -> str | None:
    if not archive.has_main_entry:
        return None
    entry = archive.main_entry
    for _ in range(MAX_REDIRECTS):
        if not entry.is_redirect:
            break
        entry = entry.get_redirect_entry()
    return entry.path


def random_path(archive: Archive) -> str | None:
    try:
        entry = archive.get_random_entry()
    except Exception:
        return main_path(archive)
    for _ in range(MAX_REDIRECTS):
        if not entry.is_redirect:
            break
        entry = entry.get_redirect_entry()
    return entry.path


_DROP = "script, style, noscript, template, svg, nav, footer, header, form, iframe, link, meta"
_DROP_CLASSES = (
    ".navbox, .mw-editsection, .reference, .reflist, .noprint, .metadata, .sidebar, "
    ".mw-jump-link, .toc, #toc, .hatnote, .thumbcaption .magnify, .infobox-navbar"
)


def clean_tree(html: bytes | str, keep_references: bool = False) -> HTMLParser:
    """Parse article HTML and drop everything that is not readable content."""
    tree = HTMLParser(html)
    selectors = [_DROP, _DROP_CLASSES if not keep_references else ""]
    for selector in filter(None, selectors):
        for node in tree.css(selector):
            node.decompose()
    return tree


def html_to_text(html: bytes | str) -> str:
    tree = clean_tree(html)
    root = tree.body or tree.root
    if root is None:
        return ""
    text = root.text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text)


@dataclass
class Article:
    path: str
    title: str
    mimetype: str
    content: bytes


def read_article(archive: Archive, path: str) -> Article | None:
    entry = find_entry(archive, path)
    if entry is None:
        return None
    for _ in range(MAX_REDIRECTS):
        if not entry.is_redirect:
            break
        entry = entry.get_redirect_entry()
    item = entry.get_item()
    return Article(entry.path, entry.title, item.mimetype, bytes(item.content))
