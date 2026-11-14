# Library Module Review — r1

**Scope:** `library/{archive_pool,naming,search,reader,scanner,article}.py`,
`api/{library,content,organise,access}.py`; context from `db.py`, `context.py`, `main.py`, `access.py`;
call sites traced in `downloads/manager.py`, `builder/jobs.py`, `retrieval.py`, `mcp_server.py`,
`scheduler.py`, `web/src/pages/Reader.tsx`.
**Commit:** `cbb1a4e` (0.5.0). **Date:** 2026-11-14.
**Method:** full read of every file in scope, grep of all call sites, targeted runtime checks in the
project venv — `posixpath`/`urlsplit` link resolution, `pathlib.rglob` symlink behaviour (CPython
3.13.16), libzim 3.13.1 docstrings (`Search.getResults` is `(start, count)`), `pytest
tests/test_units.py` (57 passed). No source modified; there is no ZIM in the repo, so
archive-dependent behaviour is reasoned from the libzim API, not executed.

## Summary

- Serving is safe against **filesystem** traversal (`book_id` is resolved through `books` only and entry
  paths are looked up inside the archive). The real risk is **untrusted ZIM HTML served same-origin with
  no CSP** (F1).
- The scanner is the weak point: it ignores ZIMs under symlinked directories and then deletes their rows
  (F2), runs with no transaction and an unguarded `stat()` that can abort it mid-way (F3), and decides
  "which file is this book" from the filename alone (F4).
- Book identity *is* the filename, so delete/rename surprise: an older shadowed release is promoted back
  after a delete, and a rename resets `books.policy` (F5).
- Pool eviction is refcount-safe, but `get()` can undo a concurrent `invalidate()` and re-cache a replaced
  file indefinitely (F7); read-path libzim errors become 500s (F11); category casing is inconsistent
  between `books`, `categories` and `/api/search` (F8, F15).
- Coverage stops at the pure functions: **no ZIM fixture exists**, so `scanner`, `reader`, `content`,
  `illustration` and `search` never run against a real archive in CI.

## Top 5 must-fix

1. **F1** — `/content` HTML is untrusted, same-origin, unsandboxed, no CSP/nosniff; a crawled or uploaded
   page can drive the whole admin API.
2. **F2** — `rglob("*.zim")` no longer follows directory symlinks on Python 3.13; a symlinked library
   directory makes the scanner delete every `books` row.
3. **F3** — `_scan` is not atomic and can raise; a failed scan empties `shadowed` and leaves `books`
   half-updated, and a completed download is marked `failed` because `_install` calls it inside its `try`.
4. **F4** — same-stem files with different UUIDs are merged into one book (one silently shadowed), and the
   `_YYYY-MMa` revision letter is dropped so the older revision can win.
5. **F5** — `DELETE /api/books/{id}` unlinks only the served release; any older file on disk brings the book
   straight back.

## Findings

### F1. [Critical] ZIM HTML is served same-origin, unsandboxed, with no CSP
- **Location:** `api/content.py:76-119`; `web/src/pages/Reader.tsx:221-227` — **Confidence:** high
- **Evidence:** `return Response(body, status, headers, media_type=mimetype)` (content.py:119) with
  `mimetype = item.mimetype or "application/octet-stream"` and only
  `{"ETag","Cache-Control","Accept-Ranges"}` (content.py:88-92); the entry is embedded with `<iframe
  ref={frame} src={src} onLoad={syncLocation} …>`, no `sandbox`. No `Content-Security-Policy` string exists
  anywhere in `src/` or `web/`.
- **Impact:** `text/html` entries run JavaScript on the app's origin, and ZIM content is
  attacker-influenced (crawled pages, uploaded ZIMs). `require_admin` is a no-op without
  `KIWIX_ADMIN_TOKEN` (`context.py:52-59`), so an article can `PUT /api/settings`, `DELETE
  /api/books/…`, create MCP keys and read `parent.document` (same-origin, like `syncLocation`).
- **Repro / fix:** crawl a page with `<script>fetch('/api/settings')</script>` (raw HTML is served by
  `/content`; `clean_tree` only runs on the Markdown path) and open the book. Serve from a separate origin,
  or at minimum `Content-Security-Policy: sandbox allow-scripts allow-popups allow-forms; default-src 'self'
  data: blob:` + `X-Content-Type-Options: nosniff`, with a matching iframe `sandbox` (never
  `allow-same-origin` together with `allow-scripts`); move `syncLocation` to `postMessage`. Test: `/content`
  of an HTML entry carries the sandbox CSP and nosniff.
