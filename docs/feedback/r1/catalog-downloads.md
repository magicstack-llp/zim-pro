# Catalog & Downloads Review — r1

**Scope:** `catalog/opds.py`, `catalog/updates.py`, `downloads/engine.py`, `downloads/manager.py`,
`downloads/metalink.py`, `api/catalog.py`, `api/downloads.py`; `db.py`, `network.py`, `queues.py`,
`scheduler.py`, `scanner.py`, `api/system.py`, `api/library.py`, `events.py`, `config.py` for context;
tests `test_engine.py`, `test_units.py`, `test_network.py`, `test_system.py`.
**Commit:** `cbb1a4e` (0.5.0) · **Date:** 2026-11-14.
**Method:** full read and call-site tracing (scheduler, API, MCP, monitor, scanner); relevant tests run
(62 passed); throw-away repro scripts against `Database(":memory:")`, `httpx.MockTransport` and the real
`Download`/`DownloadManager`. No source modified.

## Summary

- Design is sound in outline: piece-granular resume, SHA-1 per piece + SHA-256 whole file, mirror health
  ranking, fsync-before-persist. Happy path, resume and mirror fallback work.
- No total deadline or minimum-speed guard in the engine, unlike the crawler; one trickling mirror hangs
  a download and a concurrency slot forever (read timeout only fires on a fully silent read).
- Verification can be skipped: `_verify_file` returns `True` with no SHA-256, and resumed pieces are
  trusted without re-hashing. A zero-filled `.part` with a stale bitmap is installed silently (repro).
- `_run_queue`/`_tick` have no exception guard; one DB/fsync error permanently stops scheduling or
  progress persistence until restart.
- No uniqueness/lock per target and dedupe by `catalog_uuid` only: two rows resolving to one filename
  truncate each other's `.part`, and one can `os.replace` the other's half-written file (row dup repro).
- DB intentionally simple but lacks `CHECK`/`FK`/`UNIQUE` on downloads; `book_id`/`replaces` are loose,
  unvalidated references and several transitions are non-atomic.

## Top 5 must-fix

1. **F1** — add a total deadline / min-speed abort; a trickling mirror hangs a slot indefinitely.
2. **F2** — guard/restart `_run_queue` and `_tick`; one exception disables all starts or persistence.
3. **F3** — enforce one in-flight download per target filename; two rows corrupt the installed ZIM.
4. **F4** — require SHA-256 and re-hash trusted pieces on resume; zeroed files pass as complete today.
5. **F5** — fix remove/cancel: DELETE during a run is a silent no-op; cancel during install leaves a
   half-state.

## Findings

### F1. [High] No total deadline or min-speed guard: a trickling mirror hangs forever
**Loc:** `downloads/engine.py:131-135`, `199-220`, `251-277` · **Cat:** connectivity · **Conf:** high
**Evidence:** `timeout=httpx.Timeout(30, read=60)` is per-read; `async for chunk in
response.aiter_bytes(CHUNK_BYTES)` (266) never times out if a byte arrives every <60 s, so
`_fetch_segment` never returns, `gather(*workers)` (207) has no deadline, and the stall counter
(200-220) is never reached. The crawler has exactly this guard (`builder/crawler.py:130-131`
`REQUEST_TIMEOUT`/`MIN_SPEED`, whole-body deadline at 436).
**Impact/Repro:** A single slow/stuck mirror occupies a slot and socket forever; row stays
`downloading`; monitor only logs "may be stuck" after 10 min (`monitor.py:316`). Repro: serve a `206`
and write 1 byte/50 s (pattern in `tests/test_network.py:215-238` proves the crawler aborts).
**Fix:** Wrap segment/round in `asyncio.timeout` with a size-aware budget; add a bytes/s floor (e.g.
20 KB/s over 60 s) raised as a mirror failure; cap total per-download time.
**Test:** Engine test with a trickling transport/local socket asserting a bounded-time failure.

