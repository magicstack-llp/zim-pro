# Monitoring, Queues, Logs & Network Review — r1

**Scope:** `src/zim_pro/monitor.py`, `logs.py`, `queues.py`, `network.py`, `api/system.py`,
`api/network.py`, `tests/test_system.py`, `tests/test_network.py`; context from `db.py`,
`main.py`, `config.py` and the queue managers (`downloads/manager.py`, `builder/jobs.py`,
`feeds/manager.py`) at their call sites. Commit `cbb1a4e` (Release 0.5.0). Date 2026-11-14.

**Method:** all in-scope files read in full; call sites traced. Ran both test modules (10 + 18
passed). Reproduced three suspicions with throwaway scripts (no source changes): a bad Redis member
escaping as `ValueError`; a non-object Gluetun JSON body escaping as `AttributeError`; redis-py 8's
default retry costing 4.47 s for one `PING()` to a refused port with a 0.2 s timeout.

## Summary

- The Redis fallback design (DB is the record, Redis is the order, 15 s down window) is sound and
  tested, but the health probe does blocking network I/O on the event loop and ignores that window,
  so a dead Redis hangs `/api/monitor` and with it the server.
- Queue claiming is read-then-`UPDATE` with no state guard and no transaction: a cancel/pause in the
  gap is overwritten, and two brokers/processes can claim the same job.
- One malformed Gluetun response raises `AttributeError` out of `Network.status()`, breaking both
  `/api/network` and `/api/monitor`.
- Queue runner loops have no error boundary: any unexpected exception (e.g. a non-numeric Redis
  member) kills that queue permanently and silently.
- Logs, metrics and monitoring series are process-local; queue state lives in two tables with
  unconstrained TEXT states; downloads/builds duplicate state/position logic that has drifted.

## Top 5 must-fix

1. **F1** — `checks()` calls `RedisBroker.info()` on the loop; it pings inside the outage window and
   redis-py retries 10×. Move snapshot work off-loop and make `info()` honour `_usable()`.
2. **F3** — Claim atomically (`UPDATE … WHERE id=? AND state='queued'`, check `rowcount`); stop the
   claim from overwriting `cancelled`/`paused`.
3. **F2** — Validate that Gluetun JSON is an object (catch `AttributeError`/`TypeError`).
4. **F4** — Wrap each queue loop body in `try/except Exception: log.exception(...)`; discard
   non-numeric Redis members in `waiting()`.
5. **F5** — Close the enqueue TOCTOU: no UNIQUE/transaction guards `downloads.catalog_uuid` or
   `builds(kind, source_id)`, so concurrent requests create two jobs writing the same target.

## Findings

### F1. [High] Redis health probe blocks the event loop and ignores the outage window
**Location:** `monitor.py:441` (`broker = ctx.broker.info()`, inside `checks` → `snapshot`, called
synchronously by async `/api/monitor`, `api/system.py:76-80`); `queues.py:186-219` (`info()`),
`:83-84` (`_usable`), `:212` (`SCAN` whole keyspace), `:70-76` (client construction).
**Category:** blocking I/O / availability. **Confidence:** High.
**Evidence:** `info()` never consults `_usable()`, so it issues `PING`, `INFO`, a full `SCAN` and
`XLEN` on every dashboard poll — even while `self.error` is set and `_down_until` has not expired.
The client is built without `retry=`, and redis-py 8 defaults to `Retry(ExponentialWithJitter
Backoff(), 10)`; measured: one `PING()` to a refused port with a 0.2 s timeout took **4.47 s**.
Reproduced that with `_usable() == False` and `error` set, `info()` still called `ping`.
**Impact/Trigger:** with Redis down and the UI open, `/api/monitor` stalls for seconds to tens of
seconds per poll and every request queues behind it (including `/api/health`).
**Fix/Test:** return a cached/minimal `{ok: False, error}` when `not self._usable()`; cache
`INFO/SCAN/XLEN` behind a TTL; `await asyncio.to_thread(snapshot, …)`; `retry=Retry(NoBackoff(), 0)`
for probes. Test: blocking/raising fake broker → `/api/monitor` < 100 ms with `Redis: fail`, zero
commands in the down window.