### F2. [High] Symlinked library directories are ignored and their books deleted
- **Location:** `library/scanner.py:120` — **Confidence:** high (verified on CPython 3.13.16)
- **Evidence:** `for path in sorted(self.data_dir.rglob("*.zim")):`. On 3.13 `rglob` does not descend into
  directory symlinks (`Path("/tmp/rgt/data").rglob("*.zim")` → `[]` with `data/link -> other`).
- **Impact:** the common `data/zims -> /mnt/disk/zims` layout yields `groups == {}`, so the removal loop
  (`scanner.py:172-177`) deletes **every** `books` row, invalidates every pooled archive, and the library
  looks empty; per-book policies are lost.
- **Repro / fix:** `ln -s /mnt/zims data/library`, `POST /api/library/rescan` → `{"books": 0, "removed":
  N}`. Walk with `os.walk(..., followlinks=True)` and a visited-inode guard (or `rglob(...,
  recurse_symlinks=True)`), and never delete rows when a directory could not be listed. Test: scan with a
  symlinked subdirectory holding a ZIM; index it, scan twice, assert it survives.
### F3. [High] Scan is not atomic and can raise halfway through
- **Location:** `library/scanner.py:23-25,130-141,147,154-177`; `downloads/manager.py:344` — **Confidence:**
  high
- **Evidence:** `def read_book(path): stat = path.stat()` (:25) is called at :136 and again at :147 after
  the file was already stat-ed at :126 — an unguarded TOCTOU. `self.db.execute("DELETE FROM shadowed")`
  (:141) runs before the write loop, and every `INSERT … ON CONFLICT`/`DELETE FROM books` autocommits
  (`db.py:264 isolation_level=None`). `_install` runs inside the same `try` whose `except Exception` marks
  the download failed (`manager.py:344-370`).
- **Impact:** a file removed/replaced between :126 and :136 raises `FileNotFoundError` out of `_scan`:
  `shadowed` empty, some books updated, the removal loop skipped, `DELETE /api/books/{id}` → 500. From
  `_install` the `.part` is already in place, so a good download is recorded `failed`.
- **Repro / fix:** `rm data/x.zim` between the two stats, or let `_retire` delete a `replaces` path while a
  scan walks. Wrap `_scan` in one transaction (add `Database.transaction()`), collect stats first, treat
  per-file `OSError` as "leave this row alone", and only mark a download failed when the download failed.
  Test: patch `read_book` to raise `OSError` for one path; `scan()` must not raise, known rows must be
  unchanged, `shadowed` must keep its rows.
### F4. [Medium] Release selection is by filename stem and a coarse date only
- **Location:** `library/scanner.py:117-145`; `library/naming.py:3,43-46` — **Confidence:** high
- **Evidence:** `groups.setdefault(row["id"], []).append(row)` and `rows.sort(key=lambda r: (r.get("error")
  is None, r["date"]), reverse=True)` — `uuid`, `name`/`flavour` and metadata are never consulted.
  `_DATE_SUFFIX = r"_(\d{4}-\d{2}(?:-\d{2})?)[a-z]?$"` discards the revision letter (`…_2026-09a.zim` →
  `"2026-09"`, asserted at `tests/test_units.py:25`).
- **Impact:** (a) two unrelated ZIMs sharing a stem (`custom_notes_2026-01.zim` from a crawl plus a
  hand-copied `custom_notes_2026-02.zim`) are merged and one is silently `shadowed`. (b) With
  `x_2026-09.zim` beside `x_2026-09a.zim` the dates are equal, `sorted()` puts `…09.zim` first, and the
  **older** revision is served while the newer is listed as "superseded".
- **Repro / fix:** copy two unrelated ZIMs as `custom_x_2026-01.zim` / `custom_x_2026-02.zim`; one book, the
  other under `/api/library/leftovers` as `superseded_by: custom_x`. Keep the revision letter in the date
  key (sort by `(date, mtime)`), and refuse to merge files whose `uuid`/`name` differ. Test: `b_2026-01.zim`
  + `b_2026-01a.zim` → `a` revision served; two UUIDs, same stem → two books.
