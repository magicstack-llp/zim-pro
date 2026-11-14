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
    candidates.append(path)
    if not archive.has_new_namespace_scheme and not re.match(r"^[A-Za-z-]/", path):
        candidates += [f"A/{path}", f"I/{path}", f"-/{path}"]
    for candidate in candidates:
        if archive.has_entry_by_path(candidate):
            return archive.get_entry_by_path(candidate)
    return None


def resolve_redirects(entry: Entry) -> Entry | None:
    """Follow ZIM redirect entries up to MAX_REDIRECTS; return None on cycles or broken targets."""
    for _ in range(MAX_REDIRECTS):
        if not entry.is_redirect:
            return entry
        try:
            entry = entry.get_redirect_entry()
        except (RuntimeError, KeyError):
            return None
    return None if entry.is_redirect else entry


def main_path(archive: Archive) -> str | None:
    if not archive.has_main_entry:
        return None
    entry = resolve_redirects(archive.main_entry)
    return entry.path if entry is not None else None


def random_path(archive: Archive) -> str | None:
    try:
        entry = archive.get_random_entry()
    except (RuntimeError, OSError, KeyError):
        return main_path(archive)
    entry = resolve_redirects(entry)
    return entry.path if entry is not None else main_path(archive)


_DROP = (
    "script, style, noscript, template, svg, nav, footer, header, form, iframe, link, meta, "
    "button, input, select, textarea, dialog"
)
# Furniture around the text: navigation boxes, edit links, citation markers and reference
# lists, licence footers, and
# the vote counts and user cards of Q&A sites. `sup.reference` rather than `.reference`:
# Sphinx marks every ordinary cross-reference link with that class.
_DROP_CLASSES = (
    ".navbox, .mw-editsection, sup.reference, .mw-ref, .reflist, .references, "
    ".mw-references-wrap, .noprint, .metadata, .sidebar, .mw-jump-link, .toc, #toc, "
    ".hatnote, .infobox-navbar, .mw-empty-elt, "
    ".catlinks, .printfooter, ._attribution, .headerlink, .s-user-card, .comment-actions, "
    ".js-vote-count, .nav-links, .actionbar-portal, .footer-stat, [aria-hidden=true]"
)


CAPTIONS = ".thumbcaption, figcaption, .gallerytext"


def clean_tree(
    html: bytes | str, keep_references: bool = False, keep_captions: bool = False
) -> HTMLParser:
    """Parse article HTML and drop everything that is not readable content.

    Picture captions go too unless ``keep_captions``: without the picture they are stray
    sentences.
    """
    tree = HTMLParser(html)
    selectors = [
        _DROP,
        _DROP_CLASSES if not keep_references else "",
        CAPTIONS if not keep_captions else "",
    ]
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
    entry = resolve_redirects(entry)
    if entry is None:
        return None
    try:
        item = entry.get_item()
        return Article(entry.path, entry.title, item.mimetype, bytes(item.content))
    except (RuntimeError, OSError, KeyError):
        return None
