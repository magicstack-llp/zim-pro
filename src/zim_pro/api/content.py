import email.utils
import hashlib
import logging
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from ..context import Context, get_context
from ..library.article import markdown_url, to_markdown
from ..library.reader import find_entry, main_path, random_path, read_article, resolve_redirects

log = logging.getLogger(__name__)
router = APIRouter()

_RANGE = re.compile(r"bytes\s*=\s*(\d*)-(\d*)$", re.IGNORECASE)
_CONTENT_CSP = (
    "sandbox allow-scripts allow-forms allow-popups; "
    "default-src * 'unsafe-inline' 'unsafe-eval' data: blob:; "
    "connect-src 'none';"
)


def _content_url(book_id: str, path: str) -> str:
    return f"/content/{quote(book_id)}/{quote(path, safe='/:@!$&()*+,;=~')}"


def _open(ctx: Context, book_id: str):
    try:
        return ctx.archive(book_id)
    except LookupError:
        raise HTTPException(404, f"No book named {book_id}") from None


@router.get("/content/{book_id}")
@router.get("/content/{book_id}/")
def book_home(book_id: str, ctx: Context = Depends(get_context)):
    book, archive = _open(ctx, book_id)
    try:
        path = main_path(archive)
    except (RuntimeError, OSError) as exc:
        ctx.pool.invalidate(book["path"])
        log.warning("main_path failed for %s: %s", book_id, exc)
        raise HTTPException(503, "Book unavailable, try rescanning") from None
    if path is None:
        raise HTTPException(404, "This book has no main page")
    return RedirectResponse(_content_url(book_id, path), 302)


@router.get("/random/{book_id}")
def random_article(book_id: str, ctx: Context = Depends(get_context)):
    book, archive = _open(ctx, book_id)
    try:
        path = random_path(archive)
    except (RuntimeError, OSError) as exc:
        ctx.pool.invalidate(book["path"])
        log.warning("random_path failed for %s: %s", book_id, exc)
        raise HTTPException(503, "Book unavailable, try rescanning") from None
    if path is None:
        raise HTTPException(404, "This book has no articles")
    return RedirectResponse(_content_url(book_id, path), 302)


@router.get("/markdown/{book_id}")
@router.get("/markdown/{book_id}/")
def markdown_home(book_id: str, ctx: Context = Depends(get_context)):
    book, archive = _open(ctx, book_id)
    try:
        path = main_path(archive)
    except (RuntimeError, OSError) as exc:
        ctx.pool.invalidate(book["path"])
        log.warning("main_path failed for %s: %s", book_id, exc)
        raise HTTPException(503, "Book unavailable, try rescanning") from None
    if path is None:
        raise HTTPException(404, "This book has no main page")
    return RedirectResponse(markdown_url(book_id, path), 302)


@router.get("/markdown/{book_id}/{path:path}")
def markdown(book_id: str, path: str, request: Request, ctx: Context = Depends(get_context)):
    """One article as Markdown: its text without the page around it."""
    book, archive = _open(ctx, book_id)
    try:
        entry = find_entry(archive, path, request.url.query)
        article = read_article(archive, entry.path) if entry is not None else None
    except (RuntimeError, OSError) as exc:
        ctx.pool.invalidate(book["path"])
        log.warning("markdown read failed for %s/%s: %s", book_id, path, exc)
        raise HTTPException(503, "Book unavailable, try rescanning") from None
    if article is None:
        raise HTTPException(404, f"{path} was not found in {book['title']}")
    if not article.mimetype.startswith("text/"):
        raise HTTPException(415, f"{path} is {article.mimetype}, not a page of text")
    # Browsers show plain text in the tab but offer to download text/markdown.
    in_browser = "text/html" in request.headers.get("accept", "")
    cache_control = "private, max-age=3600" if ctx.config.admin_token else "public, max-age=3600"
    return Response(
        to_markdown(article, book_id),
        media_type="text/plain" if in_browser else "text/markdown",
        headers={"Cache-Control": cache_control, "X-Content-Type-Options": "nosniff"},
    )


@router.api_route("/content/{book_id}/{path:path}", methods=["GET", "HEAD"])
def content(book_id: str, path: str, request: Request, ctx: Context = Depends(get_context)):
    """Serve one entry of a ZIM archive, as kiwix-serve does."""
    book, archive = _open(ctx, book_id)
    try:
        entry = find_entry(archive, path, request.url.query)
    except (RuntimeError, OSError) as exc:
        ctx.pool.invalidate(book["path"])
        log.warning("entry lookup failed for %s/%s: %s", book_id, path, exc)
        raise HTTPException(503, "Book unavailable, try rescanning") from None
    if entry is None:
        raise HTTPException(404, f"{path} was not found in {book['title']}")
    if entry.is_redirect:
        resolved = resolve_redirects(entry)
        if resolved is None:
            raise HTTPException(404, f"{path} has a broken redirect in {book['title']}")
        return RedirectResponse(_content_url(book_id, resolved.path), 302)

    try:
        item = entry.get_item()
        size = item.size
        mimetype = item.mimetype or "application/octet-stream"
    except (RuntimeError, OSError, KeyError) as exc:
        ctx.pool.invalidate(book["path"])
        log.warning("item read failed for %s/%s: %s", book_id, path, exc)
        raise HTTPException(503, "Book unavailable, try rescanning") from None

    etag = (
        '"'
        + hashlib.sha1(
            f"{book['uuid']}/{book.get('size', '')}/{book.get('mtime', '')}/{entry.path}".encode()
        ).hexdigest()[:20]
        + '"'
    )
    cache_control = "private, max-age=3600" if ctx.config.admin_token else "public, max-age=3600"
    headers = {
        "ETag": etag,
        "Cache-Control": cache_control,
        "Accept-Ranges": "bytes",
        "Content-Security-Policy": _CONTENT_CSP,
        "X-Content-Type-Options": "nosniff",
    }
    last_modified: str | None = None
    if book.get("mtime") is not None:
        last_modified = email.utils.formatdate(book["mtime"], usegmt=True)
        headers["Last-Modified"] = last_modified

    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    if mimetype.startswith("text/") and "charset" not in mimetype:
        mimetype += "; charset=utf-8"

    start, end, status = 0, size - 1, 200
    range_header = request.headers.get("range", "").strip()
    if_range = request.headers.get("if-range", "").strip()
    range_allowed = (
        not if_range
        or if_range == etag
        or (last_modified is not None and if_range == last_modified)
    )
    if range_header and range_allowed:
        if "," in range_header or not (match := _RANGE.match(range_header)):
            return Response(
                status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"}
            )
        first, last = match.groups()
        if (not first and not last) or size == 0:
            return Response(
                status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"}
            )
        if first:
            start = int(first)
            end = min(int(last), size - 1) if last else size - 1
        elif last:
            start = max(0, size - int(last))
        if start > end or start >= size:
            return Response(
                status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"}
            )
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    if request.method == "HEAD":
        headers["Content-Length"] = str(end - start + 1)
        return Response(status_code=status, headers=headers, media_type=mimetype)
    try:
        body = bytes(item.content[start : end + 1])
    except (RuntimeError, OSError, KeyError) as exc:
        ctx.pool.invalidate(book["path"])
        log.warning("content read failed for %s/%s: %s", book_id, path, exc)
        raise HTTPException(503, "Book unavailable, try rescanning") from None
    return Response(body, status, headers, media_type=mimetype)
