import json
import logging
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from .config import DEFAULT_SETTINGS

log = logging.getLogger(__name__)


class StartupError(RuntimeError):
    """Raised when the database or library directories cannot be initialized."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    flavour TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    date TEXT NOT NULL DEFAULT '',
    uuid TEXT NOT NULL DEFAULT '',
    creator TEXT NOT NULL DEFAULT '',
    publisher TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    article_count INTEGER NOT NULL DEFAULT 0,
    media_count INTEGER NOT NULL DEFAULT 0,
    has_ftindex INTEGER NOT NULL DEFAULT 0 CHECK (has_ftindex IN (0, 1)),
    has_illustration INTEGER NOT NULL DEFAULT 0 CHECK (has_illustration IN (0, 1)),
    policy TEXT NOT NULL DEFAULT 'notify' CHECK (policy IN ('auto', 'notify', 'pinned')),
    error TEXT,
    category TEXT NOT NULL DEFAULT 'other'
);
CREATE TABLE IF NOT EXISTS shadowed (
    path TEXT PRIMARY KEY,
    book_id TEXT NOT NULL,
    size INTEGER NOT NULL,
    date TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS catalog (
    uuid TEXT PRIMARY KEY,
    stem TEXT NOT NULL,
    name TEXT NOT NULL,
    flavour TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    publisher TEXT NOT NULL DEFAULT '',
    article_count INTEGER NOT NULL DEFAULT 0,
    media_count INTEGER NOT NULL DEFAULT 0,
    issued TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    illustration TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS catalog_stem ON catalog (stem);
CREATE INDEX IF NOT EXISTS catalog_name ON catalog (name, flavour);
CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_uuid TEXT NOT NULL,
    book_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL,
    filename TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    bytes_done INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'queued' CHECK (
        state IN ('queued', 'downloading', 'verifying', 'paused', 'done', 'failed', 'cancelled')
    ),
    pieces BLOB,
    replaces TEXT,
    from_date TEXT NOT NULL DEFAULT '',
    to_date TEXT NOT NULL DEFAULT '',
    error TEXT,
    position INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL DEFAULT (unixepoch()),
    updated REAL NOT NULL DEFAULT (unixepoch()),
    started REAL,
    finished REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS downloads_active
    ON downloads (catalog_uuid)
    WHERE state IN ('queued', 'downloading', 'verifying', 'paused');
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL DEFAULT (unixepoch()),
    book_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    from_date TEXT NOT NULL DEFAULT '',
    to_date TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    result TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ai_providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'custom',
    base_url TEXT NOT NULL,
    api_key TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created REAL NOT NULL DEFAULT (unixepoch('subsec')),
    updated REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
CREATE INDEX IF NOT EXISTS chats_updated ON chats (updated DESC);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL DEFAULT '',
    sources TEXT,
    queries TEXT,
    error TEXT,
    model TEXT,
    reasoning TEXT,
    thought_seconds REAL,
    steps TEXT,
    created REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
CREATE INDEX IF NOT EXISTS chat_messages_chat ON chat_messages (chat_id, id);
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    engine TEXT NOT NULL DEFAULT 'builtin' CHECK (engine IN ('builtin', 'zimit')),
    scope TEXT NOT NULL DEFAULT 'page' CHECK (scope IN ('page', 'path', 'site')),
    max_pages INTEGER NOT NULL DEFAULT 500,
    max_depth INTEGER NOT NULL DEFAULT 5,
    mode TEXT NOT NULL DEFAULT 'clean' CHECK (mode IN ('clean', 'full')),
    images INTEGER NOT NULL DEFAULT 1 CHECK (images IN (0, 1)),
    video INTEGER NOT NULL DEFAULT 0 CHECK (video IN (0, 1)),
    subdomains INTEGER NOT NULL DEFAULT 0 CHECK (subdomains IN (0, 1)),
    sitemaps TEXT NOT NULL DEFAULT '',
    robots_url TEXT NOT NULL DEFAULT '',
    rate_limit REAL NOT NULL DEFAULT 0,
    rate_unit TEXT NOT NULL DEFAULT 'second' CHECK (rate_unit IN ('second', 'minute')),
    feeds TEXT NOT NULL DEFAULT '',
    feed_minutes INTEGER NOT NULL DEFAULT 0,
    last_feed_check REAL,
    language TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    rebuild_days INTEGER NOT NULL DEFAULT 0,
    book_id TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    icon REAL,
    last_built REAL,
    created REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('zim', 'epub', 'pdf', 'mobi')),
    size INTEGER NOT NULL DEFAULT 0,
    path TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    images INTEGER NOT NULL DEFAULT 1 CHECK (images IN (0, 1)),
    book_id TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    icon REAL,
    last_built REAL,
    created REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
CREATE TABLE IF NOT EXISTS builds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('link', 'upload', 'feed')),
    source_id INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued', 'processing', 'paused', 'finished', 'failed', 'cancelled')),
    phase TEXT NOT NULL DEFAULT '',
    done INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    size INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    log TEXT NOT NULL DEFAULT '[]',
    report TEXT NOT NULL DEFAULT '{}',
    params TEXT NOT NULL DEFAULT '{}',
    book_id TEXT NOT NULL DEFAULT '',
    position REAL,
    created REAL NOT NULL DEFAULT (unixepoch('subsec')),
    started REAL,
    finished REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS builds_active
    ON builds (kind, source_id)
    WHERE state IN ('queued', 'processing', 'paused');
CREATE INDEX IF NOT EXISTS builds_source ON builds (kind, source_id, id DESC);
CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'clean' CHECK (mode IN ('clean', 'full')),
    images INTEGER NOT NULL DEFAULT 1 CHECK (images IN (0, 1)),
    video INTEGER NOT NULL DEFAULT 0 CHECK (video IN (0, 1)),
    full_text INTEGER NOT NULL DEFAULT 1 CHECK (full_text IN (0, 1)),
    interval_minutes INTEGER NOT NULL DEFAULT 60,
    keep INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    rate_limit REAL NOT NULL DEFAULT 0,
    rate_unit TEXT NOT NULL DEFAULT 'second' CHECK (rate_unit IN ('second', 'minute')),
    book_id TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    icon REAL,
    last_attempt REAL,
    last_synced REAL,
    last_new INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
CREATE TABLE IF NOT EXISTS feed_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id INTEGER NOT NULL REFERENCES feeds (id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    site TEXT NOT NULL DEFAULT '',
    last_checked REAL,
    last_error TEXT,
    UNIQUE (feed_id, url)
);
CREATE TABLE IF NOT EXISTS feed_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id INTEGER NOT NULL REFERENCES feeds (id) ON DELETE CASCADE,
    source_id INTEGER REFERENCES feed_sources (id) ON DELETE SET NULL,
    guid TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    published REAL NOT NULL DEFAULT 0,
    content TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'new'
        CHECK (state IN ('new', 'saved', 'skipped', 'failed', 'dropped')),
    tries INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    path TEXT NOT NULL DEFAULT '',
    added REAL NOT NULL DEFAULT (unixepoch('subsec')),
    UNIQUE (feed_id, guid)
);
CREATE INDEX IF NOT EXISTS feed_items_feed ON feed_items (feed_id, state, published);
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL DEFAULT (unixepoch('subsec')),
    query TEXT NOT NULL,
    book TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    UNIQUE (query, book, category)
);
CREATE TABLE IF NOT EXISTS categories (
    name TEXT PRIMARY KEY COLLATE NOCASE,
    created REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
CREATE TABLE IF NOT EXISTS mcp_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    hint TEXT NOT NULL,
    digest TEXT NOT NULL UNIQUE,
    created REAL NOT NULL DEFAULT (unixepoch('subsec')),
    last_used REAL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL CHECK (json_valid(value))
);
"""


