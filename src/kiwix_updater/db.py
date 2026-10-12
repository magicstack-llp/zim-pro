import json
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .config import DEFAULT_SETTINGS

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
    has_ftindex INTEGER NOT NULL DEFAULT 0,
    has_illustration INTEGER NOT NULL DEFAULT 0,
    policy TEXT NOT NULL DEFAULT 'notify',
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
    state TEXT NOT NULL DEFAULT 'queued',
    pieces BLOB,
    replaces TEXT,
    from_date TEXT NOT NULL DEFAULT '',
    to_date TEXT NOT NULL DEFAULT '',
    error TEXT,
    position INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL DEFAULT (unixepoch()),
    updated REAL NOT NULL DEFAULT (unixepoch())
);
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
    created REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created REAL NOT NULL DEFAULT (unixepoch('subsec')),
    updated REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    role TEXT NOT NULL,
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
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    """A single shared SQLite connection, serialised with a lock."""

    def __init__(self, path: Path | str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        except sqlite3.OperationalError as exc:
            raise SystemExit(
                f"Cannot open the database at {path}: {exc}. The state directory must be "
                "writable by the user this server runs as."
            ) from exc
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(books)")}
        chat_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(chat_messages)")
        }
        for column, kind in (
            ("model", "TEXT"),
            ("reasoning", "TEXT"),
            ("thought_seconds", "REAL"),
            ("steps", "TEXT"),
        ):
            if column not in chat_columns:
                self._conn.execute(f"ALTER TABLE chat_messages ADD COLUMN {column} {kind}")
        if "category" not in columns:
            self._conn.execute(
                "ALTER TABLE books ADD COLUMN category TEXT NOT NULL DEFAULT 'other'"
            )

    def execute(self, sql: str, params: Iterable[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, rows: Iterable[Iterable[Any] | dict[str, Any]]) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(sql, rows)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

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

    # -- settings ---------------------------------------------------------

    def get_settings(self) -> dict[str, Any]:
        settings: dict[str, Any] = dict(DEFAULT_SETTINGS)
        for row in self.all("SELECT key, value FROM settings"):
            if row["key"] in settings:
                settings[row["key"]] = json.loads(row["value"])
        return settings

    def get_setting(self, key: str) -> Any:
        return self.get_settings()[key]

    def set_settings(self, values: dict[str, Any]) -> None:
        self.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [(k, json.dumps(v)) for k, v in values.items() if k in DEFAULT_SETTINGS],
        )

    def get_meta(self, key: str) -> Any:
        value = self.scalar("SELECT value FROM settings WHERE key = ?", (f"_{key}",))
        return json.loads(value) if value is not None else None

    def set_meta(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (f"_{key}", json.dumps(value)),
        )