### F5. [Medium] Book identity is the filename: delete resurrects, rename resets
- **Location:** `api/library.py:92-103`; `library/scanner.py:150-161,172-177` — **Confidence:** high
- **Evidence:** `Path(book["path"]).unlink(missing_ok=True)` then `ctx.scanner.scan()` removes only the
  served path, while `scanner.py:141-167` rebuilds the group from the remaining files, so an older
  `shadowed` release becomes `best` again. The id is the file stem (`scanner.py:26-27`), so renaming
  `foo_2026-01.zim` → `bar_2026-01.zim` deletes the `foo` row (:173-177) and inserts `bar` with
  `"policy": default_policy` (:155-160).
- **Impact:** "Delete book" behaves as "roll back one release" whenever another release is on disk
  (including `keep_old_versions` output); it must be pressed N times. Renaming silently drops the policy and
  orphans `history`/`downloads`/`links.book_id`.
- **Repro / fix:** two releases of `a` → `DELETE /api/books/a` → `removed: 0`, the book returns dated older.
  Delete every file of the id (`books.path` plus `SELECT path FROM shadowed WHERE book_id = ?`), surface
  `OSError`, and keep per-book settings under a stable key. Test: two releases → delete → both files gone,
  `/api/books` empty, `shadowed` empty; rename → assert policy survives.
### F6. [Medium] A ZIM that failed to open is never retried
- **Location:** `library/scanner.py:130-138,67-69` — **Confidence:** high
- **Evidence:** `if row is None or row["size"] != stat.st_size or row["mtime"] != stat.st_mtime:` — once an
  `error` row exists for an unchanged file it is never re-read, and `POST /api/library/rescan` uses the same
  path.
- **Impact:** a transient failure (EMFILE from a pool burst, NFS hiccup, a file still being copied,
  `Archive()` racing a replace) hides the book from `/content`, `/markdown`, search, retrieval and MCP
  **forever**; only `touch` or a re-download recovers it.
- **Repro / fix:** `ulimit -n 8` and scan: `error` rows remain after the limit is restored. Always retry
  `error IS NOT NULL` rows (they are rare) or record `error_at` and retry after N scans; the `ON CONFLICT`
  update already clears `error`. Test: insert an `error` row with the correct size/mtime for a valid ZIM; a
  rescan must re-read it.
### F7. [Medium] `ArchivePool.get` can undo a concurrent `invalidate`; pool never closed
- **Location:** `library/archive_pool.py:15-27` — **Confidence:** high
- **Evidence:** the miss path opens **outside** the lock and inserts unconditionally: `archive =
  Archive(path)` … `with self._lock: self._archives[path] = archive`. An `invalidate(path)` from a
  scanner/builder thread (`scanner.py:138,152,176`) between those blocks is lost. `pool.clear()` is never
  called in the lifespan (`main.py:94-102`).
- **Impact:** after an upgrade a book keeps serving the previous release's bytes and ETag while `books`
  reports the new size/date, with nothing to invalidate it later. Archives close only when the GC runs.
- **Repro / fix:** hold a request on a pool miss over a large ZIM, let a download complete and rescan,
  request the article. Add a per-path generation counter bumped by `invalidate` (insert only if unchanged)
  and call `pool.clear()` on shutdown. Test: fake `Archive` factory blocking between the two lock sections;
  `invalidate` in between; `get` must return a newly opened archive.
### F8. [Medium] A book's category lives only in its source row, so it silently changes
- **Location:** `library/scanner.py:80-96`; `api/build.py:179-185` — **Confidence:** high
- **Evidence:** `chosen[row["book_id"]] = row["category"] or "custom"` is rebuilt from
  `links`/`feeds`/`uploads` on every scan; `category = chosen.get(row["id"]) or category_of(row["id"],
  row["tags"])`. `DELETE FROM links WHERE id = ?` is documented as "the ZIM built from it stays in the
  library" and is followed by `_refile(ctx)`.
- **Impact:** deleting/renaming the source silently re-files the book — built ZIMs carry
  `tags="_category:custom"` (`builder/writer.py:72`), so they jump back to "My ZIMs". `chosen` precedence is
  accidental (links, feeds, uploads overwrite in turn). A tag-derived category bypasses `clean_category` and
  can contain `,`/`>`/be >40 chars, producing category names the UI lists but cannot create.
