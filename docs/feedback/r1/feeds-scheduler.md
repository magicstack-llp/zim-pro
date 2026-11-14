# Feeds & Scheduler Review — r1

**Scope:** `src/zim_pro/feeds/{manager,parse,sync}.py`, `src/zim_pro/scheduler.py`, `src/zim_pro/api/feeds.py`, `tests/test_feeds.py`; with `db.py`, `downloads/manager.py`, `queues.py`, `builder/jobs.py`, `builder/crawler.py`, `builder/extract.py`, `builder/writer.py`, `config.py` read for context.
**Commit:** `cbb1a4e` (0.5.0). **Date:** 2026-11-14.
**Method:** full read of every in-scope file, call-site tracing into crawler/fetcher/writer/db, plus probes against the real code with the repo venv (`pytest tests/test_feeds.py` → 14 passed; ad-hoc scripts in `/tmp`, no source changed). Findings marked *verified* were reproduced, not just read.

## Summary

- The incremental core (per-collection store, "rewrite the ZIM from disk") is sound and well tested for the happy path.
- The worst defects are **irreversible pruning** (`keep`), **uncaught parser exceptions**, and **bookkeeping that hides failure or leaves work running after a sync is recorded failed**.
- Parsing tolerates encodings, timezones, Atom and relative links (verified), but one malformed URL aborts the entire sync and 500s the OPML/`check` endpoints.
- DB integrity is by convention only: no FKs, no `CHECK` on `feed_items.state`, no transactions, and a TOCTOU "already building" guard.
- `Fetcher` connectivity (ETag/304, 429 + `Retry-After`, per-host pacing, byte caps, proxy/solver/VPN) is genuinely good; feed sync serialises sources in front of it.
- Coverage is strong for sync outcomes and ZIM contents, near-zero for the scheduler, delete/edit/retry/check APIs, 304s, malformed input, and `dropped`/`skipped` semantics.

## Top 5 must-fix

1. **F1** — a malformed URL in a feed entry or OPML `xmlUrl` raises `ValueError` past every handler: sync dies, later sources are skipped, `feeds.last_error` is not written, OPML import and `/check` return 500.
2. **F2** — `keep`-pruned articles are marked `dropped` with `content=''` and can never be revived, even after `keep` is raised (verified).
3. **F7** — an unexpected per-article exception escapes `asyncio.gather` without cancelling siblings: the sync is recorded failed while other articles keep writing.
4. **F5** — `enqueue`'s busy check and insert are two statements with no unique index, so two syncs for one collection can start.
5. **F6** — feed deletion only guards against builds already queued/running, and there is no `ON DELETE CASCADE`: a sync starting a moment later writes orphan `feed_items` and a ZIM for a deleted collection.

## Findings

### F1. [High] One malformed URL kills the whole feed sync and 500s the OPML/check endpoints
**Location:** `feeds/sync.py:133-137`, `feeds/parse.py:51-52,80,99`, `api/feeds.py:236-240,294-301`. **Category:** bugs/errors. **Confidence:** high (**verified**).

```python
parsed = await asyncio.to_thread(parse.parse, data, fetched.url)   # sync.py:133
except (FetchError, parse.NotAFeed) as exc:                        # 135 — ValueError not caught
```

`parse.parse` → `extract.normalise` → `urlsplit`, which raises on a malformed authority. Verified: `parse.parse(b'…<link>http://[bad/</link>…')` → `ValueError: Invalid IPv6 URL`; same for `read_opml('<… xmlUrl="http://[bad/"/>')` and `discover(… href="http://[bad/")`.

**Impact:** the exception leaves `_check_sources` before the `last_checked`/`last_error` write (sync.py:138), so later sources are not checked, `feed_sources.last_error` and `feeds.last_error` stay unset (manager.py:105 catches only `BuildError`), and the build says `Unexpected error: Invalid IPv6 URL`. `/api/feeds/opml` returns 500 instead of 422; `/api/feeds/check` returns 500.
**Trigger:** any entry with a bad `<link>`/permalink guid, or one bad `xmlUrl` in an old OPML export.
**Fix:** make `normalise` total (`try/except ValueError: return ""`); widen the per-source `except` to include `ValueError`/`OSError`; map `ValueError` to 422 in `_opml_folders` and `_look`.
**Test to add:** feed whose second entry has `http://[bad/` — first entry ingested, sync finishes, `feed_sources.last_error` null; same file posted to `/opml` → 422.

