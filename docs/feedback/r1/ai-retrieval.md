# AI, Retrieval & MCP Review — r1

**Scope:** `src/zim_pro/ai.py`, `retrieval.py`, `providers.py`, `api/ai.py`, `websearch.py`, `mcp_server.py`, plus
`db.py` (ai_providers, chats, chat_messages, mcp_keys), `config.py`, `access.py`, `main.py` (MCP auth
middleware), `api/access.py`, `api/library.py` (settings masking/clamping) and the AI parts of
`tests/test_units.py`; `tests/test_engine.py` read (it covers downloads only).
**Commit:** `cbb1a4e` · **Date:** 2026-11-14 · **Method:** static read of every line above, call-site tracing,
one executable probe of `_passages` (below), `tests/test_units.py` run (57 passed) to confirm baseline.

## Summary

- Retrieval math is careful (RRF, book-size damping, per-book caps, title dedupe), but `_passages()` scores
  terms from **all** blocks and from **untruncated** blocks, so reported coverage can claim a passage that is
  not in the excerpt. "Library was enough" then goes false-positive and citations point at the wrong text
  (F3, proven with a probe).
- There is **no context-window budgeting**: excerpts, 12 history turns (6 000 chars each) and user
  instructions are concatenated unbounded, and requests carry no `max_tokens` (F2).
- `/mcp` is **open by default** and deleting the last key **re-opens** it (a test asserts this); with
  `mcp_allow_writes` on, anyone on the LAN can queue downloads (F1).
- Provider robustness is thin: no retry/backoff on 429/5xx, no answer fallback, SSE parse gaps silently
  degrade to "empty reply", and `httpx.InvalidURL`/non-dict model lists escape the AI error handling and can
  kill the whole model picker or an in-flight answer (F6–F8).
- Chat storage is a set of autocommitted inserts (no transaction), so concurrent asks interleave rows and
  `regenerate` destroys the old answer before the new one exists (F12).
- Tests cover prompts, plan parsing, RRF, seeding and reasoning splitting but **nothing** covers
  `/api/ai/ask`, provider HTTP failures, SSE framing, `mcp_allow_writes`, or a real ZIM.

## Top 5 must-fix

1. **F3** — coverage from blocks not in the excerpt (wrong "enough" decision and mis-citation).
2. **F1** — MCP default-open, last-key deletion unlocks `/mcp`, write tool gated only by a setting.
3. **F2** — unbounded prompt construction; no token estimate, no `max_tokens`, 72 k-char history ceiling.
4. **F8/F6** — SSE errors and non-`HTTPError` exceptions bypass the `AIError` path ("Something went wrong"/500).
5. **F12** — no transaction around chat+message writes; destructive `regenerate`.

## Findings

### F1. [High] `/mcp` is unauthenticated by default and re-opens when the last key is deleted
**Location:** `main.py:110-121`, `access.py:27-28`, `mcp_server.py:181-191`, `config.py:13`; `test_network.py:384` · **Category:** Security / MCP auth · **Confidence:** High
**Evidence:**
```python
locked = bool(config.admin_token) or access.has_keys(ctx.db)
if locked and not is_admin and not access.check_key(ctx.db, supplied):  # 401
```
`has_keys()` is `SELECT 1 FROM mcp_keys LIMIT 1`, so with zero keys `locked` is False; the test encodes the
reopen: `assert client.post("/mcp", json={}).status_code != 401  # the last key gone: open again`. The server
binds `host: str = "0.0.0.0"`, and `start_update` only checks `mcp_allow_writes`.
**Impact / Trigger:** On a LAN instance with no `KIWIX_ADMIN_TOKEN` (default) any host can call `search`,
`get_article`, `list_updates`, and — once writes are enabled — `start_update` to queue downloads. Revoking a
leaked key is impossible without also unlocking the endpoint.
**Suggested fix:** persist an `mcp_locked` flag once any key has ever existed (or refuse deleting the last
key); require a key whenever writes are on; consider defaulting to `127.0.0.1`.
**Test to add:** delete the last key → `/mcp` still 401; writes on with no admin token/keys → setup refused.

