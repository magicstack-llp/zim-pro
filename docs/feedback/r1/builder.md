# Builder Module Review — r1

**Scope:** `builder/{crawler,jobs,extract,writer,zimit,sitemap,icons,report}.py`,
`builder/convert/{common,epub,mobi,pdf}.py`, `api/build.py`, `tests/test_builder.py`; context:
`db.py`, `network.py`, `downloads/manager.py`, `feeds/manager.py`, `queues.py`, `scanner.py`.
**Commit:** `cbb1a4e` (0.5.0) · **Date:** 2026-11-14 · **Method:** read every scoped file
(crawler in four passes), traced call sites into `jobs.py`/schema/tests; behavioural claims marked
*verified* were reproduced with throwaway scripts in the venv (no source touched).
`pytest tests/test_builder.py` → 36 passed, 4.7 s. Nothing was run against a live site or Docker.

## Summary

- The pipeline is strong (per-page isolation, validator cache, staged content + post-write
  verification, single-writer libzim); defects cluster at the seams: **cancel vs. threads**,
  **name/queue races**, **scope/robots holes for embedded resources**, **unescaped HTML building**.
- Cancel is weakest: `task.cancel()` cannot stop `asyncio.to_thread`, yet `_run_build` deletes the stage
  those threads read and can record `cancelled` for a build whose ZIM is then installed (F2);
  `Cancelled` is also swallowed as a page failure (F3).
- Two builds can pick the same book name and overwrite each other (F1); the polymorphic
  `builds.source_id`/`kind` has no FK, CHECK or unique-active index, so the "already being built"
  guard is TOCTOU (F13) and deleting a source mid-build is invisible to the build (F12).
- Untrusted content reaches the ZIM unescaped in one path (F4, reproduced); embedded resources bypass
  scope, robots and private-address checks (F5, SSRF-shaped).
- Tests cover crawl/writer/EPUB/PDF/upload happy paths well, but not cancel-mid-thread, name
  collisions, robots on assets, sitemap limits, charsets, the feeds-only crawl, or any API endpoint.

## Top 5 must-fix

1. **F1** Concurrent builds with the same slug overwrite each other's ZIM (silent data loss).
2. **F2** Cancel cannot stop thread work; the stage is deleted under it, yet a cancelled build can
   still install its ZIM without a DB record or retiring the old release.
3. **F4** HTML injection into generated ZIMs via unescaped URLs interpolated into tag strings.
4. **F5** `_resources` fetches any `http(s)` URL a page embeds: off-scope, robots-disallowed, and
   loopback/link-local/metadata addresses included.
5. **F6** Feed articles bypass robots and queue behind sitemap pages, so `max_pages` drops them.

---

## Findings

### F1. [Critical] Two builds can pick the same book id and overwrite each other's ZIM
**Location:** `jobs.py:428-441` (`_name_for`), `:496`/`:547` (assign), `:569-574` + `writer.py:308-309`.
**Category:** correctness / data loss / concurrency. **Confidence:** high (path), medium on frequency.
**Evidence** — names come from book ids plus every `links`/`uploads`/`feeds.book_id`:
```python
taken = {r["id"] for r in db.all("SELECT id FROM books")} | {r["book_id"] for ...}
while name in taken: name, number = f"{wanted}-{number}", number + 1
```
Nothing is reserved: the name reaches `books` only after `scanner.scan()` (`:577`) and `links.book_id`
only after the write (`:514`). `max_concurrent_builds` goes up to 4 (`api/library.py:268`) and
`FeedManager` is a concurrent second queue (`feeds/manager.py:26`).
**Impact:** both writers target `data_dir/custom_x_YYYY-MM.zim`; the second replaces the first. Both
sources and build rows point at one book id, one source's content is gone, two `history` rows say
success. Hits two sites whose titles slug alike (`custom_home`, `custom_documentation`). **Trigger:** `max_concurrent_builds=2`, two uploads titled "Guide" (or two links titled "Home").
**Fix:** reserve the name transactionally (unique placeholder row / `BEGIN IMMEDIATE` check+insert), or
serialise `_name_for`→`write_zim` under one lock and re-check `books` before writing.
**Test:** two `enqueue("upload", …)` with equal titles at concurrency 2 → distinct ids, files, rows.