### F2. [High] Non-object Gluetun JSON raises `AttributeError`, 500s `/api/network` and `/api/monitor`
**Location:** `network.py:116-122` (`reply.json().get("status", "")`), `:128-133` (`data.get` on the
public-IP body), `:151-157` / `:174-176` (only `httpx.HTTPError, ValueError` caught).
**Category:** malformed upstream response. **Confidence:** High (reproduced).
**Evidence:** MockTransport returning `200` with `[]`, `5` and `null` makes `await net.status()`
raise `AttributeError: 'list'/'int' object has no attribute 'get'`. `JSONDecodeError` (a
`ValueError`) is caught, valid JSON of the wrong shape is not. `_look_at_solver` (`:231-234`) does
catch `AttributeError`; the VPN path is inconsistent.
**Impact/Trigger:** a captive portal, proxy, or Gluetun variant answering with non-object JSON
breaks the VPN panel and all monitoring (`/api/monitor` awaits `status()` first,
`api/system.py:79`); `POST /api/network/test` 500s the same way. Repro:
`MockTransport(lambda r: httpx.Response(200, json=[]))`.
**Fix/Test:** parse once and `if not isinstance(data, dict): raise ValueError(...)`; add
`AttributeError, TypeError` backstops. Test: parametrised malformed bodies for both endpoints →
`vpn["error"]`, no raise, `/api/monitor` 200.

### F3. [High] Queue claim is not atomic; it overwrites a concurrent cancel/pause
**Location:** `downloads/manager.py:255-261` (`_set_state(download_id, "downloading")`), `:235-242`
(`UPDATE … SET state = ? … WHERE id = ?`, no state predicate); `builder/jobs.py:252-259`
(`UPDATE builds SET state='processing' … WHERE id = ?`); conditional cancel/pause at
`jobs.py:176-179`, `downloads/manager.py:161-164`, `:182-183`.
**Category:** concurrency / DB integrity. **Confidence:** High.
**Evidence:** `waiting()` reads `state='queued'`, then the runner unconditionally stamps
`downloading`/`processing`. A cancel/pause handled in a threadpool between the two writes its
conditional `cancelled`/`paused`, which the unconditional claim then overwrites; there is no
`BEGIN … UPDATE … WHERE state='queued'` and no `rowcount` check. The same shape lets two
processes sharing the DB/Redis claim and run the same job.
**Impact/Trigger:** cancelled downloads/builds still run, paused jobs start, duplicate execution can
corrupt the same `.part`/`stage/<id>`/ZIM target. Repro: cancel exactly inside the 5 s queue loop.
**Fix/Test:** claim with `… WHERE id=? AND state='queued'` and start only on `rowcount == 1`;
single transaction (`UPDATE … RETURNING`) or atomic Redis pop for multi-worker. Test: pause the loop
between `waiting()` and claim, cancel, resume → row stays `cancelled`, no task; two managers → one
claim.

### F4. [High] One unexpected exception kills a queue runner permanently and silently
**Location:** `queues.py:114-117` (`held = {int(member): score …}` — `ValueError` is not a
`redis.RedisError`); consumed in `_run_queue` at `downloads/manager.py:248-266` and
`builder/jobs.py:245-269`, neither of which has a `try/except`.
**Category:** exception containment. **Confidence:** High (reproduced).
**Evidence:** `server.zadd("zimpro:queue:builds", {"not-a-number": 1})` makes
`broker.waiting("builds", …)` raise `ValueError: invalid literal for int()`. A dead `asyncio.Task`
is only surfaced at GC ("Task exception was never retrieved"); the queue never starts another job.
Any DB/`KeyError`/setting-parse error in the loop has the same effect.
**Impact/Trigger:** downloads/builds/feeds stop starting jobs forever after one bad member or bug;
waiting lists grow, and the "Jobs" check is happy because nothing is active. Repro:
`redis-cli ZADD zimpro:queue:builds 1 garbage` with a queued build.
**Fix/Test:** parse defensively and `ZREM` bad members; wrap the loop body with
`except asyncio.CancelledError: raise` / `except Exception: log.exception(...)`; watchdog the task.
Test: seed garbage → `waiting()` ignores it and the runner survives a broker that raises once.

