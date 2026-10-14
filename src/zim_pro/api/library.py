import logging
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from .. import logs
from ..config import DEFAULT_SETTINGS, POLICIES
from ..context import Context, get_context, require_admin
from ..library import search as zim_search

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

BOOK_COLUMNS = (
    "id, filename, size, name, flavour, title, description, language, date, uuid, creator, "
    "publisher, tags, category, article_count, media_count, has_ftindex, has_illustration, policy, "
    "error"
)


@router.get("/health")
def health(ctx: Context = Depends(get_context)) -> dict[str, bool]:
    """For container health checks: the server answers and its database can be read."""
    ctx.db.scalar("SELECT 1")
    return {"ok": True}


@router.get("/status")
def status(ctx: Context = Depends(get_context)) -> dict[str, Any]:
    usage = shutil.disk_usage(ctx.config.data_dir)
    settings = ctx.db.get_settings()
    return {
        "books": ctx.db.scalar("SELECT COUNT(*) FROM books"),
        "library_bytes": ctx.db.scalar("SELECT COALESCE(SUM(size), 0) FROM books"),
        "disk_free": usage.free,
        "disk_total": usage.total,
        "catalog_entries": ctx.db.scalar("SELECT COUNT(*) FROM catalog"),
        "catalog_refreshed": ctx.db.get_meta("catalog_refreshed"),
        "catalog_error": ctx.db.get_meta("catalog_error"),
        "auth_required": bool(ctx.config.admin_token),
        "ai_configured": bool(settings["ai_provider"] and settings["ai_model"]),
        "ai_provider": settings["ai_provider"],
        "ai_model": settings["ai_model"],
        "web_mode": settings["web_mode"],
        "tts_mode": settings["tts_mode"],
    }


@router.get("/books")
def list_books(ctx: Context = Depends(get_context)) -> list[dict[str, Any]]:
    return ctx.db.all(f"SELECT {BOOK_COLUMNS} FROM books ORDER BY title COLLATE NOCASE")