### F2. [High] Pruned articles are permanently lost; raising `keep` never brings them back
**Location:** `feeds/sync.py:182-197,292-307,161-175`; `db.py:222`. **Category:** bugs/data loss. **Confidence:** high (**verified**).

```python
wanted = "state = 'new' OR (state = 'failed' AND tries < 3)"                       # 184
self.db.execute("UPDATE feed_items SET state = 'dropped', content = '' WHERE id=?") # 194
```

Nothing moves an item out of `dropped`, and `_remember` re-inserts with `INSERT OR IGNORE` (a no-op for an existing guid). Probe (5 items, `keep=2`, then `keep=0`): states stay `{'d':'saved','c':'dropped','b':'dropped','a':'dropped'}`, dropped `content` is `''`.

**Impact:** `keep` is a one-way door that also destroys the feed's own HTML, so history can only be recovered by deleting and re-adding the collection.
**Trigger:** `keep=10` on a 100-item feed, then `keep=0`/`keep=100`; the ZIM stays at 10.
**Fix:** let `dropped` be re-admitted when the collection holds fewer than `keep` saved items (and on `rebuild`); `DELETE` never-fetched excess rows instead of marking them dropped, and keep `content` for retired ones.
**Test to add:** the probe, asserting the article count grows after `keep` is raised.

### F3. [High] The `keep` pre-prune ignores what is already saved, and `dropped` is under-reported
**Location:** `feeds/sync.py:189-197,292-307,117,365-366`. **Category:** bugs/performance. **Confidence:** high (**verified**).

```python
if feed["keep"] and len(pending) > feed["keep"]:     # 189 — pending vs keep, saved count ignored
    for item in pending[feed["keep"]:]: ...
```

`_retire_old` then drops `saved - keep` oldest, and items dropped in `_fetch_articles` never touch `self.dropped`, which is **assigned** only at line 307. Probe with `keep=2`, 5 pending: 3 dropped, report `'dropped': 0`. With 20 saved + 20 pending, all 20 are fetched and written before 20 are retired.

**Impact:** wasted bandwidth/CPU/disk and ZIM writes for articles that cannot appear; wrong report/UI counts; `self.dropped` also gates `_prune_cache` (365), so a dropped batch's cache can escape pruning.
**Fix:** `room = keep - saved_count`; take `pending[:room]`; use `self.dropped += len(...)` in both places.
**Test to add:** keep=2 with 3 saved + 3 new — only 2 fetched, `report["dropped"]` equals the number transitioned.

### F4. [High] The ZIM's language is re-derived every sync and flips when the declaring source fails
**Location:** `feeds/sync.py:157-158,372`; `builder/writer.py:47-52`. **Category:** normalization. **Confidence:** high (**verified**).

```python
if not self.feed["language"] and parsed.language:
    self.feed["language"] = parsed.language      # 157 — in-memory only, no UPDATE
...
language=language_code(feed["language"]),         # 372 — "" becomes "eng"
```

`feeds.language` stays `''` (probe confirmed). Two sources, first declares `fr`: first sync writes `fra`; the French source then 503s while the other yields a new article → language becomes `eng`.

**Impact:** a transient failure changes the ZIM language (and `config_indexing` language, writer.py:260); with several sources the language depends on `feed_sources.id` order, not on user intent.
**Fix:** persist the discovered language like the title is persisted (sync.py:154-156), and prefer an explicit `feeds.language`.
**Test to add:** the two-source probe, asserting the language is stable across the failing sync.

### F5. [Medium] `enqueue`'s "already building" check is not atomic
**Location:** `builder/jobs.py:156-166`; `db.py:160-180,264,337-339`; `api/feeds.py:168-184`. **Category:** DB/concurrency. **Confidence:** high (code), medium (likelihood).