def _loads(raw: Any, default: Any, key: str) -> Any:
    """Decode JSON stored in SQLite, falling back to ``default`` if malformed."""
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        log.warning("Invalid JSON in settings[%r]: %s; using default", key, exc)
        return default


class Database:
    """A single shared SQLite connection, serialised with a lock."""

    BUSY_TIMEOUT_MS = 15000
    SCHEMA_VERSION = 2

    def __init__(self, path: Path | str):
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._tx_depth = 0
        try:
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(path),
                check_same_thread=False,
                isolation_level=None,
                timeout=self.BUSY_TIMEOUT_MS / 1000,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute(f"PRAGMA busy_timeout={self.BUSY_TIMEOUT_MS}")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._migrate()
        except (OSError, sqlite3.Error) as exc:
            if self._conn is not None:
                with suppress(sqlite3.Error):
                    self._conn.close()
                self._conn = None
            raise StartupError(
                f"Cannot open the database at {path}: {exc}. The state directory must be "
                "writable by the user this server runs as."
            ) from exc

    def _ensure_columns(self, table: str, specs: tuple[tuple[str, str], ...]) -> None:
        assert self._conn is not None
        have = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
        for column, kind in specs:
            if column not in have:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
                have.add(column)

    def _migrate(self) -> None:
        """Apply versioned, transactional schema migrations."""
        assert self._conn is not None
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            with self.transaction():
                self._ensure_columns("books", (("category", "TEXT NOT NULL DEFAULT 'other'"),))
                self._ensure_columns(
                    "chat_messages",
                    (
                        ("model", "TEXT"),
                        ("reasoning", "TEXT"),
                        ("thought_seconds", "REAL"),
                        ("steps", "TEXT"),
                    ),
                )
                self._ensure_columns("ai_providers", (("enabled", "INTEGER NOT NULL DEFAULT 1"),))
                self._ensure_columns(
                    "links",
                    (
                        ("subdomains", "INTEGER NOT NULL DEFAULT 0"),
                        ("sitemaps", "TEXT NOT NULL DEFAULT ''"),
                        ("robots_url", "TEXT NOT NULL DEFAULT ''"),
                        ("rate_limit", "REAL NOT NULL DEFAULT 0"),
                        ("rate_unit", "TEXT NOT NULL DEFAULT 'second'"),
                        ("feeds", "TEXT NOT NULL DEFAULT ''"),
                        ("feed_minutes", "INTEGER NOT NULL DEFAULT 0"),
                        ("last_feed_check", "REAL"),
                    ),
                )
                for table in ("links", "uploads", "feeds"):
                    self._ensure_columns(
                        table,
                        (("category", "TEXT NOT NULL DEFAULT ''"), ("icon", "REAL")),
                    )
                self._ensure_columns(
                    "builds",
                    (("params", "TEXT NOT NULL DEFAULT '{}'"), ("position", "REAL")),
                )
                self._ensure_columns("downloads", (("started", "REAL"), ("finished", "REAL")))
                self._conn.execute("PRAGMA user_version = 1")
            version = 1
        if version < 2:
            with self.transaction():
                self._conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS downloads_active "
                    "ON downloads (catalog_uuid) "
                    "WHERE state IN ('queued', 'downloading', 'verifying', 'paused')"
                )
                self._conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS builds_active "
                    "ON builds (kind, source_id) "
                    "WHERE state IN ('queued', 'processing', 'paused')"
                )
                self._conn.execute("PRAGMA user_version = 2")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run a block atomically, using a SAVEPOINT when already inside a transaction."""
        with self._lock:
            assert self._conn is not None
            nested = self._conn.in_transaction
            if nested:
                self._tx_depth += 1
                sp = f"sp_{self._tx_depth}"
                self._conn.execute(f"SAVEPOINT {sp}")
                try:
                    yield
                    self._conn.execute(f"RELEASE SAVEPOINT {sp}")
                except BaseException:
                    with suppress(sqlite3.Error):
                        self._conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                        self._conn.execute(f"RELEASE SAVEPOINT {sp}")
                    raise
                finally:
                    self._tx_depth -= 1
            else:
                self._conn.execute("BEGIN")
                try:
                    yield
                    self._conn.execute("COMMIT")
                except BaseException:
                    if self._conn.in_transaction:
                        with suppress(sqlite3.Error):
                            self._conn.execute("ROLLBACK")
                    raise

    def execute(self, sql: str, params: Iterable[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            assert self._conn is not None
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, rows: Iterable[Iterable[Any] | dict[str, Any]]) -> None:
        with self.transaction():
            assert self._conn is not None
            self._conn.executemany(sql, rows)

    def all(self, sql: str, params: Iterable[Any] | dict[str, Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: Iterable[Any] | dict[str, Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def scalar(self, sql: str, params: Iterable[Any] | dict[str, Any] = ()) -> Any:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- search history ---------------------------------------------------

    SEARCHES_KEPT = 200

    def add_search(self, query: str, book: str = "", category: str = "") -> None:
        """Remember a search; one made again moves to the top rather than repeating."""
        with self.transaction():
            # Removed and added again, so that the newest id is always the latest search.
            self.execute(
                "DELETE FROM searches WHERE query = ? AND book = ? AND category = ?",
                (query, book, category),
            )
            self.execute(
                "INSERT INTO searches (query, book, category) VALUES (?, ?, ?)",
                (query, book, category),
            )
            self.execute(
                "DELETE FROM searches WHERE id NOT IN "
                "(SELECT id FROM searches ORDER BY id DESC LIMIT ?)",
                (self.SEARCHES_KEPT,),
            )

    def list_searches(self) -> list[dict[str, Any]]:
        return self.all("SELECT id, ts, query, book, category FROM searches ORDER BY id DESC")

    # -- settings ---------------------------------------------------------

    def get_settings(self) -> dict[str, Any]:
        settings: dict[str, Any] = dict(DEFAULT_SETTINGS)
        for row in self.all("SELECT key, value FROM settings"):
            key = row["key"]
            if key in settings:
                settings[key] = _loads(row["value"], settings[key], key)
        return settings

    def get_setting(self, key: str) -> Any:
        return self.get_settings()[key]

    def set_settings(self, values: dict[str, Any]) -> None:
        unknown = [k for k in values if k not in DEFAULT_SETTINGS]
        if unknown:
            raise KeyError(f"unknown setting(s): {', '.join(unknown)}")
        self.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [(k, json.dumps(v)) for k, v in values.items()],
        )

    def get_meta(self, key: str) -> Any:
        value = self.scalar("SELECT value FROM settings WHERE key = ?", (f"_{key}",))
        return _loads(value, None, f"_{key}") if value is not None else None

    def set_meta(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (f"_{key}", json.dumps(value)),
        )
