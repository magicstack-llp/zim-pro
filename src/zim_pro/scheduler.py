import asyncio
import logging
import time

from .catalog import opds
from .catalog.updates import book_updates
from .context import Context

log = logging.getLogger(__name__)

RESCAN_SECONDS = 300
CHECK_SECONDS = 60


async def refresh_catalog(ctx: Context) -> None:
    try:
        count = await opds.refresh(ctx.db, ctx.config.catalog_url)
    except Exception as exc:
        log.warning("catalog refresh failed: %s", exc)
        ctx.db.set_meta("catalog_error", str(exc) or type(exc).__name__)
        # Try again in an hour rather than on every check.
        ctx.db.set_meta("catalog_retry_after", time.time() + 3600)
        return
    ctx.db.set_meta("catalog_error", None)
    ctx.db.set_meta("catalog_retry_after", None)
    ctx.events.publish("catalog", {"entries": count})
    queue_auto_updates(ctx)


def queue_auto_updates(ctx: Context) -> int:
    """Queue the newest release of every book whose policy is ``auto``."""
    queued = 0
    for item in book_updates(ctx.db):
        if item["status"] == "update" and item["book"]["policy"] == "auto" and not item["download"]:
            ctx.downloads.enqueue(item["latest"]["uuid"])
            queued += 1
    if queued:
        log.info("queued %d automatic updates", queued)
    return queued


def _rebuild_due_links(ctx: Context) -> None:
    """Queue a rebuild for every link whose ZIM is older than the link asks for."""
    due = ctx.db.all(
        "SELECT id, url, title FROM links WHERE rebuild_days > 0 AND book_id != '' "
        "AND last_built IS NOT NULL AND last_built < unixepoch() - rebuild_days * 86400 "
        "AND id NOT IN (SELECT source_id FROM builds WHERE kind = 'link' "
        "AND (state IN ('queued', 'processing', 'paused') OR created > unixepoch() - 86400))"
    )
    for link in due:
        try:
            ctx.builds.enqueue("link", link["id"], link["title"] or link["url"])
            log.info("queued the scheduled rebuild of %s", link["url"])
        except Exception:
            log.exception("could not queue the rebuild of %s", link["url"])


def _check_due_feeds(ctx: Context) -> None:
    """Queue an update from its RSS feeds for every link whose time to look has come."""
    due = ctx.db.all(
        "SELECT id, url, title FROM links WHERE feeds != '' AND feed_minutes > 0 "
        "AND engine = 'builtin' AND book_id != '' "
        "AND COALESCE(last_feed_check, last_built, 0) + feed_minutes * 60 <= unixepoch() "
        "AND id NOT IN (SELECT source_id FROM builds WHERE kind = 'link' "
        "AND state IN ('queued', 'processing', 'paused'))"
    )
    for link in due:
        try:
            ctx.builds.enqueue(
                "link",
                link["id"],
                link["title"] or link["url"],
                {"rss": True, "trigger": "schedule"},
            )
        except Exception:
            log.exception("could not queue the feed update of %s", link["url"])


async def run(ctx: Context) -> None:
    """Background loop: rescan the library and keep the catalog fresh."""
    last_scan = time.monotonic()
    while True:
        try:
            refreshed = ctx.db.get_meta("catalog_refreshed") or 0
            retry_after = ctx.db.get_meta("catalog_retry_after") or 0
            interval = int(ctx.db.get_setting("catalog_refresh_hours")) * 3600
            now = time.time()
            if now - refreshed >= interval and now >= retry_after:
                await refresh_catalog(ctx)
            _rebuild_due_links(ctx)
            _check_due_feeds(ctx)
            if time.monotonic() - last_scan >= RESCAN_SECONDS:
                last_scan = time.monotonic()
                result = await asyncio.to_thread(ctx.scanner.scan)
                if result["read"] or result["removed"]:
                    log.info("library changed on disk: %s", result)
                    ctx.events.publish("library", {"changed": True})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduler error")
        await asyncio.sleep(CHECK_SECONDS)
