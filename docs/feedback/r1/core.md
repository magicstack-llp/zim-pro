# Core & Database Review — r1

**Scope:** `src/zim_pro/db.py`, `config.py`, `context.py`, `events.py`, `access.py`, `main.py`, `__init__.py`; tests `tests/test_units.py`, `tests/test_system.py`; call sites in `api/`, `library/`, `downloads/`, `builder/`, `feeds/`.
**Commit:** `cbb1a4e` (Release 0.5.0)
**Date:** 2026-11-14
**Method:** static read of every in-scope file plus grep-traced call sites; no files modified, tests not run.

## Summary

- **Shutdown is not exception-safe.** The lifespan cancels the scheduler and log-shipper without awaiting them and closes the shared SQLite connection with no `try/finally`; a scheduler thread inside `asyncio.to_thread(scanner.scan)` can outlive `ctx.db.close()`.
- **The schema enforces almost nothing.** One foreign key exists in the whole database (`chat_messages.chat_id`); 11 id columns reference other tables and are unchecked. There is not a single `CHECK` constraint for any state/enum/boolean column, and `PRAGMA foreign_keys=ON` is therefore nearly decorative.
- **Two check-then-insert paths have no unique index behind them** (`downloads.enqueue`, `builds.enqueue`). Concurrent requests can create duplicate jobs that write the same `.part` / `.zim.tmp` file.
- **Startup failure modes are mostly unhandled.** `data_dir` is never created or validated (the server starts "healthy" with an empty library), and the friendly `SystemExit` only wraps `sqlite3.connect()`, not `mkdir`, `PRAGMA journal_mode=WAL`, `executescript(SCHEMA)` or `_migrate()`.
- **The schema in `SCHEMA` is not the real schema:** `links/uploads/feeds.category/icon`, `builds.position` and `downloads.started/finished` exist only in `_migrate()`, so `CREATE TABLE` alone produces a table the code cannot use.
- Tests exercise the happy paths of the API but never the migration path, an admin token, `EventBus`, or any failure branch of `Database`.

## Top 5 must-fix

1. **F1** — make lifespan shutdown `try/finally`-safe and `await` cancelled tasks before `db.close()`.
2. **F2** — add partial unique indexes for active `downloads(catalog_uuid)` / `builds(kind, source_id)` and make enqueue a single atomic statement.
3. **F3/F7** — add the missing FKs (with explicit `ON DELETE`) for `feed_*`, `builds.source_id` cleanup and `books.book_id` references; add `CHECK` constraints for the state/enum/boolean columns.
4. **F4** — create/validate `data_dir` (and fail loudly) at startup; broaden the DB-open error handling to `mkdir`/pragmas/schema/migration; make `/api/health` mean something.
5. **F5/F6** — guard every `json.loads` of DB data and move the MCP auth DB lookup plus the catalog `DELETE+INSERT` off the event loop.

---

## Findings

### F1. [High] Lifespan shutdown is not exception-safe and closes the DB under live threads

- **Location:** `src/zim_pro/main.py:92-102` (also `38-54`, `79-90`)
- **Category:** lifecycle / thread-safety / unhandled exceptions
- **Confidence:** high
- **Evidence:**
  ```python
  async with mcp.session_manager.run():
      yield
  log.info("Zim Pro is stopping")
  background.cancel()
  shipping.cancel()
  await ctx.monitor.stop()
  ...
  ctx.broker.close()
  ctx.db.close()
  ```
- **Impact:** `Task.cancel()` only schedules cancellation. `scheduler.run` is usually parked in `await asyncio.to_thread(ctx.scanner.scan)` (`scheduler.py:93`); cancelling the await does **not** stop the worker thread, which keeps calling `self.db.execute(...)` in `_scan()`. Shutdown then reaches `ctx.db.close()` and that thread hits `sqlite3.ProgrammingError: Cannot operate on a closed database` (or writes into a half-closed connection). There is also no `try/finally`: any exception in `ctx.monitor.stop()`/`feeds.stop()`/`builds.stop()` skips the rest, so the DB and broker are never closed. The whole context is built at `create_app()` time (`main.py:75`), so a failure between `downloads.start()` and `yield` also leaks everything.
- **Trigger/Repro:** start with a library large enough that `scanner.scan()` takes seconds, send SIGTERM/`Ctrl-C` during a scheduled rescan (every 300 s), or raise inside `monitor.stop()`.
- **Suggested fix:** wrap the body in `try/finally`; `await asyncio.gather(background, shipping, return_exceptions=True)` after cancelling, or run the blocking scan via an executor that is shut down (`loop.shutdown_default_executor()`) *before* `db.close()`; close the DB last, guarded by `contextlib.suppress`.
- **Test to add:** a lifespan test that monkeypatches `scanner.scan` to block on an `threading.Event`, enters/exits `TestClient`, and asserts no `ProgrammingError` and that `db.close()` was called after the scan thread finished.

### F2. [High] Enqueue uniqueness is enforced only by a check-then-insert race

- **Location:** `src/zim_pro/downloads/manager.py:122-134`, `src/zim_pro/builder/jobs.py:156-166`, schema `src/zim_pro/db.py:62-80` / `160-180`
- **Category:** DB integrity / concurrency
- **Confidence:** high (race window is real; exploitation needs concurrent requests)
- **Evidence:**
  ```python
  existing = self.db.one(
      "SELECT id FROM downloads WHERE catalog_uuid = ? "
      "AND state IN ('queued', 'downloading', 'verifying', 'paused', 'failed')", ...)
  if existing: ...
  cursor = self.db.execute("INSERT INTO downloads (...) VALUES (...)")
  ```
  and `builds`: `SELECT id FROM builds WHERE kind = ? AND source_id = ? AND state IN (...)` then `INSERT INTO builds ...` — no `UNIQUE` on either pair.
