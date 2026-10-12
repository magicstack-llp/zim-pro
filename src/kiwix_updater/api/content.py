import hashlib
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from ..context import Context, get_context
from ..library.reader import find_entry, main_path, random_path

router = APIRouter()

_RANGE = re.compile(r"bytes=(\d*)-(\d*)$")


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
    _, archive = _open(ctx, book_id)
    path = main_path(archive)
    if path is None:
        raise HTTPException(404, "This book has no main page")
    return RedirectResponse(_content_url(book_id, path), 302)


@router.get("/random/{book_id}")
def random_article(book_id: str, ctx: Context = Depends(get_context)):
    _, archive = _open(ctx, book_id)
    path = random_path(archive)
    if path is None:
        raise HTTPException(404, "This book has no articles")
    return RedirectResponse(_content_url(book_id, path), 302)


@router.api_route("/content/{book_id}/{path:path}", methods=["GET", "HEAD"])
def content(book_id: str, path: str, request: Request, ctx: Context = Depends(get_context)):
    """Serve one entry of a ZIM archive, as kiwix-serve does."""
    book, archive = _open(ctx, book_id)
    entry = find_entry(archive, path, request.url.query)
    if entry is None:
        raise HTTPException(404, f"{path} was not found in {book['title']}")
    if entry.is_redirect:
        return RedirectResponse(_content_url(book_id, entry.get_redirect_entry().path), 302)

    item = entry.get_item()
    etag = '"' + hashlib.sha1(f"{book['uuid']}/{entry.path}".encode()).hexdigest()[:20] + '"'
    headers = {
        "ETag": etag,
        "Cache-Control": "public, max-age=3600",
        "Accept-Ranges": "bytes",
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    size = item.size
    mimetype = item.mimetype or "application/octet-stream"
    if mimetype.startswith("text/") and "charset" not in mimetype:
        mimetype += "; charset=utf-8"

    start, end, status = 0, size - 1, 200
    match = _RANGE.match(request.headers.get("range", ""))
    if match and size > 0:
        first, last = match.groups()
        if first:
            start = int(first)
            end = min(int(last), size - 1) if last else size - 1
        elif last:
            start = max(0, size - int(last))
        if start > end or start >= size:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    if request.method == "HEAD":
        headers["Content-Length"] = str(end - start + 1)
        return Response(status_code=status, headers=headers, media_type=mimetype)
    body = bytes(item.content[start : end + 1])
    return Response(body, status, headers, media_type=mimetype)
