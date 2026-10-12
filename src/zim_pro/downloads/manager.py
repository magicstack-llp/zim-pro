import asyncio
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

import httpx
from libzim.reader import Archive

from ..db import Database
from ..events import EventBus
from ..library.archive_pool import ArchivePool
from ..library.scanner import Scanner
from . import metalink
from .engine import USER_AGENT, Download, DownloadError, RateLimiter

log = logging.getLogger(__name__)

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
    ):
        self.db = db
        self.events = events
        self.scanner = scanner
        self.pool = pool
        self.data_dir = data_dir
        self.limiter = RateLimiter()
        self._active: dict[int, _Active] = {}
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
            "position, id DESC LIMIT 500"
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
        entry = self.db.one("SELECT * FROM catalog WHERE uuid = ?", (catalog_uuid,))
        if entry is None:
            raise LookupError("unknown catalog entry")
        existing = self.db.one(
            "SELECT id FROM downloads WHERE catalog_uuid = ? "
            "AND state IN ('queued', 'downloading', 'verifying', 'paused', 'failed')",
            (catalog_uuid,),
        )
        if existing:
            self.resume(existing["id"])
            return self.get(existing["id"])  # type: ignore[return-value]
        book = self.db.one(
            "SELECT * FROM books WHERE id = ? OR (name = ? AND flavour = ?)",
            (entry["stem"], entry["name"], entry["flavour"]),
        )
        if book and book["uuid"] == entry["uuid"]:
            raise ValueError("this release is already in the library")
        position = (self.db.scalar("SELECT MAX(position) FROM downloads") or 0) + 1
        cursor = self.db.execute(
            "INSERT INTO downloads (catalog_uuid, book_id, title, url, size, replaces, "
            "from_date, to_date, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry["uuid"],
                book["id"] if book else entry["stem"],
                entry["title"],
                entry["url"],
                entry["size"],
                book["path"] if book else None,
                book["date"] if book else "",
                entry["issued"][:10],
                position,
            ),
        )
        self._changed()
        return self.get(cursor.lastrowid)  # type: ignore[arg-type,return-value]

    def pause(self, download_id: int) -> None:
        active = self._active.get(download_id)
        if active:
            active.stop_state = "paused"
            active.task.cancel()
        else:
            self.db.execute(
                "UPDATE downloads SET state = 'paused' WHERE id = ? AND state = 'queued'",
                (download_id,),
            )
            self._changed()

    def resume(self, download_id: int) -> None:
        self.db.execute(
            "UPDATE downloads SET state = 'queued', error = NULL "
            "WHERE id = ? AND state IN ('paused', 'failed')",
            (download_id,),
        )
        self._changed()

    def cancel(self, download_id: int) -> None:
        active = self._active.get(download_id)
        if active:
            active.stop_state = "cancelled"
            active.task.cancel()
            return
        row = self.db.one("SELECT filename, state FROM downloads WHERE id = ?", (download_id,))
        if row and row["state"] in ("queued", "paused", "failed"):
            self._set_state(download_id, "cancelled")
            self._remove_part(row["filename"])
            self._changed()

    def remove(self, download_id: int) -> None:
        """Forget a finished, failed or cancelled download."""
        self.cancel(download_id)
        self.db.execute(
            "DELETE FROM downloads WHERE id = ? AND state IN ('done', 'cancelled', 'failed')",
            (download_id,),
        )
        self._changed()

    def clear_finished(self) -> None:
        self.db.execute("DELETE FROM downloads WHERE state IN ('done', 'cancelled')")
        self._changed()

    def move(self, download_id: int, to_top: bool) -> None:
        edge = self.db.scalar(f"SELECT {'MIN' if to_top else 'MAX'}(position) FROM downloads") or 0
        self.db.execute(
            "UPDATE downloads SET position = ? WHERE id = ?",
            (edge - 1 if to_top else edge + 1, download_id),
        )
        self._changed()

    # -- internals --------------------------------------------------------

    def _changed(self) -> None:
        self._wake.set()
        self.events.publish("downloads", self.list())

    def _set_state(self, download_id: int, state: str, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE downloads SET state = ?, error = ?, updated = unixepoch() WHERE id = ?",
            (state, error, download_id),
        )

    def _remove_part(self, filename: str) -> None:
        if filename:
            (self.data_dir / (filename + ".part")).unlink(missing_ok=True)

    async def _run_queue(self) -> None:
        while True:
            self._wake.clear()
            settings = self.db.get_settings()
            self.limiter.rate = int(settings["bandwidth_limit_kbps"]) * 1024
            slots = int(settings["max_concurrent_downloads"]) - len(self._active)
            if slots > 0:
                queued = self.db.all(
                    "SELECT id FROM downloads WHERE state = 'queued' ORDER BY position, id LIMIT ?",
                    (slots,),
                )
                for row in queued:
                    if row["id"] not in self._active:
                        self._set_state(row["id"], "downloading")
                        task = asyncio.create_task(self._run_one(row["id"]))
                        self._active[row["id"]] = _Active(task)
                if queued:
                    self.events.publish("downloads", self.list())
            await self._wake.wait()

    async def _tick(self) -> None:
        """Publish live progress and persist it periodically."""
        while True:
            await asyncio.sleep(PROGRESS_SECONDS)
            if not self._active:
                continue
            now = time.monotonic()
            for download_id, active in list(self._active.items()):
                download = active.download
                if download is None:
                    continue
                elapsed = now - active.last_time
                rate = (
                    max(0, download.bytes_done - active.last_bytes) / elapsed if elapsed > 0 else 0
                )
                active.speed = rate if active.speed == 0 else 0.7 * active.speed + 0.3 * rate
                if active.speed < 1:
                    active.speed = 0.0  # the average decays forever; call a stall a stall
                active.last_bytes, active.last_time = download.bytes_done, now
                if now - active.last_persist >= PERSIST_SECONDS:
                    active.last_persist = now
                    await self._persist(download_id, download)
            self.events.publish("downloads", self.list())

    async def _persist(self, download_id: int, download: Download) -> None:
        # Snapshot first, then fsync: every piece in the snapshot is on disk once it is saved.
        pieces, done = download.bitmap.to_bytes(), download.bytes_done
        await asyncio.to_thread(download.sync)
        self.db.execute(
            "UPDATE downloads SET pieces = ?, bytes_done = ?, updated = unixepoch() WHERE id = ?",
            (pieces, done, download_id),
        )

    async def _run_one(self, download_id: int) -> None:
        active = self._active[download_id]
        row = self.db.one("SELECT * FROM downloads WHERE id = ?", (download_id,))
        assert row is not None
        download: Download | None = None
        try:
            link = await self._fetch_metalink(row["url"])
            filename = os.path.basename(link.name) or os.path.basename(row["url"]).removesuffix(
                ".meta4"
            )
            if not filename.endswith(".zim"):
                raise DownloadError(f"unexpected file name {filename!r}")
            target = self.data_dir / filename
            pieces = row["pieces"] if row["filename"] == filename else None
            self.db.execute(
                "UPDATE downloads SET filename = ?, size = ? WHERE id = ?",
                (filename, link.size, download_id),
            )
            download = Download(
                link,
                target,
                pieces=pieces,
                connections=int(self.db.get_setting("connections_per_file")),
                limiter=self.limiter,
            )
            self._check_free_space(link.size, download.part)
            active.download = download
            active.last_bytes = sum(
                link.piece_range(i)[1] for i in range(download.bitmap.count) if download.bitmap[i]
            )
            await download.run()
            self.db.execute(
                "UPDATE downloads SET state = 'verifying', pieces = ?, bytes_done = ? WHERE id = ?",
                (download.bitmap.to_bytes(), link.size, download_id),
            )
            self.events.publish("downloads", self.list())
            await asyncio.to_thread(self._install, row, download)
            self._set_state(download_id, "done")
            self._history(row, link.size, "updated" if row["replaces"] else "added")
            self.events.publish("library", {"changed": True})
        except asyncio.CancelledError:
            if download is not None:
                self.db.execute(
                    "UPDATE downloads SET pieces = ?, bytes_done = ? WHERE id = ?",
                    (download.bitmap.to_bytes(), download.bytes_done, download_id),
                )
            self._set_state(download_id, active.stop_state)
            if active.stop_state == "cancelled" and download is not None:
                download.part.unlink(missing_ok=True)
        except Exception as exc:
            log.exception("download %s failed", row["title"])
            if download is not None:
                self.db.execute(
                    "UPDATE downloads SET pieces = ?, bytes_done = ? WHERE id = ?",
                    (download.bitmap.to_bytes(), download.bytes_done, download_id),
                )
            message = str(exc) or type(exc).__name__
            self._set_state(download_id, "failed", message)
            self._history(row, row["size"], "failed", message)
        finally:
            self._active.pop(download_id, None)
            self._changed()

    async def _fetch_metalink(self, url: str) -> metalink.Metalink:
        if not url.endswith(".meta4"):
            url += ".meta4"
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=60, headers={"User-Agent": USER_AGENT}
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
        return metalink.parse(response.content)

    def _check_free_space(self, size: int, part: Path) -> None:
        # The .part file is sparse, so count the blocks it really occupies.
        allocated = part.stat().st_blocks * 512 if part.exists() else 0
        needed = max(0, size - allocated) + FREE_SPACE_MARGIN
        free = shutil.disk_usage(self.data_dir).free
        if free < needed:
            raise DownloadError(
                f"not enough disk space: {needed / 1e9:.1f} GB needed, {free / 1e9:.1f} GB free"
            )

    def _install(self, row: dict[str, Any], download: Download) -> None:
        """Check the finished file opens, move it into place and retire the old release."""
        try:
            Archive(str(download.part))
        except Exception as exc:
            download.part.unlink(missing_ok=True)
            raise DownloadError(f"the downloaded file is not a valid ZIM: {exc}") from exc
        os.replace(download.part, download.target)
        old = row["replaces"]
        if old and old != str(download.target) and not self.db.get_setting("keep_old_versions"):
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