- **Impact:** two overlapping `POST /api/downloads` (double-click / two clients) both pass the SELECT and insert. Both rows get the same `filename`, and `Download.__init__` opens the **same** `<target>.part` with `os.open(O_RDWR|O_CREAT)` (`engine.py:128`), so two writers corrupt the file. Same for builds: `write_zim` uses `target + ".tmp"` (`writer.py:246`) and starts with `temporary.unlink(missing_ok=True)`, so a second concurrent build of the same link deletes the first's temp and both write it.
- **Trigger/Repro:** `max_concurrent_downloads=2`, fire two `enqueue()` calls from two threads for the same `catalog_uuid` (the per-call lock does not span the check+insert pair).
- **Suggested fix:** add partial unique indexes and let the DB arbitrate:
  `CREATE UNIQUE INDEX downloads_active ON downloads(catalog_uuid) WHERE state IN ('queued','downloading','verifying','paused');` (and the same for `builds(kind, source_id)` over active states), then `INSERT ... ON CONFLICT DO NOTHING` + re-select. Alternatively wrap check+insert in one transaction and use `BEGIN IMMEDIATE`.
- **Test to add:** two threads calling `enqueue` for the same uuid; assert `COUNT(*) = 1`.

### F3. [Medium] Only one foreign key exists; deletes leave orphan rows

- **Location:** `src/zim_pro/db.py:35-40, 62-80, 81-91, 122-159, 160-180, 181-228`; delete sites `api/build.py:182,476`, `api/feeds.py:147-148`, `library/scanner.py:154,175`
- **Category:** DB integrity
- **Confidence:** high
- **Evidence:** the only `REFERENCES` in 244 lines of schema is
  ```sql
  chat_id TEXT NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
  ```
  Everything else is a bare integer/text id: `shadowed.book_id`, `downloads.catalog_uuid`, `downloads.book_id`, `history.book_id`, `links.book_id`, `uploads.book_id`, `builds.source_id`, `builds.book_id`, `feeds.book_id`, `feed_sources.feed_id`, `feed_items.feed_id`, `feed_items.source_id`.
- **Impact:** `DELETE FROM links WHERE id = ?` leaves every `builds` row with that `source_id` (and `builds.book_id`) dangling; retrying such a build fails with a clean `BuildError("The link this build was for has been removed.")`, but the row is stuck in the list forever. `_set_sources` (`api/feeds.py:99`) deletes `feed_sources` rows that `feed_items.source_id` still points at → the item loses its site label (`LEFT JOIN` yields NULL). `DELETE FROM books` (scanner) leaves `links.book_id`/`feeds.book_id`/`uploads.book_id` pointing at a book id that no longer exists; `file_under_categories` and `book_updates` keep trusting those strings.
- **Trigger/Repro:** delete a link that has finished builds, then `GET /api/build/jobs` and retry the old job.
- **Suggested fix:** add FKs with explicit behaviour (`builds.source_id` cannot be a plain FK because it is polymorphic — see F7/normalization; use a trigger or an explicit cleanup in the delete handlers), `feed_sources.feed_id REFERENCES feeds(id) ON DELETE CASCADE`, `feed_items.feed_id … ON DELETE CASCADE`, `feed_items.source_id … ON DELETE SET NULL`, and for `links/uploads/feeds.book_id` either `ON DELETE SET NULL` or document that the string is a historical name.
- **Test to add:** delete a feed and assert `feed_sources`/`feed_items`/`builds` have no rows for it.

### F4. [Medium] Startup does not validate `data_dir`, and the friendly DB error covers only `connect()`

- **Location:** `src/zim_pro/db.py:260-276`, `src/zim_pro/main.py:38-54,73-105`, `src/zim_pro/api/library.py:26-34`
- **Category:** startup failure modes / health semantics
- **Confidence:** high
- **Evidence:**
  ```python
  if path != ":memory:":
      Path(path).parent.mkdir(parents=True, exist_ok=True)
  try:
      self._conn = sqlite3.connect(...)
  except sqlite3.OperationalError as exc:
      raise SystemExit(...)
  ...
  self._conn.execute("PRAGMA journal_mode=WAL")
  ```
  `data_dir` is never touched: `Scanner._scan` does `self.data_dir.rglob("*.zim")`, which silently yields nothing when the directory does not exist, while `status()` does `shutil.disk_usage(ctx.config.data_dir)`.
