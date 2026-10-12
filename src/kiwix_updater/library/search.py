import re
from typing import Any

from libzim.reader import Archive
from libzim.search import Query, Searcher
from libzim.suggestion import SuggestionSearcher

from .reader import html_to_text

SNIPPET_CHARS = 240


def _title(archive: Archive, path: str) -> str:
    try:
        return archive.get_entry_by_path(path).title or path
    except Exception:
        return path


def suggest(archive: Archive, query: str, count: int = 10) -> list[dict[str, str]]:
    if not query.strip():
        return []
    results = SuggestionSearcher(archive).suggest(query)
    return [{"path": p, "title": _title(archive, p)} for p in results.getResults(0, count)]


def fulltext(
    archive: Archive, query: str, start: int = 0, count: int = 20
) -> tuple[int, list[dict[str, str]]]:
    search = Searcher(archive).search(Query().set_query(query))
    hits = [{"path": p, "title": _title(archive, p)} for p in search.getResults(start, count)]
    return search.getEstimatedMatches(), hits


def search_book(
    archive: Archive, query: str, start: int = 0, count: int = 20, snippets: bool = True
) -> dict[str, Any]:
    """Full-text search, falling back to title matches when there is no index."""
    if archive.has_fulltext_index:
        total, hits = fulltext(archive, query, start, count)
        mode = "fulltext"
    else:
        results = SuggestionSearcher(archive).suggest(query)
        total = results.getEstimatedMatches()
        hits = [{"path": p, "title": _title(archive, p)} for p in results.getResults(start, count)]
        mode = "title"
    if snippets:
        for hit in hits:
            hit["snippet"] = snippet(archive, hit["path"], query)
    return {"total": total, "mode": mode, "results": hits}


def snippet(archive: Archive, path: str, query: str) -> str:
    """A short extract of the article around the first query term found."""
    try:
        item = archive.get_entry_by_path(path).get_item()
        if not item.mimetype.startswith("text/html") or item.size > 4_000_000:
            return ""
        text = html_to_text(bytes(item.content))
    except Exception:
        return ""
    terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 2]
    lowered = text.lower()
    position = min((i for i in (lowered.find(t) for t in terms) if i >= 0), default=0)
    begin = max(0, position - SNIPPET_CHARS // 3)
    extract = text[begin : begin + SNIPPET_CHARS].strip()
    if begin > 0:
        extract = "…" + extract
    if begin + SNIPPET_CHARS < len(text):
        extract += "…"
    return extract
