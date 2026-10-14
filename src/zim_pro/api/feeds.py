"""Feeds: collections of RSS and Atom addresses, each kept as one ZIM that stays current."""

import asyncio
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..builder import icons
from ..builder.crawler import _decode
from ..builder.writer import BuildError
from ..context import Context, get_context, require_admin
from ..downloads.engine import USER_AGENT
from ..feeds import parse
from .build import MODES, RATE_UNITS, _refile, clean_addresses
from .organise import use_category

router = APIRouter(prefix="/api/feeds", dependencies=[Depends(require_admin)])

ITEM_STATES = ("saved", "new", "failed", "skipped", "dropped")
LOOK_TIMEOUT = 30  # seconds one address may take to answer in full, when checking it


class FeedBody(BaseModel):
    title: str = Field(default="", max_length=200)
    # Feed addresses, one per line.
    sources: str = Field(min_length=4, max_length=40000)
    interval_minutes: int = Field(default=60, ge=0, le=525_600)
    keep: int = Field(default=0, ge=0, le=1_000_000)
    full_text: bool = True
    mode: str = "clean"
    images: bool = True
    video: bool = False
    enabled: bool = True
    rate_limit: float = Field(default=0, ge=0, le=1000)
    rate_unit: str = "second"
    language: str = Field(default="", max_length=10)
    notes: str = Field(default="", max_length=1000)
    category: str = Field(default="", max_length=80)


def _clean(body: FeedBody) -> tuple[dict[str, Any], list[str]]:
    if body.mode not in MODES:
        raise HTTPException(422, f"mode must be one of {', '.join(MODES)}")
    if body.rate_unit not in RATE_UNITS:
        raise HTTPException(422, f"rate_unit must be one of {', '.join(RATE_UNITS)}")
    if 0 < body.interval_minutes < 5:
        raise HTTPException(422, "A feed cannot be checked more often than every 5 minutes.")
    sources = clean_addresses(body.sources, "feed")
    if not sources:
        raise HTTPException(422, "Add at least one feed address.")
    data = body.model_dump(exclude={"sources"})
    data["title"] = body.title.strip()
    for key in ("full_text", "images", "video", "enabled"):
        data[key] = int(data[key])
    return data, sources


def _feeds(ctx: Context) -> list[dict[str, Any]]:
    rows = ctx.db.all(
        "SELECT f.*, b.size AS book_size, b.title AS book_title FROM feeds f "
        "LEFT JOIN books b ON b.id = f.book_id AND f.book_id != '' ORDER BY f.id DESC"
    )
    sources: dict[int, list[dict[str, Any]]] = {}
    for source in ctx.db.all("SELECT * FROM feed_sources ORDER BY id"):
        sources.setdefault(source["feed_id"], []).append(source)
    counts: dict[int, dict[str, int]] = {}
    for row in ctx.db.all(
        "SELECT feed_id, state, COUNT(*) AS n FROM feed_items GROUP BY feed_id, state"
    ):
        counts.setdefault(row["feed_id"], {})[row["state"]] = row["n"]
    for row in rows:
        for key in ("full_text", "images", "video", "enabled"):
            row[key] = bool(row[key])
        row["sources"] = sources.get(row["id"], [])
        row["counts"] = counts.get(row["id"], {})
        due = row["enabled"] and row["interval_minutes"] > 0
        row["next_sync"] = (
            (row["last_attempt"] or row["created"]) + row["interval_minutes"] * 60 if due else None
        )
    return rows


def _listing(ctx: Context) -> dict[str, Any]:
    return {"feeds": _feeds(ctx), "auto_sync": bool(ctx.db.get_setting("feed_auto_sync"))}


def _set_sources(ctx: Context, feed_id: int, sources: list[str]) -> None:
    """Make the collection's feeds these: new ones are added, others are removed."""
    known = {
        r["url"] for r in ctx.db.all("SELECT url FROM feed_sources WHERE feed_id = ?", (feed_id,))
    }
    for url in sources:
        if url not in known:
            ctx.db.execute("INSERT INTO feed_sources (feed_id, url) VALUES (?, ?)", (feed_id, url))
    for url in known - set(sources):
        ctx.db.execute("DELETE FROM feed_sources WHERE feed_id = ? AND url = ?", (feed_id, url))


