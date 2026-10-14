"""Where the queues of downloads, builds and feed syncs wait: Redis, or the database.

The database always keeps the record of every job (its state, progress and result). The
broker keeps the order of the jobs waiting to start, and whether a queue is paused:

* :class:`LocalBroker` reads both from the database. Nothing else is needed.
* :class:`RedisBroker` keeps them in Redis (``KIWIX_REDIS_URL``): a sorted set per queue
  (``zimpro:queue:<name>``, scored by place) and a ``zimpro:queue:<name>:paused`` key, so the
  queues can be watched and changed from outside as well. Each look at the queue settles it
  with the database: jobs missing from Redis are added, jobs that are no longer waiting are
  taken out. While Redis cannot be reached, the database order is used, and Redis is filled
  again from it when it is back. Log lines also go to the ``zimpro:logs`` stream.
"""

import logging
import time
from typing import Any

import redis

from .db import Database

log = logging.getLogger(__name__)

PREFIX = "zimpro"
LOG_STREAM = f"{PREFIX}:logs"
LOG_STREAM_KEEP = 20000
RETRY_SECONDS = 15  # after a failure, Redis is left alone this long


class LocalBroker:
    """The queues as the database has them."""

    kind = "database"

    def __init__(self, db: Database):
        self.db = db

    def waiting(self, queue: str, jobs: list[tuple[int, float]]) -> list[tuple[int, float]]:
        """The jobs waiting in ``queue``, in the order they start; ``jobs`` is the database's."""
        return sorted(jobs, key=lambda job: (job[1], job[0]))

    def place(self, queue: str, job_id: int, score: float) -> None:
        """Give a waiting job another place (the database's own copy is set by the caller)."""

    def paused(self, queue: str) -> bool:
        return bool(self.db.get_meta(f"queue_paused_{queue}"))

    def set_paused(self, queue: str, paused: bool) -> None:
        self.db.set_meta(f"queue_paused_{queue}", paused)

    def send_logs(self, lines: list[dict[str, Any]]) -> None:
        pass

    def info(self) -> dict[str, Any]:
        return {"kind": self.kind, "ok": True}

    def close(self) -> None:
        pass