### F5. [Medium] Enqueue is check-then-insert; concurrent requests create duplicate jobs
**Location:** `downloads/manager.py:122-137` (`SELECT` existing then `INSERT`), `:136`
(`MAX(position)+1`); `builder/jobs.py:156-166` (same for `builds`); `db.py:62-80`, `:160-180` (no
UNIQUE/CHECK).
**Category:** DB integrity / race. **Confidence:** High (code), Medium on frequency.
**Evidence:** SELECT and INSERT are separate lock acquisitions in `db.one`/`db.execute`; FastAPI
handles requests concurrently and the scheduler (`scheduler.py:30-39`) also enqueues from the loop.
Two download rows for the same `catalog_uuid` then race on the same `filename + ".part"` target
(`manager.py:318-336`) with independent piece bitmaps.
**Impact/Trigger:** duplicate gigabytes, corrupted partial file, duplicate builds, inflated counts.
Repro: two simultaneous `POST /api/downloads` with the same UUID.
**Fix/Test:** atomic `INSERT … SELECT … WHERE NOT EXISTS (…)` or `BEGIN IMMEDIATE`; partial unique
index `UNIQUE(catalog_uuid) WHERE state IN ('queued','downloading','verifying')`, likewise for
builds. Test: two threads → one row.

### F6. [Medium] The whole monitoring snapshot runs on the event loop
**Location:** `api/system.py:76-80`; `monitor.py:333-404` (`shutil.disk_usage`, `p.stat`,
`psutil.virtual_memory`, `db.scalar`), `:406-511` (~7 SQL queries incl. two unindexed 24 h
`COUNT(*)`, `broker.info()`), `:400` (`hub.summary` over 5 000 lines).
**Category:** blocking work in async endpoint. **Confidence:** High.
**Evidence:** every snapshot field is computed synchronously; only `await ctx.network.status()`
precedes it, which with a stale cache also joins two 8 s HTTP timeouts (`network.py:84`, `:223`).
**Impact/Trigger:** dashboard polling blocks request handling; `COUNT(*)` scans grow with history,
`disk_usage` can be slow. Combined with F1 this turns a slow dashboard into a stalled server.
Repro: poll `/api/monitor` while `_persist` holds a large WAL write; watch `metrics.lag_now`.
**Fix/Test:** `await asyncio.to_thread(ctx.monitor.snapshot, network, span)`; cache `checks()`;
index `builds(state, finished)`/`downloads(state, updated)`. Test: assert psutil/disk never run on
the loop thread or measure lag under load.

### F7. [Medium] Unbounded metrics cardinality from arbitrary request paths
**Location:** `monitor.py:81` (`self.requests = {}`), `:91-95` (`setdefault(group, _Route())`),
`:149-158` (`route_group` → `"/api/" + parts[1]`), `:128-143` (whole dict serialised); request
recording in `main.py:139`.
**Category:** resource exhaustion (unauthenticated input). **Confidence:** High.
**Evidence:** `route_group("/api/anything")` → `/api/anything`; every request, including 404s for
non-existent paths, is recorded. Unlike `fetch_hosts` (`KEEP_HOSTS = 200`, `:105-107`), `requests`
and `ai` are uncapped, and each `_Route` holds a 500-element deque (`KEEP_DURATIONS`).
**Impact/Trigger:** a scanner hitting `/api/<random>` grows memory monotonically until restart and
bloats every `/api/monitor` payload. Repro: request 100 000 unique `/api/x{i}`.
**Fix/Test:** allow-list groups or normalise unknown `/api/*` to `/api/other`; LRU-cap the dict.
Test: 10 000 synthetic paths → bounded `len(view["requests"])`.