### F2. [High] `_run_queue` and `_tick` are unguarded; one exception disables the manager
**Loc:** `downloads/manager.py:248-267`, `268-290`, `292-299`; `engine.py:159-162`
**Cat:** error handling · **Conf:** high (code), medium (fsync trigger)
**Evidence:** Both loops have no `try/except` around the body (`_run_queue` only wraps the wait in
`contextlib.suppress(TimeoutError)`). `_persist` awaits `to_thread(download.sync)` while `run()`'s
finally sets `self._fd=-1` and closes it (`engine.py:150-154`); `sync()` reads `_fd` (161), so a close
between check and `os.fsync` raises `EBADF`. SQLite `disk I/O error` (disk full) does the same.
**Impact/Repro:** Runner death = no new download ever starts again, silently. Ticker death = progress
not persisted, SSE progress frozen; a crash then loses all progress since the last persist. Repro:
monkeypatch one `_persist` UPDATE to raise and observe no further ticks.
**Fix:** Catch `Exception` per iteration, log, keep the task alive, and supervise/restart exited tasks;
make `sync()` safe against a concurrently closed fd.
**Test:** Manager test injecting one failing persist/wake and asserting both loops survive.

### F3. [High] No uniqueness or lock per target: concurrent downloads share one `.part`
**Loc:** `manager.py:122-129`, `313-323`, `391-404`; `db.py:62-80` · **Cat:** concurrency/DB
**Conf:** high
**Evidence:** Dedupe is by uuid only: `SELECT id FROM downloads WHERE catalog_uuid = ? AND state IN
(...)`. Two catalog rows with different uuids resolving to the same metalink name are both queued
(reproduced); both open `data_dir/<name>.part` (`engine.py:106,128`) and both `os.replace(part,target)`
(398). The second replace is `ENOENT`, or if it still holds the fd it keeps writing into the inode the
first just installed. Also reachable when a new release appears while an older one is queued: neither
`start_update` nor `start_all` checks `item["download"]` (`api/catalog.py:125-140`).
**Impact/Repro:** Corrupt/partial ZIM installed, one row fails as "not a valid ZIM", doubled bandwidth.
Repro: insert two catalog rows for `.../a.zim.meta4`, `enqueue` both (both accepted).
**Fix:** Partial unique index on active `filename` (or manager lock keyed by target); check by
`book_id`, not just uuid; refuse a target that is another row's file.
**Test:** Two rows → second rejected/coalesced; two `Download`s on one target must not corrupt.

### F4. [High] Verification can be skipped: no SHA-256 means valid; resumed pieces are trusted
**Loc:** `engine.py:291-302`, `304-313`; `manager.py:319`; `metalink.py:37-40,67-68`
**Cat:** integrity/resume · **Conf:** high (reproduced)
**Evidence:** `if not self.metalink.sha256: return True` (292-293). `metalink.parse` accepts a meta4
without `<hash type="sha-256">` and silently drops an inconsistent piece list (67-68). On resume
`Bitmap(count, pieces)` checks only the blob's byte length, and `pieces` is used whenever
`row["filename"] == filename` — no binding to URL/size/piece length/hashes.
**Impact/Repro:** A stale bitmap plus short/zeroed `.part` is reported complete and installed; library
serves a corrupt ZIM. Realistic after power loss (SQLite `synchronous=NORMAL`, `db.py:272-273`) or a
state-dir restore. Reproduced: `pieces=b"\x03"`, zero-filled correct-length part, meta4 with piece hashes
but no SHA-256 → `run()` returns OK, file stays zeros.
**Fix:** Reject meta4 without SHA-256; re-hash all "done" pieces before trusting a resume (or bind the
blob to a size/piece-length/first-hash fingerprint and discard on mismatch).
**Test:** `sha256=None` + zeroed part must fail/re-download; bitmap from another layout ignored.

### F5. [Medium] Remove/cancel state machine: DELETE during a run is a no-op; cancel during install leaves a half-state
**Loc:** `manager.py:175-194`, `301-369`; `api/downloads.py:58-61` · **Cat:** state machine
**Conf:** high (remove repro)
**Evidence:** `remove()` calls `cancel()` (task.cancel(), returns) then `DELETE ... WHERE state IN
('done','cancelled','failed')` while the row is still `downloading` → deletes nothing; the handler
still returns `{"ok": True}`. Verified: row survives. Cancel/pause can also land while `_download`
awaits `to_thread(self._install)` (342): the thread completes the verify/rename/old-file delete, then the
`CancelledError` branch writes `paused`/`cancelled` (347-356) and no history.
**Impact/Repro:** Misleading success; row survives as cancelled; library changed but user believes it
was cancelled; resume re-downloads everything and may duplicate history.
**Fix:** Cancel and await the task before deleting (or a `remove_after` flag); make the install section
non-cancellable and write state/history only after it returns.
**Test:** Slow mocked install → pause/delete → assert exactly one consistent outcome.