`SELECT busy` → `INSERT` with no transaction (`isolation_level=None`), no unique index on `(kind, source_id)` for active states, and the feed endpoints are `def` so FastAPI runs them in a thread pool while `_schedule` runs on the loop.
**Impact:** two `FeedSync` instances for one `feed_id`: both write `state/feeds/<id>/items/*.json`, both build the same `*.zim` (unguarded `_write`), and `_retire_old`/`_prune_cache` can delete each other's files. With >1 uvicorn worker the in-process `_active` map protects nothing.
**Trigger:** double-click "Sync now", or the scheduler (30 s poll) racing the UI.
**Fix:** `INSERT … SELECT … WHERE NOT EXISTS`, or `CREATE UNIQUE INDEX builds_active ON builds(kind, source_id) WHERE state IN ('queued','processing','paused')`, mapping `IntegrityError` to `BuildError`.
**Test to add:** two concurrent `feeds.sync(1)` from threads → exactly one builds row.

### F6. [Medium] Deleting a collection races with the scheduler; no FK/CASCADE leaves orphans
**Location:** `api/feeds.py:137-153`; `feeds/sync.py:161-175,284`; `feeds/manager.py:108-112`; `db.py:202-228`. **Category:** DB integrity. **Confidence:** high (code), medium (window).

The busy check and the deletes are separate statements, and `sync()` can insert a build between them. `PRAGMA foreign_key_list(feed_sources)` and `(feed_items)` are both empty (verified) although `foreign_keys=ON`. A sync that already loaded `feed` (manager.py:77) then inserts `feed_items` for a nonexistent `feed_id`; `forget()` removes the store under it, so `write_text` (sync.py:284) raises, which also makes `fetcher.close()` raise while writing `index.json` (sync.py:88), masking the original error.
**Impact:** orphan `feed_items`/`builds` rows no delete path can reach, a ZIM written for a deleted collection, confusing failures.
**Fix:** add the FKs (`feed_sources.feed_id` and `feed_items.feed_id` → `feeds ON DELETE CASCADE`, `feed_items.source_id` → `feed_sources ON DELETE SET NULL`); delete inside one transaction with a tombstone or re-check; make the sync tolerate a vanished feed row at every write.
**Test to add:** delete while the feed response is held by an event → no `feed_items` for that id, no store directory, clean 409/410.

### F7. [Medium] One unexpected per-article exception leaves siblings running after the sync is recorded failed
**Location:** `feeds/sync.py:207-216,82-88,284-289`. **Category:** errors/state. **Confidence:** high (code).

```python
async def one(item):
    try: await self._article(item, taken)
    finally: done += 1; report.progress(done)
await asyncio.gather(*(one(item) for item in pending))   # 216 — no return_exceptions
```

`gather` propagates the first exception immediately and does **not** cancel the other tasks. `run()`'s `finally` then closes the fetcher while they are mid-flight; they still write JSON records and `UPDATE feed_items SET state='saved'` (284-289) after `_run_build` recorded the failure.
**Impact:** job says failed while items are still being added, `saved_now` increments after the report is built, later fetches hit a closed client and burn `tries`. Realistic sources: `OSError` writing the record (disk full), sqlite errors, `extract.prepare` failure.
**Fix:** `return_exceptions=True` and record a per-item failure, or create tasks and cancel them explicitly on exception; wrap the `write_text`/DB update in `try/except OSError` → `state='failed'`.
**Test to add:** make `write_text` raise for one of three pending articles; assert no item is saved after the job reached a terminal state.

### F8. [Medium] A lost item record is never noticed while the sync is "unchanged"
**Location:** `feeds/sync.py:114-118` vs `330-335`. **Category:** bugs/self-healing. **Confidence:** high (**verified**).

```python
if has_book and not (self.saved_now or self.dropped or self.rebuild):   # 117
    return None, outcome                                                # never assembles
...
except (OSError, ValueError):      # 331 — the only place a missing record is repaired
    self.db.execute("UPDATE feed_items SET state = 'new', tries = 0 WHERE id = ?", ...)
```

Probe: delete `state/feeds/1/items/1.json` after a sync, sync again → `unchanged: True`, item stays `saved`, no request, no repair.
**Impact:** losing the store (backup, `forget()` race F6, crash between write and DB update) leaves the collection claiming articles absent from the ZIM until a new article or a manual rebuild; `outcome["articles"]` (sync.py:95) counts the phantoms.
**Fix:** existence-check saved ids (or a marker/count in the store) before returning "unchanged".
**Test to add:** the probe, asserting the next sync refetches and the ZIM has the article again.

