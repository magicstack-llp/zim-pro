"""Segmented, resumable, piece-verified HTTP downloader.

The file is split on the piece boundaries given by its Metalink. Several
connections pull runs of pieces with ``Range`` requests, spread over the listed
mirrors. Every piece is SHA-1 checked before it counts as done, and the set of
finished pieces is persisted, so an interrupted download resumes where it
stopped without re-fetching or trusting unverified data.
"""

import asyncio
import errno
import hashlib
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from .metalink import Metalink

log = logging.getLogger(__name__)

SEGMENT_BYTES = 64 * 1024 * 1024
CHUNK_BYTES = 256 * 1024
MIRROR_MAX_FAILURES = 4
MAX_STALLED_ROUNDS = 6
MIN_SPEED = 20 * 1024  # bytes/s floor before a mirror is considered stalled
MIN_SPEED_GRACE = 15.0  # seconds before enforcing MIN_SPEED
SEGMENT_DEADLINE_BASE = 120.0
USER_AGENT = "zim-pro/0.1"
_CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")


class DownloadError(Exception):
    pass


class DiskError(DownloadError):
    """Local disk or permission failure that should abort immediately, not penalise mirrors."""


class RateLimiter:
    """Token bucket shared by every connection; a rate of 0 means unlimited."""

    def __init__(self, bytes_per_second: int = 0):
        self.rate = bytes_per_second
        self._allowance = float(bytes_per_second)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def take(self, amount: int) -> None:
        if self.rate <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            self._allowance = min(self.rate, self._allowance + (now - self._last) * self.rate)
            self._last = now
            self._allowance -= amount
            delay = -self._allowance / self.rate if self._allowance < 0 else 0.0
        if delay > 0:
            await asyncio.sleep(delay)


class Bitmap:
    """Which pieces are complete, stored one bit per piece."""

    def __init__(self, count: int, data: bytes | None = None):
        self.count = count
        size = (count + 7) // 8
        self._bits = bytearray(data) if data and len(data) == size else bytearray(size)

    def __getitem__(self, index: int) -> bool:
        return bool(self._bits[index >> 3] & (1 << (index & 7)))

    def set(self, index: int, value: bool = True) -> None:
        if value:
            self._bits[index >> 3] |= 1 << (index & 7)
        else:
            self._bits[index >> 3] &= ~(1 << (index & 7))

    def missing(self) -> list[int]:
        return [i for i in range(self.count) if not self[i]]

    def done_count(self) -> int:
        return sum(1 for i in range(self.count) if self[i])

    def to_bytes(self) -> bytes:
        return bytes(self._bits)


class _Mirror:
    def __init__(self, url: str):
        self.url = url
        self.failures = 0
        self.active = 0