### F6. [Medium] Enqueuing an older release replaces and deletes a newer local file
**Loc:** `manager.py:130-150`, `391-404`; `api/downloads.py:22-29` · **Cat:** catalog semantics
**Conf:** high (code), medium (UI exposure)
**Evidence:** `if book and book["uuid"] == entry["uuid"]: raise ...` — no date comparison. Any older
catalog uuid is queued, `replaces` captures the local path, and `_install` unlinks it (400-403). The
Catalog page disables this for `local == "current"` (`web/src/pages/Catalog.tsx:129-141`), but
`POST /api/downloads` (admin guard is a no-op without `KIWIX_ADMIN_TOKEN`), MCP and stale clients are
not guarded.
**Impact/Repro:** Silent downgrade; the newer release may be gone from the catalog later, so
unrecoverable. Repro: local book 2026-02, enqueue catalog uuid from 2026-01 → newer file deleted.
**Fix:** Refuse/warn when the local book is newer; re-read the local book at install and abort if its
uuid/date changed since enqueue.
**Test:** enqueue-older test; manager install test with a changed local book.

### F7. [Medium] Range responses are not validated (Content-Range, length, 416, encoding)
**Loc:** `engine.py:256-262`, `266-277` · **Cat:** HTTP correctness/resume · **Conf:** high code,
medium trigger
**Evidence:** Only `status_code == 206` or a whole-file `200` for `start==0 and end==size-1` is checked;
no `Content-Range`, no expected-length check, `416` becomes a generic mirror failure, and no
`Accept-Encoding: identity`. httpx advertises gzip and decodes, so a compressing CDN makes the requested
range refer to compressed bytes while decoded bytes are written at uncompressed offsets.
**Impact/Repro:** Misbehaving proxies/CDNs are retried on every mirror then reported as "every mirror
failed repeatedly"; wrong-offset data is caught only by hashes (and not at all without piece hashes,
F4). The test mirror returns `206` without `Content-Range` (`test_engine.py:27-36`), so the gap is
institutionalised.
**Fix:** Require `206`, validate `bytes a-b/total` against the request/size, handle `416` with the
`*/N` total, send `Accept-Encoding: identity`, verify the received length.
**Test:** Mismatched `Content-Range`, `416`, ignored Range on non-zero start, short `206`.

### F8. [Medium] Disk/permission `OSError` is treated as a mirror failure
**Loc:** `engine.py:243-246`; `manager.py:381-389` · **Cat:** error handling/disk · **Conf:** high
**Evidence:** `except (httpx.HTTPError, DownloadError, OSError)` increments `mirror.failures` and
sleeps; `os.pwrite` `ENOSPC`/`EACCES` is retried on every mirror and round, then reported as "every
mirror failed repeatedly". Free space is checked once (`size - allocated + 1 GiB`) and two concurrent
downloads can both pass it.
**Impact/Repro:** Minutes of pointless retries, misleading error, large failed `.part` left behind,
operators cannot distinguish disk-full from network. Repro: small tmpfs + a file larger than free.
**Fix:** Handle `OSError` separately and abort on `ENOSPC`/`EDQUOT`/`EACCES` with a clear message and
optional part cleanup; re-check space per segment; serialize the check/reservation.
**Test:** Patch pwrite to raise `ENOSPC`; assert fast specific failure and no mirror penalty.

### F9. [Medium] Metalink parsing is brittle and silently weakens integrity
**Loc:** `metalink.py:27-69` · **Cat:** parsing · **Conf:** high (`length="0"` repro)
**Evidence:** `piece_length = int(pieces.get("length") or DEFAULT)` (45) with `size // piece_length`
(19) → `ZeroDivisionError` on `length="0"` (reproduced). `int()` on size/priority raises `ValueError`;
`ET.fromstring` raises `ParseError`; both surface as raw messages in `downloads.error`. A piece list
whose count disagrees is silently dropped (67-68), leaving only the whole-file hash. Only the first
`<file>` is read.
**Impact/Repro:** Malformed/hostile meta4 (or an HTML 200 page) gives an unhelpful failed row; silent
verification downgrade defeats F4's fix.
**Fix:** Validate `size>0`, `piece_length>0`, hash syntax and piece count; raise a `ValueError("invalid
metalink: …")`; reject rather than degrade when a piece list is inconsistent; select by file/name.
**Test:** Parametrised malformed meta4: zero length, non-numeric size, empty file, HTML body, two files.