### F9. [Medium] `feed_auto_sync = false` still updates links from their feeds
**Location:** `scheduler.py:58-66,90-91` vs `feeds/manager.py:64`. **Category:** bugs/semantics. **Confidence:** high (code).

`_check_due_feeds(ctx)` runs unconditionally (scheduler.py:91); only `FeedManager._schedule` checks the setting. Turning off automatic feed syncing still enqueues `{"rss": True}` link builds every `feed_minutes` and rewrites their ZIMs.
**Fix:** gate `_check_due_feeds` on the same setting (or add `link_feed_auto_sync`).
**Test to add:** `feed_auto_sync=0`, a due link, one scheduler pass → nothing enqueued.

### F10. [Medium] OPML edges: namespaced files import nothing, nested folders collapse, export date is mislabelled
**Location:** `feeds/parse.py:86-127`; `api/feeds.py:330-354,369-379`. **Category:** bugs. **Confidence:** high (code).

- `body = root.find("body")` (parse.py:92) fails for `<opml xmlns="http://opml.org/spec2">` → "lists no feeds" 422 for a valid file.
- The outer loop walks only direct children while `feeds_under` uses `node.iter("outline")` (parse.py:98,104): a nested folder is folded into its parent and never offered as its own collection.
- Import matches existing collections by `title COLLATE NOCASE` and export emits one folder per collection, so two same-titled collections merge on round-trip and one is lost.
- `dateCreated` (parse.py:119) uses local time with a literal `GMT` suffix.
**Fix:** strip namespaces, recurse into nested folders, dedupe folder titles on export, use `email.utils.format_datetime(…, timezone.utc)`.
**Test to add:** namespaced OPML; two-level nesting; export of two same-titled collections.

### F11. [Medium] `/api/feeds/check` has no size cap and up to ~3.5 min of sequential timeouts
**Location:** `api/feeds.py:228-278`. **Category:** connectivity. **Confidence:** high (code).

`asyncio.timeout(LOOK_TIMEOUT)` (30 s) per address, `client.get(url)` buffers the whole body with no limit, and up to 6 discovered feeds are checked sequentially (210 s worst case). The proxy/browser usually gives up long before. A `ValueError` from parse (F1) makes it a 500.
**Fix:** stream with a byte cap, wrap the discovery loop in one overall deadline, catch `ValueError` like `NotAFeed`.
**Test to add:** 10 MB body and a hang → 422 within budget, no traceback.

### F12. [Medium] Feed checks are serial, and each dead source can cost ~3 minutes
**Location:** `feeds/sync.py:124-143`. **Category:** connectivity/UX. **Confidence:** high (code).

`Fetcher` uses `REQUEST_TIMEOUT=60`, `RETRIES=2`, `sleep(1.5*attempt)` → ≈185 s per unresponsive host, and sources are strictly sequential; `max_concurrent_feed_syncs=1` then holds every other collection behind it.
**Impact:** one dead feed does not *block* the others (good: per-source except) but delays them enough to look like a hang, and the progress bar sits at 0/N for minutes.
**Fix:** fetch sources concurrently with a small semaphore (independent work; the fetcher already paces per host).
**Test to add:** three sources, two timing out — all attempted, total bounded by one timeout.

### F13. [Medium] Unbounded per-article fan-out, and every article is prepared twice
**Location:** `feeds/sync.py:216,229-256`. **Category:** performance. **Confidence:** high (code).

`asyncio.gather(*(one(item) for item in pending))` creates a coroutine per pending item; network concurrency is capped by `Fetcher._slots`, but `extract.prepare` runs on the default thread pool with no cap (contrast `Crawler._localise`, which uses `Semaphore(self.workers)`), and the carried-content prepare (243) runs even when the fetched page was used.
**Impact:** with `keep=0` and 1,000 items, up to 2,000 lexbor parses queue on 32 threads, each holding a tree: memory spikes and to_thread latency grows. The second parse is waste in the common case.
**Fix:** bound the phase with a `Semaphore(workers)`; skip the carried prepare when `len(prepared.text) >= THIN * len(item["content"])` already holds.
**Test to add:** 200 pending items, counting wrapper around `prepare` — at most `workers` concurrent, carried parse skipped for long pages.