### F2. [High] No context-window budgeting, token estimate or output cap
**Location:** `ai.py:91-124`, `providers.py:434`, `api/ai.py:567-570` · **Category:** Prompt construction / limits · **Confidence:** High
**Evidence:** `clean_history` keeps 12 messages truncated to 6 000 chars each (≈72 k chars ≈18 k tokens) with
no total budget; `build_messages` appends up to `ai_max_sources` (≤12) × 2 400-char excerpts plus a 2 000-char
instruction block. The payload is only `{"model", "messages", "stream"}` — no `max_tokens`, no `temperature`,
no truncation.
**Impact / Trigger:** Six long turns or 12 sources overflow 4 k/8 k local models; the provider returns 400
"context length exceeded", retold verbatim. Nothing degrades gracefully and answer length is unbounded.
**Suggested fix:** estimate tokens (≈chars/4) and budget history + excerpts + instructions against a
configurable context size; drop oldest turns first, then trim excerpts; send `max_tokens`.
**Test to add:** oversized history/sources stay under a token budget; the payload includes `max_tokens`.

### F3. [High] Coverage is computed from blocks that are not sent (false "library is enough", wrong citation)
**Location:** `retrieval.py:411-441` (esp. 426, 437) · **Category:** Retrieval scoring / citation mapping · **Confidence:** High
**Evidence:** `words` is built for every block; chosen blocks are truncated to 1 200 chars, yet
```python
covered = set().union(*(words[i] for i in chosen))
together = max(len(terms & found) for found in words) / max(1, len(terms))
```
`together` scans all blocks and `words[i]` covers the whole block. Probe: a block with `zebra quokka` only
after char 1200 was excluded from a 2 302-char excerpt (`EXCERPT_CHARS=2400`) while `coverage == 1.0` and
`"zebra" in excerpt == False`.
**Impact / Trigger:** `coverage >= 0.5` drives `library_is_enough()` (`ai.py:146-148`), so Ask suppresses the
web fallback and cites a source whose visible excerpt lacks the answer; ranking also over-rewards long
single-paragraph pages.
**Suggested fix:** compute `together` and `covered` over `chosen` blocks only, using the emitted
`block[:1200]` text.
**Test to add:** assert `coverage == 0` when no chosen block contains the terms (the probe).

### F4. [Medium] The "rest of the library" search is unbounded, uncancellable and blocks the answer
**Location:** `api/ai.py:461-475`, `retrieval.py:336-387` · **Category:** Retrieval performance / cancellation · **Confidence:** High
**Evidence:** `rest = [row for row in scope if row["id"] not in focus_ids]` then
`await asyncio.to_thread(retrieval.search_books, ctx, rest, main, 3, candidates)`. `search_books` opens every
archive and runs each query, with no time budget or book cap (the devdocs family alone is hundreds of ZIMs).
**Impact / Trigger:** On a large library the SSE stream sits on "Searching the other N books" for minutes; a
client Stop abandons the `to_thread` future but the thread keeps opening ZIMs. Concurrent asks multiply it.
**Suggested fix:** cap the light pass (e.g. top 25 by relevance/size), add an overall deadline and a
per-book cancellation flag, and log count/duration.
**Test to add:** counting fake pool — the light pass is capped and stops when cancelled.

### F5. [Medium] Provider/model resolution pairs a fallback provider with the requested model
**Location:** `api/ai.py:286-301` · **Category:** Model resolution · **Confidence:** High
**Evidence:**
```python
provider = providers.get_provider(ctx.db, body.provider or settings["ai_provider"])
if provider is None:
    ...; provider = available[0]
model = body.model if body.provider else settings["ai_model"]
```
A deleted `body.provider` yields the first enabled provider but still sends `body.model`; a `body.model`
without `body.provider` is ignored; `provider` + empty model errors even when a default exists.
**Impact / Trigger:** `POST /api/ai/ask {"provider":"gone","model":"sonar",...}` → provider 400 "model not
found" or the wrong model answering; clients cannot pick a model alone.
**Suggested fix:** fall back only when no explicit model was given; validate the model against the cached
list or report the stale provider plainly.
**Test to add:** stale provider id → an error naming the provider, not model-not-found.

