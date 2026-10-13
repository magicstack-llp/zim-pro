"""Filing the ZIMs made here: the list of categories, and a picture for each source."""

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..builder import icons
from ..builder.writer import BuildError
from ..context import Context, get_context, require_admin
from ..library.scanner import file_under_categories

router = APIRouter(prefix="/api")

SOURCE_TABLES = ("links", "uploads", "feeds")
# Names the library already uses for what Kiwix publishes: one of the user's own with the
# same name would be mixed up with them.
RESERVED = {
    "custom", "my zims", "other", "wikipedia", "wiktionary", "wikibooks", "wikiquote",
    "wikisource", "wikivoyage", "wikiversity", "gutenberg", "phet", "ted", "ifixit",
    "devdocs", "stack_exchange", "stack exchange",
}  # fmt: skip


def clean_category(name: str) -> str:
    """A category name as it is kept; raises 422 if it cannot be one."""
    name = " ".join(name.split())
    if not name:
        return ""
    if len(name) > 40:
        raise HTTPException(422, "A category name can be 40 characters at most.")
    if re.search(r"[;,_<>\"/\\]", name):
        raise HTTPException(422, 'A category name cannot contain ; , _ < > " / or \\.')
    if name.lower() in RESERVED:
        raise HTTPException(422, f"“{name}” is already a category of the library.")
    return name


def library_categories(ctx: Context) -> list[dict[str, Any]]:
    """The categories the library's books are in, other than the user's own."""
    own = {r["name"].lower() for r in ctx.db.all("SELECT name FROM categories")}
    rows = ctx.db.all(
        "SELECT category AS name, COUNT(*) AS books FROM books WHERE error IS NULL "
        "GROUP BY category ORDER BY category COLLATE NOCASE"
    )
    return [r for r in rows if r["name"].lower() not in own and r["name"] not in ("custom", "")]


def use_category(ctx: Context, name: str) -> str:
    """The category to store for a source: ``name`` as the list spells it, added if new.

    One of the library's own categories (``wikipedia``, say) is taken as it is: a ZIM made
    here can be filed beside what Kiwix publishes.
    """
    wanted = " ".join(name.split())
    if wanted.lower() in ("custom", "my zims"):
        return ""
    for existing in library_categories(ctx):
        if existing["name"].lower() == wanted.lower():
            return existing["name"]
    name = clean_category(name)
    if not name:
        return ""
    known = ctx.db.one("SELECT name FROM categories WHERE name = ?", (name,))
    if known:
        return known["name"]
    ctx.db.execute("INSERT INTO categories (name) VALUES (?)", (name,))
    return name


def _listing(ctx: Context) -> dict[str, Any]:
    used: dict[str, int] = {}
    for table in SOURCE_TABLES:
        for row in ctx.db.all(
            f"SELECT category, COUNT(*) AS n FROM {table} WHERE category != '' GROUP BY category"
        ):
            used[row["category"].lower()] = used.get(row["category"].lower(), 0) + row["n"]
    rows = ctx.db.all("SELECT name FROM categories ORDER BY name COLLATE NOCASE")
    return {
        "categories": [{"name": r["name"], "used": used.get(r["name"].lower(), 0)} for r in rows],
        "library": library_categories(ctx),
    }


class CategoryBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)


@router.get("/categories")
def list_categories(ctx: Context = Depends(get_context)) -> dict[str, Any]:
    return _listing(ctx)


