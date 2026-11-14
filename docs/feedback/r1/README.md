# Zim Pro — Module Review r1

**Commit:** `cbb1a4e` (Release 0.5.0) · **Date:** 2026-11-14 · **Scope:** 12,653 lines of Python, 9,361 lines of TypeScript, the SQLite schema and the test suite.

Nine independent reviewers worked module by module. Each report is self-contained: every finding has an exact `file:line`, a quoted snippet, a confidence level, impact, trigger, suggested fix and the test that would catch it.

| Report | Module | Findings | Verdict |
|---|---|---:|---|
| [core.md](core.md) | schema, config, context, events, access, lifespan | 18 | Happy-path DB layer is clean; the schema enforces almost nothing and shutdown/startup are fragile |
| [library.md](library.md) | scanner, archive pool, reader/search, article, library/content/organise APIs | 21 | Serving logic works; the scanner can mass-delete rows, and untrusted ZIM HTML runs same-origin |
| [catalog-downloads.md](catalog-downloads.md) | OPDS, catalog updates, download engine/manager, catalog/download APIs | 18 | Resume design is sound; verification can be skipped, no total deadline, duplicate targets corrupt files |
| [feeds-scheduler.md](feeds-scheduler.md) | feed parse/sync/manager, scheduler, feeds API | 19 | Incremental ZIM core is solid; one malformed URL kills a sync, `keep` pruning is irreversible |
| [builder.md](builder.md) | crawler, jobs, extract, writer, zimit, converters, build API | 21 | Strong pipeline; dangerous seams around name collisions, cancellation, HTML escaping, resource scope |
| [ai-retrieval.md](ai-retrieval.md) | retrieval, providers, AI streaming, web search, MCP | 22 | Retrieval math is careful; coverage is computed from text never sent, prompts are unbounded, MCP is open by default |
| [system-queues.md](system-queues.md) | monitor, logs, Redis broker, network/VPN, system API | 19 | Fallback design is good; Redis health probe runs on the event loop, queue claims are not atomic |
| [frontend.md](frontend.md) | all React pages/components, API client, SSE | 25 | Unusually well structured; the Reader iframe, auth-prompt loop and missing timeouts are the exceptions |
| [testing.md](testing.md) | pytest suite, frontend tests, coverage, CI | 15 | 140 backend + 9 frontend tests pass, but 69.9 % line coverage hides untested serving, AI and download paths |

**Totals:** 2 Critical, 27 High, 88 Medium, 46 Low/Nit — 163 module findings, plus 15 test-suite findings.

---

## Release blockers (P0)

Ordered by blast radius, not by report. Each line names the owning report and finding.

1. **Untrusted content can drive the admin API.**
   `library.md` F1: `/content` serves ZIM HTML same-origin with no CSP/sandbox; `frontend.md` F1: the Reader iframe has no `sandbox` attribute. Any crawled or uploaded ZIM can call the app's API and read a stored admin token. Fix both ends together (separate origin or strict CSP + `sandbox` without `allow-same-origin`).

2. **Missing constraints allow duplicate jobs that overwrite each other's output.**
   `builder.md` F1 (two builds slugging alike write one `.zim`), `core.md` F2 and `catalog-downloads.md` F3 (two download rows target one filename and truncate the same `.part`), plus the same check-then-insert pattern in `feeds-scheduler.md` F5 and `system-queues.md` F5. Add partial unique indexes and make enqueue a single atomic statement.

3. **Downloads can be accepted corrupt, or hang a worker forever.**
   `catalog-downloads.md` F1 (no total deadline/min-speed; a trickling mirror holds a slot forever) and F4 (verification returns success when the metalink has no SHA-256, and resumed pieces are trusted without re-hashing; a zero-filled `.part` installs silently).

4. **The database cannot protect itself.**
   `core.md` F3/F7: one foreign key in 18 tables, 11 unenforced id references, zero `CHECK` constraints on state/enum/boolean columns. This is the root cause of orphaned builds/feeds/downloads and delete races reported in `library.md` F5, `feeds-scheduler.md` F6, `builder.md` F12/F13.

5. **One exception silently disables work or aborts a sync.**
   `catalog-downloads.md` F2 (`_run_queue`/`_tick` stop permanently until restart) and `system-queues.md` F4 (a queue runner dies on an unexpected value). Separately, `feeds-scheduler.md` F1: an uncaught `ValueError` from a malformed URL aborts the whole feed sync, skips later sources, leaves `last_error` stale, and 500s OPML/`check`. The top-level scheduler loop does catch exceptions (`scheduler.py:98-101`), so the process survives — the individual queue and sync do not. Wrap loop bodies and per-item work; record the error on the row and continue.

6. **Silent data loss on ordinary operations.**
   `library.md` F2 (a symlinked library directory makes the scanner delete every book row on Python 3.13), `feeds-scheduler.md` F2 (`keep`-pruned articles can never be revived, even after raising `keep`), `builder.md` F2 (cancel records `cancelled` while the ZIM is still installed).

