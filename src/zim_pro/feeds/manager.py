"""The queue of feed syncs, and the clock that starts them.

Syncs are builds of their own kind with a queue, a limit and workers of their own, so a
long crawl on the Create page never holds up the news, and the other way round.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

from ..builder.jobs import BuildManager, per_second
from ..builder.report import Reporter
from ..builder.writer import BuildError, Stage
from .sync import FeedSync, forget

log = logging.getLogger(__name__)

SCHEDULE_SECONDS = 30


class FeedManager(BuildManager):
    KINDS = ("feed",)
    EVENT = "feeds"
    JOB = "feed"
    SLOTS = "max_concurrent_feed_syncs"
    WORK = "feed-work"
    KEEP_FINISHED = 100

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.store_dir: Path = self.work_dir.parent / "feeds"

    async def start(self) -> None:
        await super().start()
        self._tasks.append(asyncio.create_task(self._schedule()))

    def store(self, feed_id: int) -> Path:
        return self.store_dir / str(feed_id)

    def forget(self, feed_id: int) -> None:
        forget(self.store(feed_id))

    def sync(self, feed_id: int, rebuild: bool = False, trigger: str = "manual") -> dict[str, Any]:
        feed = self.db.one("SELECT id, title FROM feeds WHERE id = ?", (feed_id,))
        if feed is None:
            raise BuildError("No such feed collection.")
        return self.enqueue(
            "feed", feed_id, feed["title"] or "Feeds", {"rebuild": rebuild, "trigger": trigger}
        )

    def due(self) -> list[dict[str, Any]]:
        """Collections whose time to sync has come and that are not already at it."""
        return self.db.all(
            "SELECT id, title FROM feeds WHERE enabled = 1 AND interval_minutes > 0 "
            "AND (last_attempt IS NULL OR last_attempt + interval_minutes * 60 <= unixepoch()) "
            "AND id NOT IN (SELECT source_id FROM builds WHERE kind = 'feed' "
            "AND state IN ('queued', 'processing', 'paused')) ORDER BY last_attempt"
        )

    async def _schedule(self) -> None:
        while True:
            try:
                if self.db.get_setting("feed_auto_sync"):
                    for feed in self.due():
                        self.sync(feed["id"], trigger="schedule")
                        log.info("queued the scheduled sync of %s", feed["title"] or feed["id"])
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("feed schedule error")
            await asyncio.sleep(SCHEDULE_SECONDS)

    async def _build(
        self, row: dict[str, Any], stage: Stage, reporter: Reporter
    ) -> tuple[str, dict[str, Any]]:
        feed = self.db.one("SELECT * FROM feeds WHERE id = ?", (row["source_id"],))
        if feed is None:
            raise BuildError("The feed collection this sync was for has been removed.")
        self.db.execute(
            "UPDATE feeds SET last_attempt = unixepoch('subsec') WHERE id = ?", (feed["id"],)
        )
        try:
            sync = FeedSync(
                self.db,
                feed,
                self.store(feed["id"]),
                stage,
                reporter,
                workers=self._workers(),
                rate=per_second(feed["rate_limit"], feed["rate_unit"]),
                **self._route(),
                rebuild=bool(row["params"].get("rebuild")),
                transport=self.transport,
            )
            meta, outcome = await sync.run()
            name = feed["book_id"]
            if meta is None:
                report: dict[str, Any] = {"unchanged": True, "title": sync.feed["title"]}
            else:
                meta.name = name = self._name_for("feeds", feed, meta.name)
                self._own_icon(stage, "feed", feed["id"])
                report = await self._write(stage, meta, reporter)
            report.update(outcome)
        except BuildError as exc:
            self.db.execute("UPDATE feeds SET last_error = ? WHERE id = ?", (str(exc), feed["id"]))
            raise
        self.db.execute(
            "UPDATE feeds SET book_id = ?, last_synced = unixepoch('subsec'), last_new = ?, "
            "last_error = NULL WHERE id = ?",
            (name, outcome["new"], feed["id"]),
        )
        return name, report

    def _history(self, book_id: str, title: str, size: int, replaced: bool) -> None:
        if not replaced:  # the first ZIM is news; every sync after it is not
            super()._history(book_id, title, size, replaced)
