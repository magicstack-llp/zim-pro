"""MCP server: lets LLM apps search and read the local ZIM library."""

import asyncio
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from .catalog.updates import book_updates
from .context import Context
from .library import search as zim_search
from .library.article import outline, to_markdown
from .library.reader import main_path, read_article

INSTRUCTIONS = """\
Offline knowledge library served from local ZIM archives (Wikipedia, Stack Exchange, \
documentation sets and more). Call list_books to see what is available, search to find \
articles, then get_article to read one. Paths returned by search are passed to get_article \
unchanged."""


def build_mcp(ctx: Context) -> MCPServer:
    mcp = MCPServer("kiwix-library", instructions=INSTRUCTIONS)

    def _book_summary(book: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": book["id"],
            "title": book["title"],
            "description": book["description"],
            "language": book["language"],
            "date": book["date"],
            "articles": book["article_count"],
            "fulltext_search": bool(book["has_ftindex"]),
        }

    @mcp.tool()
    async def list_books(query: str = "", language: str = "") -> list[dict[str, Any]]:
        """List the books (ZIM archives) in the library.

        Args:
            query: optional text to filter by, matched against id, title and description.
            language: optional ISO 639-3 code such as "eng".
        """
        like = f"%{query}%"
        rows = ctx.db.all(
            "SELECT * FROM books WHERE error IS NULL "
            "AND (id LIKE ? OR title LIKE ? OR description LIKE ?) "
            "AND (? = '' OR language LIKE ?) ORDER BY title COLLATE NOCASE",
            (like, like, like, language, f"%{language}%"),
        )
        return [_book_summary(row) for row in rows]

    def _search(query: str, book: str, limit: int) -> dict[str, Any]:
        if book:
            _, archive = ctx.archive(book)
            found = zim_search.search_book(archive, query, 0, limit)
            return {"book": book, **found}
        groups = []
        for row in ctx.db.all("SELECT * FROM books WHERE error IS NULL ORDER BY size DESC"):
            found = zim_search.search_book(ctx.pool.get(row["path"]), query, 0, 3, snippets=False)
            if found["results"]:
                groups.append({"book": row["id"], "title": row["title"], **found})
        groups.sort(key=lambda g: (g["mode"] != "fulltext", -g["total"]))
        return {"books": groups[:limit]}

    @mcp.tool()
    async def search(query: str, book: str = "", limit: int = 10) -> dict[str, Any]:
        """Search for articles.

        With a book id, returns that book's best matches with text snippets. Without one,
        searches every book and returns the top few matches per book, so you can pick a
        book and search it again for more.

        Args:
            query: the search terms.
            book: a book id from list_books, or empty to search all books.
            limit: maximum results (or books, when searching all of them).
        """
        try:
            return await asyncio.to_thread(_search, query, book, max(1, min(limit, 50)))
        except LookupError as exc:
            return {"error": f"{exc}. Call list_books for the valid ids."}

    @mcp.tool()
    async def suggest(book: str, prefix: str, limit: int = 10) -> list[dict[str, str]]:
        """Find articles in a book whose title starts with or contains the given text."""
        try:
            _, archive = ctx.archive(book)
        except LookupError as exc:
            return [{"error": f"{exc}. Call list_books for the valid ids."}]
        return await asyncio.to_thread(zim_search.suggest, archive, prefix, max(1, min(limit, 50)))

    def _article(book: str, path: str):
        _, archive = ctx.archive(book)
        target = path or main_path(archive) or ""
        article = read_article(archive, target)
        if article is None:
            raise LookupError(f"no article at {path!r} in {book!r}; use search to find the path")
        return article

    @mcp.tool()
    async def get_article(
        book: str, path: str = "", offset: int = 0, max_chars: int = 12000
    ) -> str:
        """Read an article as Markdown.

        Long articles are returned in pages: when the reply says more text remains, call
        again with the given offset.

        Args:
            book: a book id from list_books.
            path: the article path from search results; empty for the book's main page.
            offset: character position to start from.
            max_chars: maximum characters to return.
        """

        def work() -> str:
            article = _article(book, path)
            text = to_markdown(article)
            start = max(0, offset)
            end = min(len(text), start + max(500, min(max_chars, 100_000)))
            header = f"# {article.title or article.path}\n(book: {book}, path: {article.path}, "
            header += f"characters {start}-{end} of {len(text)})\n\n"
            footer = ""
            if end < len(text):
                footer = f"\n\n[{len(text) - end} more characters; continue with offset={end}]"
            return header + text[start:end] + footer

        try:
            return await asyncio.to_thread(work)
        except LookupError as exc:
            return f"Error: {exc}"

    @mcp.tool()
    async def get_article_outline(book: str, path: str = "") -> dict[str, Any]:
        """List an article's headings and the other articles it links to."""

        def work() -> dict[str, Any]:
            article = _article(book, path)
            return {"title": article.title, "path": article.path, **outline(article)}

        try:
            return await asyncio.to_thread(work)
        except LookupError as exc:
            return {"error": str(exc)}

    @mcp.tool()
    async def list_updates() -> list[dict[str, Any]]:
        """List books that have a newer release available in the Kiwix catalog."""
        return [
            {
                "book": item["book"]["id"],
                "title": item["book"]["title"],
                "current": item["book"]["date"],
                "available": item["latest"]["issued"][:10],
                "size_bytes": item["latest"]["size"],
                "downloading": bool(item["download"]),
            }
            for item in book_updates(ctx.db)
            if item["status"] == "update"
        ]

    @mcp.tool()
    async def start_update(book: str) -> str:
        """Start downloading the newest release of a book. Disabled unless enabled in settings."""
        if not ctx.db.get_setting("mcp_allow_writes"):
            return "Updates over MCP are turned off. Enable 'mcp_allow_writes' in the web UI."
        for item in book_updates(ctx.db):
            if item["book"]["id"] == book and item["status"] == "update":
                ctx.downloads.enqueue(item["latest"]["uuid"])
                return f"Queued {item['latest']['title']} ({item['latest']['issued'][:10]})."
        return f"No update is available for {book!r}."

    return mcp


def http_app(mcp: MCPServer):
    # Reached by IP or hostname on the LAN, so Host-header pinning is off; access is
    # controlled by the optional admin token instead.
    return mcp.streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