- **Repro / fix:** build a link filed under "Cooking", delete the link; `/api/books` shows `category:
  "custom"` (the delete path is untested; `tests/test_network.py:257-290` asserts only the rename case, as
  intended). Persist the filing on `books` (`filed_category`, with `category_of(tags)` as fallback) and make
  precedence explicit. Test: delete a link and assert the book keeps its category (or assert the documented
  reset).
### F9. [Medium] Cross-book search ignores `start`/`count`, fans out over every book, hides errors
- **Location:** `api/library.py:142-163` — **Confidence:** high
- **Evidence:** `found = zim_search.search_book(ctx.pool.get(row["path"]), q, 0, 3, snippets=False)` —
  `start` and `count` are unused in this branch (the single-book branch passes `max(0, start), count`);
  every book is searched serially in one request; `except Exception: continue` swallows corrupt-archive
  errors with no log.
- **Impact:** paging `/api/search` returns the same first page forever; a 300-book library makes one request
  O(300 full-text searches); a broken archive is invisible to operators, and the endpoint has no result cap
  (`mcp_server._search` at least slices `[:limit]`).
- **Repro / fix:** `GET /api/search?q=water&start=50` twice → identical bodies. Define the contract (page
  over books, `limit_books`) or 422 on `start`, cap the fan-out, log skipped books. Test: two fake archives;
  `start` changes the page or 422; a raising `pool.get` is logged and does not abort the response.
### F10. [Medium] Snippet generation parses up to 4 MB of HTML per hit, up to 100 hits
- **Location:** `library/search.py:47-49,53-60`; clamp at `api/library.py:133` — **Confidence:** high on
  mechanism
- **Evidence:** `for hit in hits: hit["snippet"] = snippet(archive, hit["path"], query)` and `snippet` runs
  `html_to_text(bytes(item.content))` guarded only by `if not item.mimetype.startswith("text/html") or
  item.size > 4_000_000: return ""`; `count = max(1, min(count, 100))`.
- **Impact:** one request can push 400 MB of HTML through lexbor and hold the text of every hit — RAM
  exhaustion with a few concurrent clients, seconds of latency at count=20 on Wikipedia.
- **Repro / fix:** `time curl '…/api/search?book=wikipedia_en_all_maxi&q=water&count=100'`. Guard at ~256
  kB, snippet only the first N hits, or use libzim's highlighted terms. Test: a 3 MB fake item beyond the
  snippet cap must not be parsed.
### F11. [Medium] libzim errors on read paths become 500s
- **Location:** `api/content.py:21-25,83-84`; `library/reader.py:30-34,42-46,106-115` — **Confidence:**
  medium-high
- **Evidence:** `_open` maps only `LookupError`; `Archive(path)` failures propagate.
  `main_path`/`random_path`/`read_article` call `get_item()` after the 8-hop loop even if the entry is still
  a redirect, and `entry.get_redirect_entry().path` (content.py:84) is not guarded for a dangling target.
- **Impact:** a ZIM deleted/truncated after the scan 500s every request until the next scan; a redirect
  cycle 500s `/markdown` and MCP, and 302-loops the browser on `/content`.
- **Repro / fix:** `rm data/book.zim` without rescanning, then `GET /content/book/A/Foo`. Map archive errors
  to 503 ("book unavailable, rescan"), check `is_redirect` after the loop, and resolve chains server-side
  with a hop cap. Test: missing archive → 404/503 not 500; fake redirect chain over `MAX_REDIRECTS` → clean
  error.
### F12. [Medium] Link canonicalisation: unquote happens after the `..` check
- **Location:** `library/article.py:12-18,30-35`; `library/reader.py:11-23` — **Confidence:** high
  (executed)
- **Evidence:** `joined = posixpath.normpath(posixpath.join(posixpath.dirname(article_path), parts.path));
  return None if joined.startswith("..") else unquote(joined)`. Executed: `resolve_link("A/Page",
  "%2e%2e/%2e%2e/etc/passwd")` → `'A/../../etc/passwd'`; `resolve_link("A/Page", "/A/Foo")` → `'/A/Foo'` →
  `/content/book//A/Foo`. `find_entry` also `unquote`s a path Starlette already decoded.