### F8. [Medium] redis-py's 10-retry default makes the first failure after every window very slow
**Location:** `queues.py:70-76` (`from_url(..., socket_timeout=2, socket_connect_timeout=2,
health_check_interval=30)` — no `retry=`, no `retry_on_timeout`).
**Category:** connectivity / latency. **Confidence:** High (measured `Retry._retries == 10`;
refused port with 0.2 s timeout took 4.47 s).
**Evidence:** every `ConnectionError`/`TimeoutError` is retried ten times before `_failed()` sets
`_down_until = now + 15`. `waiting()` (worker thread) therefore stalls the queue loop for seconds on
the first look after each 15 s window, and Redis API endpoints stall likewise; retries can also
re-send a pipeline, duplicating `XADD`s in `zimpro:logs` when a reply is lost.
**Impact/Trigger:** "degraded to database" is slow exactly when throughput matters (black-holed
Redis, DNS/Packet loss). Repro: point at `10.255.255.1`; time one `waiting()`.
**Fix/Test:** explicit `retry=Retry(NoBackoff(), 0)` (or one retry) for the broker; keep timeouts low;
log the fallback once. Test: fake client raising `redis.TimeoutError` and counting calls → one
attempt per command.

### F9. [Medium] Job actions are not scoped to their queue; one queue can cancel/retry another's job
**Location:** `builder/jobs.py:170-181` (`cancel`), `:183-199` (`pause`), `:201-213` (`resume`),
`:215-223` (`retry`), `:225-230` (`remove`), `:279-286` (`place`) — none filter on `self._mine`;
`api/system.py:318-334` picks the manager only from the URL name.
**Category:** integrity of queue operations. **Confidence:** High.
**Evidence:** `cancel` updates `builds WHERE id=? AND state IN ('queued','paused')` with no `kind`
predicate, so a feed-sync id posted to `/api/queues/builds/jobs/{id}/cancel` is cancelled through the
builds manager and the response shows the builds view without it. `retry` also recomputes
`position` across all kinds (`jobs.py:219`).
**Impact/Trigger:** a wrong id or UI mix-up cancels/pauses/retries/deletes a job of another kind;
feed syncs silently vanish from the feeds view; retry reorders across queues. Repro:
`POST /api/queues/builds/jobs/<feed-build-id>/cancel`.
**Fix/Test:** add `AND {self._mine}` (or resolve via `QUEUES[name]`, 404 on mismatch). Test:
cross-kind action → 404/409, row untouched.

### F10. [Medium] `change_ip()` reports "changed" when the old address could not be read
**Location:** `network.py:278-308`; `before = ""` (`:278`), swallowed read error (`:283-284`),
`changed = bool(after) and after != before` (`:302`).
**Category:** VPN control correctness / retry. **Confidence:** High (code path).
**Evidence:** if `_public_ip()` fails before the reconnect, `before` stays `""`, so any non-empty
`after` yields `changed=True`, `last_change_error=""` and increments `generation`/`changes`. The
crawler (`builder/crawler.py:283-308`) then clears the host block, strikes, delays and cookies and
retries the same URL from what may be the same exit address.
**Impact/Trigger:** "My IP changed" is shown when only a reconnect happened; a rate-limited site
keeps seeing the same address while the crawler believes it moved (extra 429s and reconnects).
Repro: fail the first `/v1/publicip/ip` call, then succeed.
**Fix/Test:** track `before: str | None`; report "could not compare" and keep
`last_change_error` instead of success. Test: first public-IP call fails → result says comparison
impossible and the crawler does not reset its block state.

### F11. [Medium] Queue-control endpoints declared `async` perform blocking DB/Redis work
**Location:** `api/downloads.py:22-29` (`create_download` → `enqueue`), `:32-35`, `:38-54`
(`act` → `pause`/`resume`/`cancel`/`move`), `:58-61`; `downloads/manager.py:203-215` (`waiting` →
`broker.waiting`, i.e. `ZRANGE`/`ZADD`).
**Category:** blocking I/O on the loop. **Confidence:** High.
**Evidence:** the handlers are `async def` but every manager call is synchronous: SQLite writes plus,
for `move`/`place`, a Redis round trip with the retry policy from F8. Sibling endpoints in
`api/system.py` are plain `def` (threadpool) — the inconsistency is the smell.
**Impact/Trigger:** "pause/top this download" freezes all requests for seconds when Redis is
slow/down. Repro: black-hole Redis, `POST /api/downloads/{id}/top`, measure `/api/health`.
**Fix/Test:** make the handlers `def` or `await asyncio.to_thread(...)`. Test: route executes
off-loop / timing test with a slow fake broker.

