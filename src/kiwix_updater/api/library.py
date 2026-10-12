import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from ..config import DEFAULT_SETTINGS, POLICIES
from ..context import Context, get_context, require_admin
from ..library import search as zim_search

router = APIRouter(prefix="/api")

BOOK_COLUMNS = (
    "id, filename, size, name, flavour, title, description, language, date, uuid, creator, "
    "publisher, tags, article_count, media_count, has_ftindex, has_illustration, policy, error"
)


@router.get("/status")
def status(ctx: Context = Depends(get_context)) -> dict[str, Any]:
    usage = shutil.disk_usage(ctx.config.data_dir)
    return {
        "books": ctx.db.scalar("SELECT COUNT(*) FROM books"),
        "library_bytes": ctx.db.scalar("SELECT COALESCE(SUM(size), 0) FROM books"),
        "disk_free": usage.free,
        "disk_total": usage.total,
        "catalog_entries": ctx.db.scalar("SELECT COUNT(*) FROM catalog"),
        "catalog_refreshed": ctx.db.get_meta("catalog_refreshed"),
        "catalog_error": ctx.db.get_meta("catalog_error"),
        "auth_required": bool(ctx.config.admin_token),
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
    q: str, book: str = "", start: int = 0, count: int = 20, ctx: Context = Depends(get_context)
) -> dict[str, Any]:
    """Search one book, or every book when ``book`` is empty.

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
    for row in ctx.db.all(
        "SELECT id, title, path, has_illustration FROM books WHERE error IS NULL"
    ):
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


@router.get("/settings")
def get_settings(ctx: Context = Depends(get_context)) -> dict[str, Any]:
    return ctx.db.get_settings()


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
            clean[key] = str(value)
    if clean.get("default_policy", "notify") not in POLICIES:
        raise HTTPException(422, f"default_policy must be one of {', '.join(POLICIES)}")
    for key, low, high in (("max_concurrent_downloads", 1, 8), ("connections_per_file", 1, 16)):
        if key in clean:
            clean[key] = max(low, min(high, clean[key]))
    if "catalog_refresh_hours" in clean:
        clean["catalog_refresh_hours"] = max(1, clean["catalog_refresh_hours"])
    ctx.db.set_settings(clean)
    ctx.downloads._wake.set()
    return ctx.db.get_settings()