### F6. [Medium] Non-`HTTPError` failures bypass the AI error handling and can break the model picker
**Location:** `providers.py:267-278`, `api/ai.py:133-143,505-514` · **Category:** Error handling / connectivity · **Confidence:** High
**Evidence:** `list_models` catches only `httpx.HTTPError`/`ValueError`, then
```python
items = data.get("data", data) if isinstance(data, dict) else data
models = sorted(str(m["id"]) for m in items if isinstance(m, dict) and m.get("id"))
```
A 200 body of `null` → `TypeError` uncaught. Base URL `http://` passes `_from_body`'s prefix check but raises
`httpx.InvalidURL`, which is **not** an `HTTPError` (`InvalidURL.__mro__` verified). `/api/ai/models` wraps
loads in `asyncio.gather`, so one provider 500s the whole endpoint; the same URL as SearXNG escapes the
`except AIError` around `websearch.search` and aborts the whole answer.
**Impact / Trigger:** One typo'd provider disables model selection for all; a misconfigured SearXNG kills an
Ask after retrieval. Repro: save `base_url:"http://"` then `GET /api/ai/models`.
**Suggested fix:** validate with `httpx.URL` at save time; catch `Exception` per provider in `load()` and
store it in `entry["error"]`; catch `httpx.HTTPError`/`ValueError` in `websearch.search`.
**Test to add:** `MockTransport` null body and invalid URL → `/api/ai/models` stays 200; web failure only
fails its step.

### F7. [Medium] No retry/backoff and no answer fallback for 429/5xx/timeouts
**Location:** `providers.py:237-252,401-472`, `api/ai.py:580-584` · **Category:** Resilience · **Confidence:** High
**Evidence:** `_fail` maps 429 to a terminal `AIError`; there is no loop, no `Retry-After`, no alternate
provider/model — only metrics in `stream_chat`'s `finally`. Keyless OVH (`LOW_RATE_KINDS`) allows "about two
requests a minute per model"; `planner_model` spreads planning but the answer call has no retry.
**Impact / Trigger:** A transient 429/502 ends the answer after retrieval and stores an error row; a flaky
provider is never bypassed even when another usable provider exists.
**Suggested fix:** jittered backoff on 429/5xx/`httpx.TimeoutException`, honour `Retry-After`, optionally fall
back to the next enabled provider/model.
**Test to add:** `MockTransport` 429-then-200 → one answer; 5xx exhausted → one error event, no duplicate row.

### F8. [Medium] SSE parsing gaps degenerate to "empty reply" and hide provider errors
**Location:** `providers.py:445-455` · **Category:** Streaming / error surfacing · **Confidence:** High
**Evidence:**
```python
if not line.startswith("data:"): continue      # drops event:, comments, keep-alives
data = line[5:].strip()
if data == "[DONE]": break
try: chunk = json.loads(data)
except ValueError: continue                    # silently discards a bad/multi-line frame
```
A gateway answering 200 with `{"error": {...}}`, or a JSON chunk split over several `data:` lines, yields no
text → `api/ai.py:573-578` raises "The model returned an empty reply", hiding the cause. A non-dict `delta`
raises `TypeError` → "Something went wrong".
**Impact / Trigger:** `data: {"error":{"message":"invalid key"}}\n\n` with HTTP 200 reports an empty model
instead of an auth failure.
**Suggested fix:** reassemble multi-line frames per spec; raise `AIError` when a chunk carries `error`;
guard the `delta` type.
**Test to add:** HTTP-200 error payload → error text contains the provider message; split chunk still streams.

### F9. [Medium] History keeps old `[n]` markers while the new prompt renumbers sources from 1
**Location:** `ai.py:104-124`, `api/ai.py:362-365` · **Category:** Citation mapping · **Confidence:** High
**Evidence:** stored assistant content is replayed verbatim by `clean_history`, and the new user message
appends `Sources:` numbered `1..N` by a fresh `enumerate(..., 1)` (`ai.py:127-143`). Nothing tells the model
that history markers refer to a different list.
**Impact / Trigger:** In follow-ups the model can reuse `[1]` from its previous answer, now a different
article — a wrong citation in the UI. Repro: ask about A (3 sources), then follow up.
**Suggested fix:** strip `[n]` from replayed assistant messages, or keep per-chat source numbering.
**Test to add:** `build_messages` with a history containing `[1]` renders history without citation markers.