### F12. [Low] The Gluetun route fallback latches permanently on one 404
**Location:** `network.py:116-122` (`self._vpn_route = "openvpn"`), `:52`, `:256-258` (`forget()`).
**Category:** VPN API compatibility. **Confidence:** High.
**Evidence:** the first `/v1/vpn/status` 404 (proxy, wrong path, transient upstream) switches the
process-wide route forever; `forget()` (called on every settings save, `api/library.py:286`) does not
reset it, and later `openvpn` 404s are never retried against `vpn`.
**Impact/Trigger:** a corrected URL/version keeps using the wrong endpoint until restart; Settings
shows HTTP 404 with no hint of the latch.
**Fix/Test:** remember the route per base URL/reset in `forget()`, or probe both routes. Test: 404 on
`vpn`, success on `openvpn`, change URL back → `vpn` tried again.

### F13. [Low] Three queue paths duplicate state/order logic, and semantics have already drifted
**Location:** `downloads/manager.py` vs `builder/jobs.py` (claim, pause, resume, waiting, place,
retry, cancel_waiting, clear_finished duplicated); states repeated in `db.py:71`, `:165`,
`monitor.py:263-267`, `api/system.py:85-119`, manager constants.
**Category:** normalisation / drift. **Confidence:** High.
**Evidence:** downloads `clear_finished` deletes only `done`/`cancelled`
(`downloads/manager.py:196-198`) while builds also delete `failed` (`jobs.py:232-237`); retrying a
download keeps its old `position` and leaves `finished` set (`manager.py:167-173`, `:217-221`)
while builds move to the bottom and clear `started/finished/position` (`jobs.py:215-223`);
`cancel_waiting` counts active paused builds as cancelled without cancelling them
(`jobs.py:294-301`) while downloads cancels and counts them (`manager.py:223-227`). No
`CHECK (state IN (...))` on either table; `queue_counts` re-hardcodes kind/state filters separately.
**Impact/Trigger:** user-visible inconsistency ("clear finished" keeps failed downloads but not
builds; retried downloads jump the queue) and new states/kinds silently invisible to monitoring.
**Fix/Test:** one `Queue` abstraction (state enum/transitions/position) or shared constants; CHECK
constraints; derive `QUEUES`/`queue_counts` from them. Test: table-driven identical semantics for
both queues.

### F14. [Low] Log lines are discarded while Redis is down; the shipping task can die silently
**Location:** `main.py:57-63` (`take_outbox()` before the broker check/send), `queues.py:173-175`
(`if not lines or not self._usable(): return`), `:183-184` (only `RedisError` caught),
`main.py:90-96` (tasks cancelled but not awaited).
**Category:** log durability / error containment. **Confidence:** High.
**Evidence:** up to 5 000 lines are removed from the outbox, then `send_logs` returns immediately
inside the 15 s down window — lost for Redis subscribers even though the file copy survives. Any
non-`RedisError` (or cancellation between drain and send) ends the `shipping` task with no log and no
restart; `background.cancel()`/`shipping.cancel()` are not awaited.
**Impact/Trigger:** the Redis log view has holes exactly during an incident; after one unexpected
error, no lines ever reach Redis again.
**Fix/Test:** keep lines until a send succeeds (bounded retry queue), catch `Exception` and log once,
`await asyncio.gather(..., return_exceptions=True)` on shutdown. Test: broker down → lines kept or
retried; broker raising a non-Redis error → later calls still ship.

### F15. [Low] `/api/logs/download` loads up to ~30 MB in memory, can 500 on rotation, and may end mid-line
**Location:** `api/system.py:46-62` (`files = logs.hub.files()`, `b"".join(file.read_bytes() …)`);
`logs.py:242-246` (`exists()` then read), `:135-136` (handler writing concurrently).
**Category:** export consistency / memory. **Confidence:** High (race), Medium (impact).
**Evidence:** `files()` snapshots names, then `read_bytes()` runs; `RotatingFileHandler` can
rotate/rename between them (uncaught `FileNotFoundError` → 500). The current file is appended to
while read, so the last line can be a truncated JSON object, breaking the "one object per line"
contract.
**Impact/Trigger:** a failed/partial audit export exactly when logs matter, and a 30 MB transient
allocation. Repro: download while logging heavily / rotate in between.
**Fix/Test:** per-file `try/except`, stream or temp-file the response, drop an unterminated final
line. Test: concurrent writes/rotation → every line parses, no 500.