### F10. [Medium] OPDS: acquisition href not resolved; missing length → size 0; multiple links last-wins
**Loc:** `opds.py:43-51`, `69` · **Cat:** parsing/normalization · **Conf:** high (relative-href repro)
**Evidence:** The thumbnail is `urljoin(base_url, href)` but the acquisition link is stored verbatim
(46). Reproduced: `href="/zim/x.zim.meta4"` stays relative and later fails in `_fetch_metalink`.
`_int(None)==0`, so a missing/short `length` becomes 0 (used for `update_bytes`, sorting, UI). No
`type`/`.meta4` filter: a `.torrent` link under the same rel (last in document order) is stored and
`_fetch_metalink` appends `.meta4` to it (372-373).
**Impact/Repro:** Valid entries undownloadable; wrong sizes and disk guidance. Note the fixture itself
has catalog `length=572416` vs metalink `size=572117`. Repro: relative-href entry.
**Fix:** `urljoin`; require a sensible length or mark unknown; accept only `application/x-zim` / href
ending `.meta4`; log duplicates.
**Test:** Relative href, missing length, ZIM + torrent links in one entry.

### F11. [Medium] Catalog refresh destroys everything; empty UUIDs collide; partial feeds accepted
**Loc:** `opds.py:54`, `76-90`, `93-102`; `scheduler.py:15-27`; `api/catalog.py:94-103`
**Cat:** DB/refresh · **Conf:** high (collision repro), medium (partial feeds)
**Evidence:** `DELETE FROM catalog` then `INSERT OR REPLACE` keyed by uuid. Two entries without `<id>`
both become `""`; reproduced: only the last survives. Duplicate uuids silently overwrite URL/size. Only
an empty-result guard exists (99-100); a truncated 200 (server cap, proxy page) is accepted and every
missing book becomes `unpublished`. No ETag/Last-Modified, no `totalResults` check, and scheduler +
API refresh can run concurrently.
**Impact/Repro:** Lost entries until the next successful refresh (24 h, or 1 h after failure); wrong
update/unpublished status; duplicate full downloads.
**Fix:** Reject/synthesise id-less entries; stage the new catalog and swap only on count sanity; send
conditional GETs; serialize refreshes with a lock.
**Test:** `store` duplicate/empty uuid, rollback preserves old catalog, partial-feed guard.

### F12. [Medium] Update comparison and auto-update semantics have dead spots
**Loc:** `catalog/updates.py:5,8-9,24-31,44-55`; `scheduler.py:30-39` · **Cat:** version logic
**Conf:** high
**Evidence:** `ACTIVE_STATES` includes `failed`, and `queue_auto_updates` skips any `item["download"]`
(scheduler:34), so a failed update is never auto-retried. With no catalog loaded (fresh offline install
or failed first refresh) `latest is None` → every book is `unpublished`, not "unknown". Comparison is
string-based: `latest["issued"][:10] > book["date"][:10]` (52) ignores same-day repacks/new uuids, and
month-only local dates behave asymmetrically. `_latest` ties follow feed order; `issued` falls back to
Atom `<updated>` (`opds.py:67`), i.e. entry-update time, not release date.
**Impact/Repro:** Misleading status offline, auto-updates disabled after one failure, missed same-day
fixes, nondeterministic "latest".
**Fix:** Distinguish no-catalog from unpublished; exclude `failed` from the in-flight check with
backoff; parse dates (date/letter suffix) and compare properly; trust only `dc:issued`.
**Test:** `book_updates` over empty catalog, same-day repack, failed download, month dates, no `issued`.

### F13. [Medium] Downloads schema has no constraints and loose references
**Loc:** `db.py:62-80`; `manager.py:117-153,235-247,305-369` · **Cat:** DB integrity · **Conf:** high
**Evidence/notes:**
- No `CHECK` on `state`; `_set_state` accepts any `active.stop_state` (353). No FK: `catalog_uuid`
  dangles after each `opds.store`; `book_id` is a real id *or* a catalog stem placeholder (142) with no
  marker; `history.book_id` likewise. Deleting a book (`api/library.py:95-102`) leaves references.