### F14. [Medium] `retry-failed` mutates item state before knowing a sync can start
**Location:** `api/feeds.py:211-218`. **Category:** state machine. **Confidence:** high (code).

`UPDATE … state='new', tries=0` then `_sync(...)`, which can raise 409 ("already being built") or, for an unknown id, 409 instead of 404. If a sync is already mid-`_fetch_articles`, the reset rows are not in its `pending` list, so the user's retry appears to work but does nothing this run.
**Fix:** validate (404), enqueue first, then reset; return the running build when one exists.
**Test to add:** `retry-failed` on a busy feed → 409 and unchanged states; unknown feed → 404.

### F15. [Low] A busy or removed collection aborts the rest of that scheduler pass
**Location:** `feeds/manager.py:61-72`. **Category:** scheduling. **Confidence:** high (code).

`for feed in self.due(): self.sync(...)` — a `BuildError` (busy via F5, or deleted between `due()` and `sync()`) abandons the loop and logs a full traceback every 30 s; later due feeds wait another cycle.
**Fix:** `except BuildError: continue` per feed; keep the outer handler for real faults.
**Test to add:** two due feeds, one deleted between `due()` and `sync()` — the second is still enqueued.

### F16. [Low] Duplicate-URL articles are `skipped` forever; if the original retires, the article vanishes
**Location:** `feeds/sync.py:266-274,292-307`. **Category:** state machine. **Confidence:** high (code).

`taken` is built from `state='saved'` only; a `skipped` item is not in `wanted` and is never reconsidered. When `_retire_old` drops the row that held the path, the ZIM loses the article while a permanent "skipped" row keeps the record.
**Fix:** on retire, move `skipped` rows back to `new`; include `skipped` in `rebuild`.
**Test to add:** two distinct guids with the same URL, keep=1, then a newer item — the article is still in the ZIM.

### F17. [Low] The dedupe key discards the feed's own `<guid>`, and long guids silently collide
**Location:** `feeds/parse.py:50-55,64`; `db.py:227`. **Category:** dedupe. **Confidence:** high (**verified**).

`guid = link or str(item.get("id","")).strip()` — the link wins over the publisher's guid; entries with neither are silently dropped (`continue`, no report). The `seen` set compares the untruncated value while the DB stores `guid[:1000]`, so guids differing only after character 1000 collapse into one ignored insert. `normalise` does not unify trailing slashes, so `x/a` and `x/a/` are two articles (two paths, two downloads).
**Fix:** prefer a non-permalink `id`; truncate before the `seen` check; unify trailing slashes; report identity-less entries as skipped.
**Test to add:** entry with no link and no guid (assert a skipped report); same page with/without trailing slash.

### F18. [Low] Failure bookkeeping hides outages; `next_sync` disagrees with `due()`
**Location:** `feeds/manager.py:105-112`; `api/feeds.py:79-82`; `feeds/sync.py:107-118`. **Category:** bookkeeping. **Confidence:** high (code).

- Only `BuildError` writes `feeds.last_error`; other exceptions leave a stale message while the build row has the real one.
- When every source fails but articles are saved, the run takes the "unchanged" path: `last_synced` is bumped, `feeds.last_error` cleared, and the collection looks freshly synced (only per-source `last_error` records it).
- `due()` treats `last_attempt IS NULL` as due now; the UI's `next_sync` computes `created + interval` (future).
- `last_new` is not written when the sync fails after saving articles.
**Fix:** record `last_error` for any exception, do not call a run unchanged when no source was readable, align `next_sync` with `due()`, persist `last_new` on failure.
**Test to add:** all sources 500 with a book present → `feeds.last_error` non-empty; never-synced `next_sync` is now.