### F2. [High] Cancel/stop cannot stop `to_thread` work; stage deleted underneath it, ZIM still installed
**Location:** `jobs.py:170-181`, `:314-352`, `:342` (`stage.clear()`), `:572-576`; converters `:538-546`.
**Category:** cancel / crash consistency / race. **Confidence:** high (mechanism), medium per-run outcome.
**Evidence**
```python
except (Cancelled, asyncio.CancelledError): state = "cancelled"
finally:
    stage.clear()                 # rmtree while the writer thread still reads entry.file
    self._active.pop(build_id, None); self._record(...state...)   # "cancelled"
```
`task.cancel()` raises into the coroutine awaiting `to_thread`; the thread runs on. `write_zim` either
dies on `FileProvider` reads (unretrieved thread exception) or has finished reading and continues
`verify` → `target.unlink()` → `temporary.replace(target)` (`writer.py:301-309`), installing a ZIM for
a build recorded `cancelled`, with `_retire`/`scanner.scan()`/`_history` skipped.
**Impact:** "cancelled" builds mutate the library; old release not retired (two files, no history); a
`.tmp` is later offered as junk. Same race for the converters; cancel during "writing" is documented
unpausable yet allowed. **Trigger:** cancel a large conversion or ZIM write mid-flight.
**Fix:** don't cancel thread-bound phases — set the flag, let the thread return, then abort after it
(`report.check()` right after `to_thread`); `stage.clear()` only when no thread can live; refuse
`cancel` while `phase in UNPAUSABLE`.
**Test:** cancel during a monkeypatched slow `write_zim`; assert no new `.zim`, old file intact, row
`cancelled`, stage removed only after the thread returned.

### F3. [High] A cancelled crawl reports every remaining queued URL as a *failed page*
**Location:** `crawler.py:888-914`; `report.py:12-14, 37-45`.
**Category:** cancel / progress accounting. **Confidence:** high.
**Evidence** — `work()` has `except asyncio.CancelledError: raise` followed by
`except Exception as exc: report.failed(url, ...)`, but `report.gate()` raises `Cancelled(Exception)`; the worker drains the whole queue (up to 50 000 URLs),
logging each as failed with "the build was cancelled", before `_run` reaches `report.check()` (`:930`).
`_record` persists `failed` (`jobs.py:367-386`).
**Impact:** cancelling a 3 000-page build stores `failed≈2900`, fills the 500-entry log with noise, and
tells the user thousands of pages failed. Cancel is also not prompt (drain + in-flight request). **Trigger:** cancel a crawl fed by a large sitemap after a few pages.
**Fix:** `except Cancelled: raise` before the generic handler; break the queue loop when cancelled.
**Test:** cancel after 2 pages → `failed_count == 0`, fast return, state `cancelled`.

### F4. [High] HTML injection into the generated ZIM (page-controlled URLs in tag strings)
**Location:** `extract.py:266-278` (media fallback), `:389-402` (localise), `:248-258` + `epub.py:127-133` (alt text).
**Category:** XSS / sanitisation. **Confidence:** high — reproduced.
**Evidence** — `f'<p><a href="{address}">{label} (not included): {address}</a></p>'` is parsed as HTML
(`extract.py:273-278`, again at `:401`). `normalise()` keeps a literal `"` in the path (verified:
`normalise('https://e.test/a"onmouseover="x')` returns it unchanged); only the query is re-encoded.
**Impact (reproduced):** `<video src='x" onmouseover="alert(1)'>` with the default `video=False` yields
`<a href="https://e.test/x" onmouseover="alert(1)">` in `Prepared.html`, and the same survives
`localise()`. `images=False` alt replacement `node.replace_with(f"[{alt}]")` also produces live markup
when alt starts with a tag (verified for `<img>`/`<video>` payloads). A hostile page injects markup into
a ZIM that is shared and served. **Trigger:** crawl any page with a quote-bearing `video`/`img`/`a` URL or a tag-bearing `alt`.
**Fix:** `html.escape(addr, quote=True)` or build nodes and set attributes so lexbor escapes.
**Test:** such a page → `"onmouseover" not in prepared.html` and not in `localise(...)`.

### F5. [High] Embedded resources bypass scope, robots and address filtering (SSRF-shaped)
**Location:** `crawler.py:1003-1004`, `:1009-1064` (`_resources`), `:1036-1041` (CSS `url()`), `:1066-1076` (`_icon`).
**Category:** scope / robots / SSRF. **Confidence:** high.
**Evidence** — `one()` checks only `address in self.saved` and the budget before `fetcher.get(...)`: no
`in_scope()`, no `allowed()`, no host allow-list; `prepare` adds `("https://cdn.other/x.png","image")`
and CSS adds absolute `url(https://internal/…)` unchecked (`_offer` does check robots, `:836`).
**Impact:** path/page-scoped builds download whole CDNs; sites disallowing `/images/` or `/static/` are
crawled anyway (robots is cosmetic for assets); a page can make the server fetch
`http://127.0.0.1:8080/…`, `http://169.254.169.254/latest/meta-data/…`, RFC1918 hosts and store the
response in a shareable ZIM (exfil channel via the proxy or directly). **Trigger:** page embedding `/img/a.png` under `Disallow: /img/`, or `http://127.0.0.1/`.
**Fix:** gate every resource on `allowed()` + a policy (same-site by default, opt-in CDN hosts) and
reject non-public addresses for non-seed hosts.
**Test:** robots `Disallow: /img/` → not requested, reported skipped; loopback URL → rejected with note.

