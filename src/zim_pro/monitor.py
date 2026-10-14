"""What Zim Pro and the machine it runs on are doing, measured every few seconds.

:data:`metrics` is filled from wherever things happen: each request to the server, each
page or file fetched for a build or a feed, each call to an AI provider. :class:`Monitor`
samples it with the machine's own figures every :data:`SAMPLE_SECONDS`, keeps an hour of
samples and a day of one-minute averages, and runs the health checks Settings shows.
It also notices jobs that stop moving, and says so in the logs.
"""

import asyncio
import logging
import os
import platform
import shutil
import sys
import threading
import time
from collections import deque
from importlib import metadata
from typing import TYPE_CHECKING, Any

import psutil

from .logs import hub

if TYPE_CHECKING:
    from .context import Context

log = logging.getLogger(__name__)

SAMPLE_SECONDS = 10
HOUR_POINTS = 360  # an hour of samples
DAY_POINTS = 1440  # a day of one-minute averages
LAG_EVERY = 0.5  # how often the event loop is timed
STALL_SECONDS = 600  # a job that has not moved for this long is reported
LOW_DISK_WARN = 5 * 1024**3
LOW_DISK_FAIL = 1024**3
KEEP_DURATIONS = 500
KEEP_HOSTS = 200


def _percentile(values: list[float], share: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(share * len(ordered)))]


class _Route:
    __slots__ = ("count", "errors", "seconds", "slowest", "recent")

    def __init__(self) -> None:
        self.count = self.errors = 0
        self.seconds = self.slowest = 0.0
        self.recent: deque[float] = deque(maxlen=KEEP_DURATIONS)

    def add(self, seconds: float, failed: bool) -> None:
        self.count += 1
        self.errors += failed
        self.seconds += seconds
        self.slowest = max(self.slowest, seconds)
        self.recent.append(seconds)

    def view(self) -> dict[str, Any]:
        recent = list(self.recent)
        return {
            "count": self.count,
            "errors": self.errors,
            "average_ms": round(self.seconds / self.count * 1000, 1) if self.count else 0,
            "p50_ms": round(_percentile(recent, 0.5) * 1000, 1),
            "p95_ms": round(_percentile(recent, 0.95) * 1000, 1),
            "slowest_ms": round(self.slowest * 1000, 1),
        }