### F19. [Nit] Small correctness nits
- `feeds/sync.py:31-36` — `article_shell(title, body, url)` never uses `url`; the base goes to `prepare` separately.
- `feeds/sync.py:257-258` — with `full_text=False` and an empty summary the message says the article "has no address", although `item["url"]` is set; suggest turning full text on.
- `api/feeds.py:199-200` — `q` is embedded in `LIKE ?` unescaped: `%`/`_` are wildcards (`q=%` returns everything). Escape `LIKE ? ESCAPE '\'`.
- `api/feeds.py:90-99` — removing a source leaves `feed_items.source_id` dangling; items lose `source_title` in `_assemble` (LEFT JOIN, sync.py:314) and in `list_items`.
- `feeds/sync.py:362` — `missing.json` is never pruned and resources skipped for the size budget are recorded as permanently missing, so normal syncs never retry them (only `rebuild`).
- `scheduler.py:86` — `int(get_setting("catalog_refresh_hours"))` raises on `"12.5"` every 60 s; `scheduler.py:27` calls `queue_auto_updates` outside the `try`, bypassing the catalog retry bookkeeping.

## Database integrity & normalization notes

- **No FKs on the feed tables** (`PRAGMA foreign_key_list(feed_sources)`/`(feed_items)` → `[]`, verified) despite `foreign_keys=ON`. `delete_feed` compensates with ordered deletes; nothing else does — `source_id` dangles after `_set_sources`, and a racing sync inserts rows for a deleted `feed_id` (F6).
- **`feed_items.state` has no `CHECK`** (`db.py:222`) and the allowed set lives twice (`db.py` default + `api/feeds.py:22`). There is no `fetching` state: `new → saved|failed|skipped|dropped` in one step, so a crash mid-fetch is indistinguishable from "not tried" (`tries` is the only evidence).
- **`UNIQUE(feed_id, guid)`** is the only dedupe: guid = normalised link, else feed id, truncated to 1000 (F17). Cross-source duplicates collapse only when the URL is byte-identical after `normalise` (tracking params/fragments dropped; trailing slash, query order and case preserved).
- **No transactions:** `execute` is autocommit (`isolation_level=None`), so `add_feed` (row → sources → optional sync), `_set_sources`, `delete_feed` and `_remember`'s loop are non-atomic; a crash between them yields a collection with no sources, where every later sync fails.
- **`feed_items.path` has no `UNIQUE`**; uniqueness is a Python `taken` set, per sync and per process (F5).
- **Derived counters** (`last_new`, `last_synced`, `report["articles"]`, `report["dropped"]`) are computed at different points in `_run` and disagree after partial failures, retirement or an `_assemble` reset (F3, F8, F18).
- **Duplicated configuration:** `mode`, `images`, `video`, `language`, `rate_limit/unit`, `category`, `book_id`, `last_built/last_synced` are mirrored between `links` and `feeds`; a new option needs edits in `db.py`, `api/build.py`, `api/feeds.py` and `feeds/sync.py`.
- **Normalization:** ZIM title = `feeds.title` → first source title → "Feeds"; language re-derived per sync (F4); `creator`/`description` joined and truncated mid-word (sync.py:371,373); the index page's `lang` uses the raw feed language while the ZIM uses `language_code(...)`.

## Connectivity & failure handling

- **Conditional requests:** `Fetcher` keeps `etag`/`last-modified` per URL, sends `If-None-Match`/`If-Modified-Since`, and a 304 returns the cache (`crawler.py:392-420`). `_check_sources` passes `fresh=True`, so every sync revalidates without re-downloading. The index is only written on `close()`, so a hard crash loses the last run's validators (full re-download, no corruption).
- **Retries/backoff:** `RETRIES=2` with linear 1.5 s backoff; 429 and 503+`Retry-After` take the `_SlowDown` path (`RATE_RETRIES=6`, exponential per-host pause, pace doubling to 8 s, recovery after 30 calm replies). Timeouts: 60 s to first byte, then `max(2*60, limit/MIN_SPEED)` for the body, streamed to `.part` with a byte cap.
- **Proxy/VPN/solver** are shared through `CrawlOptions → Fetcher → Network`; `/api/feeds/check` uses the proxy but not `Network.steady()` or the solver, so a challenge-protected feed cannot be discovered from the UI even though the sync would pass.
- **One unreachable feed** does not abort others for network errors, but does for `ValueError`/`OSError` (F1) and delays them (F12). DNS/TLS/proxy/5xx all become a readable `FetchError` stored per source.
- **Offline:** with saved articles the sync is "unchanged" and looks successful (F18); with none it fails with the first reason (`sync.py:107-113`), which is right.
- **Politeness:** per-host delay, `Crawl-delay`, the user rate limit (`feeds.rate_limit/unit` → `per_second`) and robots.txt for full-text fetches; feed fetches correctly ignore robots and honour the pace.
- **Shutdown:** `main.py:95-99` cancels the scheduler task but never awaits it before `db.close()`; a scan in `to_thread` can still touch the DB. The feed scheduler is one of the victims.