- **Impact:** (a) root-absolute hrefs (scraped/zimit pages) yield double-slash URLs that 404 — images and
  links break in the Markdown view; (b) percent-encoded dots defeat the escape check, so a crafted ZIM can
  emit `![…](/content/book/A/../../../api/settings)`, which the browser normalises to a same-origin path
  (the Reader follows `/markdown/` links with `replace`, `Reader.tsx:270`); (c) `A/Foo%20Bar` aliases `A/Foo
  Bar`.
- **Repro / fix:** `resolve_link("A/P", "%2e%2e/%2e%2e/x")` in a REPL; open a ZIM linking to `/wiki/Foo`.
  Unquote first, then resolve and reject `..`; strip a leading `/` and re-prefix the archive namespace; drop
  the second `unquote`. Test: extend `test_resolve_link` with `%2e%2e`, `%2F`, `/A/Foo`, `%2520`.
### F13. [Medium] Range requests materialise the whole entry; minor HTTP gaps
- **Location:** `api/content.py:96-119` — **Confidence:** high on code, medium on libzim
- **Evidence:** `size = item.size` … `body = bytes(item.content[start : end + 1])`;
  `dir(libzim.reader.Item)` offers only `content, mimetype, path, size, title` (no ranged read). `_RANGE =
  re.compile(r"bytes=(\d*)-(\d*)$")` is case-sensitive, single-range only, and the 416 branch drops
  `Accept-Ranges`/`Cache-Control`.
- **Impact:** every seek in a 1 GB video costs a full decompression/allocation; concurrent ranges can OOM.
  `Bytes=0-1` and multi-range fall back to a full 200.
- **Repro / fix:** `curl -H 'Range: bytes=1000000-1000100' /content/book/A/movie.webm`, watch RSS. Use an
  offset read if the pinned libzim exposes one, otherwise don't inline huge media; make the parser
  case-insensitive, 416 on multi-range, keep `Accept-Ranges`. Test: `bytes=0-0`, `bytes=-5`, `bytes=5-3`
  (416), `Bytes=0-1`, `bytes=0-1,3-4`, HEAD with a range.
### F14. [Low] Category maintenance is multi-step, unserialised, partly unchecked
- **Location:** `api/organise.py:96-145` — **Confidence:** high
- **Evidence:** `rename_category` does `UPDATE categories` + three `UPDATE {table}` + `_refile(ctx)` as five
  autocommit steps; `delete_category` never checks existence (no 404); `add_category` does
  `SELECT`-then-`INSERT`, so concurrent duplicates raise `sqlite3.IntegrityError` → 500.
- **Impact:** a failure or concurrent request mid-way leaves `categories` renamed while `books.category`
  still has the old value; deleting an unknown category reports success.
- **Repro / fix:** two simultaneous `POST /api/categories {"name":"X"}`. One transaction per operation,
  `IntegrityError` → 409, 404 when `rowcount == 0`. Test: `DELETE /api/categories/unknown` → 404; a
  simulated failure mid-rename leaves nothing renamed.
### F15. [Low] Category case is NOCASE in one table and case-sensitive elsewhere
- **Location:** `db.py:238-241`; `api/organise.py:41-48`; `api/library.py:143-147` — **Confidence:** high
- **Evidence:** `name TEXT PRIMARY KEY COLLATE NOCASE` versus `books.category TEXT NOT NULL DEFAULT 'other'`
  (no collation), `GROUP BY category` in `library_categories`, and `WHERE … (? = '' OR category = ?)` in
  `/api/search`.
- **Impact:** `_category:Wikipedia` and user category `wikipedia` are one row in `categories` but two values
  in `books`; `/api/search?category=wikipedia` misses the former; the UI (`format.ts:72-74`, keyed by the
  raw string) shows two chips; `library_categories` hides tag-derived categories colliding with a user
  category, so a book looks filed when it is not.
- **Repro / fix:** two books with `'Wikipedia'`/`'wikipedia'` → two chips, one search hit. Canonical
  lowercase keys plus a display name, `books.category … COLLATE NOCASE`, and `COLLATE NOCASE` in the search
  filter. Test: mixed-case categories → one facet, one result set.
### F16. [Low] `leftovers()` leaks absolute paths, races on `stat`, lists live work dirs
- **Location:** `library/scanner.py:180-198` (route `api/library.py:113` has no admin guard) —
  **Confidence:** high