- **Impact:** three separate failure modes. (a) A missing/unmounted `/data` starts a perfectly "healthy" server with an empty library; `/api/status` then 500s with `FileNotFoundError`. (b) A read-only state dir where the DB file already exists passes `connect()` and blows up at `PRAGMA journal_mode=WAL` with a raw traceback. The same applies to a corrupt DB (`DatabaseError` at `executescript`) and to any failed `_migrate()` ALTER. (c) `SystemExit` from library code is invisible to `except Exception` callers and kills an embedded/worker process rather than reporting a startup error.
- **Trigger/Repro:** `KIWIX_DATA_DIR=/nonexistent KIWIX_STATE_DIR=/tmp/x zim-pro serve` → `/api/health` `{"ok": true}`; `chmod a-w` the state dir and restart.
- **Suggested fix:** create `data_dir`/`state_dir` in `build_context` (or fail with one actionable message); wrap the whole `Database.__init__` body (mkdir, pragmas, script, migrate) in the same friendly error path; raise a custom `StartupError` and convert to `SystemExit` only in `__main__`. Make `/api/health` check `data_dir` is a readable directory and `state_dir` writable.
- **Test to add:** `Database(tmp_path/"ro"/"x.sqlite3")` with the parent chmod 0500 → assert the friendly message, not `PermissionError`; `create_app` with a missing `data_dir` → `/api/status` does not 500.

### F5. [Medium] `json.loads` on database/user data is unguarded, including on the startup path

- **Location:** `src/zim_pro/db.py:398-400,413-415`; `src/zim_pro/builder/jobs.py:139,146-147,321`; `src/zim_pro/api/ai.py:213-216`; `src/zim_pro/main.py:40`
- **Category:** unhandled exceptions / unhandled user data
- **Confidence:** high on the code path, medium on how a bad value gets in
- **Evidence:**
  ```python
  for row in self.all("SELECT key, value FROM settings"):
      if row["key"] in settings:
          settings[row["key"]] = json.loads(row["value"])
  ```
  and `get_meta` does `json.loads(value)`. `build_context` calls `db.get_setting("log_level")` first thing (`main.py:40`).
- **Impact:** one malformed `settings.value` (manual edit, a future writer that forgets `json.dumps`, a downgrade from a version that stored raw strings) makes **every** settings read raise `ValueError` — the app cannot start, and there is no message naming the key. `builds.report`/`params` and `chat_messages.sources`/`steps`/`queries` have the same problem: a bad row turns `GET /api/build/jobs` or `GET /api/ai/chats/{id}` into a 500 for the whole list.
- **Trigger/Repro:** `UPDATE settings SET value='INFO' WHERE key='log_level';` restart.
- **Suggested fix:** a small `_loads(value, default, key)` helper that logs the bad key and falls back to the default; use it everywhere DB JSON is read. Set `CHECK (json_valid(value))` on `settings.value` in new databases.
- **Test to add:** write a raw non-JSON value and assert `get_settings()` returns the default rather than raising.

### F6. [Medium] Blocking DB work runs on the event loop

- **Location:** `src/zim_pro/main.py:108-122`; `src/zim_pro/catalog/opds.py:76-90` reached from `scheduler.py:16` and `api/catalog.py:97`; `src/zim_pro/api/system.py:231` (`async def put_settings`), `src/zim_pro/api/downloads.py:22`
- **Category:** connectivity / responsiveness
- **Confidence:** high
- **Evidence:**
  ```python
  if request.url.path.startswith("/mcp"):
      locked = bool(config.admin_token) or access.has_keys(ctx.db)
      ...
      if locked and not is_admin and not access.check_key(ctx.db, supplied):
  ```
  `check_key` does `SELECT id, digest, last_used FROM mcp_keys` **and** a possible `UPDATE`. `opds.refresh` is awaited and then calls `store(db, rows)` synchronously, which holds the RLock for `DELETE FROM catalog` + one big `executemany` (`INSERT OR REPLACE` of the whole feed, `count=-1`).
- **Impact:** every MCP request performs 1–2 synchronous SQLite operations plus SHA-256 in the async middleware; the whole event loop (SSE fan-out, streaming article responses, downloads progress) stalls behind the DB lock. The full Kiwix catalog is tens of thousands of rows, so the refresh blocks the loop for a noticeable time (and every API request waits on the same RLock, since `all()`/`one()` take it).
- **Trigger/Repro:** trigger `POST /api/catalog/refresh` and, during the insert, request `/api/health` — latency spikes; or hold the DB lock from a thread and send an MCP request.
- **Suggested fix:** run both paths through `await asyncio.to_thread(...)` (or make the auth middleware a sync dependency on the MCP route), and cache `has_keys`/`digest` lookups in memory, invalidated when keys change.
- **Test to add:** a latency assertion around `/mcp` while a thread holds `db._lock`, or monkeypatch `access.check_key` to `time.sleep(1)` and assert other requests still complete.

### F7. [Medium] No `CHECK` constraints on any state, enum or boolean column

- **Location:** `src/zim_pro/db.py:31` (`policy`), `71` (`state`), `98,131-133,151,156` (booleans), `126-130` (`engine/scope/mode`), `163-165` (`kind/state`), `186-194` (`mode`, `rate_unit`, `enabled`), `222` (`state`), `110` (`role`)
- **Category:** DB integrity / normalization
- **Confidence:** high
- **Evidence:**
  ```sql
  policy TEXT NOT NULL DEFAULT 'notify',
  ...
  state TEXT NOT NULL DEFAULT 'queued',
  ...
  enabled INTEGER NOT NULL DEFAULT 1,
  ```
  `grep -c CHECK src/zim_pro/db.py` → 0.