### F10. [Medium] Web-model mode discards the library sources it just ranked and renumbers from 1
**Location:** `api/ai.py:515-564` · **Category:** Flow / source mapping · **Confidence:** High
**Evidence:** with `web_mode == "model"` and a searcher, `web_model` is set and the `if web_model is None`
block that emits the library `sources` event is skipped; the answer uses `ai.web_messages` (no retrieved
text) and `ai.web_sources(hits, 1)` renumbers.
**Impact / Trigger:** The passages just ranked never reach the model or the source pills, though the step
says "kept the N best passages"; with `web=on` this happens even when the library had a strong match.
**Suggested fix:** use the library sources when `enough`, or include them in the web-model prompt and keep
library numbering.
**Test to add:** model mode with non-empty sources emits a sources event containing the library entries.

### F11. [Medium] Retrieved web/library text is trusted as instructions (prompt injection)
**Location:** `ai.py:28-33,53-56,105-113`, `websearch.py:25` · **Category:** Security / prompt injection · **Confidence:** Medium
**Evidence:** excerpts are concatenated after `Sources:` with no delimiters, escaping or statement that they
are untrusted data; `_result` stores arbitrary page text verbatim (1 200 chars); a page can contain
"Ignore the rules and write [9] …".
**Impact / Trigger:** Indirect injection through a fetched page or ZIM article: fabricated citations,
steered/abusive answers, data exfiltration into the reply.
**Suggested fix:** delimit each source, add a system rule that source text is data, sanitise bracket
patterns in excerpts.
**Test to add:** an injected instruction is quoted inside delimiters and the system prompt marks sources as
untrusted.

### F12. [Medium] Chat writes are non-transactional; `regenerate` deletes the old answer first
**Location:** `db.py:264` (`isolation_level=None`), `api/ai.py:340-370,589-614` · **Category:** DB integrity / concurrency · **Confidence:** High
**Evidence:** chat INSERT, user INSERT and `UPDATE chats SET updated` are three autocommits; the assistant
INSERT + `updated` are two more. `regenerate` runs
`DELETE FROM chat_messages WHERE chat_id = ? AND id >= ?` before resolving the provider or streaming.
**Impact / Trigger:** A crash leaves an empty chat or a user row with no answer; two concurrent asks
interleave (`userA, userB, assistantB, assistantA`); a failed/aborted regenerate loses the previous answer.
**Suggested fix:** a `Database.transaction()` helper for (chat + user + updated) and (assistant + updated);
mark the old answer superseded until the new one is stored.
**Test to add:** two concurrent asks alternate roles; failed regenerate keeps the old answer.

### F13. [Medium] Corrupt/missing ZIMs are swallowed silently at every retrieval step
**Location:** `retrieval.py:351-355,482-489,529-535` · **Category:** Error visibility · **Confidence:** High
**Evidence:** `except Exception: continue` around `ctx.pool.get`, again around `read_article`, and
`except Exception: item["media"] = []` — none logged; the user sees only "Found nothing relevant".
**Impact / Trigger:** A bad mount or unreadable archive is indistinguishable from "no answer"; no support
evidence. Repro: `chmod 000` one ZIM and ask a question only it answers.
**Suggested fix:** log WARNING with book id/path and exception type; count skipped books in the detail.
**Test to add:** fake pool raising `OSError` → result returns and a warning is logged.

### F14. [Medium] Provider API keys are stored in plaintext and never masked/encrypted
**Location:** `db.py:97`, `providers.py:135-149`, `api/library.py:180-190` · **Category:** Security at rest · **Confidence:** High
**Evidence:** `api_key TEXT NOT NULL DEFAULT ''` in `ai_providers`; `save_provider` inserts it verbatim;
`SECRET_SETTINGS` masks only `web_api_key`, `vpn_control_key`, `vpn_control_password`.
**Impact / Trigger:** Any backup, volume snapshot or support bundle of `/state` leaks every provider
credential (`sqlite3 … 'select api_key from ai_providers'`). The API's masking elsewhere makes the gap easy
to miss.
**Suggested fix:** set the state dir/DB to 0600, encrypt with a key from env, or document the trust boundary
and keep the DB out of diagnostics.
**Test to add:** exported diagnostics/backups contain no `api_key`.