### F6. [High] Feed articles bypass robots/host setup and lose to sitemap pages under `max_pages`
**Location:** `crawler.py:769-793` (`_queue_from_feeds`), `:871-883` (`_run`).
**Category:** scope / robots / queue priority. **Confidence:** high.
**Evidence** — `await asyncio.gather(*self._sitemap_tasks)` fills the FIFO queue, and only afterwards
`for url in [*articles, *kept]: if ... self.allowed(url): queue.put_nowait(...)`. `allowed()` is permissive until `_meet()` has run (`:600-602`), and `_meet()` is only called from
`_offer_all` (`:817-822`) — a path this code skips.
**Impact:** (a) a subdomain article (`subdomains=True`) is fetched before its robots.txt is read, so
`Disallow` is ignored; (b) on a site whose sitemap fills `max_pages`, every feed article is queued
behind them and reported "over the page limit" — the articles the build was for are the ones dropped. **Trigger:** `max_pages=2`, sitemap with 3 pages, feed with 1 new article.
**Fix:** `await self._offer_all(articles, max_depth)`; enqueue feed articles before sitemap pages (or a
reserved budget slice).
**Test:** article survives the limit; disallowed subdomain article skipped with a reason.

### F7. [Medium] Charset handling ignores the HTTP header and any `<meta>` past 4 KiB
**Location:** `crawler.py:516-524`; callers `:660`, `:977`, `:1037`.
**Category:** encoding. **Confidence:** high — reproduced.
**Evidence** — `def _decode(data, content_type_header="")` searches `<meta…charset=…>` only in
`data[:4096]`, then tries that encoding and utf-8, then `utf-8/replace`. `content_type_header` is never
passed by any caller. Verified: `text/html; charset=iso-8859-1` with
`caf\xe9` → `caf�`; `<meta charset="windows-1252">` at offset 5000 ignored; no BOM/cp1252 fallback.
**Impact:** legacy/mis-declared sites are archived as U+FFFD permanently, in a distributed ZIM; titles
and snippets garble; long `<head>`s (the common case) exceed the 4 KiB window; UTF-16 is destroyed. **Trigger:** any ISO-8859-1/cp1252 page whose charset is in the header or late in the head.
**Fix:** header charset first, then BOM, then a larger meta scan, then cp1252→latin-1, then utf-8-replace.
**Test:** parametrised: header-only, meta >4096, BOM, cp1252 bytes, invalid utf-8.

### F8. [Medium] Blocking reads/decodes on the event loop; unbounded 403 body read
**Location:** `crawler.py:975-983`, `:1036-1037`, `:324-332`.
**Category:** performance / memory. **Confidence:** high.
**Evidence** — `prepare` is off-loaded but its input `_decode(fetched.file.read_bytes())` is evaluated
on the loop; `_challenged` does `(await reply.aread())[:40000]` *before* the size check (`:431-433`).
**Impact:** a 15 MB page or stylesheet blocks every worker fetch for the read+decode (throughput falls
to serial); a hostile 403 with a 1 GB body is fully read into RAM despite the per-file limit.
**Fix:** read+decode inside the existing `to_thread` lambda (`prepare` accepts bytes); in `_challenged`
read ≤40 KiB via `aiter_bytes` and cap by `content-length`.
**Test:** a `PAGE_LIMIT_BYTES` page (timing) and a big 403 body (bytes read bounded).

### F9. [Medium] Non-atomic install and fragile persistence in `finally`
**Location:** `writer.py:308-309`; `jobs.py:596-598` (`_adopt` copy); `crawler.py:222-226`.
**Category:** crash consistency. **Confidence:** high.
**Evidence** — `target.unlink(missing_ok=True)` then `temporary.replace(target)` (`writer.py:308-309`);
`os.replace` already overwrites atomically. `Fetcher.close()` writes `index.json` unguarded inside
`run()`'s `finally` — a read-only/full state dir turns a fully successful crawl into
"Unexpected error: …" (`jobs.py:331`); `.zim.tmp` left by SIGKILL is never cleaned
(`scanner.leftovers()` lists it as junk, `scan()` ignores it).
**Impact:** kill/ENOSPC in the window leaves `books` pointing at a missing file while a complete `.tmp`
sits in the library; a state-dir write failure masks success; upload copies double I/O for GB files.
**Fix:** drop the `unlink`; guard the index write; sweep stale `*.zim.tmp` in `scan()`; `os.replace` in
`_adopt` when on the same filesystem.
**Test:** patch `Path.replace` to raise → old file intact; read-only state dir after a crawl → success.

