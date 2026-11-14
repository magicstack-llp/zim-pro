# Test Suite Review — r1

**Scope:** backend tests (`tests/test_units.py`, `test_engine.py`, `test_network.py`, `test_feeds.py`, `test_builder.py`, `test_system.py`), fixtures, frontend test (`web/tests/tidy.test.ts`, `web/package.json`), pytest config and CI.
**Commit:** `cbb1a4e` (Release 0.5.0), working tree clean apart from untracked `docs/`.
**Date:** 2026-11-14.
**Method:** read every test and all 61 source modules, ran the suite twice, ran the frontend suite, inspected CI. `pytest-cov` and `coverage` are **not installed**, so line coverage was measured with a temporary `sys.monitoring` pytest plugin under `/tmp/opencode` (no repo files changed, nothing installed). No test or source file was modified.

---

## Executive summary

- **140 passed, 0 failed, 0 errors, 0 skipped, 0 xfail** — `uv run pytest -q`, 17.48 s first run, 14.54 s rerun (140 collected from 117 test functions).
- **36 warnings**, all `ResourceWarning: unclosed database` (sqlite connections never closed by tests). No deprecation, socket, or pending-task warnings.
- Frontend: `npm test` (Node's built-in runner, not vitest) passes **9/9 in 171 ms**.
- **Line coverage 69.9 %** (6,495 of 9,289 executable statement lines). That number is flattering: builder/crawler/feeds internals are 82–97 %, while **every read/serve path, the whole AI answer pipeline, the download orchestrator and MCP tools are 20–38 %**, largely because only module-level route registration executes.
- Highest-risk untested areas: `/content` archive serving (ETag/Range/HEAD), admin-token auth on every guarded write route, `POST /api/ask` SSE + `retrieval.plan/rank`, download manager lifecycle, MCP tools, scheduler auto-update/rebuild loops, DB migrations from old schemas.
- Test style is mostly high-value integration: in-process HTTP transports, real ZIM writes/reads, real EPUB/PDF conversion. There are no mock-only assertions, no skipped tests, and no un-awaited coroutines. Main quality problems are missing boundaries (auth, serving, AI, downloads) and test hygiene (36 leaked DB handles, lifespan bypassed, real-time sleeps).

---

## Test run results

### Backend

```
$ uv run pytest -q
........................................................................ [ 51%]
....................................................................     [100%]
140 passed in 17.48s
```

Repeat: `140 passed in 14.54s`. No test needed an individual rerun; no flakiness observed. Slowest tests (`--durations`):

| Test | Time | Note |
|---|---:|---|
| `test_network.py::test_older_gluetun_is_asked_on_its_own_route` | 3.02 s | Missing the `quick` fixture, so `change_ip()` really sleeps 1 s and `_wait_for_address()` 2 s (`network.py:287,313`). |
| `test_network.py::test_connections_lost_through_the_proxy_do_not_stop_the_fetching` | 0.71 s | Real proxy server + 0.3 s pool timeout. |
| `test_feeds.py::test_a_sync_only_fetches_what_is_new_and_keeps_the_zim_current` | 0.71 s | Real ZIM creation and verification. |
| `test_network.py::test_a_site_that_never_finishes_answering_is_given_up_on` | 0.63 s | Real trickle server, 0.3 s timeout. |

Warnings summary: 36 warnings, every one `ResourceWarning: unclosed database in <sqlite3.Connection ...>` raised at interpreter GC. They come from `Database(":memory:")` objects in `test_units`/`test_system` and from `TestClient(create_app(...))` fixtures that are never closed (`test_network.py:245-248`, `test_feeds.py:491-497`). No `DeprecationWarning`, `RuntimeWarning` un-awaited coroutine, or unclosed-socket warning was emitted.

Coverage command used (no external package):

```
$ PYTHONPATH=/tmp/opencode uv run pytest -q -p covplug   # 140 passed in 13.77s
$ uv run python /tmp/opencode/covreport.py                # TOTAL 9289 stmts, 6495 hit, 69.9%
```

`uv run python -c "import pytest_cov"` → `ModuleNotFoundError: No module named 'pytest_cov'`; `coverage` likewise absent. **So this is line coverage, not branch coverage**, and executable-line counts exclude comments, docstrings and the SQL schema strings. API-module percentages are inflated by import-time decorator/route code; e.g. `api/catalog.py` shows 35 % while every handler has 0 % executed statements.

### Frontend

```
$ cd web && npm test
> node --test tests/*.test.ts
✔ reasoning and control tokens are removed
...
ℹ tests 9   ℹ pass 9   ℹ fail 0   ℹ duration_ms 171.3
```

- **Vitest is not configured and not installed.** `package.json` has no vitest/jsdom/testing-library dependency; `npx --no-install vitest run` fails with `npm error npx canceled due to missing packages ... ["vitest@5.1.0"]`.
- `npm test` works without installing anything: the only test imports `node:assert`, `node:test` and `../src/lib/tidy.ts`, and Node 24.19 strips TypeScript types natively.
- `./node_modules/.bin/tsc --noEmit` exits 0, but `tsconfig.json` includes only `["src"]`, so `web/tests/tidy.test.ts` is **not type-checked**.

### CI

`.github/workflows/ci.yml` runs, on `ubuntu-latest` only: `uv sync --frozen`, `ruff check src tests`, `ruff format --check src tests`, `uv run pytest`; and for web `npm ci`, `npm test`, `npm run build`. No coverage, no timeout, no Python/OS matrix, no Redis service, no network sandbox.

---

## Coverage matrix

Legend: **D** = module imported/called directly by a test; **D\*** = only a helper/private function directly tested; **I** = reached only indirectly through the app/manager; **—** = never reached. LOC = physical lines. `cov%` = statement-line coverage measured above. Rating is about **behavioural** coverage, not the percentage alone.

| Module | LOC | D | I | cov% | Rating | Top untested behaviour |
|---|---:|:-:|:-:|---:|---|---|
| `__init__.py` | 40 | — | — | 14 | None | CLI `main()` entry point, arg handling |
| `config.py` | 86 | — | ✔ | 100 | Good | Ambient-env override/`.env` loading not isolated |
| `context.py` | 59 | — | ✔ | 80 | Partial | `require_admin` token rejection |
| `events.py` | 42 | — | ✔ | 75 | Weak | `subscribe`, `unsubscribe`, cross-thread `publish`, queue-full drop |
| `access.py` | 63 | — | ✔ | 100 | Good | `SEEN_EVERY` timing branch |
| `db.py` | 422 | ✔ | ✔ | 88 | Good | Every `_migrate` ALTER branch |
| `main.py` | 185 | — | ✔ | 94 | Partial | admin-token bypass, `send_logs`, SPA static path guard |
| `ai.py` | 186 | ✔ | ✔ | 82 | Partial | `library_sources`, `public_source`, history parsing |
| `retrieval.py` | 536 | ✔ | ✔ | 71 | Partial | `scope_books`, `plan`, `rank` (0 %) |
| `providers.py` | 601 | ✔ | ✔ | 56 | Weak | every HTTP call: `list_models`, streaming, grounding, speech |
| `websearch.py` | 85 | — | — | 18 | None | all four services and all error mappings |
| `mcp_server.py` | 202 | — | — | 31 | None | every MCP tool body |
| `monitor.py` | 550 | — | ✔ | 64 | Partial | `sample`, `_watch_jobs`, stall/quiet detection, `_merge` |
| `logs.py` | 291 | ✔ | ✔ | 91 | Good | Redis `take_outbox` |
| `queues.py` | 260 | ✔ | ✔ | 83 | Partial | Redis `place`/`close`, `make_broker` Redis branch |
| `network.py` | 331 | ✔ | ✔ | 88 | Good | solver property, error-text mapping |
| `scheduler.py` | 102 | — | — | 38 | None | auto-update queueing, rebuild/feed scheduling, run loop |
| `library/archive_pool.py` | 35 | — | — | 50 | Weak | `get()` LRU/concurrency/`invalidate` |
| `library/naming.py` | 46 | ✔ | — | 96 | Good | — |
| `library/search.py` | 71 | — | — | 29 | None | `search_book`, `suggest`, `snippet` |
| `library/reader.py` | 115 | — | — | 45 | None | `find_entry`, `main_path`, `random_path`, `read_article` |
| `library/scanner.py` | 198 | ✔ | ✔ | 80 | Partial | `leftovers` |
| `library/article.py` | 273 | ✔ | — | 81 | Partial | `outline`, `source_url`, less common markdown branches |
| `catalog/opds.py` | 103 | ✔ | — | 84 | Partial | `refresh` network/error path, empty feed |
| `catalog/updates.py` | 64 | ✔ | — | 97 | Good | — |
| `downloads/metalink.py` | 69 | ✔ | — | 94 | Good | malformed numeric fields |
| `downloads/engine.py` | 314 | ✔ | — | 88 | Good | `RateLimiter`, retry/backoff, all-mirrors-fail, checksum failure |
| `downloads/manager.py` | 411 | — | ✔ | 33 | None | enqueue, run, pause/resume/cancel, install, history (0 %) |
| `feeds/parse.py` | 127 | ✔ | — | 97 | Good | — |
| `feeds/manager.py` | 117 | — | ✔ | 90 | Good | `forget`, paused-scheduler branches |
| `feeds/sync.py` | 423 | — | ✔ | 92 | Good | non-HTML page, no-text failure, duplicate article, cache prune |
| `builder/report.py` | 101 | ✔ | — | 96 | Good | thread `waiting` |
| `builder/sitemap.py` | 78 | ✔ | — | 93 | Good | — |
| `builder/extract.py` | 409 | ✔ | — | 86 | Good | CSS resource rewriting (`css_resources`/`localise_css`, 0 %) |
| `builder/crawler.py` | 1,099 | ✔ | — | 89 | Good | pacing, CSS resources, icon fallbacks |
| `builder/writer.py` | 347 | ✔ | — | 94 | Good | `describe` |
| `builder/jobs.py` | 627 | — | ✔ | 82 | Partial | remove/clear/retry-failed, active pause, `_adopt` (dead code) |
| `builder/icons.py` | 50 | ✔ | — | 100 | Good | — |
| `builder/zimit.py` | 127 | — | — | 21 | None | whole Docker-API capture path |
| `builder/convert/epub.py` | 172 | ✔ | — | 86 | Good | navigation-label edge cases |
| `builder/convert/pdf.py` | 135 | ✔ | — | 90 | Good | some page/image branches |
| `builder/convert/mobi.py` | 92 | — | — | 24 | None | whole MOBI/AZW3 conversion |
| `api/access.py` | 114 | — | ✔ | 69 | Partial | `latest_release` GitHub fetch, version fallbacks |
| `api/ai.py` | 620 | D\* | — | 20 | None | providers CRUD, `/ask` SSE, speech, chats, `_pick_model` |
| `api/build.py` | 528 | D\* | ✔ | 56 | Partial | uploads, link CRUD outside CSV, build/job actions |
| `api/catalog.py` | 146 | — | — | 35 | None | catalog filters/sorts/pagination, updates, start/start-all |
| `api/content.py` | 119 | — | — | 26 | None | Range/HEAD/ETag serving, markdown, redirects, 404s |
| `api/downloads.py` | 86 | — | — | 33 | None | create/actions/delete, SSE `/api/events` |
| `api/feeds.py` | 432 | D\* | — | 49 | Partial | feed CRUD/validation, items, check, run actions (OPML only) |
| `api/library.py` | 291 | — | ✔ | 55 | Partial | books, search/suggest, delete/rescan/leftovers, illustration |
| `api/network.py` | 63 | — | — | 54 | None | status/change-IP/test connection endpoints |
| `api/organise.py` | 196 | ✔ | — | 93 | Good | minor icon branches |
| `api/system.py` | 334 | — | ✔ | 82 | Good | clear logs, job actions |

Total: **12,653 physical LOC** in source, **9,289 executable lines**, 2,830 lines of test code plus 89 lines of fixtures (22 % of source lines).

---

## Highest-risk untested paths

| Module | Untested path (file:line) | Risk |
|---|---|---|
| `downloads/manager.py` | `enqueue` (117), `_download` (305), `_install` (391), `_check_free_space` (381), `_fetch_metalink` (371) | The whole download lifecycle is unverified. `_install` validates the ZIM, atomically replaces the file, deletes the old release and rescans; `_fetch_metalink` uses a **real** `httpx.AsyncClient` with no injection seam, so it is hard to test as written. |
| `downloads/manager.py` | `pause` (155), `cancel` (175), `resume` (167), cancel handling (347-356), `_persist` (292) | Resume/cancel after restart, `.part` retention/deletion, and matrix persistence are exactly the crash-recovery behaviours users depend on. |
| `api/content.py` | `content` (77) incl. Range parsing (101-113), ETag/304 (87-94), HEAD (115), redirect (83-84) | No test ever opens an archive through the app. Off-by-one ranges, 416, content-length and ETag semantics are all unverified. |
| `api/content.py` | `markdown` (58), `_open` (21), `book_home`/`random_article`/`markdown_home` (30,39,49) | 404/415 behaviour, query-string lookup, MIME negotiation, redirect loops. |
| `mcp_server.py` | `search` (78), `get_article` (115) incl. pagination (131-143), `list_books` (47), `start_update` (182), `suggest` (97) | MCP is a headline feature; only the auth middleware is tested. Pagination maths and the `mcp_allow_writes` gate are unverified. |
| `api/ai.py` | `ask` (330) and `stream` (372-620) | Entire SSE pipeline: planning, steps, library/web sources, citation rewriting, partial answer persistence, `regenerate`, error/done events. |
| `api/ai.py` | `_pick_model` (286), `_message` (203), `speech` (158), provider CRUD (48-99), `test_provider` (112) | Model fallback/disabled-provider errors, legacy message reconstruction, TTS failure mapping, provider deletion fallbacks. |
| `retrieval.py` | `scope_books` (120), `plan` (237), `rank` (448) | The ranking core (60 statements) and the model planner are untested; only parsing/helpers are covered. A regression here degrades every AI answer silently. |
| `providers.py` | `list_models` (263), `_stream_chat` (425), `stream_chat` (401), `_gemini_grounded` (479), `complete` (530), `speech` (560) | All provider HTTP/SSE parsing, reasoning tags, citations, retries and error mapping are unexecuted. |
| `websearch.py` | `search` (29-85) | Four services, missing SearXNG URL, non-200 with hints, non-JSON, malformed result filtering — all untested. |
| `scheduler.py` | `queue_auto_updates` (30), `_rebuild_due_links` (42), `_check_due_feeds` (58), `run` (79) | Policy `auto` vs `pinned`, rebuild-due SQL, feed-due SQL, retry after catalog failure, on-disk rescan. |
| `api/catalog.py` | `catalog` (26), `facets` (75), `updates` (111), `start_update` (124), `start_all` (134), `refresh` (95) | Search/sort/pagination, local-vs-catalog matching, `pinned` exclusion, catalog error propagation to 502. |
| `api/build.py` | `upload` (399), `edit_link`/`delete_link` (167,180), `build_link` (196), `import_links`/`export_links` (327,351), `act`/`delete_job` (505,526) | Upload signature/size/empty-file guards (413/415/422), filename sanitisation, job action dispatch. |
| `api/library.py` | `search` (119), `suggest` (167), `delete_book` (93), `rescan` (107), `illustration` (67), `leftovers` (114) | Cross-book search, file deletion, pool invalidation, scanner results. |
| `api/feeds.py` | `_clean` (44), `add/edit/delete_feed` (108,125,138), `list_items` (188), `check` (253), `act`/`delete_run` (409,429), `export_opml` (370) | Feed validation (mode/rate/interval), CRUD, item filtering, URL probe, run control; only OPML import is tested. |
| `api/downloads.py` | `act` (39), `create` (23), `clear_finished` (33), `delete_download` (59), SSE `events` (65) | Unknown-action 404, 404/409 mapping, and the SSE keepalive/disconnect path. |
| `monitor.py` | `sample` (213), `queue_counts` (262), `_watch_jobs` (282), `_check_moved` (308), `quiet_for` (326), `_merge` (514) | Stall detection, CPU/RSS sampling, queue counters, series merging; `/api/monitor` only tests render-time aggregation. |
| `library/reader.py`, `search.py`, `archive_pool.py` | `find_entry` (reader:11), `main_path` (26), `random_path` (37), `read_article` (106); `search_book` (search:35), `snippet` (53), `suggest` (20); `get` (pool:15) | Old namespace (`A/…`), Zimit `/path?query`, redirect chains, title-search fallback, snippet extraction, LRU eviction. No API test opens a ZIM, so these are entirely dark. |
| `db.py` | `_migrate` ALTERs (291, 296-297, 311, 317, 321, 324, 331, 334) | No test starts from an older schema; every migration branch is unexecuted. |
| `context.py`, `main.py` | `require_admin` (context:52-59), MCP admin-token branch (main:108-121) | Every guarded write route depends on this guard; tests never set `KIWIX_ADMIN_TOKEN`, so the 401 and constant-time comparison paths never run. |
| `builder/jobs.py` | `remove` (225), `clear_finished` (232), `retry_failed` (288), active `pause` (183), `_retire` with `keep_old_versions` (607) | Only queued pause/cancel and restart recovery are tested; the active-build pause and old-version retention paths are not. |
| `builder/zimit.py`, `convert/mobi.py`, `extract.py` | `capture` (zimit:47), `convert` (mobi:17), `css_resources`/`localise_css` (extract:347,357) | Docker-socket capture, MOBI/AZW3 conversion, and CSS background/font localisation are wholly untested. |
| `feeds/sync.py` | non-HTML address (238), no-text failure (257-265), duplicate article (267-274), cache prune (410) | Failure and de-duplication semantics; tests cover the happy incremental path and the 404/feed-text fallback. |
| `queues.py`, `events.py`, `main.py` | Redis `place` (136), `close` (221), `make_broker` Redis branch (256); SSE subscribe/publish (events:16,36); `send_logs` (main:57) | Redis parity is asserted only for `waiting`/`paused`/`send_logs` with fakeredis; no manager ever runs on Redis, and the SSE fan-out is never exercised. |
| `api/network.py`, `api/access.py` | network status/change-IP/test (network:15,22,47); `latest_release` (access:95), `_numbers` (86) | VPN endpoints and the GitHub release check (real network, no injection point). |

---

## Test quality findings

1. **[High] The serving/read half of the app has no end-to-end test.** Every function in `api/content.py`, `library/search.py`, `library/reader.py` and `archive_pool.get` has 0 % executed statements, and no test calls `/content`, `/api/search`, `/api/books/{id}`, `/markdown` or `/random`. Builder tests open archives with `libzim` directly, not through `Context.archive`, so ETag/Range/path-lookup regressions cannot be caught.

2. **[High] Admin authentication is never enabled.** No test passes `admin_token` to `Config` (`Config.admin_token` default `None`). `context.py:52-59` executes only the "no token" early return, and the MCP admin bypass in `main.py:113-115` never runs. Every guarded route — the `/api/build`, `/api/feeds` and `/api/ai` routers (router-level guard) plus the 30 routes with `dependencies=[Depends(require_admin)]` — is therefore tested only in its unguarded mode.

3. **[High] The AI answer pipeline is tested only through pure helpers.** `retrieval.plan` (237), `retrieval.rank` (448), `providers.stream_chat` (401), `websearch.search` (29) and `api/ai.ask` (330) are all unexecuted. The suite verifies prompts, citation numbering and reasoning-tag splitting, but not that an answer is retrieved, streamed, persisted, numbered or regenerated.

4. **[High] `DownloadManager` is essentially a black box.** `downloads/engine.py` is well covered (88 %), but the orchestrator that turns a catalog UUID into an installed ZIM has 0 % on every operation: enqueue/duplicate detection, metalink fetch, free-space check, install/retire, pause/cancel persistence, history. Engine tests also create the part file and client themselves, bypassing the manager's wiring.

5. **[Medium] `!= 401` assertions can pass on a 500.** In `tests/test_network.py`:

   ```python
   client = TestClient(app, raise_server_exceptions=False)   # :363
   assert client.post("/mcp", json={}).status_code != 401    # :366
   ...
   assert client.post("/mcp", json={}, headers=right).status_code != 401   # :380
   ```

   Because server exceptions are turned into 500 responses, these three assertions would pass if `/mcp` were broken. Assert the exact expected status (or `in (200, 400, 406)`), not `!= 401`.

6. **[Medium] Most API tests bypass the lifespan.** The shared client fixture returns an un-entered client:

   ```python
   def client(tmp_path):
       ...
       return TestClient(app)  # not entered: the background work is not what is tested here
   ```

   (`tests/test_network.py:245-248`; same pattern in `tests/test_feeds.py:497`). Startup scan, downloads/builds/feeds start, monitor start, MCP session manager and shutdown are only exercised by the two `app_client` tests in `test_system.py`. Lifespan regressions are invisible to these nine API tests.

7. **[Medium] 36 unclosed-database warnings.** Every `Database(...)` and `TestClient`-owned app leaves its connection open; pytest reports 36 `ResourceWarning: unclosed database`. There is no `conftest.py` fixture that closes `ctx.db` or enters `TestClient` as a context manager. The warnings are suppressed by the default warning filter, so leaks are invisible in CI.

8. **[Medium] One test spends 3 s in real sleeps because it omits the `quick` fixture.** `test_network.py:105` (`test_older_gluetun_is_asked_on_its_own_route`) calls `change_ip()`, which does `await asyncio.sleep(1)` and `_wait_for_address()`'s `await asyncio.sleep(2)` (`network.py:287,313`) — 21 % of the entire suite's wall time for one assertion. It will grow with `CHANGE_TIMEOUT` changes.

9. **[Medium] Global `asyncio.sleep` patching hides retry/backoff behaviour.** Four modules do this:

   ```python
   monkeypatch.setattr(asyncio, "sleep", lambda _seconds: real_sleep(0))  # test_engine.py:49
   ```

   (`test_builder.py:211`, `test_feeds.py:72`, `test_network.py:61`). It patches the stdlib module for the whole process during the test, not the code under test. In `test_engine.py` no test then asserts retry counts or delays; a broken exponential backoff would still pass, and `RateLimiter.take` (engine:44, 22 %) is never exercised.

10. **[Medium] Negative paths are thin outside the crawler.** There are good 404/409/415/422 assertions for categories, icons, settings and OPML, but none for uploads (`api/build.py:399` signature/size/empty), download actions (`api/downloads.py:54`), catalog update start (409/404 at `api/catalog.py:127-130`), feed CRUD or MCP tool errors. Most remaining endpoints are only exercised on the happy path — or not at all.

11. **[Low] Implementation-detail coupling.** `test_engine.py:65` asserts `len(mirror.requests) == 3` with `SEGMENT_BYTES` patched to `4 * PIECE` (`:50`), encoding the current segment planner; `test_engine.py:75` asserts `bitmap.done_count() == 3`. Similarly `fetcher._network = net` (`test_network.py:150,174`) and `broker._down_until = 0` (`test_system.py:162`) poke private attributes. These are useful for unit tests but will break on harmless refactors.

12. **[Low] Time-based white-box test.** `test_builder.py:446-456` starts `threading.Timer(0.05, ...)` and asserts `time.monotonic() - started >= 0.04`. On a heavily loaded runner the timer may fire before `check()` observes the paused state, making the elapsed assertion flaky. A deterministic event/clock would be better.

13. **[Low] Shared helper duplication and fixture drift.** The `no_waiting` fixture is copied in `test_builder.py:208`, `test_feeds.py:69`, `test_network.py:58`; the `manager()` factory in `test_builder.py:627` and `test_feeds.py:78`; RSS/ITEM builders are duplicated between `test_feeds.py` and `tests/fixtures/feed.xml`. There is no `tests/conftest.py`.

14. **[Low] Ambient environment can change test behaviour.** `Config` is a `pydantic-settings` model with `env_prefix="KIWIX_"` and `env_file=".env"`. Tests pass only `data_dir`/`state_dir`; a developer with `KIWIX_ADMIN_TOKEN`, `KIWIX_REDIS_URL` or `KIWIX_CATALOG_URL` in the environment/`.env` would silently change auth, broker and startup network behaviour, and `test_system.py:206` hard-codes `names["Redis"] == "off"`.

15. **[Low, checked] No mock-only assertions were found.** Transport-level mocks (`httpx.MockTransport`) are used correctly: assertions are on bytes served, requests made, DB rows, ZIM entries and files on disk. Likewise no skipped/xfail tests, no un-awaited coroutines, no assertion-free tests. Those common failure modes are absent.

One latent network seam: the lifespan starts `scheduler.run`, whose first iteration calls `refresh_catalog` and therefore a real HTTPS GET to the configured catalog. In a logged `test_system.py` run no catalog request appeared (the background task may not run before shutdown), but I reproduced the call in a standalone `TestClient` session with a 3 s wait: `catalog refresh failed: All connection attempts failed` when pointed at `http://127.0.0.1:9`. There is no `pytest-socket`/respx guard, so timing or a slower CI machine can make tests touch the internet.

---

## Missing test infrastructure

- **No `pytest-cov`** (nor `coverage`), no coverage config, no `--cov-fail-under`, no CI coverage artifact. Line/branch regressions are invisible.
- **No `pytest-timeout`**: an async deadlock in feeds/builds/downloads would hang CI until the workflow timeout.
- **No `pytest-randomly` / order randomisation** and no `conftest.py`; the suite has never been proven order-independent.
- **No network guard** (`pytest-socket`, `respx` autouse) and no fake for the global `httpx.AsyncClient` used by `_fetch_metalink`, `latest_release`, `opds.refresh` and `websearch.search`.
- **No shared fixtures**: app/client/db factories, a `no_waiting` fixture, an `admin_token` client, an "old schema" database and a "library with a real ZIM" fixture are all re-created ad hoc.
- **One CI job only** (`ubuntu-latest`, resolved Python 3.13); no OS/Python matrix, no Redis service job, no Docker/libzim variant, no timeout, no concurrency cancellation.
- **No frontend test framework beyond `node:test`**: no vitest config, no jsdom/React Testing Library, no component tests, no coverage; `tsconfig` excludes tests, so they are not type-checked.
- **No E2E/browser test** (Playwright), no Docker Compose smoke test, no real-ZIM-mounted API smoke test (`/content`, `/api/search`, `/mcp`).
- **No migration fixture**: no SQL dump/DB from an older release, so `_migrate`'s ALTERs and idempotency are never run.
- **No fake Docker socket** for Zimit, no MOBI/AZW3 fixture, no fixture for Gemini/Perplexity grounding payloads.

---

## Prioritized test plan

### P0 — close the largest correctness blind spots

1. `tests/test_content.py::test_content_serves_real_zim_entry` — build a ZIM with `write_zim`, scan it, then call `/content/{id}/{path}` through `TestClient`; assert bytes, MIME, ETag and `Cache-Control`.
2. `tests/test_content.py::test_range_and_head_requests` — `bytes=0-9`, `bytes=-10`, suffix beyond EOF, invalid range → 206/416, `Content-Range`, and HEAD `Content-Length`.
3. `tests/test_content.py::test_markdown_endpoint_and_missing_entries` — `Accept: text/html` → `text/plain`; otherwise `text/markdown`; missing entry → 404; non-text → 415.
4. `tests/test_auth.py::test_every_state_changing_route_requires_admin_token` — parametrise over the guarded routes (`/api/build*`, `/api/feeds*`, `/api/ai*` and the 30 explicitly guarded routes) and assert 401 without token, success with `Authorization: Bearer`.
5. `tests/test_auth.py::test_mcp_admin_token_and_key_lock` — `KIWIX_ADMIN_TOKEN` unlocks `/mcp`; wrong token 401; key still works.
6. `tests/test_downloads_manager.py::test_enqueue_installs_metalink_download_and_retires_old_release` — fake metalink transport, real bytes, assert DB row `done`, file on disk, old file removed, history row.
7. `tests/test_downloads_manager.py::test_cancel_and_resume_keep_partial_progress` — cancel mid-run, assert `cancelled`, `.part` removed for cancel, bitmap persisted for pause, resume continues from the byte offset.
8. `tests/test_downloads_manager.py::test_free_space_and_invalid_zim_fail_cleanly` — assert `failed` row with message, no `.zim` left, `.part` handling.
9. `tests/test_mcp.py::test_mcp_search_read_and_update_gate` — call tools through the MCP server: `list_books`, `search` (one book and cross-book), `get_article` pagination footer, `list_updates`, `start_update` disabled/enabled.
10. `tests/test_ai_stream.py::test_ask_streams_steps_sources_and_persists_chat` — fake provider streaming; assert SSE event order (`chat`, `step`, `sources`, `delta`, `done`), assistant row with sources/reasoning, and chat title.
11. `tests/test_migrations.py::test_database_from_previous_release_is_migrated` — create an old-schema DB (missing chat `model`/`steps`, provider `enabled`, link `category`/`feeds`, build `position`, download `started`, books `category`), open with `Database`, assert columns and data preserved, run twice.
12. `tests/test_scheduler.py::test_auto_policy_queues_only_new_updates` and `test_due_links_and_feeds_are_queued_once` — fixed clock/DB metadata; assert `auto` only, `pinned` skipped, no duplicate build enqueue.

### P1 — REST surface, queues and provider boundaries

13. `tests/test_library_api.py::test_search_groups_books_and_suggests` — two real ZIMs, full-text and title-only modes, category filter, empty query, 404 for unknown book.
14. `tests/test_library_api.py::test_delete_book_removes_zim_and_meta4` and `test_rescan_and_leftovers`.
15. `tests/test_catalog_api.py::test_catalog_filters_sorts_and_marks_local_state` plus `test_start_all_skips_pinned`.
16. `tests/test_build_api.py::test_upload_rejects_wrong_signature_oversize_and_empty` and `test_link_crud_and_job_actions`.
17. `tests/test_feeds_api.py::test_feed_validation_and_crud` (mode/rate/interval → 422; sources required; edit/delete) and `test_item_filters_and_retry_failed`.
18. `tests/test_providers.py::test_stream_chat_parses_openai_sse` / `test_gemini_grounding_citations` / `test_provider_errors_map_to_ai_error` — `httpx.MockTransport` at the provider client seam.
19. `tests/test_provider_api.py::test_provider_crud_and_switch_off` — create/update/patch/delete and default fallbacks on `api_provider`.
20. `tests/test_websearch.py::test_each_service_maps_results` and `test_errors_are_plain` — all four services plus SearXNG missing URL.
21. `tests/test_queue_redis.py::test_redis_place_close_and_make_broker` — fakeredis plus a manager running on a Redis broker; assert ordering parity and reconnect/fallback.
22. `tests/test_events.py::test_sse_subscriber_receives_events_and_handles_disconnect` — two subscribers, cross-thread publish, queue-full drop, `EventSourceResponse` keepalive.
23. `tests/test_monitor.py::test_sample_watch_jobs_and_stall_detection` — fake clock; jobs that stop moving are flagged stalled and `quiet_for` is right.
24. `tests/test_reader.py::test_find_entry_handles_old_namespace_and_zimit_query` plus redirects/random path.
25. `tests/test_engine.py::test_rate_limit_and_retry_backoff` / `test_all_mirrors_fail_then_give_up` / `test_checksum_mismatch_is_fatal`.

### P2 — boundaries, UI and tooling

26. `tests/test_convert_mobi.py::test_mobi_with_images_becomes_a_zim` and DRM/unsupported-format errors (requires a tiny committed MOBI fixture).
27. `tests/test_zimit.py::test_capture_via_fake_docker_api` — fake `httpx.AsyncHTTPTransport(uds=...)`: pull, create, logs with stats, non-zero exit, cleanup (`DELETE`), no ZIM.
28. `tests/test_extract.py::test_css_background_and_font_are_localised` — currently 0 % on `css_resources`.
29. `web/tests/api.test.ts`, `web/tests/ai.test.ts` under **vitest** — 401 prompt/retry, SSE frame parsing, `splitModelKey`, abort handling.
30. `web/tests/live.test.tsx` — EventSource reconnect and dedupe (jsdom) once vitest/RTL exist.
31. `tests/e2e/test_smoke.py` — Docker Compose up, `/api/health`, mount a real small ZIM, `/content` and `/mcp` round trip.
32. Property tests for `extract.normalise`/`zim_path`/`relative` to pin path-traversal and encoding behaviour.

**Tooling, in order:** add `pytest-cov` (branch coverage, baseline at the measured 69.9 % and ratchet upward, upload HTML), `pytest-timeout`, `pytest-randomly`, an autouse network guard, and `filterwarnings = error::ResourceWarning`. Add `tests/conftest.py` with `app`/`client`/`db` factories that close resources, a `no_waiting` fixture, and an `admin_client`. Add a Redis service job and a small matrix (3.13/3.14, ubuntu + one macOS) to CI. Add vitest + jsdom + coverage for `web/src/lib`. Include tests in `tsconfig` type-checking.

---

## What is done well

- **Fast, green and reproducible:** 140 tests in ~14 s, green twice, no skips/xfails, no un-awaited coroutines.
- **Correct use of transport mocks:** `httpx.MockTransport` for Gluetun, feeds, sites; in-process mirrors and proxy servers; assertions on bytes, requests and artefacts rather than mock calls.
- **Real format boundaries:** ZIM write → `libzim` open → search → metadata/illustration verification; generated EPUB and PDF; real image resizing; scanner tests with work-in-progress folders.
- **Crawler coverage is genuinely strong:** robots.txt, sitemap indexes, gzip/plain-text sitemaps, conditional GET/304, 429 + `Retry-After`, permanent refusals, page limits, subdomains, pause/resume/cancel, proxy reconnect, trickle timeout.
- **Feed sync is tested as behaviour:** incremental fetch, unchanged feed leaves ZIM mtime untouched, feed-text fallback for 404/thin pages, `keep` eviction, ZIM entry assertions, and link-feed updates without re-crawling.
- **Security positives:** MCP keys stored only as digests (asserted against the DB), secret masking on settings, and log redaction of bearer tokens/API keys/passwords.
- **Build lifecycle:** upload → queued → finished → library row, duplicate protection, cancel/retry, paused resume, restart recovery, category filing and user-chosen icon rendering at 48 px.
- **Good pure-function parametrisation:** filename/category parsing, plan parsing, prompt rules, reasoning-tag splitting (including split-across-chunks), citation shapes, CSV rows, retry-after date parsing.

---

## Unverified / out of scope

- Real Docker/Zimit capture, real MOBI/AZW3 files, and a real Redis server (fakeredis only) were not exercised.
- Real AI providers (OpenAI/Gemini/OVH/Kilo), real web-search APIs and the GitHub release endpoint were not called; no credentials were used.
- No browser/component/E2E tests exist, so the React UI, SSE client, streaming markdown rendering and accessibility are unverified.
- No load, performance or long-running soak test; concurrency of multiple downloads/builds/feed syncs and SSE clients against one SQLite connection is untested.
- Packaging (PyPI wheel, standalone executables), Docker image, Windows/macOS paths and upgrades from a real deployed database were not tested.
- Coverage is statement-line only, measured by a temporary tracer; branch/condition coverage, and coverage of code only reachable in threads not started by tests, are not established.
- The lifecycle/scheduler timing observation is reproduced in a standalone script, not observed inside the pytest run itself; whether a slower machine makes the suite hit the real catalog remains unverified.
