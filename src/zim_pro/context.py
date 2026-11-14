import hmac
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request
from libzim.reader import Archive

from .builder.jobs import BuildManager
from .config import Config
from .db import Database
from .downloads.manager import DownloadManager
from .events import EventBus
from .feeds.manager import FeedManager
from .library.archive_pool import ArchivePool
from .library.scanner import Scanner
from .network import Network
from .queues import LocalBroker

if TYPE_CHECKING:
    from .monitor import Monitor


@dataclass
class Context:
    """Everything the request handlers and background jobs share."""

    config: Config
    db: Database
    pool: ArchivePool
    scanner: Scanner
    events: EventBus
    downloads: DownloadManager
    builds: BuildManager
    feeds: FeedManager
    network: Network
    broker: LocalBroker
    monitor: "Monitor | None" = None

    def book(self, book_id: str) -> dict[str, Any] | None:
        return self.db.one("SELECT * FROM books WHERE id = ? AND error IS NULL", (book_id,))

    def archive(self, book_id: str) -> tuple[dict[str, Any], Archive]:
        book = self.book(book_id)
        if book is None:
            raise LookupError(f"no book named {book_id!r}")
        try:
            return book, self.pool.get(book["path"])
        except (RuntimeError, OSError) as exc:
            self.pool.invalidate(book["path"])
            raise LookupError(f"archive for {book_id!r} could not be opened: {exc}") from exc


def get_context(request: Request) -> Context:
    return request.app.state.ctx


def bearer_token(request: Request) -> str:
    """Extract the token from an Authorization: Bearer header, case-insensitively."""
    header = request.headers.get("authorization", "").strip()
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer":
        return token.strip()
    return ""


def is_admin_token(supplied: str, expected: str | None) -> bool:
    """Constant-time comparison for the configured admin token."""
    return bool(expected) and hmac.compare_digest(supplied.encode(), expected.encode())


def require_admin(request: Request) -> None:
    """Guard for routes that change state; a no-op unless KIWIX_ADMIN_TOKEN is set."""
    token = request.app.state.ctx.config.admin_token
    if not token:
        return
    if not is_admin_token(bearer_token(request), token):
        raise HTTPException(401, "admin token required")
