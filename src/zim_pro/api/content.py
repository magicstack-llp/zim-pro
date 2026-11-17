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


def _content_csp(request: Request) -> str:
    """Book pages run sandboxed (an opaque origin, no access to the app's token) and may
    fetch only other book files: ZIMs that are JavaScript apps (Maps2ZIM and the like)
    load their tiles and data with fetch(). /api stays out of reach."""
    host = request.headers.get("host")
    files = f"{request.url.scheme}://{host}/content/ data: blob:" if host else "'none'"
    return (
        "sandbox allow-scripts allow-forms allow-popups; "
        "default-src * 'unsafe-inline' 'unsafe-eval' data: blob:; "
        f"connect-src {files};"
    )


# Sandboxed book pages may not use cookies or Web Storage: touching them throws, and
# many ZIM pages (Gutenberg's jQuery cookie plugin, Maps2ZIM's saved view) then stop
# running. This stands in an in-memory version, only where the browser refuses the real
# one, so those scripts carry on. Nothing outlives the page.
_STORAGE_SHIM_JS = """
(function () {
  function memory() {
    var data = {};
    var has = function (k) { return Object.prototype.hasOwnProperty.call(data, k); };
    return {
      getItem: function (k) { return has(k) ? data[k] : null; },
      setItem: function (k, v) { data[k] = String(v); },
      removeItem: function (k) { delete data[k]; },
      clear: function () { data = {}; },
      key: function (i) { return Object.keys(data)[i] || null; },
      get length() { return Object.keys(data).length; }
    };
  }
  ["localStorage", "sessionStorage"].forEach(function (name) {
    try { window[name]; } catch (refused) {
      try {
        Object.defineProperty(window, name, { value: memory(), configurable: true });
      } catch (ignored) {}
    }
  });
  try { document.cookie; } catch (refused) {
    var jar = {};
    try {
      Object.defineProperty(document, "cookie", {
        configurable: true,
        get: function () {
          return Object.keys(jar).map(function (k) { return k + "=" + jar[k]; }).join("; ");
        },
        set: function (value) {
          var pair = String(value).split(";")[0];
          var at = pair.indexOf("=");
          if (at > 0) jar[pair.slice(0, at).trim()] = pair.slice(at + 1);
        }
      });
    } catch (ignored) {}
  }
})();
"""
_STORAGE_SHIM = f"<script>{_STORAGE_SHIM_JS}</script>".encode()
# Part of every ETag, so browsers drop pages cached before the shim (or a change to it).
_SHIM_VERSION = "shim1"
_SHIM_AFTER = [
    re.compile(rb"<head\b[^>]*>", re.IGNORECASE),
    re.compile(rb"<html\b[^>]*>", re.IGNORECASE),
    re.compile(rb"<!doctype[^>]*>", re.IGNORECASE),
]


def _with_storage_shim(html: bytes) -> bytes:
    """The page with _STORAGE_SHIM placed before any of its own scripts can run."""
    for tag in _SHIM_AFTER:
        if match := tag.search(html, 0, 65536):
            return html[: match.end()] + _STORAGE_SHIM + html[match.end() :]
    return _STORAGE_SHIM + html


def _from_sandboxed_page(request: Request) -> bool:
    """A request from a sandboxed page, which has the origin "null".

    Book pages are sandboxed, so their module scripts, crossorigin stylesheets and
    fetch() calls need CORS. A sandboxed page sends no Referer, so a book page on this
    server can't be told apart from a sandboxed page elsewhere. That is accepted: book
    content needs no token to read anyway, and this applies to /content only, never /api.
    """
    return request.headers.get("origin") == "null"


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
        # Pages get the storage shim, so their length is that of the rewritten body.
        page: bytes | None = None
        if mimetype.split(";")[0].strip().lower() in ("text/html", "application/xhtml+xml"):
            page = _with_storage_shim(bytes(item.content))
            size = len(page)
    except (RuntimeError, OSError, KeyError) as exc:
        ctx.pool.invalidate(book["path"])
        log.warning("item read failed for %s/%s: %s", book_id, path, exc)
        raise HTTPException(503, "Book unavailable, try rescanning") from None

    version = f"{book['uuid']}/{book.get('size', '')}/{book.get('mtime', '')}"
    etag = (
        '"'
        + hashlib.sha1(f"{version}/{entry.path}/{_SHIM_VERSION}".encode()).hexdigest()[:20]
        + '"'
    )
    scope = "private" if ctx.config.admin_token else "public"
    # Pages are checked with the server every time (a cheap 304 while unchanged), so a
    # change to what the server adds to them reaches pages opened from inside a book;
    # their images, scripts and styles keep an hour's cache.
    cache_control = f"{scope}, no-cache" if page is not None else f"{scope}, max-age=3600"
    headers = {
        "ETag": etag,
        "Cache-Control": cache_control,
        "Accept-Ranges": "bytes",
        "Content-Security-Policy": _content_csp(request),
        "X-Content-Type-Options": "nosniff",
        "Vary": "Origin",
    }
    if _from_sandboxed_page(request):
        headers["Access-Control-Allow-Origin"] = "null"
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
    if page is not None:
        return Response(page[start : end + 1], status, headers, media_type=mimetype)
    try:
        body = bytes(item.content[start : end + 1])
    except (RuntimeError, OSError, KeyError) as exc:
        ctx.pool.invalidate(book["path"])
        log.warning("content read failed for %s/%s: %s", book_id, path, exc)
        raise HTTPException(503, "Book unavailable, try rescanning") from None
    return Response(body, status, headers, media_type=mimetype)