## Testing gaps

Mapping: `test_feeds.py:107` → `parse`/`discover`; `:143` → OPML round-trip; `:152` → incremental sync, unchanged path, ZIM contents, `book_id`; `:191` → feed-text fallback, `keep=2`, retire; `:228` → `NotAFeed` + `feeds.last_error`; `:244` → `due()`; `:263/:284/:331/:338/:389` → `Fetcher`/`Crawler` (not this module); `:500/:510/:524` → OPML preview/import. DB state *is* asserted after syncs (books, item states, feed title/`book_id`) — a real strength.

Untested branches:

- **Zero coverage:** `scheduler.py` in its entirety (`run`, `_check_due_feeds`, `_rebuild_due_links`, `refresh_catalog`) and `FeedManager._schedule`.
- **APIs:** delete, edit, `sync-all`, `check`, `runs*`, `retry-failed`, `items` (state/`q`), OPML **export**; no DB assertions after delete/edit.
- **Parsing:** 304/`If-None-Match` in `_check_sources` (only `tests/test_builder.py:201` covers 304, for links); malformed XML/bozo; non-UTF-8 (works, unasserted); malformed URL (F1); entry with no link/guid (F17); duplicate URL across sources → `skipped`; trailing-slash duplicates; script-laden summaries.
- **Sync semantics:** `tries` reaching `MAX_TRIES`; `rebuild=True` retrying failed items and rewriting an unchanged ZIM; `keep=0` and raising `keep` (F2/F3); keep at the limit (F3); multi-source partial failure (only `:228`, single source); per-source `last_checked`/`last_error`; total outage with a book present (F18); language propagation (F4).
- **Concurrency/lifecycle:** two simultaneous `sync()`s (F5); delete during sync (F6); unexpected exception in one `_article` (F7); missing item JSON (F8); cancel mid-fetch; restart with `state='processing'`.
- **OPML:** malformed XML (only `<opml/>`), namespaced OPML, nested folders, relative `xmlUrl`, duplicate folder titles (F10), export→import of a real listing.

## What is done well

- Per-collection store + "re-assemble from saved records" makes incremental sync cheap: the test proves only `/feed.xml`, `/clove`, `/pic-clove.png` are fetched on the third run and that the old picture survives in the archive.
- `Fetcher` is strong engineering: streamed byte caps, adaptive per-host pacing on 429s, `Retry-After`, ETag/304, solver clearances reused, connection-pool renewal through broken proxies, metrics.
- The `THIN` ratio is a pragmatic answer to paywalls/JS shells, and the "kept the article as the feed carries it" note is user-visible.
- Per-source error isolation (for expected errors) plus `feed_sources.last_checked/last_error` give the UI something honest to show.
- The scheduler loop's `except Exception: log.exception(...)` keeps the loop alive, and both due-queues isolate per-link enqueue failures.
- `delete_feed` cleans items, sources, store and icon in the right order with a helpful 409.
- Tests assert real ZIM contents (`article_count`, `has_entry_by_path`, index order, localised `href`s), not just DB rows.

## Unverified / out of scope

- The web UI is not in this repo, so `feed_auto_sync`'s label and the `next_sync`/`last_error` display could not be checked against what users see (F9, F18).
- Multi-process deployment was inferred from the in-memory `_active` map; not exercised.
- `selectolax`/lexbor on deliberately hostile HTML, and libzim behaviour on concurrent writes to one target path, were not fuzzed (F5, F7).
- `scanner`, `book_updates`, `opds.refresh`, `file_under_categories`, `Network` and `RedisBroker` were read only where feeds depends on them.
- Live DNS/TLS failures, a real FlareSolverr and the VPN were not reproduced; assessed from `Fetcher` code and its tests.
- Growth of `feed_items` (never pruned) and link `builds` rows on a long-lived instance was not measured.