### F15. [Medium] `choose_focus` routes on question words even when the model planned queries
**Location:** `retrieval.py:299-302` · **Category:** Ranking / book routing · **Confidence:** High
**Evidence:**
```python
# Route on the planned queries ... the question's own words include verbs like "make" ...
terms = the_plan.queries if the_plan.books else keywords(question)
```
The condition tests `the_plan.books`, not `the_plan.queries` — a plan with queries but no book picks takes
the branch the comment calls wrong.
**Impact / Trigger:** Wrong focus books (the "make" book for "how do I make …"), wasted retrieval, the
encyclopedia doing the real work. Repro: `Plan(queries=["reverse list python"], books=[])`.
**Suggested fix:** `terms = the_plan.queries or keywords(question)`.
**Test to add:** a plan with queries and no books routes on the queries (`make` unreachable).

### F16. [Medium] Non-Latin tokenisation drives coverage to zero for CJK and 2-char terms
**Location:** `retrieval.py:97-98,411,460-461` · **Category:** Normalisation / i18n · **Confidence:** Medium
**Evidence:** `keywords()` keeps only `len(w) > 2` and drops English stopwords; `_passages` tokenises with
`re.findall(r"\w+", block.lower())`, which treats a whole CJK sentence as one token. `STOPWORDS` and
`_SMALL_TALK` are English-only.
**Impact / Trigger:** For Chinese/Japanese ZIMs (or a 2-char query like "北京") coverage stays ~0 → the
library is never "enough", Ask falls through to web/"found nothing", sources ranked by title only.
**Suggested fix:** character tokenisation for scripts without spaces (or use ZIM index terms), allow 2-char
non-Latin tokens, language-aware stopwords.
**Test to add:** `_passages` on a CJK article with a CJK term yields non-zero coverage.

### F17. [Low] `_match_book` accepts an arbitrary prefix match
**Location:** `retrieval.py:166-178` · **Category:** Book routing · **Confidence:** High
**Evidence:** `close = sorted(i for i in ids if i.lower().startswith(lowered) or lowered.startswith(i.lower()))`
then `close[0]` — alphabetical first wins, and `len(lowered) > 3` admits any 4-letter prefix.
**Impact / Trigger:** `BOOKS: wiki` routes to `wikipedia_en_all_nopic`/`_medicine`, spending focus budget on
the wrong edition. Fix: prefer exact/longest match, reject ambiguity.
**Test to add:** ambiguous prefix returns `None`; exact id always wins.

### F18. [Low] Chat list is silently capped and `updated` drifts
**Location:** `api/ai.py:242-269` · **Category:** DB / pagination · **Confidence:** High
**Evidence:** `ORDER BY updated DESC LIMIT 500` with no offset/cursor; rename and message deletes do not
touch `updated`; no index on `chats(updated)`.
**Impact / Trigger:** Chat 501+ unreachable; a renamed chat does not move; full scan on a large table.
**Suggested fix:** `?before=` pagination, `CREATE INDEX chats_updated`, update `updated` on rename.
**Test to add:** 501 chats → page two returns the oldest; rename reorders.

### F19. [Low] DB JSON blobs are loaded unguarded; one bad row 500s the whole chat
**Location:** `api/ai.py:203-239` · **Category:** Error handling / normalisation · **Confidence:** High
**Evidence:** `json.loads(row["sources"])`, `json.loads(row["queries"] or "[]")` and
`json.loads(row["steps"])` have no `try`; `_message` assumes a list.
**Impact / Trigger:** A corrupt/legacy row makes `GET /api/ai/chats/{id}` fail for the conversation, so the
user cannot open the chat to delete it.
**Suggested fix:** parse defensively, fall back to `None`/`[]` and warn.
**Test to add:** `sources='not json'` → chat loads, blob skipped.

### F20. [Low] `_number_citations` is fragile
**Location:** `api/ai.py:319-326` · **Category:** Citation mapping · **Confidence:** High
**Evidence:** the regex `(https?://[^)\s]+)` stops at the first `)` (Wikipedia `…_(disambiguation)` is cut);
only `?utm_source=` is stripped, and scheme/host/fragment variants never match.
**Impact / Trigger:** Some cited links stay inline beside numbered pills; truncated URLs can only mismatch.
**Suggested fix:** parse Markdown links, normalise URLs (fragment/trackers/scheme), map with a dict instead
of `urls.index`.
**Test to add:** URL with parentheses and `#section` maps to the right `[n]`.