### F16. [Low] Restart log history is only the current file's last 4 MB; rotated files are ignored
**Location:** `logs.py:151-169` (`_read_back` opens `self.path`, seeks `size - 4 MB`, keeps
`[-KEEP:]`).
**Category:** log persistence semantics. **Confidence:** High.
**Evidence:** only `zim-pro.log` is read back, never `.1`…`.5` that `files()` knows about, and a
leading truncated line is silently skipped. After rotation, the Logs panel shows a partial window,
and `hub.newest`/`after` restart from 1 while a browser may hold an old cursor.
**Impact/Trigger:** "logs disappeared" after restart/rotation; `after` polling across a restart can
skip lines.
**Fix/Test:** read the newest rotated file too until `KEEP` lines; make/document cursor reset. Test:
rotate with small `maxBytes`, restart a hub, assert expected lines/order.

### F17. [Low] `psutil` failures in `snapshot`/`checks` are unguarded and 500 the dashboard
**Location:** `monitor.py:336-348` (`memory_info`, `num_threads`, `create_time`,
`virtual_memory`, `cpu_count`, `boot_time`), `:435`; contrast guarded `num_fds`/`getloadavg`
(`:339-342`, `:345-348`) and `sample()` protected by `_sample_loop` (`:202-209`).
**Category:** error handling. **Confidence:** Medium (platform/container dependent).
**Evidence:** only `num_fds` and `getloadavg` are wrapped; `AccessDenied`/`NoSuchProcess`/masked
`/proc` makes the whole `/api/monitor` 500 even though the sampled series exists.
**Impact/Trigger:** monitoring unavailable in hardened containers with partially masked `/proc`.
**Fix/Test:** wrap process/system gathering in `try/except psutil.Error` with `None`/last-sample
fallback. Test: monkeypatch `psutil.virtual_memory`/`memory_info` to raise → `/api/monitor` 200.

### F18. [Low] Monitor bookkeeping is touched from two threads without synchronisation
**Location:** `monitor.py:196-197` vs `:230` (`metrics.lag_worst` read-then-reset in the sample
thread while `_time_loop` updates it on the loop); `:174-175`, `:308-329` vs `api/system.py:166,174`
(`quiet_for`, `key in monitor.stalled` read from a threadpool request).
**Category:** thread-safety. **Confidence:** Medium (lost update, not a crash).
**Evidence:** worst lag is read and zeroed non-atomically, so a spike recorded in between is lost;
`stalled` reads can observe `_moved` updated before `stalled`, briefly mis-flagging a job.
**Impact/Trigger:** occasionally under-reported event-loop lag — the exact signal used by the
"Responsiveness" health check — and cosmetic stall flicker.
**Fix/Test:** reset under a lock / atomic max-and-swap; keep monitor state behind a lock or
loop-confined. Test: concurrent timer/sampler asserting no lost worst-lag sample.

### F19. [Nit] `/api/network/test` accepts any `kind` and silently runs the VPN test
**Location:** `api/network.py:34` (`kind: str = "vpn"`), `:56-63`. **Confidence:** High.
`{"kind": "sovler"}` (typo) is treated as a VPN test; no enum/`Literal`. Admin-only, no security
impact. Fix: `Literal["vpn", "solver"]`; test unknown kind → 422.

## Database integrity & normalization notes

- **Good:** `_meta` keys use a `_` prefix (`db.py:413-422`) and `get_settings` only returns
  `DEFAULT_SETTINGS` keys (`:396-401`), so meta cannot collide with settings; `set_settings` drops
  unknown keys, so the API cannot write `_meta`. Settings are a per-key partial upsert, so
  concurrent edits of different keys do not clobber each other.
- **No CHECK constraints** on `downloads.state`/`builds.state`/`builds.kind`; invalid or mistyped
  states silently vanish from `waiting()`, counts and the UI (F13).
- **Position typing is inconsistent:** `downloads.position INTEGER NOT NULL DEFAULT 0` (`db.py:77`)
  vs `builds.position REAL` from migration (`db.py:323-324`, read with `COALESCE(position, id)`,
  `jobs.py:274`). `place()` writes midpoints (e.g. 2.5) into the INTEGER-affinity downloads column;
  SQLite stores REAL so it works today, but the schema documents the wrong type and a typed
  migration would truncate ordering.