- **Evidence:** `result.append({"path": str(path), … "size": path.stat().st_size})` with no `try`;
  `rglob("*")` includes `.zimit-<id>` working directories (which the scanner skips at :123-124) and
  `.meta4`/`.part` files.
- **Impact:** a running capture's files appear in Settings as junk to delete; a file removed between `rglob`
  and `stat` (or a permission error) returns 500; the server path (`/home/<user>/…/data/…`) is disclosed to
  any LAN client.
- **Repro / fix:** `GET /api/library/leftovers` during a zimit build. Skip dot-parts and active download
  files, wrap `stat`, return relative paths or guard the route with `require_admin`. Test: leftovers with a
  `.zimit-1/x` file and a `.meta4` → neither listed.
### F17. [Low] Disk usage is read unguarded in `/api/status`
- **Location:** `api/library.py:34`; also `api/catalog.py:118` — **Confidence:** high
- **Evidence:** `usage = shutil.disk_usage(ctx.config.data_dir)` at the top of `status`, while
  `monitor.py:350-352` wraps the same call in `try`.
- **Impact:** a missing/unmounted `data_dir` 500s the whole status payload (books, catalog, settings)
  exactly when the operator needs it.
- **Repro / fix:** point `data_dir` at a removed directory, `GET /api/status`. Return `disk_free: null` on
  `OSError`, as `monitor` does. Test: status with a missing `data_dir` → 200 with null disk fields.
### F18. [Low] ETag/caching keyed on uuid+path, not on the bytes served
- **Location:** `api/content.py:87-94` — **Confidence:** high
- **Evidence:** `etag = sha1(f"{book['uuid']}/{entry.path}")`, `Cache-Control: public, max-age=3600`; no
  `Last-Modified`, no `If-Range`.
- **Impact:** repairing/re-installing a ZIM that keeps its uuid (same release re-downloaded, recrawl reusing
  the uuid) leaves hour-old bytes cached, and a range request after a swap resumes into the wrong content.
  `public` lets shared proxies cache archive content from an origin that may be behind a token.
- **Repro / fix:** overwrite `data/x.zim` with another build of the same uuid; `If-None-Match` → 304. Hash
  `size`/`mtime` into the ETag, add `Last-Modified`, use `private` when a token is configured. Test: bump
  the row's `mtime`; the ETag must change.
### F19. [Low] Illustration endpoint assumes PNG and hides archive failures
- **Location:** `api/library.py:66-73` — **Confidence:** medium
- **Evidence:** `return Response(data, media_type="image/png", …)` after `except Exception: raise
  HTTPException(404, "No illustration")`; `has_illustration` (`scanner.py:65`) is stored but never consulted
  and the item's `mimetype` is ignored.