### F10. [Medium] Disk pre-flight under-estimates by ~the whole stage size
**Location:** `jobs.py:562-567`.
**Category:** disk-full. **Confidence:** high.
**Evidence** — `needed = stage.size + FREE_SPACE_MARGIN`, but writing needs `stage.size` **plus** the
new ZIM while the stage is on disk, so only 512 MB of slack is guaranteed.
**Impact:** large crawls (`max_total_mb=2048`) hit ENOSPC inside libzim after hours; message becomes
"The ZIM could not be written: …"; temp file may remain. The limits themselves are hard-coded
(`crawler.py:41,67-68`; never mapped from settings in `jobs.py:460-476`). **Trigger:** a stage near the free space minus 512 MB.
**Fix:** require `stage.size * 1.3 + margin`, or re-check inside the writer; expose the caps as settings.
**Test:** fake `disk_usage` = `stage.size + margin` → clear BuildError *before* writing.

### F11. [Medium] Importing a ready-made `.zim` deletes the upload, so the build can never be retried
**Location:** `jobs.py:530-535` + `:599-600`; consumer `:527-529`; `api/build.py:470-479`.
**Category:** uploads / path handling. **Confidence:** high.
**Evidence** — `_adopt(source, target, True)` (move) → `if move: made.unlink(...)` deletes
`uploads/<uuid>.zim` while `uploads.path` keeps pointing at it.
**Impact:** "Build again"/`retry()` fail with "The uploaded file is no longer on the server. Upload it
again." for a file the user never deleted; `delete_upload` no-ops. `_adopt` also `shutil.copyfile`s the
ZIM (2× space/time) instead of renaming. **Trigger:** upload a `.zim`, build twice.
**Fix:** keep the upload, or `os.replace` and rewrite `uploads.path` to the library copy.
**Test:** build twice; second succeeds and the upload row still resolves.

### F12. [Medium] Deleting a source mid-build is invisible to the build and leaves permanent orphans
**Location:** `api/build.py:179-185`, `:470-479`; `jobs.py:446-448`, `:524-529`, `:514-518`, `:551-554`.
**Category:** DB integrity / lifecycle. **Confidence:** high.
**Evidence** — `delete_link`/`delete_upload` run a bare `DELETE`: no cancel, no active-build check; the
build later runs `UPDATE links SET book_id=…` (0 rows); upload deletion unlinks a file a converter
may still read.
**Impact:** hours of wasted fetching; a ZIM/`books` row with no owning source; `builds` rows pointing at
a non-existent `source_id` forever (no FK); conversion fails with an opaque file error.
**Fix:** cancel active builds for the source on delete, refuse while queued/processing/paused, and sweep
orphan `builds` rows.
**Test:** enqueue, wait for `processing`, delete the link → cancelled; no unresolvable `source_id`.

### F13. [Medium] `enqueue`'s guard is a check-then-insert race; no constraint backs it
**Location:** `jobs.py:156-168`; `db.py:160-180`.
**Category:** DB integrity / concurrency. **Confidence:** high.
**Evidence** — `SELECT … WHERE kind=? AND source_id=? AND state IN (…)` then a separate `INSERT` (each
`db.execute` locks separately), endpoints are sync `def` (threadpool → concurrent); no partial unique
index, no CHECK on `state`/`kind`/`phase`.
**Impact:** two concurrent `POST /links/1/build` both pass → two active builds for one source → F1's
clobbering plus two `_active` entries.
**Fix:** partial unique index on `(kind, source_id) WHERE state IN ('queued','processing','paused')`,
CHECKs for state/kind, map IntegrityError to `BuildError`.
**Test:** `asyncio.gather` of two enqueues → exactly one succeeds.

### F14. [Medium] Sitemap parsing is O(n²) on hostile input and unbounded per host
**Location:** `sitemap.py:33-36, 60-77`; driver `crawler.py:706-725`.
**Category:** DoS (untrusted XML). **Confidence:** medium-high.
**Evidence** — `_INDEX_ENTRY = re.compile(rb"<(?:\\w+:)?sitemap\\b[^>]*>(.*?)</(?:\\w+:)?sitemap>", re.S | re.I)`
(and `_LOC`): a 20 MB body of unclosed `<sitemap`/`<loc` starts forces a scan to EOF per match; up to `MAX_FILES=50`
× 20 MB are read/parsed sequentially in `to_thread`, with no `report.check()` inside.
**Impact:** one malicious sitemap occupies a build thread for minutes–hours with no cancel point; memory
peak is the whole body (+60 MB gunzip output).
**Fix:** cap match counts, reject documents with excessive `<loc`/`<sitemap` counts, use
`ElementTree.iterparse` with early stop, `report.check()` between files.
**Test:** synthetic 20 MB unclosed-tag sitemap → bounded runtime, reported note, not a hang.