@router.get("")
def list_feeds(ctx: Context = Depends(get_context)) -> dict[str, Any]:
    return _listing(ctx)


@router.post("")
def add_feed(
    body: FeedBody, hold: bool = False, ctx: Context = Depends(get_context)
) -> dict[str, Any]:
    """Add a collection and fill it. ``hold``: the caller starts the first sync itself,
    once it has also sent the collection's picture."""
    data, sources = _clean(body)
    data["category"] = use_category(ctx, data["category"])
    cursor = ctx.db.execute(
        f"INSERT INTO feeds ({', '.join(data)}) VALUES ({', '.join(':' + k for k in data)})", data
    )
    _set_sources(ctx, cursor.lastrowid, sources)
    if data["enabled"] and not hold:
        _sync(ctx, cursor.lastrowid)  # a new collection is filled straight away
    return {**_listing(ctx), "id": cursor.lastrowid}


@router.put("/{feed_id}")
def edit_feed(feed_id: int, body: FeedBody, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    data, sources = _clean(body)
    if ctx.db.one("SELECT id FROM feeds WHERE id = ?", (feed_id,)) is None:
        raise HTTPException(404, "No such feed collection")
    data["category"] = use_category(ctx, data["category"])
    assignments = ", ".join(f"{key} = :{key}" for key in data)
    ctx.db.execute(f"UPDATE feeds SET {assignments} WHERE id = :id", {**data, "id": feed_id})
    _set_sources(ctx, feed_id, sources)
    _refile(ctx)
    return {**_listing(ctx), "id": feed_id}


@router.delete("/{feed_id}")
def delete_feed(feed_id: int, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    """Remove a collection and what was kept for it; its ZIM stays in the library."""
    busy = ctx.db.one(
        "SELECT id FROM builds WHERE kind = 'feed' AND source_id = ? "
        "AND state IN ('queued', 'processing', 'paused')",
        (feed_id,),
    )
    if busy:
        raise HTTPException(409, "This collection is being synced. Cancel the sync first.")
    for table in ("feed_items", "feed_sources"):
        ctx.db.execute(f"DELETE FROM {table} WHERE feed_id = ?", (feed_id,))
    ctx.db.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
    ctx.feeds.forget(feed_id)
    icons.path(ctx.config.state_dir, "feed", feed_id).unlink(missing_ok=True)
    _refile(ctx)
    return _listing(ctx)


def _sync(ctx: Context, feed_id: int, rebuild: bool = False) -> dict[str, Any]:
    try:
        return ctx.feeds.sync(feed_id, rebuild)
    except BuildError as exc:
        raise HTTPException(409, str(exc)) from exc


class SyncBody(BaseModel):
    # Also ask again for every article that failed, and write the ZIM even if nothing is new.
    rebuild: bool = False


@router.post("/{feed_id}/sync")
def sync_feed(
    feed_id: int, body: SyncBody | None = None, ctx: Context = Depends(get_context)
) -> dict[str, Any]:
    return _sync(ctx, feed_id, bool(body and body.rebuild))


@router.post("/sync-all")
def sync_all(ctx: Context = Depends(get_context)) -> dict[str, int]:
    started = 0
    for feed in ctx.db.all("SELECT id FROM feeds WHERE enabled = 1"):
        try:
            ctx.feeds.sync(feed["id"])
            started += 1
        except BuildError:
            pass  # already at it
    return {"started": started}


@router.get("/{feed_id}/items")
def list_items(
    feed_id: int,
    state: str = "",
    q: str = "",
    start: int = 0,
    count: int = 50,
    ctx: Context = Depends(get_context),
) -> dict[str, Any]:
    """The articles of a collection, newest first."""
    if state and state not in ITEM_STATES:
        raise HTTPException(422, f"state must be one of {', '.join(ITEM_STATES)}")
    where = "i.feed_id = ? AND (? = '' OR i.state = ?) AND (? = '' OR i.title LIKE ?)"
    args = (feed_id, state, state, q.strip(), f"%{q.strip()}%")
    rows = ctx.db.all(
        "SELECT i.id, i.url, i.title, i.author, i.published, i.state, i.error, i.path, "
        "s.title AS source FROM feed_items i LEFT JOIN feed_sources s ON s.id = i.source_id "
        f"WHERE {where} ORDER BY i.published DESC, i.id DESC LIMIT ? OFFSET ?",
        (*args, max(1, min(count, 200)), max(0, start)),
    )
    total = ctx.db.scalar(f"SELECT COUNT(*) FROM feed_items i WHERE {where}", args)
    return {"items": rows, "total": total}


@router.post("/{feed_id}/retry-failed")
def retry_failed(feed_id: int, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    ctx.db.execute(
        "UPDATE feed_items SET state = 'new', tries = 0, error = NULL "
        "WHERE feed_id = ? AND state = 'failed'",
        (feed_id,),
    )
    return _sync(ctx, feed_id)


# -- finding and moving feeds -----------------------------------------------------------


class CheckBody(BaseModel):
    url: str = Field(min_length=4, max_length=2000)


async def _look(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    try:
        async with asyncio.timeout(LOOK_TIMEOUT):
            reply = await client.get(url)
    except TimeoutError:
        raise HTTPException(422, f"{url} did not answer within {LOOK_TIMEOUT} seconds") from None
    if reply.status_code >= 400:
        raise HTTPException(422, f"{url} answered HTTP {reply.status_code}")
    kind = reply.headers.get("content-type", "")
    try:
        feed = parse.parse(reply.content, str(reply.url))
    except parse.NotAFeed:
        feed = None
    if feed is not None and (feed.entries or "html" not in kind):
        return {
            "url": url,
            "title": feed.title,
            "articles": len(feed.entries),
            "latest": [entry.title for entry in feed.entries[:3]],
            "full_text": any(len(entry.html) > 1500 for entry in feed.entries),
        }
    return {"url": url, "page": _decode(reply.content), "final": str(reply.url)}


@router.post("/check")
async def check(body: CheckBody, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    """Look at an address: is it a feed, or a page that says where its feeds are?"""
    url = "".join(clean_addresses(body.url, "feed")[:1])
    proxy = ctx.network.proxy or None
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=20, proxy=proxy
        ) as client:
            first = await _look(client, url)
            if "page" not in first:
                return {"feeds": [first]}
            found = []
            for address in parse.discover(first["page"], first["final"])[:6]:
                try:
                    seen = await _look(client, address)
                except HTTPException:
                    continue
                if "page" not in seen:
                    found.append(seen)
    except httpx.HTTPError as exc:
        raise HTTPException(
            422, f"{url} could not be reached: {exc or type(exc).__name__}"
        ) from exc
    if not found:
        raise HTTPException(422, "No feed was found at this address, or on the page it shows.")
    return {"feeds": found}


class OpmlBody(BaseModel):
    text: str = Field(max_length=5_000_000)
    # Only say what the file holds; add nothing.
    preview: bool = False
    # Which of the file's folders to bring in, by their place in the preview; all if None.
    folders: list[int] | None = None
    # Put every feed chosen into one collection of this name, in place of one per folder.
    single: str = Field(default="", max_length=200)
    category: str = Field(default="", max_length=80)
    # Start filling the collections straight away.
    sync: bool = False


def _opml_folders(text: str) -> list[tuple[str, list[tuple[str, str]]]]:
    try:
        folders = parse.read_opml(text)
    except parse.NotAFeed as exc:
        raise HTTPException(422, str(exc)) from exc
    if not folders:
        raise HTTPException(422, "This OPML file lists no feeds.")
    return [(name or "Imported feeds", feeds) for name, feeds in folders]


@router.post("/opml")
def import_opml(body: OpmlBody, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    """Add the feeds of an OPML file: one collection per folder, or all in one."""
    folders = _opml_folders(body.text)
    if body.preview:
        known = {r["url"] for r in ctx.db.all("SELECT url FROM feed_sources")}
        titles = {r["title"].lower() for r in ctx.db.all("SELECT title FROM feeds")}
        return {
            "folders": [
                {
                    "name": name,
                    "exists": name.lower() in titles,
                    "feeds": [
                        {"title": title, "url": url, "known": url in known} for title, url in feeds
                    ],
                }
                for name, feeds in folders
            ]
        }
    chosen = [
        folder
        for index, folder in enumerate(folders)
        if body.folders is None or index in body.folders
    ]
    if not chosen:
        raise HTTPException(422, "Choose at least one folder to import.")
    if body.single.strip():
        chosen = [(body.single.strip(), [feed for _, feeds in chosen for feed in feeds])]
    category = use_category(ctx, body.category)
    interval = int(ctx.db.get_setting("feed_default_interval"))
    keep = int(ctx.db.get_setting("feed_default_keep"))
    added, touched = 0, []
    for title, feeds in chosen:
        row = ctx.db.one("SELECT id FROM feeds WHERE title = ? COLLATE NOCASE", (title,))
        feed_id = (
            row["id"]
            if row
            else ctx.db.execute(
                "INSERT INTO feeds (title, interval_minutes, keep, category) VALUES (?, ?, ?, ?)",
                (title, interval, keep, category),
            ).lastrowid
        )
        if row and category:
            ctx.db.execute("UPDATE feeds SET category = ? WHERE id = ?", (category, feed_id))
        for source_title, address in feeds:
            cursor = ctx.db.execute(
                "INSERT OR IGNORE INTO feed_sources (feed_id, url, title) VALUES (?, ?, ?)",
                (feed_id, address, source_title[:200]),
            )
            added += cursor.rowcount
        touched.append(feed_id)
    _refile(ctx)
    started = 0
    if body.sync:
        for feed_id in touched:
            enabled = ctx.db.one("SELECT enabled FROM feeds WHERE id = ?", (feed_id,))
            if enabled and enabled["enabled"]:
                try:
                    ctx.feeds.sync(feed_id, False)
                    started += 1
                except BuildError:
                    pass  # already syncing
    return {**_listing(ctx), "added": added, "collections": len(chosen), "syncing": started}


@router.get("/opml")
def export_opml(ctx: Context = Depends(get_context)) -> Response:
    folders = [
        (
            feed["title"] or "Feeds",
            [(s["title"] or s["url"], s["url"], s["site"]) for s in feed["sources"]],
        )
        for feed in _feeds(ctx)
    ]
    headers = {"Content-Disposition": 'attachment; filename="zim-pro-feeds.opml"'}
    return Response(parse.write_opml(folders), media_type="text/x-opml", headers=headers)


# -- syncs --------------------------------------------------------------------------------


@router.get("/runs")
def list_runs(ctx: Context = Depends(get_context)) -> list[dict[str, Any]]:
    return ctx.feeds.list()


@router.post("/runs/clear-finished")
def clear_runs(ctx: Context = Depends(get_context)) -> list[dict[str, Any]]:
    ctx.feeds.clear_finished()
    return ctx.feeds.list()


def _run(ctx: Context, run_id: int) -> dict[str, Any]:
    run = ctx.feeds.get(run_id)
    if run is None or run["kind"] != "feed":
        raise HTTPException(404, "No such sync")
    return run


@router.get("/runs/{run_id}")
def get_run(run_id: int, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    return _run(ctx, run_id)


@router.post("/runs/{run_id}/{action}")
def act(run_id: int, action: str, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    _run(ctx, run_id)
    try:
        match action:
            case "cancel":
                ctx.feeds.cancel(run_id)
            case "retry":
                ctx.feeds.retry(run_id)
            case "pause":
                ctx.feeds.pause(run_id)
            case "resume":
                ctx.feeds.resume(run_id)
            case _:
                raise HTTPException(404, "Unknown action")
    except BuildError as exc:
        raise HTTPException(409, str(exc)) from exc
    return ctx.feeds.get(run_id) or {}


@router.delete("/runs/{run_id}")
def delete_run(run_id: int, ctx: Context = Depends(get_context)) -> dict[str, bool]:
    _run(ctx, run_id)
    ctx.feeds.remove(run_id)
    return {"ok": True}