- **Missing indexes** on `downloads(state, position)`, `builds(kind, state)`, `builds(finished)`,
  `downloads(updated)`; monitor/queue views scan (fine now, grows with history).
- **Settings reads are whole-table:** `get_setting()` calls `get_settings()` (select + `json.loads`
  every row, `db.py:396-404`); `Network` does this 4–6× per look (`network.py:135-148`) and each
  `_run_queue` every 5 s (`manager.py:251-253`, `jobs.py:250`). Correct but O(all settings) per
  field; a per-key cache would be cheaper.
- **Monitoring series are process-local** (`Monitor.hour/day` deques), so `?span=24h` is empty after
  every restart and never survives a redeploy. Logs are mirrored to file/Redis; metrics have no
  durable store. Either persist a minute rollup or label the panels "since start".
- **Storage split is sensible:** memory ring (5 000), rotating file (5 × 5 MB), Redis stream
  (~20 000) — no DB table to grow; `hub.counts` is monotonic, which the delta math relies on
  (`monitor.py:225-229`).
- **Broker contract:** `LocalBroker.place` is a deliberate no-op because the caller writes
  `position` (`queues.py:43-44`, `manager.py:213-214`); `RedisBroker.place` writes both. Parity holds
  today, but "DB order is authoritative when Redis has no opinion" is implicit and depends on every
  caller doing both writes.

## Connectivity & failure handling

- **Redis start-up is safe:** `from_url` is lazy, no connection in `make_broker`
  (`queues.py:256-260`); `waiting`/`paused`/`send_logs` fall back per call with a 15 s window and one
  warning (`:86-95`). Tested.
- **Fallback latency is poor** because of the 10-retry default (F8) and the health probe ignoring the
  window (F1): a dead Redis is slow to detect and then hammered by `/api/monitor`.
- **Reconnect** clears `error` and logs once (`:97-101`); queues reseed from DB on the next look and
  externally made Redis-only order changes are intentionally lost on Redis data loss
  (`:152-153`). Reasonable, documented trade-off.
- **TLS/cluster:** `rediss://` and URL credentials work via `from_url`; no Sentinel/cluster support,
  no TLS CA/verify knobs. Fine for a single URL, worth stating in docs.
- **Blocking calls in async code:** `/api/monitor` (F1/F6) and `/api/downloads/*` (F11); by contrast
  `/api/queues`, `queue_action`, `job_action`, `set_concurrency` are sync `def` (threadpool) — the
  right shape and the model to copy.
- **VPN HTTP:** every control/solver/proxy-test call has an explicit timeout (8/15 s; stop→start
  bounded by 120 s); `_steady` gates fetchers during a change (`network.py:78-80`,
  `crawler.py:406`); error wording is human (`_problem`, `:100-114`). Gaps: F2, F10, F12.
- **SSE** (`api/downloads.py:64-86`, `events.py`): slow clients drop events instead of blocking
  (`QueueFull` pass, maxsize 256); disconnects are detected after each yield/20 s keepalive and
  `finally` unsubscribes. A client disconnecting while waiting can linger up to 20 s — acceptable.
- **No logging recursion:** `LogHub.emit` wraps everything and calls `handleError` (stderr), never
  re-logs. `handleError` output can be lost in a closed-stderr container but cannot hurt the caller.
- **Shutdown:** `background`/`shipping` are cancelled but not awaited; `ctx.broker.close()` can race
  an in-flight `send_logs` thread (only `RedisError` is caught).

## Testing gaps

**Covered:** log tagging/redaction (`test_system.py:31-79`), `new_place` math (`:92-111`), Redis
order/seed/eviction and paused parity (`:114-142`, fakeredis), one `ConnectionError` fallback and
recovery (`:145-164`, private `_down_until` reset by hand), metrics counting/`route_group`
(`:170-183`), API smoke for logs/monitor/queues/pause/reorder/concurrency clamp/cancel-waiting/404
(`:197-238`). Network: VPN status/IP, key vs basic auth (`test_network.py:80-102`), older Gluetun
route (`:105-110`), stop/start and `since`/cooldown happy paths (`:113-127`), no control server
(`:130-134`), change-on-429 through `Fetcher` (`:137-179`), proxy churn/trickle timeout
(`:182-238`), `try_vpn` and solver happy path (`:387-415`).