### F15. [Medium] A DB failure while recording leaves the build stuck `processing`, exception unretrieved
**Location:** `jobs.py:334-352` (`_active.pop` before `_record`), `:262` (no done callback).
**Category:** job state / recovery. **Confidence:** high.
**Evidence** — in `finally`: `stage.clear()`, `_active.pop(build_id, None)`, then an unguarded
`_record(...)`. If it raises (disk full, locked DB) the task ends with an unawaited exception and the
row stays `state='processing'`; `_run_queue` only starts `queued` rows.
**Impact:** the build shows "processing" forever, blocks new builds for that source (F13 guard), and
only a restart repairs it; the failure appears only as asyncio's GC warning.
**Fix:** wrap `_record` in try/except → best-effort `state='failed'`; add a task done-callback; pop
`_active` after recording.
**Test:** make the final UPDATE raise once → state `failed` (or `queued` after restart) and logged.

### F16. [Medium] Pause does nothing during "fetching files" (and the icon fetch)
**Location:** `crawler.py:1021`, `:1066-1076`; `report.py:37-45`.
**Category:** pause semantics. **Confidence:** high.
**Evidence** — `check()` only waits off the event loop, and `_resources.one()` is a coroutine calling
`check()`, not `gate()`; `pause()` refuses only for writing/verifying/zimit (`jobs.py:44-49`), so the
DB says `paused` while images/CSS/fonts keep downloading.
**Impact:** user believes the build is held; bandwidth/VPN use continues; resume is a no-op.
**Fix:** `await report.gate()` in `one()`/`_icon`, or gate the whole phase.
**Test:** pause during `_resources` with a slow transport → no further requests until resume.

### F17. [Medium] `max_total_mb` is not what it says
**Location:** `crawler.py:1011-1012, 1024-1029, 1044-1046, 1060-1063`.
**Category:** limits/accounting. **Confidence:** high.
**Evidence** — the budget is compared to `stage.size_estimate`, which only counts resources copied in
`_resources`; page HTML/text is never counted and CSS added at `:1060` bypasses it, so pages alone can
reach `max_pages * PAGE_LIMIT_BYTES`.
**Impact:** a 5 000-page crawl with `max_total_mb=2048` can stage far more than 2 GB while claiming to
stop at 2048 MB; the real limit is only enforced late by the disk check (F10).
**Fix:** count pages (`add_page` adds file + text sizes) and CSS; enforce per page too.
**Test:** 3 × 10 MB pages with `max_total_mb=15` → stops/notes at the budget.

### F18. [Low-Medium] URL identity gaps: port-blind dedupe, unordered query, `..`/quote paths
**Location:** `crawler.py:604-608` (`_key`), `:583-598` (`in_scope`); `extract.py:51-75`.
**Category:** frontier / dedupe / canonicalisation. **Confidence:** high.
**Evidence** — `_key` = netloc-without-www + path + "?" + query: scheme *and port* dropped, so
`https://h:8443/x` and `https://h/x` collide while `_same_site` compares full netloc and calls the second
out of scope (inverted); query params are unsorted (`?a=1&b=2` ≠ `?b=2&a=1`); `normalise` resolves no
`..` and encodes no path; `zim_path` maps `/a/` and `/a/index` to the same `h/a/index` (a resource can
then alias a page at `:1044-1047`).
**Impact:** duplicate pages (double fetch/entries, split links), false out-of-scope, odd entry paths
(`h/a/../b`) readers may or may not normalise.
**Fix:** include scheme/port policy in `_key`, sort query pairs, resolve dot segments, percent-encode
and escape paths, disambiguate `zim_path` collisions with a short hash.
**Test:** `_key` differs by port; `?a=1&b=2` ≡ `?b=2&a=1`; `/a/` and `/a/index` both survive.