- `replaces` is an absolute path captured at enqueue; `_install` unlinks it later (399-403), so a
  changed local book can cause the wrong file to be deleted; only exact equality with the new target is
  guarded.
- `resume()` leaves `finished` set (verified) and `_set_state('downloading')` resets `started`, so a
  resumed job looks finished while running and metrics mix wait/run time.
- Timestamps mix `unixepoch()` (schema, `_persist`) with `unixepoch('subsec')` (`_set_state`).
- `position` is `INTEGER` but `new_place` writes midpoints; verified `1.5` stored as REAL. Ties break
  `id DESC` in `list()` (93-98) vs `id ASC` in the broker (`queues.py:41`).
- `pieces` is an unversioned blob with no fingerprint; identical byte length from another release is
  accepted.
- Non-atomic transitions: filename/size (320-323), verifying (337-340), install (342), done (343),
  history (345) — each crash window yields a different inconsistency.
**Impact:** Stuck states, orphan/stale refs, wrong deletes, duplicate downloads/history after crashes.
**Fix:** `CHECK`/`UNIQUE` indexes and cleanup on refresh/delete; `replaces` as book id + path snapshot
revalidated at install; clear `finished` in `resume`; one timestamp unit; `position REAL`; bitmap
fingerprint; transaction per transition + history.
**Test:** resume clears `finished`; delete book with active download; refresh with queued download;
raise between transitions to simulate crash points.

### F14. [Low-Medium] Success-path bookkeeping error flips a completed download to failed
**Loc:** `manager.py:337-366` · **Cat:** error handling/state · **Conf:** high
**Evidence:** `_set_state(done)`, log, `_history(...)`, `events.publish(...)` are all inside the same
`try`; the `except Exception` branch then calls `_set_state('failed')` and writes a failure history
(357-366). If the history INSERT or publish raises (DB I/O, schema), the installed ZIM is marked failed.
**Impact/Repro:** Library updated but queue says failed; retry re-downloads and duplicates history.
Repro: make the history INSERT raise once.
**Fix:** Narrow the try to download/install; write state+history in one guarded transaction.
**Test:** Failing history INSERT → state stays `done` (with a logged warning).

### F15. [Low] Catalog API: unescaped LIKE, silent cap, empty-date comparison
**Loc:** `api/catalog.py:35-51`, `61-70`, `117` · **Cat:** API · **Conf:** high
**Evidence:** `LIKE f"%{word}%"` with no `ESCAPE` (a `%` query matches all); `count` silently clamped
to 200 (`max(1,min(count,200))`), so a client paging with 500 can conclude it is done; `book["date"]
[:10] >= row["issued"][:10]` marks an unreadable local book (`date=""`, different uuid) outdated for
every entry; `update_bytes` sums catalog `size`, which can differ from the real ZIM (fixture 572416 vs
572117).
**Impact/Repro:** Search surprises, pagination bugs, wrong badges, wrong disk estimates. `GET
/api/catalog?q=%25` returns everything.
**Fix:** Escape wildcards (or FTS); document/return the effective limit; parse dates with uuid
fallback; use metalink size once known.
**Test:** Wildcard query, paging limit, `local` status with empty dates.

### F16. [Low] Download API: delete always reports success; action can return `{}`
**Loc:** `api/downloads.py:22-29`, `38-61` · **Cat:** API · **Conf:** high
**Evidence:** `delete_download` ignores whether `remove` deleted anything and returns `{"ok": true}`
(for a nonexistent id too); `act` reads the row before the action and returns `{}` if it vanished
between action and read; `create_download` exposes internal row fields including `replaces`.
**Impact/Repro:** Silent no-op deletes and misleading contracts for UI/CLI/MCP. `DELETE
/api/downloads/999999` → `{"ok": true}`.
**Fix:** 404 when absent, 409 when it cannot be removed in its state, return the resulting row.
**Test:** Delete nonexistent/active; action on a vanished row.

