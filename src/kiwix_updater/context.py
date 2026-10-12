from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request
from libzim.reader import Archive

from .config import Config
from .db import Database
from .downloads.manager import DownloadManager
from .events import EventBus
from .library.archive_pool import ArchivePool
from .library.scanner import Scanner


@dataclass
class Context:
    """Everything the request handlers and background jobs share."""

    config: Config
    db: Database
    pool: ArchivePool
    scanner: Scanner
    events: EventBus
    downloads: DownloadManager

    def book(self, book_id: str) -> dict[str, Any] | None:
        return self.db.one("SELECT * FROM books WHERE id = ? AND error IS NULL", (book_id,))

    def archive(self, book_id: str) -> tuple[dict[str, Any], Archive]:
        book = self.book(book_id)
        if book is None:
            raise LookupError(f"no book named {book_id!r}")
        return book, self.pool.get(book["path"])


def get_context(request: Request) -> Context:
    return request.app.state.ctx


def require_admin(request: Request) -> None:
    """Guard for routes that change state; a no-op unless KIWIX_ADMIN_TOKEN is set."""
    token = request.app.state.ctx.config.admin_token
    if not token:
        return
    supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if supplied != token:
        raise HTTPException(401, "admin token required")