### F19. [Low-Medium] `..` in EPUB/MOBI hrefs becomes a `..` ZIM entry path
**Location:** `epub.py:82, 111` (`normpath` + `unquote` then `f"book/{href}"`); `mobi.py:46-49`.
**Category:** path hygiene (no true zip-slip: nothing is extracted). **Confidence:** high.
**Evidence** — `unquote("%2e%2e%2f")` → `../`, kept by `normpath`; `stage.add_resource(f"book/../x", …)`
and `add_page` accept it (MOBI's containment check protects the *read*, not the name).
**Impact:** entries escape the `book/` namespace (`book/../index` can alias the front page in
`Stage.entries`); `localise`/`relative` then emit `../../` links.
**Fix:** reject `""`, `.`, `..` segments (or any path not under `book/`), report the item failed.
**Test:** EPUB item `href="%2e%2e%2fevil.xhtml"` → rejected, `report.failed`, no `..` in entries.

### F20. [Low] Reporting/persistence nits: JSON grows unchecked, log cap keeps the *first* 500, retry keeps stale fields, lists scan all builds
**Location:** `report.py:69-72`; `jobs.py:139-147, 215-223`; `api/build.py:135-141, 380-386`; `db.py:173-175.
**Category:** reporting / DB hygiene. **Confidence:** high.
**Evidence** — (a) `if len(self.log) < LOG_LIMIT` keeps the *first* entries, so a crawl that first
skips 500 pages stores no failure reasons while `failed_count` is exact, and `_run`'s last-error
message is then `""` (`crawler.py:932`); (b) `log`/`report`/`params` are schema-less TEXT read with bare
`json.loads`, so one bad row 500s `GET /api/build/jobs`; (c) `retry()` resets counters but not
`size`/`report`/`book_id`; (d) `_links`/`_uploads` `SELECT … FROM builds WHERE kind=?` with no LIMIT pick
the last per source in Python.
**Impact:** undiagnosable large builds; one blob takes the queue UI down; misleading rows; O(history)
query per Create-page load.
**Fix:** ring-buffer/log per kind; `CHECK (json_valid(...))` + defensive loads; reset the stale fields;
`MAX(id)`-per-source query plus an index on `(kind, source_id, id)`.
**Test:** 600 skips + 1 failure → failure present; bad JSON row → `list()` still works; retry → size 0.

### F21. [Low] Zimit: colliding container names, unchecked start, container leak on cancel
**Location:** `zimit.py:95-123`.
**Category:** orchestration. **Confidence:** medium (Docker semantics), high (paths).
**Evidence** — `name=f"zim-pro-zimit-{slugify(name,40)}-{os.getpid()}"`: two links slugging alike, or a
retry while a container survives SIGKILL, → 409; the `/start` response is ignored (failed start then
reads logs from a dead container → "no output"). `asyncio.shield(client.delete(...))` in `finally` runs
after cancellation while `async with _client()` closes the transport → delete can fail, leaving a
browser container running. `:latest` is pulled every build (no digest pin); `folder.chmod(0o777)`.
**Impact:** stuck containers burning CPU/RAM; confusing errors; non-reproducible captures.
**Fix:** uuid names, check start, delete on a dedicated client outside the `async with`, pin the digest.
**Test:** mock Docker returning 409/500/cancel → message asserted and delete attempted.

---

## Database integrity & normalization notes

- **Polymorphic `builds.source_id` with no discriminator enforcement** (`db.py:160-166`): `kind TEXT`,
  `source_id INTEGER`, no FK/trigger, no `CHECK (kind IN ('link','upload','feed'))`. `_mine` is built
  from the class constant (`jobs.py:92`, injection-safe), but an invalid `kind` is invisible to every
  manager, and `_build` routes anything non-`link` to `_build_upload` (`jobs.py:400-406`) → KeyError in
  `CONVERTERS[upload["kind"]]` (`:537`) or "has been removed".
- **Orphaning on delete:** deleting a link/upload/feed leaves its `builds` rows and `book_id` behind;
  `feed_items`/`feed_sources` also reference feeds with no FK (`db.py:202-228`). `clear_finished()`
  erases the last record of what a deleted source built.
- **`state`/`phase` unconstrained:** TEXT enums plus free-text phases. `start()` repairs
  `processing`→`queued` only for its own kinds (`jobs.py:105-111`) — the only crash recovery — and
  zeroes `phase`/`done` for every paused build on restart.
- **`book_id` linkage:** `builds`/`links`/`uploads`/`feeds.book_id` are plain TEXT, no FK to `books.id`,
  no uniqueness. Book rows vanish when a ZIM file is removed (`scanner._scan` deletes them), leaving
  dangling ids; conversely two sources can share one id (F1). The id *is* the filename stem.
- **JSON blobs:** `log` (≤500), `report`, `params` (`rss`, `trigger`, `rebuild`) are schema-less and
  drive branches: `jobs.py:349-352` *deletes* a finished build if `params["trigger"] == "schedule"`, and
  `:481-487` decides feeds-only from `params["rss"]`. A typo silently changes behaviour.
- **Triplicated source columns:** `links`/`uploads`/`feeds` each carry `title`, `language`, `notes`,
  `mode`, `images`, `video`, `rate_limit`, `rate_unit`, `category`, `icon`, `book_id`, `last_built`,
  `created`; every new knob needs three ALTERs (see `_migrate`, `db.py:314-318`), and `_name_for` unions
  all three by hand (`jobs.py:433-437`). No shared `sources` table or view.
  The icon is doubly stored too: `icons/<kind>-<id>.png` plus an `icon REAL` column in three tables
  (`icons.py:10,15` couples the kind string to the table name), and `category` is re-derived into
  `books.category` on every build/edit (`file_under_categories`).
- **Derived progress columns:** `done/total/failed/skipped` are end-of-run snapshots of `Reporter`;
  `failed` can include cancel noise (F3), `total` grows/shrinks with the frontier, and `start()` zeroes
  them. They are never updated live (only SSE snapshots are), so after a crash they are stale.
- **`position REAL NULL`** is compared against integer `id` via `COALESCE(position, id)`
  (`jobs.py:219, 274`), and `retry()` takes `MAX(position)` across *all* kinds, so a retried link can
  sort behind a feed. No `(state, position)` index.
- **`uploads.path`** is an absolute filesystem path (breaks on a state-dir move/restore), `uploads.size`
  is never refreshed, and the path dangles by design for `.zim` imports (F11).
- **Transactionality:** no build step uses a transaction (`isolation_level=None`, every `execute`
  autocommits). `books` is reconciled from disk by `scanner.scan()`, but `builds`/`links` have no
  reconciler, so a crash between "ZIM installed" and "`_record`/`UPDATE links`" leaves rows disagreeing
  with the library until the next build.

## Connectivity & failure handling

- **Timeouts:** `httpx.Timeout(30, connect=15, pool=10)` per request inside `asyncio.timeout(60)`,
  re-armed to `max(120 s, limit/50 KiB/s)` for the body (≈314 s at 15 MB). No overall build deadline: a
  site answering 1 page/min at `max_pages=5000` runs for days; only cancel ends it. The 60 s constant is
  mostly shadowed by the 30 s read timeout.
- **Retries/backoff:** 2 retries for 5xx/transport (`sleep 1.5·attempt`), 6 for 429/503-with-Retry-After
  (site's value capped at 300 s, then doubling), none for other 4xx/3xx/size limits. Retry-After parses
  seconds and dates and is unit-tested (`test_builder.py:313`).
- **Politeness:** adaptive per-host start spacing (0.12 s → 1–8 s), `Crawl-delay` honoured (cap 10 s)
  *only when no user rate is set* (`crawler.py:241-249`), global host pause on 429. Spacing serialises
  *starts* only: with 16 workers, 16 requests to one host can be in flight. A 300 s `_blocked` is paid
  once per queued URL in sequence (hours for 100 URLs on that host, other hosts unaffected).
- **Proxy/solver/TLS/DNS:** all surface as `httpx.HTTPError`/`OSError` → retried with the transport
  text; a proxy 407 has no "check the proxy" hint. `_renew_client()` (timeouts/pool exhaustion) is not
  locked and accumulates retired clients until `close()`; concurrent renewals can drop solver cookies.
  The solver is single-flight (90 s); a malformed solver response raises `ValueError`/`AttributeError`
  from inside the `_Challenge` handler → generic "Unexpected error".
- **Conditional requests:** ETag/Last-Modified per URL, 304 → reuse (tested). Cache files and
  `index.json` are never evicted; feeds-only `prefer_cache` serves validator-less pages indefinitely.
- **Cancel/hang:** cancel relies on `task.cancel()`; requests are bounded by timeouts, thread phases are
  not (F2), and `_challenged` can read an unbounded body (F8). `_wait_turn` sleeps while holding the
  per-host lock, delaying every queued URL for that host.
- **SSE/progress:** each active manager publishes the full 200-row list once a second plus on every
  change; slow clients drop ticks (queue max 256). Payloads are bounded, but `list()` runs a DB query per
  publish and `_links`/`_uploads` scan the whole history (F20).
- **Blocking I/O in async endpoints:** `POST /api/build/uploads` writes up to 2 GB (`upload_limit_mb`)
  with `out.write` on the event loop (`api/build.py:399-419`), stalling SSE and all requests for the
  upload duration. Use `aiofiles`/`to_thread(shutil.copyfileobj)` with a chunk callback.
- **Offline:** robots/sitemap failures degrade to allow-all; a build with zero saved pages ends with
  "The page could not be fetched" plus the last failure, and the feeds manifest is not written on
  failure, so retries lose `feed_tries` progress.

## Testing gaps

`tests/test_builder.py` (36 passing) covers: writer/verify/dedupe/icon/permissions, `prepare`
(clean/noise/media/images/fallback/canonical), `normalise`/`zim_path`/`relative`/`localise`, path-scope
crawl + accounting, page scope, conditional rebuild, page limit, 429 retry and exhaustion,
`_retry_after`, sitemap shapes/index/discovery ordering, subdomains + per-host robots, pause on
loop/thread, EPUB (chapters/cover/no-images/broken), PDF (pages/text/scan flag/broken), CSV parsing,
upload build happy path/retry/pause/restart/log JSON/scanner/icon. It asserts DB **and** filesystem state
in the build-queue tests and ZIM content in the writer/book tests. Untested:

- **Robots on assets (F5)** — the fixture disallows only a page path; no `Disallow: /img/`.
- **Private/off-scope resources (F5)** — no `http://127.0.0.1/` or metadata-IP case.
- **Feeds crawl path (F6)** — `_feed_articles`, `_queue_from_feeds`, `NothingNew`, `_write_manifest`,
  `feed_tries`, `_missing`, feeds-only `max_pages`: zero coverage (no crawler test sets `feed_urls`);
  `tests/fixtures/feed.xml` belongs to the feeds tests.
- **Cancel mid-run (F2/F3)** — `test_cancelled_build_stops_at_the_next_step` only pre-sets the flag.
- **Name collisions / concurrent builds (F1/F13)** — all manager tests run one build at a time.
- **Sitemap limits/hostile XML (F14)** — no `MAX_FILES`, `MAX_BYTES`, nested-limit, gzip-bomb or
  self-referencing index cases.
- **Zip-slip/`..` hrefs (F19)**, page-vs-resource path collisions, **charsets (F7)**, and **redirects**
  (chain, loop, seed redirect out of scope at `crawler.py:967`, redirect to a non-HTML payload).
- **Writer/libzim failures** (no `Creator` exception, `replace` failure or byte-mismatch verify) and
  **huge/slow responses (F8)** (declared/streamed overrun, slow-drip timeout, large 403).
- **Per-page failure threshold** — none exists (`crawler.py:931-933` fails only at zero pages) and no
  test documents that a 99 %-failing crawl still produces a ZIM.
- **Resource budget/CSS (F17)** — `max_total_mb`/`max_file_mb`, `@import`/`url()` rewriting,
  `css_resources`/`localise_css`, `_icon` order: no test at all (no CSS fixture page).
- **`api/build.py` endpoints** — only `_parse_csv` is tested; nothing for `POST/PUT/DELETE /links`,
  `POST /uploads/{id}/build`, upload signature/limit rejection, `DELETE` mid-build (F12), or
  `/jobs/{id}/{action}` cross-kind authorisation.
- **MOBI converter, zimit (mocked Docker), `icons.normalise` non-picture, `_adopt` (F11), `_name_for`,
  `retry` resets (F20)** — none. Fixtures: `tests/fixtures/` holds only `bash.meta4`/`feed.xml`; the
  builder tests synthesise EPUB/PDF in code (good isolation) but have no realistic corpus — legacy
  encodings, malformed OPF, EPUB3 `nav.xhtml` TOCs (only NCX is read, `epub.py:33-50`).

## What is done well

- **Staged build + verify before publish** (`writer.py:235-343`): `.tmp`, reopen with `Archive`,
  compare article count, every entry path and ~20 pages byte-for-byte, then install; SHA-1 content
  dedupe via aliases; correct `Hint.COMPRESS`; libzim fed from one thread.
- **Per-page failure isolation** with explicit saved/skipped/failed accounting and a clear terminal
  error when nothing was saved; **rebuild efficiency** via validators, 304 reuse and a per-link cache.
- **Robots/sitemap design:** rules once per host (www folded), robots-declared sitemaps beat guesses,
  section-local guesses, one queue holding each address once, sitemaps only for in-scope hosts.
- **Operational care:** free-space pre-flight with a human-readable permission error, per-build stage
  cleanup, restart recovery, pause/resume with named unpausable phases, queue re-ordering without
  renumbering, capped log with exact counters, `keep_old_versions`, history, category refiling.
- **Security-minded details:** metadata clamps; zimit via Docker JSON argv (no shell quoting); EPUB/MOBI
  read-containment; DRM reported. Tests are fast: `MockTransport`, no network, delays zeroed, with DB +
  filesystem + ZIM assertions.

## Unverified / out of scope

- Real Docker/zimit behaviour: log format vs. `_STATS` (`zimit.py:24`) — whether progress is ever
  parsed — `wait` after a `follow` log stream ends, and `VolumesFrom` path visibility were not exercised.
- libzim edge cases: `add_item`/`add_alias` with `..`, NULs or >255-byte segments; default
  `FRONT_ARTICLE` for aliases (the dedupe test implies False); `article_count` for redirects.
- `pypdfium2`/`mobi` errors beyond the constructor (encrypted pages, damaged page trees, AZW3/DRM
  wording, temp-dir layout) — no tests, not executed; `feeds/sync.py`/`feeds/parse.py` internals beyond
  the `BuildManager` seam; Redis broker failure modes; `ArchivePool` invalidation; web/SSE behaviour.
- All concurrency findings assume a single uvicorn process: with multiple processes the in-memory
  `_active`/`Reporter`/`_name_for` maps and `LocalBroker` would diverge (not investigated).
