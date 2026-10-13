"""Making ZIMs: the directory of links, uploaded books, and the builds made from them."""

import csv
import io
import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..builder import extract, zimit
from ..builder.writer import BuildError
from ..context import Context, get_context, require_admin

router = APIRouter(prefix="/api/build", dependencies=[Depends(require_admin)])

ENGINES = ("builtin", "zimit")
SCOPES = ("page", "path", "site")
MODES = ("clean", "full")
RATE_UNITS = ("second", "minute")
# What an uploaded file is, by its extension and by how it starts.
UPLOAD_KINDS = {
    ".zim": "zim",
    ".epub": "epub",
    ".pdf": "pdf",
    ".mobi": "mobi",
    ".azw3": "mobi",
    ".azw": "mobi",
}
_SIGNATURES = {"zim": b"ZIM\x04", "epub": b"PK", "pdf": b"%PDF"}
_YES = {"1", "yes", "y", "true", "on", "include"}
_NO = {"0", "no", "n", "false", "off", "exclude", ""}
CSV_FIELDS = (
    "url", "title", "engine", "scope", "subdomains", "mode", "images", "video", "max_pages",
    "sitemaps", "robots_url", "rate_limit", "rate_unit", "feeds", "feed_minutes", "language",
    "notes",
)  # fmt: skip


# -- links ----------------------------------------------------------------------------


class LinkBody(BaseModel):
    url: str = Field(min_length=4, max_length=2000)
    title: str = Field(default="", max_length=200)
    engine: str = "builtin"
    scope: str = "page"
    max_pages: int = Field(default=500, ge=1, le=50000)
    max_depth: int = Field(default=5, ge=0, le=20)
    mode: str = "clean"
    images: bool = True
    video: bool = False
    subdomains: bool = False
    # Sitemaps to read, one address per line or separated by spaces; empty: look for them.
    sitemaps: str = Field(default="", max_length=8000)
    robots_url: str = Field(default="", max_length=2000)
    rate_limit: float = Field(default=0, ge=0, le=1000)
    rate_unit: str = "second"
    # The site's RSS or Atom feeds, one per line, and how often to look at them for new
    # articles (minutes; 0: only when asked).
    feeds: str = Field(default="", max_length=8000)
    feed_minutes: int = Field(default=0, ge=0, le=525_600)
    language: str = Field(default="", max_length=10)
    notes: str = Field(default="", max_length=1000)
    rebuild_days: int = Field(default=0, ge=0, le=365)


def clean_addresses(text: str, what: str) -> list[str]:
    """Addresses typed one per line (or with spaces or commas between), each once."""
    found: list[str] = []
    for word in re.split(r"[\s,|]+", text.strip()):
        if not word:
            continue
        if not re.match(r"^https?://", word, re.I):
            word = "https://" + word
        url = extract.normalise(word) if "." in word or "localhost" in word else ""
        if not url:
            raise HTTPException(422, f"{word!r} is not a {what} address")
        if url not in found:
            found.append(url)
    return found


def _clean_link(body: LinkBody) -> dict[str, Any]:
    address = body.url.strip()
    if not re.match(r"^https?://", address, re.I):
        address = "https://" + address
    url = extract.normalise(address)
    if not url:
        raise HTTPException(422, "Enter a web address, such as https://example.org/guide")
    for value, allowed, label in (
        (body.engine, ENGINES, "engine"),
        (body.scope, SCOPES, "scope"),
        (body.mode, MODES, "mode"),
        (body.rate_unit, RATE_UNITS, "rate_unit"),
    ):
        if value not in allowed:
            raise HTTPException(422, f"{label} must be one of {', '.join(allowed)}")
    data = body.model_dump()
    data["sitemaps"] = "\n".join(clean_addresses(body.sitemaps, "sitemap"))
    data["robots_url"] = "".join(clean_addresses(body.robots_url, "robots.txt")[:1])
    data["feeds"] = "\n".join(clean_addresses(body.feeds, "feed"))
    if 0 < body.feed_minutes < 5:
        raise HTTPException(422, "Feeds cannot be checked more often than every 5 minutes.")
    data.update(
        url=url,
        title=body.title.strip(),
        images=int(body.images),
        video=int(body.video),
        subdomains=int(body.subdomains and body.scope == "site"),
    )
    return data