class Metrics:
    """Counters anything may add to, from any thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests: dict[str, _Route] = {}
        self.fetch_hosts: dict[str, dict[str, Any]] = {}
        self.fetch = _Route()
        self.fetch_bytes = 0
        self.ai: dict[str, _Route] = {}
        self.lag_worst = 0.0  # since the last sample
        self.lag_now = 0.0
        # Running totals, read and differenced by each sample.
        self.totals = {"requests": 0, "request_errors": 0, "fetches": 0, "fetch_errors": 0}

    def request(self, group: str, status: int, seconds: float) -> None:
        with self._lock:
            self.requests.setdefault(group, _Route()).add(seconds, status >= 500)
            self.totals["requests"] += 1
            self.totals["request_errors"] += status >= 500

    def fetched(self, host: str, ok: bool, size: int, seconds: float, error: str = "") -> None:
        with self._lock:
            self.fetch.add(seconds, not ok)
            self.fetch_bytes += size
            self.totals["fetches"] += 1
            self.totals["fetch_errors"] += not ok
            entry = self.fetch_hosts.get(host)
            if entry is None:
                if len(self.fetch_hosts) >= KEEP_HOSTS:  # forget the quietest
                    quiet = min(self.fetch_hosts, key=lambda h: self.fetch_hosts[h]["last"])
                    del self.fetch_hosts[quiet]
                entry = self.fetch_hosts[host] = {
                    "count": 0,
                    "errors": 0,
                    "bytes": 0,
                    "seconds": 0.0,
                    "last": 0.0,
                    "last_error": "",
                }
            entry["count"] += 1
            entry["errors"] += not ok
            entry["bytes"] += size
            entry["seconds"] += seconds
            entry["last"] = time.time()
            if error:
                entry["last_error"] = error[:200]

    def ai_call(self, provider: str, ok: bool, seconds: float) -> None:
        with self._lock:
            self.ai.setdefault(provider or "?", _Route()).add(seconds, not ok)

    def view(self) -> dict[str, Any]:
        with self._lock:
            hosts = sorted(self.fetch_hosts.items(), key=lambda item: -item[1]["count"])[:25]
            return {
                "requests": {group: route.view() for group, route in sorted(self.requests.items())},
                "fetches": {**self.fetch.view(), "bytes": self.fetch_bytes},
                "hosts": [
                    {
                        "host": host,
                        **{k: v for k, v in entry.items() if k != "seconds"},
                        "average_ms": round(entry["seconds"] / entry["count"] * 1000, 1),
                    }
                    for host, entry in hosts
                ],
                "ai": {provider: route.view() for provider, route in sorted(self.ai.items())},
            }


metrics = Metrics()


def route_group(path: str) -> str:
    """Requests counted together: ``/api/feeds/3/sync`` is ``/api/feeds``."""
    parts = [part for part in path.split("/") if part]
    if not parts:
        return "pages"
    if parts[0] == "api":
        return "/api/" + parts[1] if len(parts) > 1 else "/api"
    if parts[0] in ("content", "mcp", "assets"):
        return "/" + parts[0]
    return "pages"


class Monitor:
    def __init__(self, ctx: "Context"):
        self.ctx = ctx
        self.started = time.time()
        self.hour: deque[dict[str, Any]] = deque(maxlen=HOUR_POINTS)
        self.day: deque[dict[str, Any]] = deque(maxlen=DAY_POINTS)
        self._minute: list[dict[str, Any]] = []
        self._process = psutil.Process()
        self._process.cpu_percent(None)
        psutil.cpu_percent(None)
        self._last_totals = dict(metrics.totals)
        self._last_bytes = 0
        self._last_logs = dict(hub.counts)
        self._moved: dict[str, tuple[Any, float]] = {}  # job -> (what it last showed, when)
        self.stalled: dict[str, float] = {}  # job -> since when
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._time_loop()),
            asyncio.create_task(self._sample_loop()),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _time_loop(self) -> None:
        """How late the event loop wakes: everything the server does waits that long."""
        loop = asyncio.get_running_loop()
        while True:
            expected = loop.time() + LAG_EVERY
            await asyncio.sleep(LAG_EVERY)
            lag = max(0.0, loop.time() - expected)
            metrics.lag_now = lag
            metrics.lag_worst = max(metrics.lag_worst, lag)

    async def _sample_loop(self) -> None:
        while True:
            await asyncio.sleep(SAMPLE_SECONDS)
            try:
                point = await asyncio.to_thread(self.sample)
                self._watch_jobs()
                self._keep(point)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("monitoring sample failed")

    # -- samples -------------------------------------------------------------------------

    def sample(self) -> dict[str, Any]:
        process = self._process
        with process.oneshot():
            cpu = process.cpu_percent(None)
            rss = process.memory_info().rss
            threads = process.num_threads()
        memory = psutil.virtual_memory()
        totals = dict(metrics.totals)
        diff = {key: totals[key] - self._last_totals.get(key, 0) for key in totals}
        self._last_totals = totals
        fetched = metrics.fetch_bytes - self._last_bytes
        self._last_bytes = metrics.fetch_bytes
        counts = dict(hub.counts)
        errors = counts.get("ERROR", 0) + counts.get("CRITICAL", 0)
        errors -= self._last_logs.get("ERROR", 0) + self._last_logs.get("CRITICAL", 0)
        warnings = counts.get("WARNING", 0) - self._last_logs.get("WARNING", 0)
        self._last_logs = counts
        lag, metrics.lag_worst = metrics.lag_worst, 0.0
        queues = self.queue_counts()
        return {
            "t": round(time.time()),
            "cpu": round(cpu, 1),
            "system_cpu": round(psutil.cpu_percent(None), 1),
            "rss": rss,
            "memory": round(memory.percent, 1),
            "threads": threads,
            "lag_ms": round(lag * 1000, 1),
            "requests": diff["requests"],
            "request_errors": diff["request_errors"],
            "fetches": diff["fetches"],
            "fetch_errors": diff["fetch_errors"],
            "fetch_bytes": fetched,
            "download_speed": round(self.download_speed()),
            "running": sum(q["running"] for q in queues.values()),
            "waiting": sum(q["waiting"] for q in queues.values()),
            "errors": max(0, errors),
            "warnings": max(0, warnings),
        }

    def _keep(self, point: dict[str, Any]) -> None:
        self.hour.append(point)
        self._minute.append(point)
        if len(self._minute) * SAMPLE_SECONDS >= 60:
            self.day.append(_merge(self._minute))
            self._minute = []

    def download_speed(self) -> float:
        return sum(active.speed for active in list(self.ctx.downloads._active.values()))

    def queue_counts(self) -> dict[str, dict[str, int]]:
        counts = {}
        for name, table, running, waiting, extra in (
            ("downloads", "downloads", "'downloading', 'verifying'", "'queued'", ""),
            ("builds", "builds", "'processing'", "'queued'", "AND kind IN ('link', 'upload')"),
            ("feeds", "builds", "'processing'", "'queued'", "AND kind = 'feed'"),
        ):
            row = self.ctx.db.one(
                f"SELECT SUM(state IN ({running})) AS running, "
                f"SUM(state IN ({waiting})) AS waiting "
                f"FROM {table} WHERE 1 {extra}"
            )
            counts[name] = {
                "running": (row or {}).get("running") or 0,
                "waiting": (row or {}).get("waiting") or 0,
            }
        return counts

    # -- jobs that stop moving -----------------------------------------------------------

    def _watch_jobs(self) -> None:
        now = time.monotonic()
        seen: set[str] = set()
        for name, manager in (("build", self.ctx.builds), ("feed", self.ctx.feeds)):
            for job_id, active in list(manager._active.items()):
                reporter = active.reporter
                key = f"{name} {job_id}"
                seen.add(key)
                snap = reporter.snapshot()
                waiting = bool(snap["detail"]) or reporter.paused.is_set()
                self._check_moved(
                    key,
                    (snap["phase"], snap["done"], snap["total"], snap["failed"], snap["skipped"]),
                    now,
                    waiting,
                )
        for job_id, active in list(self.ctx.downloads._active.items()):
            key = f"download {job_id}"
            seen.add(key)
            done = active.download.bytes_done if active.download else 0
            self._check_moved(key, done, now, False)
        for key in list(self._moved):
            if key not in seen:
                del self._moved[key]
                self.stalled.pop(key, None)

    def _check_moved(self, key: str, shows: Any, now: float, waiting: bool) -> None:
        last = self._moved.get(key)
        if last is None or last[0] != shows or waiting:
            self._moved[key] = (shows, now)
            if key in self.stalled:
                log.info("%s is moving again", key, extra={"job": key})
                del self.stalled[key]
            return
        if now - last[1] >= STALL_SECONDS and key not in self.stalled:
            self.stalled[key] = time.time() - (now - last[1])
            log.warning(
                "%s has not moved for %d minutes; it may be stuck "
                "(cancel and retry it if it stays so)",
                key,
                (now - last[1]) // 60,
                extra={"job": key},
            )

    def quiet_for(self, key: str) -> float | None:
        """Seconds since the job last moved, if it is being watched."""
        last = self._moved.get(key)
        return time.monotonic() - last[1] if last else None

    # -- what Settings shows -------------------------------------------------------------

    def snapshot(self, network: dict[str, Any], span: str = "1h") -> dict[str, Any]:
        ctx = self.ctx
        process = self._process
        with process.oneshot():
            rss = process.memory_info().rss
            threads = process.num_threads()
            try:
                files = process.num_fds()
            except (AttributeError, psutil.Error):
                files = None
            created = process.create_time()
        memory = psutil.virtual_memory()
        try:
            load = [round(x, 2) for x in os.getloadavg()]
        except (AttributeError, OSError):
            load = []
        disks = []
        for label, path in (("Library", ctx.config.data_dir), ("State", ctx.config.state_dir)):
            try:
                usage = shutil.disk_usage(path)
            except OSError:
                continue
            disks.append(
                {
                    "label": label,
                    "path": str(path),
                    "total": usage.total,
                    "used": usage.used,
                    "free": usage.free,
                }
            )
        database = ctx.config.db_path
        db_size = sum(
            p.stat().st_size
            for p in (database, database.with_name(database.name + "-wal"))
            if p.exists()
        )
        return {
            "now": time.time(),
            "version": _version(),
            "python": sys.version.split()[0],
            "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
            "pid": os.getpid(),
            "started": created,
            "process": {
                "cpu": self.hour[-1]["cpu"] if self.hour else 0,
                "rss": rss,
                "threads": threads,
                "files": files,
                "lag_ms": round(metrics.lag_now * 1000, 1),
                "listeners": len(ctx.events._subscribers),
            },
            "system": {
                "cpus": psutil.cpu_count(),
                "cpu": self.hour[-1]["system_cpu"] if self.hour else 0,
                "memory_total": memory.total,
                "memory_used": memory.total - memory.available,
                "memory": memory.percent,
                "load": load,
                "booted": psutil.boot_time(),
            },
            "disks": disks,
            "database": {
                "size": db_size,
                "books": ctx.db.scalar("SELECT COUNT(*) FROM books") or 0,
            },
            "metrics": metrics.view(),
            "logs": hub.summary(time.time() - 3600),
            "checks": self.checks(network),
            "series": list(self.day if span == "24h" else self.hour),
            "step": 60 if span == "24h" else SAMPLE_SECONDS,
        }

    def checks(self, network: dict[str, Any]) -> list[dict[str, Any]]:
        ctx = self.ctx
        out: list[dict[str, Any]] = []

        def add(name: str, status: str, detail: str) -> None:
            out.append({"name": name, "status": status, "detail": detail})

        for label, path in (
            ("Library disk", ctx.config.data_dir),
            ("State disk", ctx.config.state_dir),
        ):
            try:
                free = shutil.disk_usage(path).free
            except OSError as exc:
                add(label, "fail", f"Cannot be read: {exc}")
                continue
            status = "fail" if free < LOW_DISK_FAIL else "warn" if free < LOW_DISK_WARN else "ok"
            add(label, status, f"{free / 1024**3:.1f} GB free")
        try:
            ctx.db.scalar("SELECT 1")
            add("Database", "ok", "Answers")
        except Exception as exc:
            add("Database", "fail", str(exc))
        lag = max([p["lag_ms"] for p in list(self.hour)[-6:]] or [metrics.lag_now * 1000])
        add(
            "Responsiveness",
            "fail" if lag > 2000 else "warn" if lag > 500 else "ok",
            f"The server was up to {lag:.0f} ms late in the last minute",
        )
        memory = psutil.virtual_memory().percent
        add(
            "Memory",
            "fail" if memory > 95 else "warn" if memory > 85 else "ok",
            f"{memory:.0f}% of the machine's memory in use",
        )
        broker = ctx.broker.info()
        if broker["kind"] == "redis":
            add(
                "Redis",
                "ok" if broker["ok"] else "fail",
                f"Version {broker.get('version')}, {broker.get('latency_ms')} ms"
                if broker["ok"]
                else broker.get("error", ""),
            )
        else:
            add("Redis", "off", "Not set up: the queues are kept in the database")
        vpn, solver = network.get("vpn") or {}, network.get("solver") or {}
        if not vpn.get("proxy_set"):
            add("VPN", "off", "No proxy set")
        elif not vpn.get("enabled"):
            add("VPN", "off", "Switched off")
        elif vpn.get("error"):
            add("VPN", "fail", vpn["error"])
        else:
            where = ", ".join(x for x in (vpn.get("public_ip"), vpn.get("country")) if x)
            add("VPN", "ok", where or "Connected")
        if not solver.get("set"):
            add("Captcha solver", "off", "Not set")
        elif not solver.get("enabled"):
            add("Captcha solver", "off", "Switched off")
        else:
            add(
                "Captcha solver",
                "ok" if solver.get("ready") else "fail",
                solver.get("error") or f"Ready, version {solver.get('version', '')}",
            )
        catalog_error = ctx.db.get_meta("catalog_error")
        refreshed = ctx.db.get_meta("catalog_refreshed")
        add(
            "Catalog",
            "warn" if catalog_error else "ok",
            catalog_error or (f"Refreshed {_ago(refreshed)}" if refreshed else "Not loaded yet"),
        )
        stuck = sorted(self.stalled)
        add(
            "Jobs",
            "warn" if stuck else "ok",
            f"Not moving: {', '.join(stuck)}" if stuck else "Every running job is moving",
        )
        failed = (
            ctx.db.scalar(
                "SELECT COUNT(*) FROM builds "
                "WHERE state = 'failed' AND finished > unixepoch() - 86400"
            )
            or 0
        )
        failed += (
            ctx.db.scalar(
                "SELECT COUNT(*) FROM downloads "
                "WHERE state = 'failed' AND updated > unixepoch() - 86400"
            )
            or 0
        )
        add(
            "Failures",
            "warn" if failed else "ok",
            f"{failed} job{'s' if failed != 1 else ''} failed in the last day",
        )
        errors = hub.summary(time.time() - 3600)["levels"]
        bad = errors.get("ERROR", 0) + errors.get("CRITICAL", 0)
        add(
            "Errors",
            "warn" if bad else "ok",
            f"{bad} error{'s' if bad != 1 else ''} logged in the last hour",
        )
        return out


def _merge(points: list[dict[str, Any]]) -> dict[str, Any]:
    """A minute of samples as one: counts added up, levels averaged, the worst lag kept."""
    summed = (
        "requests",
        "request_errors",
        "fetches",
        "fetch_errors",
        "fetch_bytes",
        "errors",
        "warnings",
    )
    merged: dict[str, Any] = {"t": points[-1]["t"]}
    for key in points[0]:
        if key == "t":
            continue
        values = [p[key] for p in points]
        if key in summed:
            merged[key] = sum(values)
        elif key == "lag_ms":
            merged[key] = max(values)
        else:
            merged[key] = round(sum(values) / len(values), 1)
    return merged


def _ago(when: float) -> str:
    minutes = (time.time() - float(when)) / 60
    if minutes < 90:
        return f"{minutes:.0f} min ago"
    return f"{minutes / 60:.0f} h ago"


def _version() -> str:
    try:
        return metadata.version("zim-pro")
    except metadata.PackageNotFoundError:
        return "dev"
