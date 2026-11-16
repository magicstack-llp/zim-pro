import asyncio
import contextlib
import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from libzim.reader import Archive

from .. import logs
from ..catalog.updates import compare_release_dates
from ..db import Database
from ..events import EventBus
from ..library.archive_pool import ArchivePool
from ..library.scanner import Scanner
from ..queues import LocalBroker, new_place
from . import metalink
from .engine import USER_AGENT, Download, DownloadError, RateLimiter

log = logging.getLogger(__name__)

QUEUE = "downloads"
LOOK_AGAIN = 5  # seconds between looks at the queue, for changes made from outside
PERSIST_SECONDS = 5
PROGRESS_SECONDS = 1
FREE_SPACE_MARGIN = 1024**3
PUBLIC_COLUMNS = (
    "id, catalog_uuid, book_id, title, filename, size, bytes_done, state, replaces, "
    "from_date, to_date, error, position, created, updated"
)


class _Active:
    def __init__(self, task: asyncio.Task[None]):
        self.task = task
        self.download: Download | None = None
        self.stop_state = "paused"
        self.installing = False
        self.speed = 0.0
        self.last_bytes = 0
        self.last_time = time.monotonic()
        self.last_persist = 0.0


class DownloadManager:
    """Owns the download queue: starts, pauses, resumes and finalises downloads."""

    def __init__(
        self,
        db: Database,
        events: EventBus,
        scanner: Scanner,
        pool: ArchivePool,
        data_dir: Path,
        broker: LocalBroker | None = None,
        proxy: str | None = None,
    ):
        self.db = db
        self.broker = broker or LocalBroker(db)
        self.events = events
        self.scanner = scanner
        self.pool = pool
        self.data_dir = data_dir
        self.proxy = proxy or None
        self.network: Any = None
        self.limiter = RateLimiter()
        self._active: dict[int, _Active] = {}
        self._active_targets: dict[str, int] = {}
        self._wake = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._ticker: asyncio.Task[None] | None = None

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        # Anything that was running when the process stopped goes back in the queue.
        self.db.execute(
            "UPDATE downloads SET state = 'queued' WHERE state IN ('downloading', 'verifying')"
        )
        self._runner = asyncio.create_task(self._run_queue())
        self._ticker = asyncio.create_task(self._tick())

    async def stop(self) -> None:
        for task in (self._runner, self._ticker):
            if task:
                task.cancel()
        for active in list(self._active.values()):
            active.stop_state = "queued"
            active.task.cancel()
        tasks = [a.task for a in self._active.values()]
        await asyncio.gather(*tasks, return_exceptions=True)

    # -- queue operations -------------------------------------------------

    def list(self) -> list[dict[str, Any]]:
        rows = self.db.all(
            f"SELECT {PUBLIC_COLUMNS} FROM downloads "
            "ORDER BY CASE state WHEN 'downloading' THEN 0 WHEN 'verifying' THEN 0 "
            "WHEN 'queued' THEN 1 WHEN 'paused' THEN 2 WHEN 'failed' THEN 3 ELSE 4 END, "
            "position, id ASC LIMIT 500"
        )
        return [self._decorate(row) for row in rows]

    def get(self, download_id: int) -> dict[str, Any] | None:
        row = self.db.one(f"SELECT {PUBLIC_COLUMNS} FROM downloads WHERE id = ?", (download_id,))
        return self._decorate(row) if row else None

    def _decorate(self, row: dict[str, Any]) -> dict[str, Any]:
        active = self._active.get(row["id"])
        row["speed"] = 0.0
        row["connections"] = 0
        if active and active.download:
            row["bytes_done"] = active.download.bytes_done
            row["speed"] = active.speed
            row["connections"] = active.download.active_connections
        remaining = row["size"] - row["bytes_done"]
        row["eta"] = remaining / row["speed"] if row["speed"] > 0 else None
        return row

    def enqueue(self, catalog_uuid: str) -> dict[str, Any]:
        """Queue the catalog entry for download, replacing the local copy if any."""
        try:
            with self.db.transaction():
                entry = self.db.one("SELECT * FROM catalog WHERE uuid = ?", (catalog_uuid,))
                if entry is None:
                    raise LookupError("unknown catalog entry")
                existing = self.db.one(
                    "SELECT id FROM downloads WHERE catalog_uuid = ? "
                    "AND state IN ('queued', 'downloading', 'verifying', 'paused', 'failed')",
                    (catalog_uuid,),
                )
                if existing:
                    self.db.execute(
                        "UPDATE downloads SET state = 'queued', error = NULL, finished = NULL, "
                        "updated = unixepoch('subsec') "
                        "WHERE id = ? AND state IN ('paused', 'failed')",
                        (existing["id"],),
                    )
                    row_id = existing["id"]
                else:
                    book = self.db.one(
                        "SELECT * FROM books WHERE id = ? OR (name = ? AND flavour = ?)",
                        (entry["stem"], entry["name"], entry["flavour"]),
                    )
                    if book:
                        if book["uuid"] == entry["uuid"]:
                            raise ValueError("this release is already in the library")
                        if (
                            book["date"]
                            and entry["issued"]
                            and compare_release_dates(entry["issued"], book["date"]) < 0
                        ):
                            raise ValueError("a newer release is already in the library")
                    book_id = book["id"] if book else entry["stem"]
                    url_name = os.path.basename(urlsplit(entry["url"]).path).removesuffix(".meta4")
                    if book_id:
                        conflict = self.db.one(
                            "SELECT id FROM downloads WHERE book_id = ? "
                            "AND state IN ('queued', 'downloading', 'verifying', 'paused')",
                            (book_id,),
                        )
                        if conflict:
                            raise ValueError("a download for this book is already active")
                    if url_name:
                        file_conflict = self.db.one(
                            "SELECT id FROM downloads WHERE filename = ? "
                            "AND state IN ('queued', 'downloading', 'verifying', 'paused')",
                            (url_name,),
                        )
                        if file_conflict:
                            raise ValueError(f"a download for {url_name} is already active")
                    position = (self.db.scalar("SELECT MAX(position) FROM downloads") or 0) + 1
                    cursor = self.db.execute(
                        "INSERT INTO downloads (catalog_uuid, book_id, title, url, size, replaces, "
                        "from_date, to_date, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            entry["uuid"],
                            book_id,
                            entry["title"],
                            entry["url"],
                            entry["size"],
                            book["path"] if book else None,
                            book["date"] if book else "",
                            entry["issued"][:10],
                            position,
                        ),
                    )
                    row_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            existing = self.db.one(
                "SELECT id FROM downloads WHERE catalog_uuid = ? "
                "AND state IN ('queued', 'downloading', 'verifying', 'paused')",
                (catalog_uuid,),
            )
            if existing is None:
                raise
            row_id = existing["id"]
        self._changed()
        return self.get(row_id)  # type: ignore[arg-type,return-value]

    def pause(self, download_id: int) -> None:
        active = self._active.get(download_id)
        if active:
            if active.installing:
                return
            active.stop_state = "paused"
            active.task.cancel()
        else:
            self.db.execute(
                "UPDATE downloads SET state = 'paused', updated = unixepoch('subsec') "
                "WHERE id = ? AND state = 'queued'",
                (download_id,),
            )
            self._changed()

    def resume(self, download_id: int) -> None:
        active = self._active.get(download_id)
        if active and active.stop_state == "paused":
            active.stop_state = "queued"
        self.db.execute(
            "UPDATE downloads SET state = 'queued', error = NULL, finished = NULL, "
            "updated = unixepoch('subsec') WHERE id = ? AND state IN ('paused', 'failed')",
            (download_id,),
        )
        self._changed()

    def cancel(self, download_id: int) -> None:
        active = self._active.get(download_id)
        if active:
            if active.installing:
                return
            active.stop_state = "cancelled"
            active.task.cancel()
            return
        row = self.db.one("SELECT filename, state FROM downloads WHERE id = ?", (download_id,))
        if row and row["state"] in ("queued", "paused", "failed"):
            self._set_state(download_id, "cancelled")
            self._remove_part(row["filename"])
            self._changed()

    async def remove(self, download_id: int) -> str:
        """Forget a download, cancelling it first if active.

        Returns ``'deleted'``, ``'not_found'``, or ``'conflict'``.
        """
        row = self.db.one("SELECT id, filename, state FROM downloads WHERE id = ?", (download_id,))
        if row is None:
            return "not_found"
        active = self._active.get(download_id)
        if active:
            if active.installing:
                return "conflict"
            active.stop_state = "cancelled"
            active.task.cancel()
            await asyncio.gather(active.task, return_exceptions=True)
        elif row["state"] in ("queued", "paused", "downloading", "verifying"):
            self._set_state(download_id, "cancelled")
            self._remove_part(row["filename"])
        cursor = self.db.execute(
            "DELETE FROM downloads WHERE id = ? AND state IN ('done', 'cancelled', 'failed')",
            (download_id,),
        )
        if cursor.rowcount == 0:
            return "conflict"
        self._changed()
        return "deleted"

    def clear_finished(self) -> None:
        self.db.execute("DELETE FROM downloads WHERE state IN ('done', 'cancelled')")
        self._changed()

    def move(self, download_id: int, to_top: bool) -> None:
        self.place(download_id, "top" if to_top else "bottom")

    def waiting(self) -> "list[tuple[int, float]]":
        """The downloads waiting to start, in the order they will."""
        rows = self.db.all("SELECT id, position FROM downloads WHERE state = 'queued'")
        return self.broker.waiting(QUEUE, [(row["id"], row["position"]) for row in rows])

    def place(self, download_id: int, where: str) -> None:
        """Move a waiting download to the top or bottom, or one place up or down."""
        score = new_place(self.waiting(), download_id, where)
        if score is None:
            return
        self.db.execute("UPDATE downloads SET position = ? WHERE id = ?", (score, download_id))
        self.broker.place(QUEUE, download_id, score)
        self._changed()

    def retry_failed(self) -> int:
        cursor = self.db.execute(
            "UPDATE downloads SET state = 'queued', error = NULL, finished = NULL, "
            "updated = unixepoch('subsec') WHERE state = 'failed'"
        )
        if cursor.rowcount:
            self._changed()
        return cursor.rowcount

    def cancel_waiting(self) -> int:
        rows = self.db.all("SELECT id, filename FROM downloads WHERE state IN ('queued', 'paused')")
        if not rows:
            return 0
        with self.db.transaction():
            self.db.execute(
                "UPDATE downloads SET state = 'cancelled', finished = unixepoch('subsec'), "
                "updated = unixepoch('subsec') WHERE state IN ('queued', 'paused')"
            )
        for row in rows:
            self._remove_part(row["filename"])
        self._changed()
        return len(rows)

    # -- internals --------------------------------------------------------

    def _changed(self) -> None:
        self._wake.set()
        self.events.publish("downloads", self.list())

    def _set_state(self, download_id: int, state: str, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE downloads SET state = ?, error = ?, updated = unixepoch('subsec'), "
            "started = CASE WHEN ? = 'downloading' THEN unixepoch('subsec') ELSE started END, "
            "finished = CASE WHEN ? IN ('done', 'failed', 'cancelled') THEN unixepoch('subsec') "
            "ELSE finished END WHERE id = ?",
            (state, error, state, state, download_id),
        )

    def _remove_part(self, filename: str) -> None:
        if filename:
            (self.data_dir / (filename + ".part")).unlink(missing_ok=True)

    async def _run_queue(self) -> None:
        while True:
            try:
                self._wake.clear()
                settings = self.db.get_settings()
                self.limiter.rate = int(settings["bandwidth_limit_kbps"]) * 1024
                slots = int(settings["max_concurrent_downloads"]) - len(self._active)
                started = False
                if slots > 0 and not await asyncio.to_thread(self.broker.paused, QUEUE):
                    for download_id, _ in (await asyncio.to_thread(self.waiting))[:slots]:
                        if download_id not in self._active:
                            cursor = self.db.execute(
                                "UPDATE downloads SET state = 'downloading', error = NULL, "
                                "finished = NULL, started = unixepoch('subsec'), "
                                "updated = unixepoch('subsec') WHERE id = ? AND state = 'queued'",
                                (download_id,),
                            )
                            if cursor.rowcount == 1:
                                task = asyncio.create_task(self._run_one(download_id))
                                self._active[download_id] = _Active(task)
                                started = True
                if started:
                    self.events.publish("downloads", self.list())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("download queue iteration failed")
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(LOOK_AGAIN):
                    await self._wake.wait()

    async def _tick(self) -> None:
        """Publish live progress and persist it periodically."""
        while True:
            await asyncio.sleep(PROGRESS_SECONDS)
            try:
                if not self._active:
                    continue
                now = time.monotonic()
                for download_id, active in list(self._active.items()):
                    download = active.download
                    if download is None:
                        continue
                    elapsed = now - active.last_time
                    rate = (
                        max(0, download.bytes_done - active.last_bytes) / elapsed
                        if elapsed > 0
                        else 0
                    )
                    active.speed = rate if active.speed == 0 else 0.7 * active.speed + 0.3 * rate
                    if active.speed < 1:
                        active.speed = 0.0  # the average decays forever; call a stall a stall
                    active.last_bytes, active.last_time = download.bytes_done, now
                    if now - active.last_persist >= PERSIST_SECONDS:
                        active.last_persist = now
                        await self._persist(download_id, download)
                self.events.publish("downloads", self.list())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("download ticker iteration failed")

    async def _persist(self, download_id: int, download: Download) -> None:
        # Snapshot first, then fsync: every piece in the snapshot is on disk once it is saved.
        pieces, done = download.bitmap.to_bytes(), download.bytes_done
        await asyncio.to_thread(download.sync)
        self.db.execute(
            "UPDATE downloads SET pieces = ?, bytes_done = ?, updated = unixepoch('subsec') "
            "WHERE id = ?",
            (pieces, done, download_id),
        )

    async def _run_one(self, download_id: int) -> None:
        with logs.job(f"download {download_id}"):
            await self._download(download_id)

    async def _download(self, download_id: int) -> None:
        active = self._active[download_id]
        row = self.db.one("SELECT * FROM downloads WHERE id = ?", (download_id,))
        assert row is not None
        download: Download | None = None
        filename = ""
        log.info("downloading %s", row["title"])
        installed = False
        link_size = row["size"]
        try:
            link = await self._fetch_metalink(row["url"])
            link_size = link.size
            filename = os.path.basename(link.name) or os.path.basename(
                urlsplit(row["url"]).path
            ).removesuffix(".meta4")
            if not filename.endswith(".zim"):
                raise DownloadError(f"unexpected file name {filename!r}")
            if filename in self._active_targets or self.db.one(
                "SELECT id FROM downloads WHERE id != ? AND filename = ? "
                "AND state IN ('queued', 'downloading', 'verifying', 'paused')",
                (download_id, filename),
            ):
                raise DownloadError(f"another active download is already writing {filename}")
            self._active_targets[filename] = download_id
            target = self.data_dir / filename
            pieces = (
                row["pieces"] if row["filename"] == filename and row["size"] == link.size else None
            )
            with self.db.transaction():
                self.db.execute(
                    "UPDATE downloads SET filename = ?, size = ?, updated = unixepoch('subsec') "
                    "WHERE id = ?",
                    (filename, link.size, download_id),
                )
            download = Download(
                link,
                target,
                pieces=pieces,
                connections=int(self.db.get_setting("connections_per_file")),
                limiter=self.limiter,
                proxy=self.proxy,
                network=self.network,
            )
            self._check_free_space(link.size, download.part)
            active.download = download
            active.last_bytes = sum(
                link.piece_range(i)[1] for i in range(download.bitmap.count) if download.bitmap[i]
            )
            await download.run()
            with self.db.transaction():
                self.db.execute(
                    "UPDATE downloads SET state = 'verifying', pieces = ?, bytes_done = ?, "
                    "updated = unixepoch('subsec') WHERE id = ?",
                    (download.bitmap.to_bytes(), link.size, download_id),
                )
            self.events.publish("downloads", self.list())
            active.installing = True
            fut = asyncio.ensure_future(asyncio.to_thread(self._install, row, download))
            while not fut.done():
                try:
                    await asyncio.shield(fut)
                except asyncio.CancelledError:
                    pass
            fut.result()
            installed = True
        except asyncio.CancelledError:
            if download is not None:
                await asyncio.to_thread(download.sync)
                with self.db.transaction():
                    self.db.execute(
                        "UPDATE downloads SET pieces = ?, bytes_done = ? WHERE id = ?",
                        (download.bitmap.to_bytes(), download.bytes_done, download_id),
                    )
            self._set_state(download_id, active.stop_state)
            log.info("download of %s %s", row["title"], active.stop_state)
            if active.stop_state == "cancelled" and download is not None:
                download.part.unlink(missing_ok=True)
        except Exception as exc:
            log.exception("download %s failed", row["title"])
            message = str(exc) or type(exc).__name__
            with self.db.transaction():
                if download is not None:
                    self.db.execute(
                        "UPDATE downloads SET pieces = ?, bytes_done = ? WHERE id = ?",
                        (download.bitmap.to_bytes(), download.bytes_done, download_id),
                    )
                self._set_state(download_id, "failed", message)
                self._history(row, link_size, "failed", message)
        finally:
            if filename and self._active_targets.get(filename) == download_id:
                self._active_targets.pop(filename, None)
            self._active.pop(download_id, None)

        if installed:
            try:
                with self.db.transaction():
                    self._set_state(download_id, "done")
            except Exception:
                log.exception("failed to mark %s done", row["title"])
            try:
                with self.db.transaction():
                    self._history(row, link_size, "updated" if row["replaces"] else "added")
            except Exception:
                log.exception("bookkeeping failed after installing %s", row["title"])
            log.info("downloaded %s (%.1f MB)", row["title"], link_size / 1024**2)
            with contextlib.suppress(Exception):
                self.events.publish("library", {"changed": True})
        self._changed()

    async def _fetch_metalink(self, url: str) -> metalink.Metalink:
        parts = urlsplit(url)
        if not parts.path.endswith(".meta4"):
            url = urlunsplit(parts._replace(path=parts.path + ".meta4"))
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(30, read=60),
            headers={"User-Agent": USER_AGENT},
            proxy=self.proxy,
        ) as client:
            response: httpx.Response | None = None
            for attempt in range(3):
                if self.network is not None:
                    await self.network.steady()
                try:
                    response = await client.get(url)
                    if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                        await asyncio.sleep(2**attempt)
                        continue
                    response.raise_for_status()
                    break
                except httpx.TransportError:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(2**attempt)
            assert response is not None
        return metalink.parse(response.content)

    def _check_free_space(self, size: int, part: Path) -> None:
        # The .part file is sparse, so count the blocks it really occupies.
        allocated = part.stat().st_blocks * 512 if part.exists() else 0
        in_flight = sum(
            max(0, a.download.metalink.size - a.download.bytes_done)
            for a in self._active.values()
            if a.download and a.download.part != part
        )
        needed = max(0, size - allocated) + in_flight + FREE_SPACE_MARGIN
        free = shutil.disk_usage(self.data_dir).free
        if free < needed:
            raise DownloadError(
                f"not enough disk space: {needed / 1e9:.1f} GB needed, {free / 1e9:.1f} GB free"
            )

    def _install(self, row: dict[str, Any], download: Download) -> None:
        """Check the finished file opens, move it into place and retire the old release."""
        old = row["replaces"]
        current = None
        if old or row["book_id"]:
            current = self.db.one(
                "SELECT uuid, date, path FROM books WHERE path = ? OR id = ?",
                (old or "", row["book_id"]),
            )
            if (
                current
                and current["date"]
                and row["to_date"]
                and compare_release_dates(current["date"], row["to_date"]) > 0
            ):
                download.part.unlink(missing_ok=True)
                raise DownloadError("a newer release was installed while downloading")
        try:
            Archive(str(download.part))
        except Exception as exc:
            download.part.unlink(missing_ok=True)
            raise DownloadError(f"the downloaded file is not a valid ZIM: {exc}") from exc
        os.replace(download.part, download.target)
        if (
            old
            and old != str(download.target)
            and (current is None or current["path"] == old)
            and not self.db.get_setting("keep_old_versions")
        ):
            self.pool.invalidate(old)
            Path(old).unlink(missing_ok=True)
            Path(old + ".meta4").unlink(missing_ok=True)
        self.scanner.scan()

    def _history(self, row: dict[str, Any], size: int, result: str, message: str = "") -> None:
        self.db.execute(
            "INSERT INTO history (book_id, title, from_date, to_date, size, result, message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row["book_id"], row["title"], row["from_date"], row["to_date"], size, result, message),
        )