class RedisBroker(LocalBroker):
    """The queues in Redis, settled with the database on every look."""

    kind = "redis"

    def __init__(self, db: Database, url: str, client: Any = None):
        super().__init__(db)
        self.url = url
        self.client = client or redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=2,
            socket_connect_timeout=2,
            health_check_interval=30,
        )
        self.error = ""
        self._down_until = 0.0
        self.calls = self.failures = 0

    # -- reaching Redis ----------------------------------------------------------------

    def _usable(self) -> bool:
        return time.monotonic() >= self._down_until

    def _failed(self, exc: Exception) -> None:
        self.failures += 1
        if not self.error:
            log.warning(
                "Redis at %s cannot be reached (%s); the queues use the database until it is back",
                _safe(self.url),
                exc,
            )
        self.error = str(exc) or type(exc).__name__
        self._down_until = time.monotonic() + RETRY_SECONDS

    def _worked(self) -> None:
        self.calls += 1
        if self.error:
            log.info("Redis is back; the queues are kept there again")
            self.error = ""

    @staticmethod
    def key(queue: str) -> str:
        return f"{PREFIX}:queue:{queue}"

    # -- the queues --------------------------------------------------------------------

    def waiting(self, queue: str, jobs: list[tuple[int, float]]) -> list[tuple[int, float]]:
        if not self._usable():
            return super().waiting(queue, jobs)
        key = self.key(queue)
        try:
            held = {
                int(member): score
                for member, score in self.client.zrange(key, 0, -1, withscores=True)
            }
            wanted = {job_id for job_id, _ in jobs}
            pipe = self.client.pipeline()
            missing = {str(job_id): score for job_id, score in jobs if job_id not in held}
            if missing:
                pipe.zadd(key, missing, nx=True)
            extra = [str(job_id) for job_id in held if job_id not in wanted]
            if extra:
                pipe.zrem(key, *extra)
            if missing or extra:
                pipe.execute()
            self._worked()
        except redis.RedisError as exc:
            self._failed(exc)
            return super().waiting(queue, jobs)
        # Redis has the last word on the order: it may have been changed from outside.
        scored = [(job_id, held.get(job_id, score)) for job_id, score in jobs]
        return sorted(scored, key=lambda job: (job[1], job[0]))

    def place(self, queue: str, job_id: int, score: float) -> None:
        if not self._usable():
            return
        try:
            self.client.zadd(self.key(queue), {str(job_id): score})
            self._worked()
        except redis.RedisError as exc:
            self._failed(exc)

    def paused(self, queue: str) -> bool:
        kept = super().paused(queue)
        if not self._usable():
            return kept
        key = self.key(queue) + ":paused"
        try:
            flag = self.client.get(key)
            if flag is None:  # a Redis that lost its data: give it the database's word
                self.client.set(key, "1" if kept else "0")
            self._worked()
        except redis.RedisError as exc:
            self._failed(exc)
            return kept
        if flag is None:
            return kept
        if (flag == "1") != kept:  # changed in Redis: the database follows
            super().set_paused(queue, flag == "1")
        return flag == "1"

    def set_paused(self, queue: str, paused: bool) -> None:
        super().set_paused(queue, paused)  # kept in the database too, for when Redis is not
        if self._usable():
            try:
                self.client.set(self.key(queue) + ":paused", "1" if paused else "0")
                self._worked()
            except redis.RedisError as exc:
                self._failed(exc)

    def send_logs(self, lines: list[dict[str, Any]]) -> None:
        if not lines or not self._usable():
            return
        try:
            pipe = self.client.pipeline(transaction=False)
            for line in lines:
                fields = {k: str(v) for k, v in line.items() if k != "id"}
                pipe.xadd(LOG_STREAM, fields, maxlen=LOG_STREAM_KEEP, approximate=True)
            pipe.execute()
            self._worked()
        except redis.RedisError as exc:
            self._failed(exc)

    def info(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "url": _safe(self.url),
            "ok": False,
            "error": self.error,
            "calls": self.calls,
            "failures": self.failures,
        }
        try:
            started = time.perf_counter()
            self.client.ping()
            out["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
            try:
                server = self.client.info()
            except redis.ResponseError:  # some hosted Redis services do not allow INFO
                server = {}
            out.update(
                ok=True,
                error="",
                version=server.get("redis_version", ""),
                uptime=server.get("uptime_in_seconds", 0),
                memory=server.get("used_memory", 0),
                peak_memory=server.get("used_memory_peak", 0),
                clients=server.get("connected_clients", 0),
                ops_per_second=server.get("instantaneous_ops_per_sec", 0),
                keys=sum(1 for _ in self.client.scan_iter(f"{PREFIX}:*", count=500)),
                log_lines=self.client.xlen(LOG_STREAM),
            )
            self._worked()
        except redis.RedisError as exc:
            self._failed(exc)
            out["error"] = self.error
        return out

    def close(self) -> None:
        try:
            self.client.close()
        except redis.RedisError:
            pass


def _safe(url: str) -> str:
    """The address without its password."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    return f"{scheme}://•••@{rest.rpartition('@')[2]}"


def new_place(order: list[tuple[int, float]], job_id: int, where: str) -> float | None:
    """The score that puts ``job_id`` at ``where`` (top, up, down, bottom) in ``order``."""
    ids = [item for item, _ in order]
    if job_id not in ids:
        return None
    index = ids.index(job_id)
    scores = [score for _, score in order]
    if where == "top":
        return scores[0] - 1 if index else None
    if where == "bottom":
        return scores[-1] + 1 if index < len(ids) - 1 else None
    if where == "up" and index > 0:
        before = scores[index - 2] if index > 1 else scores[index - 1] - 2
        return (before + scores[index - 1]) / 2
    if where == "down" and index < len(ids) - 1:
        after = scores[index + 2] if index + 2 < len(ids) else scores[index + 1] + 2
        return (scores[index + 1] + after) / 2
    return None


def make_broker(db: Database, url: str | None) -> LocalBroker:
    if url:
        log.info("queues are kept in Redis at %s", _safe(url))
        return RedisBroker(db, url)
    return LocalBroker(db)