### F21. [Low] No SSE heartbeat during long planning/retrieval
**Location:** `api/ai.py:329-620` · **Category:** Connectivity · **Confidence:** Medium
**Evidence:** between `chat` and the first `delta` the server can spend 25 s planning plus minutes searching;
`X-Accel-Buffering: no` is set but no `ping`/comment events are emitted.
**Impact / Trigger:** Reverse proxies and some clients drop an idle stream while the server keeps working.
**Suggested fix:** emit a `: ping` comment or `status` event every ~15 s around long steps.
**Test to add:** no body idle gap longer than the heartbeat when searches are slow.

### F22. [Low] Provider admin edge cases: test probe ignores saved kind, `/v1` not added, duplicates allowed
**Location:** `api/ai.py:102-124`, `providers.py:113-151` · **Category:** Provider model resolution · **Confidence:** High
**Evidence:** `ProviderTest.kind` defaults to `"custom"` and `test_provider` always passes `body.kind`, so
testing a saved Kilo provider misses `_MODELS_URL`; `clean_url` never adds `/v1` (OpenAI host → `/chat/completions`
404); `save_provider` upserts by id only, so duplicate `base_url`s become extra picker entries and rate-limit
budgets.
**Impact / Trigger:** "Test" fails for valid providers; pasted hosts look unreachable; duplicate free
providers multiply 429s.
**Suggested fix:** use the saved kind when none is supplied, warn on a known host missing `/v1`, reject
duplicate base URLs.
**Test to add:** `POST /api/ai/providers/test` with only `id` uses the saved kind/url; duplicate URL rejected.

## Database integrity & normalization notes

- **FK/cascade is genuinely on:** `PRAGMA foreign_keys=ON` (`db.py:274`) and `chat_messages.chat_id …
  ON DELETE CASCADE` (`db.py:109`); deletes also remove messages explicitly (`api/ai.py:274-282`), so no
  orphans either way. Good.
- **No `CHECK (role IN ('user','assistant'))`:** only the API writes roles, but one stray write breaks
  `_message`/`clean_history`.
- **No transaction across chat + messages** (F12); `isolation_level=None` commits each statement, so "chat
  exists, user message missing" is reachable on crash.
- **`chats.updated`** is written twice per ask and nowhere else; rename leaves it stale (F18).
- **Normalisation:** sources/queries/steps exist only as JSON blobs on `chat_messages`; no relational
  `chat_sources`, so they cannot be queried/joined and malformed blobs are detected only at read time.
  `public_source` drops `coverage`, so stored messages can never be audited for weak matches.
- **`message.model`** stores `"Provider name · model"` (`used`), not the provider id; provider rename/delete
  makes old messages unattributable, and the web-model path stores the searcher rather than an answering model.
- **Provider ids** are 12 hex chars (`new_id`), collision-safe in practice; no unique constraint on
  `base_url` (F22). `api_key` plaintext (F14). `mcp_keys.digest` is UNIQUE, only SHA-256 kept, `last_used`
  writes throttled to once a minute — solid.
- **Unbounded growth:** `content`/`reasoning`/`steps` have no retention or size cap; `chats` has no pagination.
- **Seeding/migration idempotency:** `migrate_single_provider` guards on `COUNT(*)` (re-run test at
  `test_units.py:167`), and `_free_providers_seeded` makes removals stick. Edge: a crash between
  `save_provider` and the settings cleanup leaves `ai_provider` unset, and the next seeding then points the
  default at `free-ovh`, ignoring the migrated provider.

## Connectivity & failure handling

- **Timeouts:** `httpx.Timeout(20, read=300)` (`providers.py:232`); planning is additionally capped at 25 s
  by `asyncio.wait_for` (`retrieval.py:255`), which does propagate cancellation through the async generator.
  There is no connect-timeout retry, and a 300 s inter-chunk stall is very long for a UI.
- **No retries/backoff, no `Retry-After`, no fallback** (F7). **No rate limiting** on `/api/ai/ask`, which can
  trigger many provider/model calls per request.
- **Proxy:** `build_proxy` is applied to the fetcher/builder, not to `providers._client` or `websearch.search`;
  those rely only on httpx's env-based `trust_env`, so traffic a user routes through a VPN/proxy container
  bypasses it. Wire it or document it.
- **TLS/redirects** are httpx defaults (`verify=True`, `follow_redirects=True`); a 307 re-POSTs the prompt to
  the redirect target while httpx strips `Authorization` cross-origin — acceptable but surprising.
- **Mid-stream failure** is reported as `_unreachable` ("Could not reach …") because
  `RemoteProtocolError`/`ReadTimeout` are `HTTPError`; the partial answer is kept but the wording is wrong.