7. **MCP is open by default.**
   `ai-retrieval.md` F1: `/mcp` is unauthenticated when no admin token and no keys exist, and deleting the last key re-opens it. Default bind is `0.0.0.0`; `mcp_allow_writes` is the only guard on write tools.

---

## Cross-cutting themes

### Database integrity & constraints

| Issue | Where | Evidence |
|---|---|---|
| Only one FK in the whole schema (`chat_messages.chat_id`); 11 loose id columns | `db.py` | `core.md` F3/F7 |
| No `CHECK` on any `state`/`kind`/boolean column; invalid states accepted | `db.py`, all managers | `core.md` F7 |
| `PRAGMA foreign_keys=ON` is nearly decorative | `db.py:274` | `core.md` F3 |
| Polymorphic `builds.source_id` + `kind` with no FK, no discriminator check | `db.py:160-180` | `builder.md` F12/F13 |
| `feed_sources`/`feed_items` have no FK or cascade; deleting a feed orphans rows | `db.py:202-229` | `feeds-scheduler.md` F6 |
| Schema in `SCHEMA` is not the real schema — columns added only by `_migrate()` | `db.py:284-335` | `core.md` F4 |
| Non-atomic multi-statement writes (scan, chat+message, enqueue, delete) | multiple | `core.md` F2, `library.md` F3, `ai-retrieval.md` F12 |
| `UNIQUE` looks sufficient but is not (`feed_id,guid`; `catalog_uuid` dedupe ignores filename) | `db.py`, managers | `feeds-scheduler.md` F5, `catalog-downloads.md` F3 |

### Database normalization

- Three near-duplicate source tables (`links`, `uploads`, `feeds`) with divergent column sets, then copied again into `builds`; `category`/`icon` were bolted onto all three by migration (`core.md`, `builder.md`).
- Two models for RSS input (`links.feeds` text plus a separate `feeds`/`feed_sources` schema) — `core.md`, `feeds-scheduler.md`.
- Derived state stored and allowed to drift: `books.category` (last-writer-wins recompute), `chat.updated`, `items.last_new`, `books.article_count`, shadowed copies of size/date (`library.md` F8, `ai-retrieval.md` F18).
- JSON-in-TEXT used for structured data: `sources`, `queries`, `steps` on messages; `log`, `report`, `params` on builds. Unvalidated on read; one bad row 500s a whole chat (`ai-retrieval.md` F19, `core.md` F5).

### Concurrency & atomicity

- Enqueue/claim is read-then-write in every queue: downloads, builds, feeds, Redis broker (`core.md` F2, `catalog-downloads.md` F3, `feeds-scheduler.md` F5, `builder.md` F13, `system-queues.md` F3).
- Queue claims overwrite a concurrent `cancelled`/`paused` state (`system-queues.md` F3).
- Cancellation cannot stop `asyncio.to_thread`; builders delete the staging directory under running writers (`builder.md` F2, F3).
- Delete-vs-worker races: downloads removed mid-run, feeds deleted mid-sync, books removed mid-read (`catalog-downloads.md` F5, `feeds-scheduler.md` F6, `library.md` F5).

### Connectivity & failure handling

- **Blocking network I/O on the event loop:** Redis `PING` inside `/api/monitor` with redis-py’s 10-retry policy (~4.5 s per call, ignoring the 15 s outage window) — `system-queues.md` F1; synchronous DB/auth work in async middleware and catalog refresh — `core.md` F6.
- **No overall deadlines:** download engine has only read timeouts — `catalog-downloads.md` F1; provider calls have no retry/backoff on 429/5xx — `ai-retrieval.md` F7.
- **Errors bypassing handlers:** `httpx.InvalidURL` is not an `HTTPError`, and malformed Gluetun JSON raises `AttributeError` past `status()` — `ai-retrieval.md` F6, `system-queues.md` F2.
- **Graceful degradation mostly works** (DB remains the record, Redis is only order; builder retries, feed ETag/304, mirror health ranking), but the health probe and queue loops break the pattern.

### Security

- ZIM HTML same-origin + unsandboxed iframe (`library.md` F1, `frontend.md` F1).
- `/mcp` open by default; key deletion re-opens it (`ai-retrieval.md` F1).
- Builder interpolates unescaped URLs into tag strings — reproduced HTML injection (`builder.md` F4).
- `_resources` fetches any URL a page embeds, including loopback/link-local/private addresses — SSRF-shaped (`builder.md` F5).
- Provider API keys stored in plaintext and only masked in the UI, not the DB (`ai-retrieval.md` F14).
- Path traversal checks run before `%`-decoding in `resolve_link` (`library.md` F12).

### Testing

The suite is green and mostly integration-style, not mock theatre: **140 backend tests pass in ~15 s**, **9 frontend tests pass**, no skips or xfails. The risk is what is not exercised:

