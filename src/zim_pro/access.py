"""Keys for LLM apps that reach the library over MCP.

A key is shown once, when it is made. Only its SHA-256 is kept, so the database, a backup of
it, or anyone reading it cannot give the key back; a lost key is replaced, not recovered.
"""

import hashlib
import hmac
import secrets
import time
from typing import Any

from .db import Database

PREFIX = "zp_mcp_"
SEEN_EVERY = 60  # seconds between notes of a key's use: reading must not write each time


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def list_keys(db: Database) -> list[dict[str, Any]]:
    return db.all("SELECT id, name, hint, created, last_used FROM mcp_keys ORDER BY id DESC")


def has_keys(db: Database) -> bool:
    return db.scalar("SELECT 1 FROM mcp_keys LIMIT 1") is not None


def create_key(db: Database, name: str) -> tuple[dict[str, Any], str]:
    """Make a key; returns its record and, this once, the key itself."""
    key = PREFIX + secrets.token_urlsafe(32)
    hint = f"{key[: len(PREFIX) + 4]}…{key[-4:]}"
    cursor = db.execute(
        "INSERT INTO mcp_keys (name, hint, digest) VALUES (?, ?, ?)",
        (name.strip()[:80] or "Unnamed key", hint, _digest(key)),
    )
    row = db.one(
        "SELECT id, name, hint, created, last_used FROM mcp_keys WHERE id = ?",
        (cursor.lastrowid,),
    )
    return row, key


def delete_key(db: Database, key_id: int) -> None:
    db.execute("DELETE FROM mcp_keys WHERE id = ?", (key_id,))


def check_key(db: Database, key: str) -> bool:
    """Whether ``key`` is one of the keys made here; notes that it was used."""
    if not key.startswith(PREFIX):
        return False
    digest = _digest(key)
    for row in db.all("SELECT id, digest, last_used FROM mcp_keys"):
        if hmac.compare_digest(row["digest"], digest):
            if time.time() - (row["last_used"] or 0) > SEEN_EVERY:
                db.execute(
                    "UPDATE mcp_keys SET last_used = unixepoch('subsec') WHERE id = ?",
                    (row["id"],),
                )
            return True
    return False