### F17. [Low] Catalog/downloads bypass the proxy and have no retry policy
**Loc:** `engine.py:131-135`; `manager.py:371-379`; `opds.py:93-100`; `network.py:56-59`
**Cat:** connectivity/config · **Conf:** high code, medium intent
**Evidence:** None of the three clients passes `proxy=`/`transport=`; `Network.proxy` is used by the
crawler only ("how builds and feeds reach the web", `network.py:1`). `_fetch_metalink` has one 60 s
attempt; `opds.refresh` one 180 s total attempt; neither sends conditional headers, and `opds` uses the
httpx default UA. `_fetch_metalink` appends `.meta4` whenever the URL does not end with it, so a URL
with a query becomes `...meta4?x=1.meta4` (372-373).
**Impact/Repro:** On a network requiring the VPN/proxy, builds work but catalog/ZIM downloads fail; big
catalogs/slow links fail at 180 s; transient meta4 5xx has no retry; some mirrors reject the UA.
**Fix:** Pass the configured proxy (if enabled) or document build-only; bounded retries with backoff;
separate connect/read timeouts; strip query before suffixing; use `USER_AGENT`.
**Test:** Client-construction asserts the proxy; meta4 retry test on a transient 503.

### F18. [Low/Nit] Rate limiting and bulk publishing inefficiencies
**Loc:** `engine.py:44-53,220`; `manager.py:217-227` · **Cat:** performance · **Conf:** high
**Evidence:** `RateLimiter.take` sleeps while holding `self._lock`, serialising all connections behind
one sleeper; limiting happens after bytes arrive (TCP backpressure only), so rates are bursty. The
retry loop sleeps `min(60, 2**stalled)` even when `stalled == 0` (1 s between productive rounds).
`retry_failed`/`cancel_waiting` call `_changed()` per row, each publishing a full `list()` query/SSE
payload (O(n²) on mass retry).
**Impact/Repro:** Inaccurate bandwidth, added latency, heavy DB/SSE work on bulk actions.
**Fix:** Compute the sleep outside the lock or use a shared deadline; skip sleep when progress was
made; batch publish after bulk operations.
**Test:** Concurrent `RateLimiter` throughput; one publish per bulk call.

## Database integrity & normalization notes

- **Missing constraints:** no `CHECK (state …)`, no `UNIQUE` on active `catalog_uuid`/`filename`, no
  foreign keys (`downloads.catalog_uuid`, `book_id`, `history.book_id`). UUID casing/prefix
  (`opds.py:54`, `manager.py:141-143`) is the only join key.
- **Duplication is deliberate:** `catalog` and `downloads` both hold title/url/size/dates so an
  in-flight download survives a catalog refresh (good), but nothing reconciles them: `downloads.size` is
  catalog size until the metalink arrives, then metalink size; the catalog row is never corrected.
- **Loose refs:** `book_id` is a real id or a stem placeholder; `replaces` is a path snapshot. Both go
  stale on rename/delete/refresh. Catalog refresh wipes `catalog_uuid` references; operationally fine
  (URL cached) but invisible to `/updates` and the UI.
- **State machine:** `queued→downloading→verifying→done|failed|cancelled|paused`; `start()` resets only
  `downloading`/`verifying` (good), but a row stuck in `downloading` without a task can be neither
  paused (only `queued`), resumed (only `paused`/`failed`) nor deleted (F5) — restart is the only fix.
- **Pieces:** persisted after fsync in `_persist` (correct) but *without* fsync in the cancellation
  handler (349-352); `bytes_done` is set to full size while `verifying` and on failure, so failed rows
  can show 100%.
- **Queue position:** INTEGER holding REAL midpoints; tie-break mismatch UI vs broker; enqueue
  `MAX(position)+1` ignores Redis-held scores (fallback only).
- **Time:** mixed epoch precision; `resume` keeps `finished` and resets `started`, so `api/system.py`
  "waited"/"run" metrics are skewed.

## Connectivity & failure handling

- **Timeouts:** engine 30 s connect/write/pool, 60 s per read, no total; metalink 60 s total; catalog
  180 s total; no separate connect budget, no idle watchdog (F1).
- **Retries:** per-piece/segment rounds, per-mirror failure counts, 1-10 s sleeps, max 6 stalled rounds
  (~2 min backoff) — good. No retry for meta4/catalog GETs, no 429/`Retry-After` handling (F17).
- **Resume:** piece-granular, verified, can change mirror; correct for truncation and short reads;
  unsafe without SHA-256 or with a stale bitmap (F4). No `If-Range`/ETag, so a file changed between
  attempts is caught only via fresh piece hashes or the final SHA (whole-file cost).