@router.post("/categories", dependencies=[Depends(require_admin)])
def add_category(body: CategoryBody, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    name = clean_category(body.name)
    if not name:
        raise HTTPException(422, "Type a name for the category.")
    if ctx.db.one("SELECT name FROM categories WHERE name = ?", (name,)):
        raise HTTPException(409, f"There is already a category named “{name}”.")
    ctx.db.execute("INSERT INTO categories (name) VALUES (?)", (name,))
    return _listing(ctx)


def _refile(ctx: Context) -> None:
    file_under_categories(ctx.db)
    ctx.events.publish("library", {"changed": True})


@router.put("/categories/{name}", dependencies=[Depends(require_admin)])
def rename_category(
    name: str, body: CategoryBody, ctx: Context = Depends(get_context)
) -> dict[str, Any]:
    """Rename a category; everything filed under it moves with it."""
    old = ctx.db.one("SELECT name FROM categories WHERE name = ?", (name,))
    if old is None:
        raise HTTPException(404, "No such category")
    new = clean_category(body.name)
    if not new:
        raise HTTPException(422, "Type a name for the category.")
    clash = ctx.db.one("SELECT name FROM categories WHERE name = ?", (new,))
    if clash and clash["name"].lower() != old["name"].lower():
        raise HTTPException(409, f"There is already a category named “{new}”.")
    ctx.db.execute("UPDATE categories SET name = ? WHERE name = ?", (new, old["name"]))
    for table in SOURCE_TABLES:
        ctx.db.execute(
            f"UPDATE {table} SET category = ? WHERE category = ? COLLATE NOCASE",
            (new, old["name"]),
        )
    _refile(ctx)
    return _listing(ctx)


@router.delete("/categories/{name}", dependencies=[Depends(require_admin)])
def delete_category(name: str, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    """Remove a category; what was filed under it goes back to "My ZIMs"."""
    ctx.db.execute("DELETE FROM categories WHERE name = ?", (name,))
    for table in SOURCE_TABLES:
        ctx.db.execute(
            f"UPDATE {table} SET category = '' WHERE category = ? COLLATE NOCASE", (name,)
        )
    _refile(ctx)
    return _listing(ctx)


# -- pictures ---------------------------------------------------------------------------


def _source(ctx: Context, kind: str, source_id: int) -> str:
    table = icons.KINDS.get(kind)
    if table is None:
        raise HTTPException(404, "No such kind of source")
    if ctx.db.one(f"SELECT id FROM {table} WHERE id = ?", (source_id,)) is None:
        raise HTTPException(404, "No such source")
    return table


@router.get("/icons/{kind}/{source_id}")
def get_icon(kind: str, source_id: int, ctx: Context = Depends(get_context)) -> Response:
    """The picture chosen for a link, an upload or a feed collection."""
    if kind not in icons.KINDS:
        raise HTTPException(404, "No such kind of source")
    data = icons.load(ctx.config.state_dir, kind, source_id)
    if data is None:
        raise HTTPException(404, "No picture has been chosen")
    return Response(data, media_type="image/png", headers={"Cache-Control": "no-cache"})


@router.post("/icons/{kind}/{source_id}", dependencies=[Depends(require_admin)])
async def set_icon(
    kind: str, source_id: int, file: UploadFile, ctx: Context = Depends(get_context)
) -> dict[str, Any]:
    """Choose a picture; it goes into the ZIM the next time the ZIM is made."""
    table = _source(ctx, kind, source_id)
    data = await file.read(icons.MAX_BYTES + 1)
    if len(data) > icons.MAX_BYTES:
        raise HTTPException(413, "The picture is larger than 10 MB.")
    try:
        picture = icons.normalise(data)
    except BuildError as exc:
        raise HTTPException(415, str(exc)) from exc
    target = icons.path(ctx.config.state_dir, kind, source_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(picture)
    ctx.db.execute(f"UPDATE {table} SET icon = unixepoch('subsec') WHERE id = ?", (source_id,))
    return {"ok": True}


@router.delete("/icons/{kind}/{source_id}", dependencies=[Depends(require_admin)])
def remove_icon(kind: str, source_id: int, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    table = _source(ctx, kind, source_id)
    icons.path(ctx.config.state_dir, kind, source_id).unlink(missing_ok=True)
    ctx.db.execute(f"UPDATE {table} SET icon = NULL WHERE id = ?", (source_id,))
    return {"ok": True}