- **Client disconnect:** no `request.is_disconnected()`; Starlette cancels the generator and `finally`
  persists the partial answer (good). `to_thread` retrieval is not cancellable (F4); `stream_chat` marks a
  cancelled call `ok=True`, so aborted asks inflate success metrics.
- **One bad provider:** does not block the others in the picker *unless* it raises a non-AIError (F6);
  `/api/ai/models` already isolates `AIError`s per provider in `load()`.
- **Web search:** failure marks only its step failed (good) except non-AIError exceptions (F6). Keys travel in
  headers only; SearXNG `base_url` is admin-controlled.

## Testing gaps

Existing AI tests (`tests/test_units.py`) cover small talk (126-145), prompt shape (148-153, 212-223),
migration/seeding (156-173, 277-306), citation shapes/numbering (176-209), reasoning splitting (226-274), plan
parsing (355-384), local planning/history (387-402), book routing/focus (405-427), `_passages` (430-444,
493-508, 572-579), RRF fusion (447-490), keyword tying (511-527), provider on/off (641-659).
`tests/test_network.py:360-384` covers MCP key hashing and the 401 path.

**Untested and important:**
- **`/api/ai/ask` has no test at all** — no SSE framing/order (`chat`/`status`/`step`/`sources`/`delta`/
  `replace`/`done`/`error`), persistence, `regenerate`, or partial-answer-on-Stop.
- **`providers.stream_chat` has no test**: there is no provider-level `httpx.MockTransport` anywhere (only
  downloads in `test_engine.py`). 401/403/429/5xx, `Retry-After`, timeouts, malformed JSON, HTTP-200-with-
  error, multi-line `data:` frames, `[DONE]` and mid-stream disconnect are unverified; only pure helpers are.
- **`_passages`:** no assertion that the excerpt contains the reported coverage (F3); the long-page test only
  covers scattered words.
- **Retrieval with missing/corrupt ZIM, empty library, empty/zero-keyword and huge questions** is untested;
  `search_books` only ever sees a fake `search_book`.
- **`choose_focus`:** no test for queries-without-books (F15) or ambiguous prefixes (F17).
- **MCP write gating:** `mcp_allow_writes` true/false never tested; no test calls a tool over `/mcp`.
- **Key revocation** is tested only as "last key gone → open" — the bug (F1); no test that a revoked key stops
  a session or that the endpoint stays locked.
- **Model listing failures:** no test for one provider erroring, a `null` body, or an invalid URL (F6).
- **`/api/ai/speech`:** no test for provider 401/5xx or the Gemini inline-audio parse.
- **`test_engine.py` is downloads-only** (range/resume/checksum) and contributes nothing here.

## What is done well

- `ReasoningSplitter` is careful: tags split across chunks, closing-tag-only templates, `retract` for text
  that turns out to be thinking, tested at 1/3/7-char chunk sizes.
- Planning degrades gracefully: 25 s cap, `AIError`/timeout → `plan_locally`, "NONE" handling, tolerant
  `parse_plan` for small-model formatting.
- RRF with book-size damping (`log10(2+articles)/3`), focus boost, translation penalty, per-book and
  per-title dedupe, and a shortlist cap are sensible and tested.
- `public_source` strips markup and truncates before storage/transport; provider keys never appear in
  `Provider.public()`; settings mask their own secrets with round-trip handling.
- MCP keys: `secrets.token_urlsafe(32)`, SHA-256 only, display hint, `hmac.compare_digest`, throttled
  `last_used`.
- SSE event names are a simple stable contract, and the partial answer is persisted in `finally` so Stop
  does not lose text.

## Unverified / out of scope

- MCP JSON-RPC framing, tool schema validation and sessions live in the `mcp` SDK
  (`MCPServer.streamable_http_app`); not reviewed here.
- libzim fulltext tokenisation/ranking (the mechanism behind F16) and `estimated_matches` semantics were not
  exercised against a real archive.
- No live provider was called: vendor SSE dialects (Perplexity citations, OpenRouter annotations, Gemini
  grounding) are inferred from code and unit tests only.
- Frontend (`web/src/pages/Ask.tsx`, `lib/ai.ts`) was read only to confirm it always sends `provider`+`model`
  and how it parses events; not reviewed.