- **Impact:** the DB accepts `downloads.state='finished'` (the queue code only knows `done`/`failed`/`cancelled`; `api/system._queue` counts would silently miss it), `builds.kind='whatever'` (which then never matches any manager's `_mine` and is never run or shown), `links.engine='zimit '`, `posts.images=7`. Every invariant lives in Pydantic models in `api/`, so anything else that writes — migrations, repair scripts, the MCP server, a future importer — can corrupt semantics with no error.
- **Trigger/Repro:** `db.execute("UPDATE downloads SET state='finished' WHERE id=1")` → the row disappears from both "waiting" and "done" counts.
- **Suggested fix:** add `CHECK (state IN ('queued','downloading','verifying','paused','done','failed','cancelled'))`, `CHECK (policy IN ('auto','notify','pinned'))`, `CHECK (images IN (0,1))` etc. For existing databases use `PRAGMA writable_schema`-free table rebuild in a migration step (this is where a real migration framework is needed — see F12).
- **Test to add:** an insert with an invalid state must raise `sqlite3.IntegrityError`.

### F8. [Medium] `require_admin` uses a non-constant-time compare and different header parsing than `/mcp`

- **Location:** `src/zim_pro/context.py:52-59` vs `src/zim_pro/main.py:111-115`
- **Category:** access control
- **Confidence:** high (behaviour); low–medium (practical exploitability over HTTP)
- **Evidence:**
  ```python
  supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
  if supplied != token:
      raise HTTPException(401, "admin token required")
  ```
  while MCP uses `hmac.compare_digest(supplied.encode(), config.admin_token.encode())`.
- **Impact:** inconsistent security posture: `!=` short-circuits on the first differing byte, so the admin token guarding every write endpoint is timing-comparable, while the MCP path is not. `removeprefix` is case-sensitive, so a legitimate client sending `authorization: bearer <token>` gets 401 on `/api/*` (HTTP auth schemes are case-insensitive). Assumes `admin_token` is not `""`: an empty env var silently disables auth entirely (`if not token: return`).
- **Trigger/Repro:** `curl -H 'authorization: bearer …' /api/settings` → 401 while `curl -H 'Authorization: Bearer …'` works; time repeated bad tokens against `PATCH /api/books/x`.
- **Suggested fix:** one shared helper in `context.py` used by both paths: parse scheme case-insensitively, `hmac.compare_digest(supplied.encode(), token.encode())`, and require `min_length=1` for `admin_token` in `Config`.
- **Test to add:** `Config(admin_token="t")` + `create_app`; assert 401 for missing/wrong/lowercase-scheme and 200 for the right one on both `/api/settings` and `/mcp`.

### F9. [Medium] `zim-pro mcp` builds a full context but never starts the managers, so write tools queue work nothing runs

- **Location:** `src/zim_pro/__init__.py:18-27`; call site `src/zim_pro/mcp_server.py:181-191`
- **Category:** logic error / lifecycle
- **Confidence:** medium-high
- **Evidence:**
  ```python
  ctx = build_context(config)
  ctx.scanner.scan()
  asyncio.run(build_mcp(ctx).run_stdio_async())
  ```
  no `ctx.downloads.start()`, `ctx.builds.start()`, `ctx.feeds.start()`; but the MCP tool does
  `ctx.downloads.enqueue(item["latest"]["uuid"])` and returns `"Queued …"`.
- **Impact:** with `mcp_allow_writes=true` and no web server running against the same database (the documented desktop stdio use case), the download sits at `queued` forever and the tool tells the user it started. If a server *is* running, the server's `_run_queue` will eventually pick it up (its `LOOK_AGAIN` timeout), so the bug is silent rather than loud.
- **Trigger/Repro:** `KIWIX_STATE_DIR=/tmp/s zim-pro mcp`, call `start_update`, inspect `downloads.state`.
- **Suggested fix:** either start the queues in stdio mode too (and close them on exit), or make `start_update` return "the server will pick this up" and document that `mcp` is read-only unless a server is running.
- **Test to add:** drive `build_mcp(ctx).call_tool("start_update", …)` in stdio mode and assert the DB row's state advances (or that the tool reports it needs a server).

### F10. [Medium] Two processes on the same DB have no lock retry beyond sqlite3's default 5 s

- **Location:** `src/zim_pro/db.py:260-276`; `src/zim_pro/__init__.py:24-26`
- **Category:** robustness / DB integrity
- **Confidence:** medium
- **Evidence:**
  ```python
  self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
  ```
  no `timeout=`, no `PRAGMA busy_timeout`, and no retry anywhere. (`sqlite3.connect` defaults to a 5 s busy timeout.)
- **Impact:** the single connection + RLock serialises *within* a process, so this only bites across processes — which is a supported configuration: `zim-pro mcp` opens the same file and immediately runs `scanner.scan()`, which writes `books`, `shadowed` and `shadowed` deletes. If the server is mid-refresh (`DELETE FROM catalog` + tens of thousands of `INSERT OR REPLACE`, holding the write lock for seconds), the MCP process raises `sqlite3.OperationalError: database is locked` uncaught and exits.
- **Trigger/Repro:** start a catalog refresh in the server, then start `zim-pro mcp` on the same state dir.
- **Suggested fix:** `PRAGMA busy_timeout=10000` (or `timeout=10`), plus a retry/backoff wrapper for `OperationalError` on the scan path; consider making stdio MCP read-only (`mode=ro` URI) except for `start_update`.
- **Test to add:** two `Database` objects on one file; hold a write transaction in one and time an `execute` in the other → assert it waits and then succeeds within the configured timeout.

### F11. [Medium] `EventBus` can raise into publishers after shutdown, and publishing from a worker thread is only safe after `bind()`

- **Location:** `src/zim_pro/events.py:24-34`; `src/zim_pro/main.py:81,95-102`
- **Category:** event bus / lifecycle
- **Confidence:** high on the mechanism, medium on frequency
- **Evidence:**
  ```python
  if loop is not None and not on_loop:
      loop.call_soon_threadsafe(self._publish, event, data)
  ```
  `self._loop` is set in lifespan and never cleared; `background.cancel()` / `shipping.cancel()` are not awaited before the loop closes.
- **Impact:** any thread that publishes after the loop stops (a lingering `to_thread` build/download finaliser, the un-awaited scheduler) gets `RuntimeError: Event loop is closed`, which propagates into that worker and can mark a job failed at shutdown. Before `bind()` (during `build_context`/startup) `loop is None`, so `_publish` runs directly on whatever thread called it — including `queue.put_nowait` on an `asyncio.Queue` from a non-loop thread, which the queue does not support.
- **Trigger/Repro:** stop the server while a build's `_adopt` thread finishes and calls `self.events.publish("library", …)`.
- **Suggested fix:** catch `RuntimeError` around `call_soon_threadsafe` and drop the event; set `_loop = None` at shutdown; scope publishes during startup.
- **Test to add:** bind a loop, close it, call `publish` from a thread → no exception; publish before `bind()` with a subscriber attached → no cross-thread `put_nowait`.

### F12. [Medium] Migrations are unversioned, non-transactional and ALTER-only; `SCHEMA` is not the real schema

- **Location:** `src/zim_pro/db.py:275-335`
- **Category:** DB integrity / migration
- **Confidence:** high
- **Evidence:** every DDL statement in `_migrate()` runs on an `isolation_level=None` connection with no `BEGIN`/`COMMIT` and no `PRAGMA user_version` check. The `SCHEMA` literal lacks columns the code requires:
  ```python
  for table in ("links", "uploads", "feeds"):
      ... ("category", "TEXT NOT NULL DEFAULT ''"), ("icon", "REAL")
  ```
  and `builds.position`, `downloads.started/finished` are added only by `_migrate` (they are absent from the `CREATE TABLE`s at lines 62-80 and 160-180).
- **Impact:** a failure halfway through `_migrate()` (disk full, the F13 duplicate-column case) leaves a partially migrated database with no marker to know where it stopped; the next start re-runs `IF NOT EXISTS`-style guesses. `CREATE TABLE` can never add a `CHECK`, a `UNIQUE` or an `ON DELETE` to an existing database, so every integrity constraint added in a future release silently applies only to fresh installs (this is what makes F7/F2 unenforceable retroactively).
- **Trigger/Repro:** create a DB, drop one added column (`ALTER TABLE uploads DROP COLUMN icon`), restart; then compare `sqlite3 .schema` with `db.SCHEMA`.
- **Suggested fix:** introduce `PRAGMA user_version` with numbered migration steps, each wrapped in one transaction; make the `CREATE TABLE` statements complete so fresh installs and upgrades converge, and use table-rebuild migrations when constraints change.
- **Test to add:** build a fixture DB with the 0.4.0 schema, open it, assert `user_version`, all columns and (future) constraints; open it twice to prove idempotency.

### F13. [Low] Stale `build_columns` can produce a duplicate-column migration failure

- **Location:** `src/zim_pro/db.py:319-324`
- **Category:** migration bug
- **Confidence:** medium (bug is certain; reachable state is not shipped)
- **Evidence:**
  ```python
  build_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(builds)")}
  if "params" not in build_columns:
      self._conn.execute("ALTER TABLE builds ADD COLUMN params ...")
  if "position" not in build_columns:          # <- stale set, not refreshed
      self._conn.execute("ALTER TABLE builds ADD COLUMN position REAL")
  ```
- **Impact:** a database that already has `position` but not `params` (dev build, or a hand-repaired DB) gets `ALTER TABLE builds ADD COLUMN position` a second time → `sqlite3.OperationalError: duplicate column name: position` at startup, uncaught (F4). Both columns were added in the same release, so shipped 0.5.0 databases never hit it — but the next migration written this way can.
- **Trigger/Repro:** `ALTER TABLE builds ADD COLUMN position REAL` on a 0.4.0 DB, then start.
- **Suggested fix:** refresh the column set after each `ALTER`, or use one `for column, ddl in (...)` loop against a set that is updated.
- **Test to add:** construct the pathological DB state and assert `Database(path)` opens.

### F14. [Low] `execute()` hands out the shared connection's cursor outside the lock

- **Location:** `src/zim_pro/db.py:337-339`; consumer `src/zim_pro/catalog/opds.py:76-90`
- **Category:** thread-safety / encapsulation
- **Confidence:** medium (no current misuse, but the API invites it)
- **Evidence:**
  ```python
  def execute(self, sql, params=()):
      with self._lock:
          return self._conn.execute(sql, params)
  ```
  and `opds.store` does `with db._lock: db.execute("BEGIN"); db._conn.executemany(...)`.
- **Impact:** the class docstring says "serialised with a lock", but any caller can iterate/fetch from the returned cursor after the lock is released, on a connection another thread is using; that is why `opds.store` reaches into `db._conn`/`db._lock` (`opds.py:77-81`). `BEGIN`/`COMMIT` are also exposed as raw SQL, so `executemany()` has no way to know it is already inside a transaction.
- **Suggested fix:** return only `lastrowid`/`rowcount`, or add `insert()`/`transaction()` helpers; give `opds.store` a `db.executemany` inside a `db.transaction()` context manager implemented with a depth counter or `SAVEPOINT`. Note `executemany()`'s `except BaseException: ROLLBACK` (`db.py:347-348`) currently replaces the original error with `cannot rollback - no transaction is active` if a transaction is already open — suppress errors from the rollback.
- **Test to add:** `db.execute("BEGIN"); db.set_settings({...})` → one clear exception and a still-usable connection; a `transaction()` rollback test.

### F15. [Low] `access.create_key` can return `None`; `check_key` treats a write failure as an auth failure

- **Location:** `src/zim_pro/access.py:31-43,50-63`
- **Category:** access control / type mismatch
- **Confidence:** medium
- **Evidence:**
  ```python
  row = db.one("SELECT id, name, hint, created, last_used FROM mcp_keys WHERE id = ?", ...)
  return row, key            # annotated tuple[dict, str]
  ...
  if time.time() - (row["last_used"] or 0) > SEEN_EVERY:
      db.execute("UPDATE mcp_keys SET last_used = ...")
  return True
  ```
- **Impact:** `api/access.py:58` does `{**row, "key": key}`; if the SELECT ever returns `None` the endpoint 500s on the type lie. If the `UPDATE last_used` fails (DB locked per F10, read-only volume), the exception escapes **before** `return True`, so a valid key is rejected with a 500 on every request until the DB accepts writes again. `hmac.compare_digest` on `str` also raises `TypeError` for non-ASCII input, so a corrupted `digest` row bricks MCP auth.
- **Suggested fix:** update `last_used` best-effort in `contextlib.suppress(sqlite3.Error)` (or via a queue), compare `bytes.fromhex(...)`, and make `create_key` raise if the read-back fails.
- **Test to add:** monkeypatch `db.execute` to raise for the UPDATE and assert `check_key` still returns `True`.

### F16. [Low] Settings coercion is lossy and unknown keys are silently dropped

- **Location:** `src/zim_pro/db.py:406-411`; `src/zim_pro/api/library.py:231-245`
- **Category:** validation / silent failure
- **Confidence:** high
- **Evidence:**
  ```python
  [(k, json.dumps(v)) for k, v in values.items() if k in DEFAULT_SETTINGS]
  ```
  and
  ```python
  if isinstance(default, bool):
      clean[key] = bool(value)
  ```
- **Impact:** `{"keep_old_versions": "false"}` stores `true` (`bool("false") is True`), and `{"max_concurrent_downloads": true}` becomes `1` via `int()` only after the numeric branch. A typo in a key (`max_concurrent_download`) is accepted with 200 and ignored, so the UI shows the old value with no error. `set_settings` silently drops the same keys a second time, hiding the caller's mistake from tests.
- **Suggested fix:** validate with an explicit pydantic model (one field per setting) and return 422 for unknown keys; for booleans accept only real JSON booleans or `"true"/"false"/"1"/"0"`.
- **Test to add:** `PUT /api/settings {"keep_old_versions": "false"}` → stored `False` (or 422); unknown key → 422.

### F17. [Low] `Context.archive()` only translates "missing book"; corrupt/missing files become 500s

- **Location:** `src/zim_pro/context.py:38-45`; caller `src/zim_pro/api/content.py:23-27`
- **Category:** unhandled exceptions
- **Confidence:** high
- **Evidence:**
  ```python
  book = self.book(book_id)
  if book is None:
      raise LookupError(f"no book named {book_id!r}")
  return book, self.pool.get(book["path"])
  ```
  `_open()` catches only `LookupError`.
- **Impact:** a ZIM deleted between scans, truncated by a failed download, or unreadable by libzim raises `RuntimeError`/`ArchiveError` from `Archive(path)` → uncaught → 500 for `/content/...`, `/markdown/...`, `/random/...`, and `/api/books/{id}/illustration` (that one catches everything, others do not). The book row still exists until the next 300 s scan, so the UI advertises a broken book.
- **Suggested fix:** catch libzim errors in `archive()` and re-raise as `LookupError` with the path, or invalidate the pool entry and trigger a rescan.
- **Test to add:** insert a `books` row whose path does not exist, then `GET /content/<id>` → 404 (not 500).

### F18. [Low/Nit] Static/SPA serving can fail at startup and hides 404s

- **Location:** `src/zim_pro/main.py:172-183`
- **Category:** connectivity / static files
- **Confidence:** high
- **Evidence:**
  ```python
  app.mount("/assets", StaticFiles(directory=web_dir / "assets"), name="assets")
  ...
  @app.get("/{path:path}", include_in_schema=False)
  def spa(path: str):
      if path.startswith(("api/", "content/", "mcp")):
          raise HTTPException(404)
      ...
      return FileResponse(web_dir / "index.html", ...)
  ```
- **Impact:** `find_web_dir` only checks `index.html`, so a `web_dir` with an `index.html` but no `assets/` makes `create_app` raise `RuntimeError: Directory ... does not exist` (Starlette `check_dir=True`), i.e. a startup crash from a packaging mistake. Any unknown path that is not exactly under those prefixes (`/api`, `/content`, `/favicon.ico`, `/robots.txt`) returns the SPA with HTTP 200, which hides broken links and skews the request metrics (`route_group` counts them as `pages`).
- **Suggested fix:** check `(web_dir/"assets").is_dir()` before mounting (fall back or warn); return a real 404 for `/api`, `/content` and known static file names.
- **Test to add:** `find_web_dir` with a temp dir containing only `index.html` → no mount/clear error; `GET /api` → 404.

---

## Database schema & integrity notes

- **Tables:** `books`, `shadowed`, `catalog`, `downloads`, `history`, `ai_providers`, `chats`, `chat_messages`, `links`, `uploads`, `builds`, `feeds`, `feed_sources`, `feed_items`, `searches`, `categories`, `mcp_keys`, `settings` (18).
- **Primary keys:** text natural keys for `books.id` (archive stem), `shadowed.path`, `catalog.uuid`, `ai_providers.id`, `chats.id`, `categories.name` (`COLLATE NOCASE`), `settings.key`; `INTEGER PRIMARY KEY AUTOINCREMENT` for the rest. `searches` mixes an autoincrement `id` with `UNIQUE(query, book, category)`.
- **Foreign keys:** exactly one (`chat_messages.chat_id → chats.id ON DELETE CASCADE`), and it is redundant because `api/ai.py:273-275` deletes the messages explicitly. See F3 for the eleven unenforced references.
- **`ON DELETE`:** therefore effectively unspecified everywhere else. Feed deletion (`api/feeds.py:147`) manually removes `feed_items`/`feed_sources`; link/upload deletion removes nothing; scanner book deletion removes nothing.
- **`CHECK`:** none. Boolean columns (`enabled`, `images`, `video`, `subdomains`, `full_text`, `has_ftindex`, `has_illustration`) are plain `INTEGER`; state columns are plain `TEXT` (F7).
- **"Absent" has three conventions:** `''` (`links/uploads/feeds.book_id`), `NULL` (`builds.position`, `last_built`, `last_synced`) and a nullable error flag (`books.error`, used by `Context.book` to hide broken books).
- **Indexes:** `catalog_stem`, `catalog_name`, `chat_messages_chat`, `feed_items_feed`. Missing for the hot shapes: `downloads(catalog_uuid,state)`, `builds(kind,source_id,state)`, `builds(state)`, `downloads(state)`, `books(category)`, `books(name,flavour)`, `links(book_id)`.
- **Type drift:** `downloads.position` is `INTEGER NOT NULL DEFAULT 0` while `builds.position` is `REAL` (nullable), both fed fractional scores from `new_place()` (`queues.py:236-253`). Integer affinity preserves the REAL values today, but the two sibling queues now disagree on type and absence semantics.
- **Timestamps:** `unixepoch('subsec')` everywhere except `downloads.created/updated` (`unixepoch()`, integer seconds). `searches.ts` is written but never read for ordering (`list_searches` uses `id DESC`).
- **Transactions:** `executemany()` is the only helper (used by `set_settings` only); `opds.store` rolls its own through private attributes; `add_search` does three writes without one (safe under the RLock, but a crash mid-way loses the entry); `Scanner._scan` writes one statement at a time outside any transaction (an interrupted scan leaves a partially updated library, not corruption).
- **Pragmas:** WAL, `synchronous=NORMAL`, `foreign_keys=ON` once per connection (correct, one connection per process). The `journal_mode=WAL` result is ignored, so a filesystem that refuses WAL silently leaves rollback-journal mode. No explicit `busy_timeout` (F10), no `user_version` (F12).
- **Concurrency model:** one connection, `check_same_thread=False`, `isolation_level=None`, `RLock`. Sync endpoints run in the threadpool, so the lock serialises correctly and `all/one/scalar` copy rows before releasing. The weak spots are `execute()` returning a cursor (F14) and long transactions holding the lock while the loop waits (F6).

## Normalization assessment

- **Sibling source tables diverge.** `links`, `uploads`, `feeds` all mean "a thing a ZIM is made from" and all carry `title, category, icon, book_id, created`, but each has a different column set and type vocabulary: `uploads.kind` vs `links.engine`; crawl options are links-only; `images/video` are duplicated in all three. Every shared field must be added and migrated three times.
- **`builds.source_id` is polymorphic** (`kind` ∈ `link|upload|feed`) with no constraint tying the pair together. `(kind='feed', source_id=<links.id>)` is accepted and dispatches into the wrong table — the main reason F3 cannot simply add a FK.
- **RSS sources are modelled twice:** `links.feeds` is a newline-joined string (`api/build.py:109`, parsed at `builder/jobs.py:471`) while `feeds` + `feed_sources` use a child table with `UNIQUE(feed_id,url)`.
- **`books.category` is derived and last-writer-wins.** `scanner.file_under_categories` builds `chosen[book_id]` from `links`, then `feeds`, then `uploads` (`scanner.py:80-96`), so if two sources produce the same book id the upload's category silently wins; `''` in a source means `custom`. Nothing currently exposes a manual book category, which is the only thing keeping this consistent.
- **Deliberate, sound snapshots:** `downloads.title/url/size/from_date/to_date/replaces`, `history.*` and `builds.title` copy at enqueue time, and `catalog` is a rebuildable cache (`DELETE` + re-insert). Downloads copying what they need instead of joining `catalog` is why `downloads.catalog_uuid` has no FK — the right call.
- **JSON-in-TEXT:** `builds.log/report/params` and `chat_messages.sources/queries/steps` are opaque blobs; acceptable while unqueried, but they must be read defensively (F5). `links.sitemaps/feeds` are newline lists rather than JSON.
- **`settings` doubles as a metadata store** via the `_` prefix; nothing prevents a future real setting from starting with `_`, and stale rows for removed settings are kept forever.
- **Retention is inconsistent:** `searches` capped at 200; `builds`/`downloads` cleared manually; `history`, `chat_messages`, `feed_items` unbounded.

## Testing gaps

Mapped from `tests/test_units.py` / `tests/test_system.py` (plus the two tests in `tests/test_network.py:360-385,425` that touch this scope):

- **`db.py`:** `add_search`/`list_searches` are well covered (`test_units.py:622-638`). Nothing covers `_migrate()` on an old schema or twice on one file; the F13 stale-column state; `executemany()` rollback; `get_settings`/`get_meta` with invalid JSON; unknown-key filtering; `get_setting` on an unknown key (`KeyError`); `close()` then use; an unwritable/read-only path (F4); or that the pragmas are actually on.
- **`config.py`:** no tests. `KIWIX_*` env parsing, `db_path`, and the `admin_token=""` degenerate case are untested.
- **`context.py`:** `book()`/`archive()` are never exercised (no test hits `/api/books/{id}` or `/content/...`); `require_admin`'s 401 branch is untested because no test constructs `Config(admin_token=...)` (grep: zero hits). Add the F8 matrix.
- **`events.py`:** no direct test; `EventBus()` is only an inert argument in `test_builder.py:632`, `test_feeds.py:83`, `test_network.py:77`. Untested: delivery, cross-thread publish, publish before `bind()`, queue-full drop, publish after loop close, unsubscribe, and `/api/events` itself.
- **`access.py`:** the lifecycle is covered (`test_network.py:360-385`). Untested: the non-`zp_mcp_` fast path, the `SEEN_EVERY` throttle, blank/whitespace name → "Unnamed key", truncation at 80, deleting a nonexistent id, and the F15 write-failure path.
- **`main.py`:** startup is exercised by `TestClient` fixtures; shutdown ordering/failure is not. Untested: `find_web_dir`'s three candidates, the SPA guard, `/assets` mounting with a missing folder (F18), the `timed` middleware's 500/401/slow classification, `routes.extend(mcp_app.routes)` with a real MCP `initialize`/`tools/list` handshake (the test only asserts `status != 401`), and a manager failing during startup (F1).
- **`__init__.py`:** the CLI is untested — `serve` vs `mcp`, the no-argument default, and the `mcp` mode's missing managers (F9).
- **Concurrency:** nothing runs two threads against one `Database`. The F2 duplicate-enqueue race, the F14 cursor path, and two `Database` objects on one file (F10) have no tests.
- **Settings API:** `GET /api/settings` is tested; `PUT` only for the secret-mask/VPN-URL cases (`test_network.py:329-341`). Unknown-key rejection, boolean coercion (F16) and the `log_level` branch are untested; `/api/health` only happy-path (`test_network.py:425`).

## What is done well

- **No SQL injection surface found.** Values are always bound (`?`/named params); the f-strings that build SQL interpolate only compile-time constants or pydantic field names (`api/build.py:162`, `api/feeds.py:115`, `api/system.py:145-151`, `api/organise.py:128`), and `SORTS`, `QUEUES`, `SOURCE_TABLES`, `icons.KINDS` are whitelists.
- **Reads are copied under the lock** (`all`/`one`/`scalar` return plain dicts/scalars), removing a whole class of "row used after cursor reuse" bugs.
- **MCP keys are stored only as SHA-256 digests**, compared with `hmac.compare_digest`, listed without the digest and shown in full exactly once; `last_used` is throttled to one write per minute.
- **Secret handling in settings is careful:** `SECRET_SETTINGS` never leave the server (masked), the mask round-trips without overwriting the stored value, and `Provider.public()` omits API keys.
- **Corrupt library files degrade gracefully:** `read_book` catches per-file exceptions into `books.error` instead of failing the scan, and `Context.book` hides those books.
- **The catalog refresh is one transaction**, so readers never see a half-replaced catalog; the refresh timestamp/error are surfaced in `/api/status` and the monitor.
- **The event bus is non-blocking by design** (`put_nowait` + drop, full-state snapshots), so a slow SSE client cannot stall a download; worker-thread publishes are marshalled with `call_soon_threadsafe`.
- **Tests cover the non-obvious behaviour** most projects forget: search-history dedupe/cap, category refiling, provider-migration idempotency, queue reordering, log redaction, and the MCP key lifecycle.

## Unverified / out of scope

- Everything under `builder/`, `downloads/engine.py`, `feeds/sync.py`, `retrieval/`, `ai.py`, `mcp_server.py` beyond the call sites quoted above — considered only as it reads/writes the schema.
- Whether the SQLite bundled in release artefacts is ≥ 3.42 (needed by `unixepoch('subsec')`); not checked in packaging/Docker.
- WAL behaviour on the filesystems used by Docker volume/NAS deployments (whether the `journal_mode=WAL` PRAGMA result is really `wal`) — cannot be determined statically.
- Whether two concurrent builds of the same link are reachable in the shipped UI (double-click + `max_concurrent_builds > 1`) beyond the API-level race in F2.
- The `BaseHTTPMiddleware` + `StreamingResponse` interaction for `/api/events` (both middlewares wrap the SSE response); Starlette versions differ, so this needs an observed test rather than a static claim.
- Resource limits: maximum `/api/events` subscribers, `feed_items` growth, and the production size of the Kiwix catalog feed — no data in the repo.
