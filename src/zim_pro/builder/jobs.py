"""The queue of builds: each one makes a ZIM from a link or an uploaded book.

A build is queued, then processing (fetching, converting, writing, verifying), then
finished, failed or cancelled. It can be paused while queued or processing, and resumed
from where it stopped. Its work is spread over threads: page fetches run
concurrently, cleaning and converting run on worker threads, libzim compresses and indexes
on its own threads, and one thread writes the file.
"""

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any

from libzim.reader import Archive

from ..db import Database
from ..events import EventBus
from ..library.archive_pool import ArchivePool
from ..library.naming import split_filename
from ..library.scanner import Scanner
from . import zimit
from .convert import epub, mobi, pdf
from .crawler import Crawler, CrawlOptions, NothingNew
from .report import Cancelled, Reporter
from .writer import BuildError, Stage, ZimMeta, slugify, write_zim

log = logging.getLogger(__name__)

TICK_SECONDS = 1
FREE_SPACE_MARGIN = 512 * 1024 * 1024
LIST_COLUMNS = (
    "id, kind, source_id, title, state, phase, done, total, failed, skipped, size, error, "
    "report, params, book_id, created, started, finished"
)
# Steps that cannot be held half-way, and what to tell someone who tries.
UNPAUSABLE = {
    "writing": "The ZIM is being written and will be done shortly; it cannot be paused now.",
    "verifying": "The ZIM is being checked and will be done shortly; it cannot be paused now.",
    "preparing the browser": "A browser capture cannot be paused. Cancel it instead.",
    "capturing": "A browser capture cannot be paused. Cancel it instead.",
}
CONVERTERS = {"epub": epub.convert, "pdf": pdf.convert, "mobi": mobi.convert}


def per_second(amount: float, unit: str) -> float:
    """A rate limit as requests per second; 0 when none was set."""
    amount = float(amount or 0)
    return amount / 60 if unit == "minute" else amount


class _Active:
    def __init__(self, task: asyncio.Task[None], reporter: Reporter):
        self.task = task
        self.reporter = reporter


