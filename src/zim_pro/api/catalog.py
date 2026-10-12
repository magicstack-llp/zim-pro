import shutil
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..catalog import opds
from ..catalog.updates import book_updates
from ..context import Context, get_context, require_admin
from .library import BOOK_COLUMNS

router = APIRouter(prefix="/api")

SORTS = {
    "title": "title COLLATE NOCASE",
    "title_desc": "title COLLATE NOCASE DESC",
    "newest": "issued DESC",
    "oldest": "issued ASC",
    "size": "size DESC",
    "smallest": "size ASC",
    "articles": "article_count DESC",
}
_PUBLIC_BOOK = [c.strip() for c in BOOK_COLUMNS.split(",")]


@router.get("/catalog")
def catalog(
    q: str = "",
    language: str = "",
    category: str = "",
    sort: str = "title",
    start: int = 0,
    count: int = 48,
    ctx: Context = Depends(get_context),
) -> dict[str, Any]:
    where, params = ["1 = 1"], []
    for word in q.split():
        where.append("(title LIKE ? OR summary LIKE ? OR name LIKE ? OR tags LIKE ?)")
        params += [f"%{word}%"] * 4
    if language:
        where.append("(',' || language || ',') LIKE ?")
        params.append(f"%,{language},%")
    if category:
        where.append("category = ?")
        params.append(category)
    clause = " AND ".join(where)
    total = ctx.db.scalar(f"SELECT COUNT(*) FROM catalog WHERE {clause}", params)
    rows = ctx.db.all(
        f"SELECT * FROM catalog WHERE {clause} ORDER BY {SORTS.get(sort, SORTS['title'])} "
        "LIMIT ? OFFSET ?",
        [*params, max(1, min(count, 200)), max(0, start)],
    )
    local = {b["id"]: b for b in ctx.db.all("SELECT id, uuid, date, name, flavour FROM books")}
    by_name = {(b["name"], b["flavour"]): b for b in local.values()}
    queued = {
        d["catalog_uuid"]: d["state"]
        for d in ctx.db.all(
            "SELECT catalog_uuid, state FROM downloads "
            "WHERE state IN ('queued', 'downloading', 'verifying', 'paused', 'failed')"
        )
    }
    for row in rows:
        book = local.get(row["stem"]) or by_name.get((row["name"], row["flavour"]))
        if book is None:
            row["local"] = None
        elif book["uuid"] == row["uuid"] or book["date"][:10] >= row["issued"][:10]:
            row["local"] = "current"
        else:
            row["local"] = "outdated"
        row["book_id"] = book["id"] if book else None
        row["download_state"] = queued.get(row["uuid"])
    return {"total": total, "results": rows}


@router.get("/catalog/facets")
def facets(ctx: Context = Depends(get_context)) -> dict[str, Any]:
    languages: dict[str, int] = {}
    for row in ctx.db.all("SELECT language, COUNT(*) AS n FROM catalog GROUP BY language"):
        for code in row["language"].split(","):
            if code:
                languages[code] = languages.get(code, 0) + row["n"]
    categories = ctx.db.all(
        "SELECT category AS value, COUNT(*) AS count FROM catalog "
        "WHERE category != '' GROUP BY category ORDER BY category"
    )
    return {
        "languages": [
            {"value": code, "count": n}
            for code, n in sorted(languages.items(), key=lambda item: -item[1])
        ],
        "categories": categories,
    }


@router.post("/catalog/refresh", dependencies=[Depends(require_admin)])
async def refresh(ctx: Context = Depends(get_context)) -> dict[str, Any]:
    try:
        count = await opds.refresh(ctx.db, ctx.config.catalog_url)
    except Exception as exc:
        ctx.db.set_meta("catalog_error", str(exc) or type(exc).__name__)
        raise HTTPException(502, f"Could not fetch the Kiwix catalog: {exc}") from exc
    ctx.db.set_meta("catalog_error", None)
    ctx.events.publish("catalog", {"entries": count})
    return {"entries": count}


def _public(item: dict[str, Any]) -> dict[str, Any]:
    return {**item, "book": {k: item["book"][k] for k in _PUBLIC_BOOK}}


@router.get("/updates")
def updates(ctx: Context = Depends(get_context)) -> dict[str, Any]:
    items = [_public(item) for item in book_updates(ctx.db)]
    pending = [i for i in items if i["status"] == "update"]
    return {
        "items": items,
        "update_count": len(pending),
        "update_bytes": sum(i["latest"]["size"] for i in pending),
        "disk_free": shutil.disk_usage(ctx.config.data_dir).free,
        "catalog_refreshed": ctx.db.get_meta("catalog_refreshed"),
    }


@router.post("/updates/{book_id}/start", dependencies=[Depends(require_admin)])
async def start_update(book_id: str, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    for item in book_updates(ctx.db):
        if item["book"]["id"] == book_id:
            if item["status"] != "update":
                raise HTTPException(409, "No update is available for this book")
            return ctx.downloads.enqueue(item["latest"]["uuid"])
    raise HTTPException(404, "No such book")


@router.post("/updates/start-all", dependencies=[Depends(require_admin)])
async def start_all(ctx: Context = Depends(get_context)) -> dict[str, Any]:
    """Queue every available update, except for pinned books."""
    queued = 0
    for item in book_updates(ctx.db):
        if item["status"] == "update" and item["book"]["policy"] != "pinned":
            ctx.downloads.enqueue(item["latest"]["uuid"])
            queued += 1
    return {"queued": queued}


@router.get("/history")
def history(limit: int = 200, ctx: Context = Depends(get_context)) -> list[dict[str, Any]]:
    return ctx.db.all("SELECT * FROM history ORDER BY id DESC LIMIT ?", (max(1, min(limit, 1000)),))