**Untested:**
- `Monitor.sample()` math: CPU/rate deltas, `download_speed`, error/warning deltas, lag reset,
  `_keep`, `_merge`, hour/day wraparound, `span="24h"` (`monitor.py:213-257`, `:514-536`);
  `_watch_jobs`/`_check_moved` (600 s stall, "moving again", paused never stalled, `quiet_for`,
  `monitor.py:282-329`).
- Every `checks()` branch except Redis-off/DB-ok: low disk, responsiveness, memory, Redis failure,
  VPN off/disabled/error, solver not-ready, catalog, failures, errors (`monitor.py:406-511`).
- Redis failure in `paused()`, `set_paused()`, `send_logs()` and `info()` (only `zrange` is made to
  fail); `_usable()` expiry without poking `_down_until`; non-numeric members (F4); no assertion that
  fallback is fast (no retry-storm test).
- Queue claim races (F3/F5), cross-kind actions (F9), retry/clear-finished drift (F13), position
  REAL/INTEGER and migration of pre-0.5 rows.
- Log rotation (`maxBytes`), `files()` ordering, `/api/logs/download` with rotated files and a
  rotation race, `query(before=…)`, `limit` edges, `clear()`, `attach_file` `OSError`, `_read_back`
  truncated first line / >4 MB tail; log loss on Redis outage and the shipping loop
  (`main.py:57-63`).
- Malformed VPN/control responses (F2), `change_ip` failure/timeout/unknown-before (F10), route latch
  (F12), `status` TTL/`forget`, `steady()` during a change, and the `/api/network` +
  `/api/network/test` HTTP layer (mask replacement, 422s) — tests call `Network.try_vpn` directly.
- `psutil` failure paths (F17), metrics cardinality (F7), settings concurrency.

Tests use `fakeredis` plus a subclass raising `redis.ConnectionError` and assert DB-order fallback
and reseeding, but not socket timeouts, retry behaviour, or a Redis outage mid-claim. No test asserts
loop responsiveness.

## What is done well

- Startup recovery is explicit: downloads reset `downloading/verifying → queued`
  (`downloads/manager.py:73-76`), builds reset `processing → queued` and clear the stale phase
  (`builder/jobs.py:103-111`) — no row left "running" after a crash.
- `_meta` namespacing plus the `DEFAULT_SETTINGS` allow-list avoids settings/meta collisions and
  whole-dict overwrites; settings are a per-key partial upsert.
- Secrets are redacted before memory/file/Redis/console (`logs.py:76-79`, `:125`), the file is
  JSON-encoded (no newline injection), and `emit` never raises into the logging caller.
- Redis URL masking (`queues.py:228-233`) covers logs and `broker.info()`; a test asserts no
  password leaks.
- Redis degradation (DB authoritative, Redis for order, one-shot warning, DB follows external pause
  changes) is coherent and partially tested.
- VPN control: single-flight `_changing`, `_steady` gate, bounded 120 s reconnect wait, older-Gluetun
  route fallback, human-readable errors, and `httpx.MockTransport` seams.
- Disk-space checks account for sparse `.part` files and reserve margins
  (`downloads/manager.py:381-389`, `jobs.py:562-567`).

## Unverified / out of scope

- Frontend rendering of log messages (possible DOM XSS from remote-controlled text) — assets were
  not reviewed; server-side JSON is safe, console format is not sanitised.
- Real Gluetun versions, real Redis Sentinel/Cluster/TLS, and multi-process deployments sharing one
  DB+Redis (F3/F5 assume one process).
- `psutil` on Windows/macOS or masked-`/proc` containers (F17 reasoned from code).
- Crawler/fetcher internals beyond their `Network` calls; `ai.py`, `providers.py`, `mcp_server.py`
  and other API modules were read only where called.
- No load/soak testing; F1/F6/F8 latency claims rest on code plus the measured redis-py retry timing.