- **HTTP:** `206` or whole-file `200` only; no Content-Range/416/identity-encoding handling (F7);
  redirects followed; HTTP/2 off; one client per download (no cross-download reuse).
- **DNS/TLS:** surface as `httpx.HTTPError` → mirror failure → generic final error; details only at
  INFO log level.
- **Offline:** refresh failure keeps the cached catalog and retries hourly (good); a fresh install with
  no catalog reports every book `unpublished` (F12).
- **Refresh:** full feed every `catalog_refresh_hours` (24 h default), no conditional GET, no lock
  between scheduler/API, destructive replace (F11).
- **Disk:** one free-space check with 1 GiB margin and sparse accounting; ENOSPC retried as mirror
  failure (F8); failed `.part` is kept.
- **Proxy/TLS config:** not applied to catalog/downloads; only the crawler has proxy/captcha/pool
  renewal (F17).

## Testing gaps

- `test_engine.py`: only engine coverage — happy path (53-65), resume after a truncated body (68-83),
  corrupt mirror (86-92), stale bitmap caught by final SHA (95-104), missing part (107-114). `Mirror`
  (27-36) always returns `206` with no `Content-Range`, so F7 is untested by construction.
- Engine untested: `416`, Content-Range/length mismatch, `200` ignoring Range on a non-zero start,
  redirects, TLS/DNS failure, trickle/stall (F1), cancellation/pause mid-piece, `RateLimiter`, ENOSPC/
  EACCES (F8), meta4 without SHA-256 (F4), malformed meta4/zero piece length (F9), two downloads on one
  target (F3).
- `test_units.py`: metalink happy + missing mirrors (49-66), Bitmap round-trip (69-76), OPDS happy
  fixture + one update case (97-123). Missing: id-less collisions, relative hrefs, missing length,
  multiple acquisition links, `store` rollback/old-catalog preservation, empty feed, facets/sorts, most
  `book_updates` edge cases.
- `test_system.py`: queue tests are **builds-only** (220-238); the app fixture starts the download
  manager but never enqueues a download. No `/api/downloads` or `/api/catalog` tests, no startup
  recovery test (`downloading`/`verifying` → `queued`), no stuck-row/delete-while-active (F5), no
  duplicate enqueue (F3), no SSE payload assertion.
- `test_network.py` is crawler/VPN only; its trickle test (215-238) is exactly the guard the engine
  lacks, which is why F1 stands out.
- Failure paths use `MockTransport` (fast/deterministic) but there is no real-socket test for
  range/resume/TLS/redirect/truncated streams.

## What is done well

- `opds.store` wraps DELETE+INSERT in one transaction with rollback (`opds.py:76-90`), so an insert
  failure cannot leave an empty catalog after a successful parse.
- Piece SHA-1 + whole-file SHA-256, mirror health ranking, `MIRROR_MAX_FAILURES`, stall rounds: a solid
  integrity/availability baseline.
- Persist-then-fsync ordering in `_persist` is correct; `run()` fsyncs before close; cancellation keeps
  the bitmap, so verified pieces are not re-fetched.
- `_install` opens the `.part` with libzim before replacing, uses atomic `os.replace`, invalidates the
  pool and removes old sidecars; `start()` resets interrupted states; catalog cached in SQLite for
  offline starts.
- Dedupe of repeated uuid enqueue, resumable paused/failed rows, cancel of queued rows removing `.part`,
  queue pause/concurrency settings are coherent.
- OPDS uses correct Atom/DC namespaces with `dc:issued` → `updated` fallback; updates skip locally made
  ZIMs (links/feeds/uploads) and custom books.

## Unverified / out of scope

- Whether the real Kiwix feed emits relative acquisition hrefs, multiple acquisition links, or entries
  without `<id>` (F10/F11 are spec/robustness findings; the fixture is clean).
- How often Kiwix republishes the same filename with a new UUID/date (F3/F6 trigger frequency).
- Multi-process deployments sharing `state_dir`/SQLite (duplicate rows and refresh races would be much
  more likely; the app is designed as one process).
- Product intent for proxying catalog/downloads (F17); FIPS hosts where `hashlib.sha1()` needs
  `usedforsecurity=False`.
- libzim behaviour on truncated/renamed files, directory-fsync durability, Redis ordering under external
  mutation beyond what was read in `queues.py`; no browser run of the UI beyond reading
  `Catalog.tsx`/`Downloads.tsx`.