@router.get("/books/{book_id}")
def get_book(book_id: str, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    book = ctx.db.one(f"SELECT {BOOK_COLUMNS} FROM books WHERE id = ?", (book_id,))
    if book is None:
        raise HTTPException(404, "No such book")
    return book


@router.get("/books/{book_id}/illustration")
def illustration(book_id: str, ctx: Context = Depends(get_context)) -> Response:
    try:
        _, archive = ctx.archive(book_id)
        data = bytes(archive.get_illustration_item(48).content)
    except Exception:
        raise HTTPException(404, "No illustration") from None
    return Response(data, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


class BookPatch(BaseModel):
    policy: str


@router.patch("/books/{book_id}", dependencies=[Depends(require_admin)])
def patch_book(book_id: str, patch: BookPatch, ctx: Context = Depends(get_context)):
    if patch.policy not in POLICIES:
        raise HTTPException(422, f"policy must be one of {', '.join(POLICIES)}")
    updated = ctx.db.execute(
        "UPDATE books SET policy = ? WHERE id = ?", (patch.policy, book_id)
    ).rowcount
    if not updated:
        raise HTTPException(404, "No such book")
    return get_book(book_id, ctx)


@router.delete("/books/{book_id}", dependencies=[Depends(require_admin)])
def delete_book(book_id: str, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    """Delete a book's ZIM file from disk."""
    book = ctx.db.one("SELECT path FROM books WHERE id = ?", (book_id,))
    if book is None:
        raise HTTPException(404, "No such book")
    ctx.pool.invalidate(book["path"])
    Path(book["path"]).unlink(missing_ok=True)
    Path(book["path"] + ".meta4").unlink(missing_ok=True)
    result = ctx.scanner.scan()
    ctx.events.publish("library", {"changed": True})
    return result


@router.post("/library/rescan", dependencies=[Depends(require_admin)])
def rescan(ctx: Context = Depends(get_context)) -> dict[str, Any]:
    result = ctx.scanner.scan()
    ctx.events.publish("library", {"changed": True})
    return result


@router.get("/library/leftovers")
def leftovers(ctx: Context = Depends(get_context)) -> list[dict[str, Any]]:
    return ctx.scanner.leftovers()


@router.get("/search")
def search(
    q: str,
    book: str = "",
    category: str = "",
    start: int = 0,
    count: int = 20,
    ctx: Context = Depends(get_context),
) -> dict[str, Any]:
    """Search one book, or every book (optionally of one category) when ``book`` is empty.

    libzim does not expose relevance scores, so results from different books cannot be
    ranked against each other; a cross-book search returns the top hits grouped by book.
    """
    q = q.strip()
    count = max(1, min(count, 100))
    if not q:
        return {"total": 0, "results": [], "books": []}
    if book:
        try:
            _, archive = ctx.archive(book)
        except LookupError:
            raise HTTPException(404, "No such book") from None
        return {"book": book, **zim_search.search_book(archive, q, max(0, start), count)}
    groups = []
    rows = ctx.db.all(
        "SELECT id, title, path, has_illustration FROM books "
        "WHERE error IS NULL AND (? = '' OR category = ?)",
        (category, category),
    )
    for row in rows:
        try:
            found = zim_search.search_book(ctx.pool.get(row["path"]), q, 0, 3, snippets=False)
        except Exception:
            continue
        if found["results"]:
            groups.append(
                {
                    "book": row["id"],
                    "title": row["title"],
                    "has_illustration": row["has_illustration"],
                    **found,
                }
            )
    groups.sort(key=lambda g: (g["mode"] != "fulltext", -g["total"]))
    return {"books": groups}


@router.get("/suggest")
def suggest(q: str, book: str, count: int = 8, ctx: Context = Depends(get_context)):
    try:
        _, archive = ctx.archive(book)
    except LookupError:
        raise HTTPException(404, "No such book") from None
    return zim_search.suggest(archive, q, max(1, min(count, 30)))


SECRET_MASK = "••••••••"
SECRET_SETTINGS = ("web_api_key", "vpn_control_key", "vpn_control_password")
CHOICES = {
    "default_policy": POLICIES,
    "web_mode": ("off", "api", "model"),
    "web_service": ("tavily", "brave", "serper", "searxng"),
    "tts_mode": ("browser", "provider", "off"),
}


class SearchEntry(BaseModel):
    query: str
    book: str = ""
    category: str = ""


@router.get("/search/history")
def search_history(ctx: Context = Depends(get_context)) -> dict[str, Any]:
    return {"enabled": ctx.db.get_setting("search_history"), "items": ctx.db.list_searches()}


@router.post("/search/history")
def remember_search(entry: SearchEntry, ctx: Context = Depends(get_context)) -> dict[str, bool]:
    query = " ".join(entry.query.split())[:200]
    saved = bool(query) and bool(ctx.db.get_setting("search_history"))
    if saved:
        ctx.db.add_search(query, entry.book[:200], entry.category[:100])
    return {"saved": saved}


@router.delete("/search/history/{entry_id}")
def forget_search(entry_id: int, ctx: Context = Depends(get_context)) -> dict[str, bool]:
    ctx.db.execute("DELETE FROM searches WHERE id = ?", (entry_id,))
    return {"ok": True}


@router.delete("/search/history")
def clear_search_history(ctx: Context = Depends(get_context)) -> dict[str, bool]:
    ctx.db.execute("DELETE FROM searches")
    return {"ok": True}


def _public_settings(ctx: Context) -> dict[str, Any]:
    """Settings as shown to the browser: secrets never leave the server."""
    settings = ctx.db.get_settings()
    for key in SECRET_SETTINGS:
        settings[key] = SECRET_MASK if settings[key] else ""
    return settings


@router.get("/settings")
def get_settings(ctx: Context = Depends(get_context)) -> dict[str, Any]:
    return _public_settings(ctx)


@router.put("/settings", dependencies=[Depends(require_admin)])
async def put_settings(
    values: dict[str, Any], ctx: Context = Depends(get_context)
) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in values.items():
        default = DEFAULT_SETTINGS.get(key)
        if default is None:
            continue
        if isinstance(default, bool):
            clean[key] = bool(value)
        elif isinstance(default, int):
            try:
                clean[key] = max(0, int(value))
            except (TypeError, ValueError):
                raise HTTPException(422, f"{key} must be a number") from None
        else:
            clean[key] = str(value).strip()[:2000]
    for key in SECRET_SETTINGS:
        if clean.get(key) == SECRET_MASK:
            del clean[key]  # unchanged: the form only ever saw the mask
    for key, allowed in CHOICES.items():
        if key in clean and clean[key] not in allowed:
            raise HTTPException(422, f"{key} must be one of {', '.join(allowed)}")
    if clean.get("web_api_url") and not clean["web_api_url"].startswith(("http://", "https://")):
        raise HTTPException(422, "The search server address must start with http:// or https://")
    for key, schemes in (
        ("build_proxy", ("http://", "https://")),
        ("captcha_solver_url", ("http://", "https://")),
        ("vpn_control_url", ("http://", "https://")),
    ):
        if clean.get(key) and not clean[key].startswith(schemes):
            raise HTTPException(422, f"{key} must start with {' or '.join(schemes)}")
    if "ai_max_sources" in clean:
        clean["ai_max_sources"] = max(1, min(12, clean["ai_max_sources"]))
    for key, low, high in (
        ("max_concurrent_downloads", 1, 8),
        ("connections_per_file", 1, 16),
        ("max_concurrent_builds", 1, 4),
        ("max_concurrent_feed_syncs", 1, 4),
        ("feed_default_interval", 5, 525_600),
        ("build_workers", 1, 16),
        ("upload_limit_mb", 1, 200_000),
    ):
        if key in clean:
            clean[key] = max(low, min(high, clean[key]))
    if "catalog_refresh_hours" in clean:
        clean["catalog_refresh_hours"] = max(1, clean["catalog_refresh_hours"])
    if "log_level" in clean:
        clean["log_level"] = clean["log_level"].upper()
        if clean["log_level"] not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise HTTPException(422, "log_level must be DEBUG, INFO, WARNING or ERROR")
        logs.set_level(clean["log_level"])
    changed = sorted(key for key in clean if key not in SECRET_SETTINGS)
    log.info("settings changed: %s", ", ".join(changed) or "secrets only")
    ctx.db.set_settings(clean)
    ctx.network.forget()
    ctx.events.publish("network", {"settings": True})
    ctx.downloads._wake.set()
    ctx.builds._wake.set()
    ctx.feeds._wake.set()
    return _public_settings(ctx)