class BuildManager:
    """A queue of builds of some kinds. Each queue has its own workers and its own limit."""

    KINDS: tuple[str, ...] = ("link", "upload")
    EVENT = "builds"  # the name its progress is published under
    SLOTS = "max_concurrent_builds"  # the setting that says how many run at once
    WORK = "builds"  # its folder under the state directory
    KEEP_FINISHED = 0  # finished builds kept in the list; 0 keeps them until cleared

    def __init__(
        self,
        db: Database,
        events: EventBus,
        scanner: Scanner,
        pool: ArchivePool,
        data_dir: Path,
        state_dir: Path,
    ):
        self.db = db
        self.events = events
        self.scanner = scanner
        self.pool = pool
        self.data_dir = data_dir
        self.work_dir = state_dir / self.WORK
        self._mine = "kind IN (" + ", ".join(f"'{kind}'" for kind in self.KINDS) + ")"
        self.upload_dir = state_dir / "uploads"
        self._active: dict[int, _Active] = {}
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False
        self.transport = None  # tests put a fake web here

    async def start(self) -> None:
        # Builds cut short by a restart start again from the queue. One that was paused
        # stays paused, and starts again when it is resumed.
        self.db.execute(
            "UPDATE builds SET state = 'queued', phase = '', done = 0 "
            f"WHERE state = 'processing' AND {self._mine}"
        )
        self.db.execute(
            f"UPDATE builds SET phase = '', done = 0 WHERE state = 'paused' AND {self._mine}"
        )
        shutil.rmtree(self.work_dir / "stage", ignore_errors=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self._tasks = [asyncio.create_task(self._run_queue()), asyncio.create_task(self._tick())]

    async def stop(self) -> None:
        self._stopping = True
        for active in self._active.values():
            active.reporter.cancelled.set()
            active.task.cancel()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(
            *self._tasks, *(a.task for a in self._active.values()), return_exceptions=True
        )

    # -- the queue, as the API sees it ----------------------------------------------

    def list(self) -> list[dict[str, Any]]:
        rows = self.db.all(
            f"SELECT {LIST_COLUMNS} FROM builds WHERE {self._mine} ORDER BY id DESC LIMIT 200"
        )
        return [self._decorate(row) for row in rows]

    def get(self, build_id: int) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM builds WHERE id = ?", (build_id,))
        if row is None:
            return None
        row["log"] = json.loads(row["log"] or "[]")
        active = self._active.get(build_id)
        if active:
            row["log"] = list(active.reporter.log)
        return self._decorate(row)

    def _decorate(self, row: dict[str, Any]) -> dict[str, Any]:
        row["report"] = json.loads(row.get("report") or "{}")
        row["params"] = json.loads(row.get("params") or "{}")
        active = self._active.get(row["id"])
        if active:
            row.update(active.reporter.snapshot())
        return row

    def enqueue(
        self, kind: str, source_id: int, title: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        busy = self.db.one(
            "SELECT id FROM builds WHERE kind = ? AND source_id = ? "
            "AND state IN ('queued', 'processing', 'paused')",
            (kind, source_id),
        )
        if busy:
            raise BuildError("This is already being built.")
        cursor = self.db.execute(
            "INSERT INTO builds (kind, source_id, title, params) VALUES (?, ?, ?, ?)",
            (kind, source_id, title, json.dumps(params or {})),
        )
        self._changed()
        return self.get(cursor.lastrowid)  # type: ignore[return-value]

    def cancel(self, build_id: int) -> None:
        active = self._active.get(build_id)
        if active:
            active.reporter.cancelled.set()
            active.task.cancel()
        else:
            self.db.execute(
                "UPDATE builds SET state = 'cancelled', finished = unixepoch('subsec') "
                "WHERE id = ? AND state IN ('queued', 'paused')",
                (build_id,),
            )
        self._changed()

    def pause(self, build_id: int) -> None:
        """Hold a build where it is. One that is waiting keeps its place but does not start."""
        active = self._active.get(build_id)
        if active:
            phase = active.reporter.phase_name
            if phase in UNPAUSABLE:
                raise BuildError(UNPAUSABLE[phase])
            active.reporter.paused.set()
            self.db.execute(
                "UPDATE builds SET state = 'paused' WHERE id = ? AND state = 'processing'",
                (build_id,),
            )
        else:
            self.db.execute(
                "UPDATE builds SET state = 'paused' WHERE id = ? AND state = 'queued'", (build_id,)
            )
        self._changed()

    def resume(self, build_id: int) -> None:
        active = self._active.get(build_id)
        if active:
            active.reporter.paused.clear()
            self.db.execute(
                "UPDATE builds SET state = 'processing' WHERE id = ? AND state = 'paused'",
                (build_id,),
            )
        else:
            self.db.execute(
                "UPDATE builds SET state = 'queued' WHERE id = ? AND state = 'paused'", (build_id,)
            )
        self._changed()

    def retry(self, build_id: int) -> None:
        self.db.execute(
            "UPDATE builds SET state = 'queued', phase = '', done = 0, total = 0, failed = 0, "
            "skipped = 0, error = NULL, log = '[]', started = NULL, finished = NULL "
            "WHERE id = ? AND state IN ('failed', 'cancelled', 'finished')",
            (build_id,),
        )
        self._changed()

    def remove(self, build_id: int) -> None:
        self.db.execute(
            "DELETE FROM builds WHERE id = ? AND state NOT IN ('queued', 'processing', 'paused')",
            (build_id,),
        )
        self._changed()

    def clear_finished(self) -> None:
        self.db.execute(
            "DELETE FROM builds WHERE state IN ('finished', 'failed', 'cancelled') "
            f"AND {self._mine}"
        )
        self._changed()

    def _changed(self) -> None:
        self._wake.set()
        self.events.publish(self.EVENT, self.list())

    # -- running --------------------------------------------------------------------

    async def _run_queue(self) -> None:
        while True:
            self._wake.clear()
            # A paused build gives its place to the next one in the queue.
            running = sum(not a.reporter.paused.is_set() for a in self._active.values())
            slots = int(self.db.get_setting(self.SLOTS)) - running
            if slots > 0:
                queued = self.db.all(
                    f"SELECT id FROM builds WHERE state = 'queued' AND {self._mine} "
                    "ORDER BY id LIMIT ?",
                    (slots,),
                )
                for row in queued:
                    if row["id"] in self._active:
                        continue
                    self.db.execute(
                        "UPDATE builds SET state = 'processing', started = unixepoch('subsec') "
                        "WHERE id = ?",
                        (row["id"],),
                    )
                    reporter = Reporter()
                    task = asyncio.create_task(self._run_one(row["id"], reporter))
                    self._active[row["id"]] = _Active(task, reporter)
                if queued:
                    self.events.publish(self.EVENT, self.list())
            await self._wake.wait()

    async def _tick(self) -> None:
        """Publish live progress while anything is building."""
        while True:
            await asyncio.sleep(TICK_SECONDS)
            if self._active:
                self.events.publish(self.EVENT, self.list())

    async def _run_one(self, build_id: int, reporter: Reporter) -> None:
        row = self.db.one("SELECT * FROM builds WHERE id = ?", (build_id,))
        stage = Stage(self.work_dir / "stage" / str(build_id))
        state, error, report, book_id = "finished", None, {}, ""
        try:
            assert row is not None
            row["params"] = json.loads(row.get("params") or "{}")
            book_id, report = await self._build(row, stage, reporter)
        except (Cancelled, asyncio.CancelledError):
            state = "cancelled"
        except BuildError as exc:
            state, error = "failed", str(exc)
        except Exception as exc:
            log.exception("build %s failed", build_id)
            state, error = "failed", f"Unexpected error: {exc or type(exc).__name__}"
        finally:
            stage.clear()
            self._active.pop(build_id, None)
            # When the server is going down the record is left alone: start() picks the
            # build up again.
            if not (self._stopping and state == "cancelled"):
                self._record(build_id, reporter, state, error, report, book_id)
                quiet = report.get("unchanged") and (row or {}).get("kind") == "link"
                if quiet and row["params"].get("trigger") == "schedule":
                    # A scheduled look at the feeds that found nothing is not worth a line.
                    self.db.execute("DELETE FROM builds WHERE id = ?", (build_id,))
                    self._changed()

    def _record(
        self,
        build_id: int,
        reporter: Reporter,
        state: str,
        error: str | None,
        report: dict[str, Any],
        book_id: str,
    ) -> None:
        snapshot = reporter.snapshot()
        if state == "finished" and report.get("pages"):
            # The last phase is not about pages; what the record keeps is how many.
            snapshot["done"] = snapshot["total"] = report["pages"]
        self.db.execute(
            "UPDATE builds SET state = ?, error = ?, phase = '', done = ?, total = ?, "
            "failed = ?, skipped = ?, size = ?, log = ?, report = ?, book_id = ?, "
            "title = COALESCE(NULLIF(?, ''), title), "
            "finished = unixepoch('subsec') WHERE id = ?",
            (
                state,
                error,
                snapshot["done"],
                snapshot["total"],
                snapshot["failed"],
                snapshot["skipped"],
                report.get("size", 0),
                json.dumps(reporter.log),
                json.dumps(report),
                book_id,
                report.get("title", ""),
                build_id,
            ),
        )
        if self.KEEP_FINISHED:
            self.db.execute(
                "DELETE FROM builds WHERE state IN ('finished', 'failed', 'cancelled') "
                f"AND {self._mine} AND id NOT IN (SELECT id FROM builds WHERE {self._mine} "
                "ORDER BY id DESC LIMIT ?)",
                (self.KEEP_FINISHED,),
            )
        self._changed()
        self.events.publish("library", {"changed": True})

    async def _build(
        self, row: dict[str, Any], stage: Stage, reporter: Reporter
    ) -> tuple[str, dict[str, Any]]:
        """Do the work of one build; returns the book it made and what went into it."""
        if row["kind"] == "link":
            return await self._build_link(row, stage, reporter)
        return await self._build_upload(row, stage, reporter)

    def _workers(self) -> int:
        return max(1, min(16, int(self.db.get_setting("build_workers"))))

    def _name_for(self, table: str, source: dict[str, Any], wanted: str) -> str:
        """The book id for a source: the one it already has, or a free one."""
        if source["book_id"]:
            return source["book_id"]
        taken = {r["id"] for r in self.db.all("SELECT id FROM books")}
        for other in ("links", "uploads", "feeds"):
            taken |= {
                r["book_id"]
                for r in self.db.all(f"SELECT book_id FROM {other} WHERE book_id != ''")
            }
        name, number = wanted, 2
        while name in taken:
            name, number = f"{wanted}-{number}", number + 1
        return name

    async def _build_link(
        self, row: dict[str, Any], stage: Stage, reporter: Reporter
    ) -> tuple[str, dict[str, Any]]:
        link = self.db.one("SELECT * FROM links WHERE id = ?", (row["source_id"],))
        if link is None:
            raise BuildError("The link this build was for has been removed.")
        if link["engine"] == "zimit":
            name = self._name_for("links", link, f"custom_{slugify(link['title'] or link['url'])}")
            folder = self.data_dir / f".zimit-{row['id']}"
            try:
                made = await zimit.capture(link, folder, reporter, name)
                reporter.phase("verifying")
                target = self.data_dir / f"{name}_{time.strftime('%Y-%m')}.zim"
                report = await asyncio.to_thread(self._adopt, made, target, True)
            finally:
                shutil.rmtree(folder, ignore_errors=True)
        else:
            options = CrawlOptions(
                url=link["url"],
                scope=link["scope"],
                max_pages=link["max_pages"],
                max_depth=link["max_depth"],
                clean=link["mode"] != "full",
                images=bool(link["images"]),
                video=bool(link["video"]),
                subdomains=bool(link["subdomains"]),
                sitemap_urls=tuple(link["sitemaps"].split()),
                robots_url=link["robots_url"],
                feed_urls=tuple(link["feeds"].split()),
                rate=per_second(link["rate_limit"], link["rate_unit"]),
                proxy=str(self.db.get_setting("build_proxy")),
                solver=str(self.db.get_setting("captcha_solver_url")),
                title=link["title"],
                language=link["language"],
            )
            cache = self.work_dir / "cache" / f"link-{link['id']}"
            crawler = Crawler(options, stage, cache, reporter, self._workers(), self.transport)
            # An update from the feeds needs an earlier build, and its ZIM, to add to;
            # without them this is an ordinary build.
            from_feeds = bool(
                row["params"].get("rss")
                and options.feed_urls
                and crawler.can_update_from_feeds()
                and self.db.one("SELECT id FROM books WHERE id = ?", (link["book_id"],))
            )
            options.feeds_only = from_feeds
            self.db.execute(
                "UPDATE links SET last_feed_check = unixepoch('subsec') WHERE id = ?",
                (link["id"],),
            )
            try:
                meta = await crawler.run()
            except NothingNew:
                return link["book_id"], {"unchanged": True, "from_feeds": 0, "rss": True}
            meta.name = self._name_for("links", link, meta.name)
            report = await self._write(stage, meta, reporter, history=not from_feeds)
            report.update(
                rss=from_feeds,
                from_feeds=crawler.from_feeds,
                downloaded=crawler.fetcher.downloaded,
                reused_from_cache=crawler.fetcher.reused,
                from_sitemaps=crawler.from_sitemaps,
                slowed=crawler.fetcher.slowed,
                solved=crawler.fetcher.solved,
            )
            name = meta.name
            if from_feeds:
                # The site was not crawled again, so its next full rebuild is still due
                # when it was.
                return name, report
        self.db.execute(
            "UPDATE links SET book_id = ?, last_built = unixepoch('subsec'), "
            "title = CASE WHEN title = '' THEN ? ELSE title END WHERE id = ?",
            (name, report.get("title", ""), link["id"]),
        )
        return name, report

    async def _build_upload(
        self, row: dict[str, Any], stage: Stage, reporter: Reporter
    ) -> tuple[str, dict[str, Any]]:
        upload = self.db.one("SELECT * FROM uploads WHERE id = ?", (row["source_id"],))
        if upload is None:
            raise BuildError("The file this build was for has been removed.")
        source = Path(upload["path"])
        if not source.is_file():
            raise BuildError("The uploaded file is no longer on the server. Upload it again.")
        if upload["kind"] == "zim":
            reporter.phase("verifying")
            stem, _ = split_filename(upload["filename"])
            target = self.data_dir / upload["filename"]
            report = await asyncio.to_thread(self._adopt, source, target, True)
            name = stem
        else:
            convert = CONVERTERS[upload["kind"]]
            meta: ZimMeta = await asyncio.to_thread(
                convert,
                source,
                stage,
                reporter,
                upload["filename"],
                bool(upload["images"]),
                upload["title"],
            )
            meta.name = self._name_for("uploads", upload, meta.name)
            report = await self._write(stage, meta, reporter)
            name = meta.name
        self.db.execute(
            "UPDATE uploads SET book_id = ?, last_built = unixepoch('subsec') WHERE id = ?",
            (name, upload["id"]),
        )
        return name, report

    async def _write(
        self, stage: Stage, meta: ZimMeta, reporter: Reporter, history: bool = True
    ) -> dict[str, Any]:
        """Write the staged content as a ZIM in the library and retire the one it replaces."""
        reporter.check()
        needed = stage.size + FREE_SPACE_MARGIN
        free = shutil.disk_usage(self.data_dir).free
        if free < needed:
            raise BuildError(
                f"Not enough disk space: {needed / 1e9:.1f} GB needed, {free / 1e9:.1f} GB free."
            )
        reporter.phase("writing", len(stage.entries))
        target = self.data_dir / meta.filename
        previous = self.db.one("SELECT path FROM books WHERE id = ?", (meta.name,))
        self.pool.invalidate(str(target))
        report = await asyncio.to_thread(
            write_zim, stage, meta, target, self._workers(), reporter.progress
        )
        reporter.phase("verifying")
        self._retire(previous, target)
        await asyncio.to_thread(self.scanner.scan)
        if history:
            self._history(meta.name, meta.title, report["size"], bool(previous))
        report["title"] = meta.title
        return report

    def _adopt(self, made: Path, target: Path, move: bool = False) -> dict[str, Any]:
        """Take a ready-made ZIM into the library after checking that it opens."""
        try:
            archive = Archive(str(made))
            pages = archive.article_count
            searchable = bool(archive.has_fulltext_index)
            title = archive.get_metadata("Title").decode("utf-8", "replace")
        except Exception as exc:
            raise BuildError(f"This is not a valid ZIM file ({exc}).") from exc
        del archive
        stem, _ = split_filename(target.name)
        previous = self.db.one("SELECT path FROM books WHERE id = ?", (stem,))
        self.pool.invalidate(str(target))
        temporary = target.with_name(target.name + ".tmp")
        shutil.copyfile(made, temporary)
        temporary.replace(target)
        if move:
            made.unlink(missing_ok=True)
        self._retire(previous, target)
        self.scanner.scan()
        size = target.stat().st_size
        self._history(stem, title, size, bool(previous))
        return {"pages": pages, "searchable": searchable, "size": size, "title": title}

    def _retire(self, previous: dict[str, Any] | None, target: Path) -> None:
        """Remove the release a new build replaces, unless old versions are kept."""
        if not previous or previous["path"] == str(target):
            return
        if self.db.get_setting("keep_old_versions"):
            return
        self.pool.invalidate(previous["path"])
        Path(previous["path"]).unlink(missing_ok=True)

    def _history(self, book_id: str, title: str, size: int, replaced: bool) -> None:
        self.db.execute(
            "INSERT INTO history (book_id, title, to_date, size, result, message) "
            "VALUES (?, ?, ?, ?, ?, 'Made by Zim Pro')",
            (
                book_id,
                title,
                time.strftime("%Y-%m-%d"),
                size,
                "updated" if replaced else "added",
            ),
        )