class Download:
    """One file being fetched into ``<target>.part``."""

    def __init__(
        self,
        metalink: Metalink,
        target: Path,
        *,
        pieces: bytes | None = None,
        connections: int = 4,
        limiter: RateLimiter | None = None,
        on_progress: Callable[["Download"], None] | None = None,
        client: httpx.AsyncClient | None = None,
        proxy: str | None = None,
    ):
        self.metalink = metalink
        self.target = target
        self.part = target.with_name(target.name + ".part")
        self.connections = max(1, connections)
        self.limiter = limiter or RateLimiter()
        self.on_progress = on_progress
        self.bitmap = Bitmap(metalink.piece_count, pieces)
        self.mirrors = [_Mirror(url) for url in metalink.urls]
        self.bytes_done = 0
        self.active_connections = 0
        self._client = client
        self._proxy = proxy or None
        self._fd = -1
        self._fd_lock = threading.Lock()
        self._segments: asyncio.Queue[list[int]] = asyncio.Queue()

    # -- public -----------------------------------------------------------

    async def run(self) -> None:
        """Download every missing piece, then verify the whole file.

        Raises ``DownloadError`` when the mirrors cannot supply a valid file.
        Cancelling the task stops cleanly; ``bitmap`` holds the progress.
        """
        if not self.metalink.sha256:
            raise DownloadError("metalink has no SHA-256 checksum")
        if not self.part.exists():
            self.bitmap = Bitmap(self.metalink.piece_count)
        self._fd = os.open(self.part, os.O_RDWR | os.O_CREAT, 0o644)
        own_client = self._client is None
        if own_client:
            self._client = httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(30, read=60),
                headers={"User-Agent": USER_AGENT},
                proxy=self._proxy,
            )
        try:
            if os.fstat(self._fd).st_size != self.metalink.size:
                os.ftruncate(self._fd, self.metalink.size)
            if self.bitmap.done_count() > 0:
                if self.metalink.piece_hashes:
                    await asyncio.to_thread(self._recheck_pieces, True)
                else:
                    self.bitmap = Bitmap(self.metalink.piece_count)
            self._recount()
            for attempt in range(2):
                await self._fetch_missing()
                await asyncio.to_thread(os.fsync, self._fd)
                if await asyncio.to_thread(self._verify_file):
                    return
                if attempt == 0:
                    log.warning("%s failed its checksum; re-checking pieces", self.target.name)
                    await asyncio.to_thread(self._recheck_pieces, False)
            raise DownloadError("the file does not match its published SHA-256 checksum")
        finally:
            with self._fd_lock:
                fd, self._fd = self._fd, -1
                if fd >= 0:
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
            if own_client and self._client is not None:
                await self._client.aclose()
                self._client = None

    def sync(self) -> None:
        """Flush written pieces to disk; call before persisting ``bitmap``."""
        with self._fd_lock:
            if self._fd >= 0:
                os.fsync(self._fd)

    # -- internals --------------------------------------------------------

    def _recount(self) -> None:
        self.bytes_done = sum(
            self.metalink.piece_range(i)[1] for i in range(self.bitmap.count) if self.bitmap[i]
        )

    def _notify(self) -> None:
        if self.on_progress:
            self.on_progress(self)

    def _plan_segments(self) -> int:
        """Queue the missing pieces as contiguous runs of up to SEGMENT_BYTES."""
        # Small enough that every connection has work, capped at SEGMENT_BYTES per request.
        missing = self.bitmap.missing()
        per_segment = max(
            1,
            min(
                SEGMENT_BYTES // self.metalink.piece_length,
                -(-len(missing) // (self.connections * 2)),
            ),
        )
        self._segments = asyncio.Queue()
        run: list[int] = []
        queued = 0
        for index in missing:
            if run and (index != run[-1] + 1 or len(run) >= per_segment):
                self._segments.put_nowait(run)
                run = []
            run.append(index)
            queued += 1
        if run:
            self._segments.put_nowait(run)
        return queued

    async def _fetch_missing(self) -> None:
        stalled = 0
        while True:
            before = self.bitmap.done_count()
            if not self._plan_segments():
                return
            workers = [asyncio.create_task(self._worker()) for _ in range(self.connections)]
            try:
                await asyncio.gather(*workers)
            except BaseException:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                raise
            if not self.bitmap.missing():
                return
            if not any(m.failures < MIRROR_MAX_FAILURES for m in self.mirrors):
                raise DownloadError("every mirror failed repeatedly")
            stalled = 0 if self.bitmap.done_count() > before else stalled + 1
            if stalled >= MAX_STALLED_ROUNDS:
                raise DownloadError("no progress after repeated retries")
            if stalled > 0:
                await asyncio.sleep(min(60, 2**stalled))

    def _pick_mirror(self) -> _Mirror | None:
        usable = [m for m in self.mirrors if m.failures < MIRROR_MAX_FAILURES]
        if not usable:
            return None
        # Spread connections across mirrors, preferring the healthiest then the listed order.
        return min(usable, key=lambda m: (m.active, m.failures))

    async def _worker(self) -> None:
        while True:
            try:
                segment = self._segments.get_nowait()
            except asyncio.QueueEmpty:
                return
            mirror = self._pick_mirror()
            if mirror is None:
                return
            mirror.active += 1
            self.active_connections += 1
            try:
                await self._fetch_segment(mirror, segment)
                mirror.failures = max(0, mirror.failures - 1)
            except DiskError:
                raise
            except (httpx.HTTPError, DownloadError, OSError) as exc:
                mirror.failures += 1
                log.info("%s: %s from %s", self.target.name, exc or type(exc).__name__, mirror.url)
                await asyncio.sleep(min(10, mirror.failures))
            finally:
                mirror.active -= 1
                self.active_connections -= 1

    def _effective_min_speed(self) -> float:
        if self.limiter.rate > 0:
            return min(MIN_SPEED, max(1024.0, self.limiter.rate * 0.5))
        return float(MIN_SPEED)

    def _validate_range_response(self, response: httpx.Response, start: int, end: int) -> None:
        expected_len = end - start + 1
        if response.status_code == 416:
            raise DownloadError(f"HTTP 416 (range {start}-{end} not satisfiable)")
        whole_file = response.status_code == 200 and start == 0 and end == self.metalink.size - 1
        if response.status_code != 206 and not whole_file:
            raise DownloadError(f"HTTP {response.status_code}")
        if response.status_code == 206:
            cr = response.headers.get("content-range", "")
            m = _CONTENT_RANGE_RE.match(cr.strip())
            if not m:
                raise DownloadError(f"invalid Content-Range: {cr!r}")
            r_start, r_end, r_total = int(m.group(1)), int(m.group(2)), m.group(3)
            if r_start != start or r_end != end or r_total not in ("*", str(self.metalink.size)):
                raise DownloadError(
                    f"mismatched Content-Range {cr!r} for {start}-{end}/{self.metalink.size}"
                )
        cl = response.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) != expected_len:
                    raise DownloadError(f"unexpected Content-Length {cl} (expected {expected_len})")
            except ValueError as exc:
                raise DownloadError(f"invalid Content-Length: {cl!r}") from exc

    async def _fetch_segment(self, mirror: _Mirror, segment: list[int]) -> None:
        assert self._client is not None
        start, _ = self.metalink.piece_range(segment[0])
        last_start, last_length = self.metalink.piece_range(segment[-1])
        end = last_start + last_length - 1
        headers = {"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"}
        min_speed = self._effective_min_speed()
        budget = max(SEGMENT_DEADLINE_BASE, (end - start + 1) / min_speed + 60.0)
        try:
            async with (
                asyncio.timeout(budget),
                self._client.stream("GET", mirror.url, headers=headers) as response,
            ):
                self._validate_range_response(response, start, end)
                position = 0
                index, (offset, length) = segment[0], self.metalink.piece_range(segment[0])
                buffer = bytearray()
                started_at = time.monotonic()
                received = 0
                async for chunk in response.aiter_bytes(CHUNK_BYTES):
                    await self.limiter.take(len(chunk))
                    received += len(chunk)
                    elapsed = time.monotonic() - started_at
                    if elapsed > MIN_SPEED_GRACE and received / elapsed < min_speed:
                        raise DownloadError("mirror too slow")
                    buffer += chunk
                    while len(buffer) >= length:
                        await self._store_piece(index, offset, bytes(buffer[:length]))
                        del buffer[:length]
                        position += 1
                        if position == len(segment):
                            return
                        index = segment[position]
                        offset, length = self.metalink.piece_range(index)
                raise DownloadError("connection closed early")
        except TimeoutError as exc:
            raise DownloadError("segment timed out") from exc

    async def _store_piece(self, index: int, offset: int, data: bytes) -> None:
        hashes = self.metalink.piece_hashes
        if hashes:
            digest = await asyncio.to_thread(lambda: hashlib.sha1(data).hexdigest())
            if digest != hashes[index]:
                raise DownloadError(f"piece {index} is corrupt")
        try:
            await asyncio.to_thread(os.pwrite, self._fd, data, offset)
        except OSError as exc:
            if exc.errno in (errno.ENOSPC, errno.EDQUOT, errno.EACCES, errno.EPERM, errno.EROFS):
                raise DiskError(f"disk error: {exc.strerror or exc}") from exc
            raise
        if not self.bitmap[index]:
            self.bitmap.set(index)
            self.bytes_done += len(data)
        self._notify()

    def _verify_file(self) -> bool:
        if not self.metalink.sha256:
            return False
        digest = hashlib.sha256()
        offset = 0
        while offset < self.metalink.size:
            block = os.pread(self._fd, 4 * 1024 * 1024, offset)
            if not block:
                return False
            digest.update(block)
            offset += len(block)
        return digest.hexdigest() == self.metalink.sha256

    def _recheck_pieces(self, only_done: bool = False) -> None:
        """Re-hash pieces on disk and clear the ones that are wrong."""
        hashes = self.metalink.piece_hashes
        for index in range(self.bitmap.count):
            if only_done and not self.bitmap[index]:
                continue
            if not hashes:
                self.bitmap.set(index, False)
                continue
            offset, length = self.metalink.piece_range(index)
            data = os.pread(self._fd, length, offset)
            self.bitmap.set(
                index, len(data) == length and hashlib.sha1(data).hexdigest() == hashes[index]
            )
        self._recount()