- **69.9 % line coverage** (not branch), but 0 % of the actual statements in `api/content.py`, `library/search.py`, `library/reader.py`, `archive_pool.get`, `retrieval.plan/rank`, `providers.stream_chat`, `websearch.search` and `api/ai.ask`.
- **No admin token is ever enabled**, so every guarded write route is tested only unguarded (`testing.md` #2).
- **No ZIM fixture exists anywhere in the repo**, so serving, searching and reading have never run against a real archive in CI (`library.md`, `testing.md` #1).
- **Scheduler loops, `_migrate` ALTER branches, Redis-parity paths, cancellation and queue lifecycle are untested** — see `testing.md` “Highest-risk untested paths” (`scheduler.py:79`, `db.py:_migrate`, `queues.py` Redis branch) and findings #4, #6, #9, #10.
- Hygiene: 36 leaked SQLite connections, lifespan bypassed in 9 API tests, weak `!= 401` assertions that pass on a 500, one real 3 s sleep from a missing fixture.

---

## Per-module digest

**Core/db.** Parameterised SQL throughout, lock-copied reads, hashed MCP keys with constant-time compare, per-file scanner error capture. Weak points: lifespan shutdown can close the DB under a cancelled `to_thread`; `data_dir` is never validated so the server reports healthy with an unmounted library; unguarded `json.loads` on settings can break startup; the declared schema and the migrated schema differ.

**Library.** Filesystem traversal is correctly prevented by resolving `book_id` through `books`. The scanner is the weak point: symlinked directories, non-atomic scans, filename-as-identity, revision letters dropped. Reader/search behave, but libzim read errors become 500s and category casing is inconsistent.

**Catalog & downloads.** Piece-level resume, per-piece SHA-1 and whole-file SHA-256, fsync-before-persist and mirror ranking are well done. Verification is optional when SHA-256 is missing, resumed pieces are trusted, no deadline/min-speed, and duplicate targets can `os.replace` each other's half-written file.

**Feeds & scheduler.** Encoding, timezone, Atom and relative-link handling are more robust than typical. But `ValueError` from a bad URL escapes every handler, pruning is irreversible, language is not persisted, and the “already building” guard is TOCTOU. The scheduler itself has no tests.

**Builder.** Best-tested subsystem (82–97 % on crawler/feeds internals), with per-page isolation, a validator cache and post-write verification. The defects are at the seams: slug collisions, cancellation semantics, escaping, off-scope resource fetches, robots bypass for feed articles and sitemap pages crowding out `max_pages`.

**AI & MCP.** RRF, book-size damping, per-book caps and title dedupe are careful. But coverage is scored on blocks that are not in the excerpt, history/excerpts are unbounded with no `max_tokens`, SSE gaps degrade to “empty reply”, chat writes are non-transactional, and MCP defaults open.

**System, queues & logs.** The “DB is the record, Redis is the order, 15 s down window” design is sound and tested. The implementation defeats it: the health probe does blocking Redis I/O on the loop, claims are not atomic, one bad Redis member kills a queue loop, and malformed VPN JSON 500s two endpoints.

**Frontend.** Shared `useData` with stale-response sequencing, `Ask` with `AbortController` and run generations, consistent secret masking and confirmations are genuinely good. The problems are edge behaviour: unsandboxed iframe, background 401 prompt loop, no request timeouts/abort-on-unmount, pagination capped by an API limit, and a failed PUT adopted as “saved”.

---

## Suggested remediation order

| Phase | Work | Source |
|---|---|---|
| **P0 — before the next release** | Sandbox/CSP ZIM content; unique indexes + atomic enqueue/claim; download deadline + mandatory verification; queue/loop error boundaries; MCP auth default; `CHECK`/FK migration | `library` F1, `frontend` F1, `core` F2/F3/F7, `catalog-downloads` F1/F3/F4, `system-queues` F4, `ai` F1 |
| **P1 — next iteration** | Transactional scan and chat writes; cancellation protocol for builders; feed URL validation + revive-on-`keep`; context budgeting + coverage fix; frontend timeouts/401 policy | `library` F3, `ai` F12/F2/F3, `builder` F2/F3, `feeds` F1/F2, `frontend` F2/F3 |
| **P2 — hardening** | Startup validation (`data_dir`, friendly DB errors); JSON read guards; normalization of source tables; log rotation/export; accessibility and error-state polish | `core` F4/F5, `builder` F12, `system-queues` F7, `frontend` F6-F25 |
| **Continuous** | A real ZIM fixture; auth-enabled API tests; scheduler/Redis-outage/cancel/crash-recovery tests; `pytest-cov` in CI; vitest + jsdom for components | `testing.md` |

---

## Method & limitations

- Reviews are static, with targeted throw-away repro scripts for claims that could be executed (no repository files were modified). Several plausible findings were explicitly rejected by the reviewers (SQL injection, middleware ordering, pagination parameter order, `position` affinity).
- Coverage in `testing.md` was measured with a temporary `sys.monitoring` plugin because `pytest-cov` is not installed; it is **line**, not branch, coverage.
- No findings were validated against a live Docker stack with Redis, Gluetun or a real VPN, nor against a ZIM fixture (none exists in the repo). Findings that depend on those environments are marked “unverified” in their reports.
- Severity is impact × likelihood for a self-hosted deployment, not CVSS. “Critical” means normal use can corrupt data or expose the admin API.