def _links(ctx: Context) -> list[dict[str, Any]]:
    """Every link with the book built from it and the state of its latest build."""
    rows = ctx.db.all(
        "SELECT l.*, b.date AS book_date, b.size AS book_size, b.article_count AS book_pages, "
        "b.title AS book_title FROM links l LEFT JOIN books b ON b.id = l.book_id "
        "AND l.book_id != '' ORDER BY l.created DESC, l.id DESC"
    )
    latest = {
        r["source_id"]: r
        for r in ctx.db.all(
            "SELECT source_id, id, state, error, failed, skipped FROM builds "
            "WHERE kind = 'link' ORDER BY id"
        )
    }
    for row in rows:
        row["images"], row["video"] = bool(row["images"]), bool(row["video"])
        row["subdomains"] = bool(row["subdomains"])
        row["build"] = latest.get(row["id"])
    return rows


@router.get("/links")
def list_links(ctx: Context = Depends(get_context)) -> dict[str, Any]:
    return {"links": _links(ctx), "zimit_unavailable": zimit.unavailable()}


@router.post("/links")
def add_link(body: LinkBody, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    data = _clean_link(body)
    if ctx.db.one("SELECT id FROM links WHERE url = ?", (data["url"],)):
        raise HTTPException(409, "That address is already in your links.")
    columns = ", ".join(data)
    marks = ", ".join(f":{key}" for key in data)
    ctx.db.execute(f"INSERT INTO links ({columns}) VALUES ({marks})", data)
    return {"links": _links(ctx)}


@router.put("/links/{link_id}")
def edit_link(link_id: int, body: LinkBody, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    data = _clean_link(body)
    clash = ctx.db.one("SELECT id FROM links WHERE url = ? AND id != ?", (data["url"], link_id))
    if clash:
        raise HTTPException(409, "That address is already in your links.")
    assignments = ", ".join(f"{key} = :{key}" for key in data)
    ctx.db.execute(f"UPDATE links SET {assignments} WHERE id = :id", {**data, "id": link_id})
    return {"links": _links(ctx)}


@router.delete("/links/{link_id}")
def delete_link(link_id: int, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    """Remove a link from the directory; the ZIM built from it stays in the library."""
    ctx.db.execute("DELETE FROM links WHERE id = ?", (link_id,))
    return {"links": _links(ctx)}


def _queue(ctx: Context, kind: str, source_id: int, title: str) -> dict[str, Any]:
    try:
        return ctx.builds.enqueue(kind, source_id, title)
    except BuildError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/links/{link_id}/build")
def build_link(
    link_id: int, rss: bool = False, ctx: Context = Depends(get_context)
) -> dict[str, Any]:
    """Build the ZIM again. ``rss``: only add what the link's feeds list that is new."""
    link = ctx.db.one("SELECT * FROM links WHERE id = ?", (link_id,))
    if link is None:
        raise HTTPException(404, "No such link")
    if rss:
        if not link["feeds"] or link["engine"] != "builtin":
            raise HTTPException(409, "This link has no RSS feeds to update from.")
        title = link["title"] or link["url"]
        try:
            return ctx.builds.enqueue("link", link_id, title, {"rss": True})
        except BuildError as exc:
            raise HTTPException(409, str(exc)) from exc
    if link["engine"] == "zimit" and zimit.unavailable():
        raise HTTPException(409, f"Browser capture is not available: {zimit.unavailable()}")
    return _queue(ctx, "link", link_id, link["title"] or link["url"])


# -- CSV ------------------------------------------------------------------------------


def _flag(value: str, default: bool) -> bool | None:
    text = (value or "").strip().lower()
    if not text:
        return default
    return True if text in _YES else False if text in _NO else None


def _parse_csv(ctx: Context, text: str) -> list[dict[str, Any]]:
    """Read a CSV of links: each row is new, a duplicate, or invalid with the reason."""
    sample = text.lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(sample[:2000], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(sample), dialect) if any(c.strip() for c in r)]
    if not rows:
        return []
    header = [c.strip().lower().replace(" ", "_") for c in rows[0]]
    if "url" not in header and "link" not in header:
        # No header row: the first column is the address, the second a title.
        header, body = ["url", "title"], rows
    else:
        header = ["url" if c == "link" else c for c in header]
        body = rows[1:]
    known = {r["url"] for r in ctx.db.all("SELECT url FROM links")}
    seen: set[str] = set()
    result = []
    for number, cells in enumerate(body, 1):
        raw = dict(zip(header, (c.strip() for c in cells), strict=False))
        entry: dict[str, Any] = {
            "row": number,
            "url": raw.get("url", ""),
            "status": "new",
            "why": "",
        }
        problems = []
        address = raw.get("url", "")
        if address and not re.match(r"^https?://", address, re.I):
            address = "https://" + address
        url = extract.normalise(address) if "." in address else ""
        if not url:
            problems.append("not a web address")
        entry["url"] = url or raw.get("url", "")
        entry["title"] = raw.get("title", "")[:200]
        for key, allowed, default in (
            ("engine", ENGINES, "builtin"),
            ("scope", SCOPES, "page"),
            ("mode", MODES, "clean"),
        ):
            value = (raw.get(key) or default).lower()
            if value not in allowed:
                problems.append(f"{key} must be {' or '.join(allowed)}")
            entry[key] = value
        for key, default in (("images", True), ("video", False), ("subdomains", False)):
            flag = _flag(raw.get(key, ""), default)
            if flag is None:
                problems.append(f"{key} must be yes or no")
            entry[key] = bool(flag)
        try:
            entry["max_pages"] = max(1, min(50000, int(raw.get("max_pages") or 500)))
        except ValueError:
            problems.append("max_pages must be a number")
        try:
            entry["sitemaps"] = "\n".join(clean_addresses(raw.get("sitemaps", ""), "sitemap"))
            entry["robots_url"] = "".join(
                clean_addresses(raw.get("robots_url", ""), "robots.txt")[:1]
            )
        except HTTPException as exc:
            problems.append(str(exc.detail))
        try:
            entry["rate_limit"] = max(0.0, min(1000.0, float(raw.get("rate_limit") or 0)))
        except ValueError:
            problems.append("rate_limit must be a number")
        try:
            entry["feeds"] = "\n".join(clean_addresses(raw.get("feeds", ""), "feed"))
            minutes = int(raw.get("feed_minutes") or 0)
            entry["feed_minutes"] = 0 if minutes <= 0 else max(5, min(525_600, minutes))
        except HTTPException as exc:
            problems.append(str(exc.detail))
        except ValueError:
            problems.append("feed_minutes must be a number")
        entry["rate_unit"] = (raw.get("rate_unit") or "second").lower()
        if entry["rate_unit"] not in RATE_UNITS:
            problems.append("rate_unit must be second or minute")
        entry["language"] = raw.get("language", "")[:10]
        entry["notes"] = raw.get("notes", "")[:1000]
        if problems:
            entry.update(status="invalid", why="; ".join(problems))
        elif url in known:
            entry.update(status="duplicate", why="already in your links")
        elif url in seen:
            entry.update(status="duplicate", why="repeated in this file")
        seen.add(url)
        result.append(entry)
    return result


class CsvBody(BaseModel):
    text: str = Field(max_length=5_000_000)
    # False: only report what the file holds. True: add the new rows.
    commit: bool = False


@router.post("/links/import")
def import_links(body: CsvBody, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    rows = _parse_csv(ctx, body.text)
    counts = {
        status: sum(r["status"] == status for r in rows)
        for status in ("new", "duplicate", "invalid")
    }
    added = 0
    if body.commit:
        fields = CSV_FIELDS
        for row in rows:
            if row["status"] != "new":
                continue
            values = {f: (int(row[f]) if isinstance(row[f], bool) else row[f]) for f in fields}
            ctx.db.execute(
                f"INSERT OR IGNORE INTO links ({', '.join(fields)}) "
                f"VALUES ({', '.join(':' + f for f in fields)})",
                values,
            )
            added += 1
    return {"rows": rows[:500], "counts": counts, "added": added, "links": _links(ctx)}


@router.get("/links/export")
def export_links(ctx: Context = Depends(get_context)) -> Response:
    fields = CSV_FIELDS
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(fields)
    for link in ctx.db.all("SELECT * FROM links ORDER BY created, id"):
        writer.writerow(
            [
                ("yes" if link[f] else "no")
                if f in ("images", "video", "subdomains")
                else " ".join(link[f].split())
                if f in ("sitemaps", "feeds")
                else link[f]
                for f in fields
            ]
        )
    headers = {"Content-Disposition": 'attachment; filename="zim-pro-links.csv"'}
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8", headers=headers)


# -- uploads --------------------------------------------------------------------------


def _uploads(ctx: Context) -> list[dict[str, Any]]:
    rows = ctx.db.all(
        "SELECT u.id, u.filename, u.kind, u.size, u.title, u.images, u.book_id, u.last_built, "
        "u.created, b.title AS book_title, b.size AS book_size FROM uploads u "
        "LEFT JOIN books b ON b.id = u.book_id AND u.book_id != '' ORDER BY u.id DESC"
    )
    latest = {
        r["source_id"]: r
        for r in ctx.db.all(
            "SELECT source_id, id, state, error, failed, skipped FROM builds "
            "WHERE kind = 'upload' ORDER BY id"
        )
    }
    for row in rows:
        row["images"] = bool(row["images"])
        row["build"] = latest.get(row["id"])
    return rows


@router.get("/uploads")
def list_uploads(ctx: Context = Depends(get_context)) -> dict[str, Any]:
    return {"uploads": _uploads(ctx), "limit_mb": ctx.db.get_setting("upload_limit_mb")}


@router.post("/uploads")
async def upload(file: UploadFile, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    """Take a ZIM, EPUB, PDF, MOBI or AZW3 file and queue its conversion."""
    name = Path(file.filename or "").name
    kind = UPLOAD_KINDS.get(Path(name).suffix.lower())
    if kind is None:
        raise HTTPException(415, "Upload a .zim, .epub, .pdf, .mobi or .azw3 file.")
    limit = int(ctx.db.get_setting("upload_limit_mb")) * 1024 * 1024
    ctx.builds.upload_dir.mkdir(parents=True, exist_ok=True)
    stored = ctx.builds.upload_dir / f"{uuid.uuid4().hex}{Path(name).suffix.lower()}"
    size = 0
    try:
        with stored.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                if size == 0 and kind in _SIGNATURES and not chunk.startswith(_SIGNATURES[kind]):
                    raise HTTPException(415, f"{name} is not really a {kind.upper()} file.")
                size += len(chunk)
                if size > limit:
                    raise HTTPException(
                        413, f"{name} is larger than the {limit // 1024**2} MB upload limit."
                    )
                out.write(chunk)
        if size == 0:
            raise HTTPException(422, f"{name} is empty.")
    except BaseException:
        stored.unlink(missing_ok=True)
        raise
    cursor = ctx.db.execute(
        "INSERT INTO uploads (filename, kind, size, path) VALUES (?, ?, ?, ?)",
        (name, kind, size, str(stored)),
    )
    _queue(ctx, "upload", cursor.lastrowid, name)
    return {"uploads": _uploads(ctx)}


class UploadOptions(BaseModel):
    title: str = Field(default="", max_length=200)
    images: bool = True


@router.post("/uploads/{upload_id}/build")
def build_upload(
    upload_id: int, body: UploadOptions, ctx: Context = Depends(get_context)
) -> dict[str, Any]:
    """Convert an uploaded file again, with a different title or without its pictures."""
    row = ctx.db.one("SELECT * FROM uploads WHERE id = ?", (upload_id,))
    if row is None:
        raise HTTPException(404, "No such upload")
    ctx.db.execute(
        "UPDATE uploads SET title = ?, images = ? WHERE id = ?",
        (body.title.strip(), int(body.images), upload_id),
    )
    return _queue(ctx, "upload", upload_id, body.title.strip() or row["filename"])


@router.delete("/uploads/{upload_id}")
def delete_upload(upload_id: int, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    """Forget an uploaded file; the ZIM made from it stays in the library."""
    row = ctx.db.one("SELECT path FROM uploads WHERE id = ?", (upload_id,))
    if row:
        Path(row["path"]).unlink(missing_ok=True)
        ctx.db.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))
    return {"uploads": _uploads(ctx)}


# -- builds ---------------------------------------------------------------------------


@router.get("/jobs")
def list_jobs(ctx: Context = Depends(get_context)) -> list[dict[str, Any]]:
    return ctx.builds.list()


@router.post("/jobs/clear-finished")
def clear_jobs(ctx: Context = Depends(get_context)) -> list[dict[str, Any]]:
    ctx.builds.clear_finished()
    return ctx.builds.list()


@router.get("/jobs/{build_id}")
def get_job(build_id: int, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    job = ctx.builds.get(build_id)
    if job is None:
        raise HTTPException(404, "No such build")
    return job


@router.post("/jobs/{build_id}/{action}")
def act(build_id: int, action: str, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    if ctx.builds.get(build_id) is None:
        raise HTTPException(404, "No such build")
    match action:
        case "cancel":
            ctx.builds.cancel(build_id)
        case "retry":
            ctx.builds.retry(build_id)
        case "pause":
            try:
                ctx.builds.pause(build_id)
            except BuildError as exc:
                raise HTTPException(409, str(exc)) from exc
        case "resume":
            ctx.builds.resume(build_id)
        case _:
            raise HTTPException(404, "Unknown action")
    return ctx.builds.get(build_id) or {}


@router.delete("/jobs/{build_id}")
def delete_job(build_id: int, ctx: Context = Depends(get_context)) -> dict[str, bool]:
    ctx.builds.remove(build_id)
    return {"ok": True}