- **Impact:** WebP/SVG illustrations are mislabelled (browsers sniff, other clients don't); a corrupt
  archive, a closed entry and a missing illustration are indistinguishable 404s with no log.
- **Repro / fix:** a WebP-illustrated ZIM → `image/png`; `rm` the ZIM → still 404. Use `item.mimetype`,
  check `has_illustration`, `log.warning` before the 404. Test: WebP illustration → correct content type.
### F20. [Low] Blocking work inside `async def` endpoints
- **Location:** `api/organise.py:172-188`; `api/library.py:231-291` — **Confidence:** high
- **Evidence:** `set_icon` is `async def` and does `data = await file.read(icons.MAX_BYTES+1)` (spooled to
  disk) plus `target.write_bytes(picture)`; `put_settings` is `async def` but does synchronous SQLite writes
  and fan-out. Every other endpoint in scope is plain `def` and correctly threaded.
- **Impact:** uploading an icon or saving settings stalls the event loop (SSE `/api/events` and every
  in-flight request) for the duration of the disk I/O.
- **Repro / fix:** upload a 10 MB icon to `/api/icons/link/1` while polling `/api/events`. Make both `def`,
  or wrap the I/O in `asyncio.to_thread`. Test: not practical; add a review rule "no blocking calls in
  `async def`".
### F21. [Nit] Bare `except Exception` and two consistency nits
- **Location:** `library/search.py:13-17,55-61`; `api/library.py:68-72,132,148-152,205-214` —
  **Confidence:** high
- **Evidence:** `_title`/`snippet`/`illustration` fall back on *any* exception, the cross-book loop does
  `except Exception: continue`, both `DELETE /search/history…` routes lack
  `dependencies=[Depends(require_admin)]` (unlike `delete_book`/`rescan`/`patch_book`), and `q` goes to
  libzim unbounded while `count` is clamped.
- **Impact:** a closed archive or corrupt index yields silent empty results instead of a log line — the
  failure class of F6/F7/F11; anyone on the LAN can wipe shared history; a multi-megabyte `q` is handed to
  Xapian for every book.
- **Repro / fix:** `DELETE /api/search/history` without a token → 200. Catch the specific libzim errors
  (`KeyError`, `RuntimeError`) and log the book id/path; add the admin guard; clamp `q` (e.g. 512 chars →
  422). Test: delete with a token configured → 401; `q` of 1 MB → 422; a failing book logs a warning.

## Database integrity & normalization notes

- **No FKs on library tables.** `links.book_id`, `uploads.book_id`, `feeds.book_id`, `builds.book_id`,
  `downloads.book_id`, `history.book_id`, `shadowed.book_id` are unvalidated strings
  (`db.py:35-40,122-228`); neither `delete_book` nor the removal loop cleans them, and
  `file_under_categories` keeps `chosen` entries for vanished books — a *new* book later taking the same id
  inherits the dead source's category.
- **Book identity is the filename stem** (`scanner.py:26-27`) while `book_updates`
  (`catalog/updates.py:47-49`) matches by stem *then* name+flavour, so the two disagree.
- **No transactions**: `_scan` commits per row (`db.py:264` autocommit), `rename_category` touches four
  tables plus derived rows, and `file_under_categories` is also called from `_refile` outside the scanner
  lock, so two writers can interleave.
- **Derived columns refresh only when a file is read** (`article_count`, `media_count`, `has_ftindex`,
  `has_illustration`, `name`, `flavour`, `date`, `uuid`, `error`); with F6 a wrong value is sticky.
  `has_illustration` is computed at 48 px and only asked for at 48 px.
- **`shadowed` is rebuilt wholesale** (`DELETE FROM shadowed`, scanner.py:141) — right when a scan
  completes, destructive when it does not (F3). It has no `uuid`/`filename`/`error`, so a shadowed corrupt
  file is invisible outside `leftovers()`.
- **Three spellings of "no category"**: `''` in sources, `'custom'` on books built here, `'other'` for
  unknown Kiwix books; `format.ts:67-74` maps `custom` but merely uppercases `other`.
- **Uniqueness:** `books.path UNIQUE` plus the pre-delete at scanner.py:154 avoids UNIQUE violations, but
  that delete is not counted in `removed`, does not invalidate the pool, and in the contrived "two files
  swap names" case can delete a row `seen` believes exists.

## Connectivity / robustness notes

- Every library endpoint is a sync `def`, so blocking SQLite/libzim work runs in the threadpool —
  correct. Exception: F20.
- Archive opens happen outside the pool lock (good, no global stall) but with **no timeout**: a slow NFS
  mount blocks a worker per open, and `/api/search` incurs one open per book. `read_book` is likewise
  unbounded in the scan thread, and `Scanner._lock` then queues downloads, builds and the scheduler behind
  a stalled scan.
- I/O error handling is inconsistent: `Archive()` failures are caught, `path.stat()` (F3) and
  `leftovers()` (F16) are not, and `delete_book` ignores `OSError` from a read-only mount.
- No streaming: `/content` always builds a full `bytes` body (F13); no `If-Range`, `Content-Disposition`
  or `nosniff`. Cache headers are `public, max-age=3600` for content/Markdown and `max-age=86400` for
  illustrations — fine for immutable ZIMs, weak ETag identity (F18).
- Disk-full is handled at write time by the builder/downloader (`FREE_SPACE_MARGIN`); the library module
  only reads.

## Testing gaps

Mapped against `test_units.py`, `test_engine.py`, `test_system.py` (plus
`test_network.py`/`test_builder.py`, which do touch the module):

- `test_units.py` covers four library things only: `split_filename`, `category_of`, `resolve_link` (four
  cases) and the Markdown/media pipeline; it is strong on HTML→Markdown. `test_engine.py` has **zero**
  library coverage; `test_system.py` touches it only through `route_group("/content/…")`, metrics and
  settings.
- `tests/fixtures/` holds `bash.meta4` and `feed.xml` only: **there is no ZIM in the repo**, so
  `ArchivePool`, `read_book`, `find_entry`/`main_path`/`random_path`, `content`, `markdown`, `random`,
  `illustration` and `search.*` never run against a real archive in CI.
- Untested branches: all of `ArchivePool` (`invalidate`, `clear`, eviction); `leftovers`; scanner
  replacement/shadowing/removal/`error` rows/symlinked dirs/`keep_old_versions`; `read_article`'s redirect
  loop and `MAX_REDIRECTS` overflow; `find_entry`'s legacy `A/`/`I/`/`-` fallbacks and `?query`;
  `snippet`; `search_book`'s title mode; `outline`; `source_url`; `_prune_sections`; `_caption`.
- API branches with no test: `DELETE /api/books/{id}` (delete/404/file-missing), `PATCH` policy,
  `POST /library/rescan`, `GET /library/leftovers`, `/api/search` (single and cross-book, pagination,
  category, unknown book), `/api/suggest`, `/content` (304, HEAD, all Range forms, 416, redirect entries,
  non-ASCII paths), `/markdown` (415, query-string archives), `/illustration` 404, `/api/health`,
  `/api/status` (unmounted volume), `/api/about/latest` errors. `test_network.py:365-383` covers only the
  MCP-key happy path.
- Traversal is tested only at function level (`test_units.py:84`); nothing asserts `%2e%2e`, `%2F`,
  `/A/Foo` or `//` is rejected/normalised, and no endpoint test drives `/content`.
- No corrupt/truncated ZIM, closed-archive, eviction-while-in-use or concurrent scan/read test, and no
  test that a failed scan leaves the DB unchanged. `books`/`shadowed` assertions after operations exist
  only in `test_network.py` (categories) and `test_builder.py` (one scan, one built-book category); no
  test asserts `policy` survives a rescan.

## What is done well

- **No user-controlled filesystem paths.** `book_id` always goes through `books` (`context.py:38-45`), and
  `BOOK_COLUMNS` (`library.py:18-22`) omits `path`, so `/api/books` does not leak the data directory (only
  `error` text does).
- **Eviction while in use is safe:** `ArchivePool.get` returns a strong reference, so
  `popitem(last=False)` cannot close a file another request is reading, and opens happen outside the lock.
- Redirect handling (one hop in `content`, `MAX_REDIRECTS` chains in `reader`), the legacy `A/`/`I/`/`-`
  namespaces and Zimit `path?query` handling show real knowledge of how Kiwix ZIMs differ, as does the
  shadowing model plus keeping `error` rows visible in `/api/books`.
- The HTML→Markdown pipeline is careful and well tested: per-site furniture selectors, caption pairing,
  decoration filtering, and a model-facing variant that strips links and link-list sections.
- `ON CONFLICT (id) DO UPDATE` deliberately excludes `policy`, so per-book policy survives a rescan.
  `clean_category` forbids `;`/`_` because the value is embedded in a `_category:` tag, and `RESERVED`
  stops user categories colliding with Kiwix's; `rename_category` compares case-insensitively against the
  `COLLATE NOCASE` key.
- `require_admin` guards every mutating library/organise route; MCP keys are SHA-256 only, `last_used` is
  written at most once a minute, and secrets are masked on read.

## Unverified / out of scope

- Whether libzim `Archive`/`Entry`/`Item` are safe for concurrent use from several FastAPI threadpool
  workers: the pool shares one object across threads and I could not stress-test it without a ZIM.
- Whether `get_illustration_item(48)` scales down when only 96/192 exist (affects F19's impact, not its
  fix), and the memory behaviour of `Item.content` for large entries (F13) — verified only that the Python
  API exposes no ranged read.
- Front-end behaviour beyond `Reader.tsx` (iframe/CSP) and `Scope.tsx`/`format.ts` (category strings); the
  SPA route, `web/tests` and the rest of the UI were not reviewed.
- Builder/zimit/download internals were read only where they call into the library (`jobs.py:557-614`,
  `manager.py:391-404`); `api/build.py`, `api/feeds.py` and `api/catalog.py` only where they call
  `use_category`/`_refile`/`file_under_categories`.
- No real ZIM was available, so F2/F4/F6/F11/F13 rest on code and API semantics rather than a running
  server. Windows/macOS packaging was not considered (unlink-while-open differs).
