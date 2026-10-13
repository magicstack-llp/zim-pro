"""One sync of a feed collection: find the new articles, save them, and remake the ZIM.

Only what is new is downloaded. Every article that was saved is kept on disk in the
collection's store (its cleaned page, and its pictures in the fetch cache), so the ZIM, which
cannot be added to in place, is written again from the store without asking the web for
anything it already has.
"""

import asyncio
import html as html_text
import json
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..builder import extract
from ..builder.crawler import MB, PAGE_LIMIT_BYTES, Crawler, CrawlOptions, FetchError, _decode
from ..builder.report import Reporter
from ..builder.writer import BuildError, Stage, ZimMeta, language_code, slugify
from ..db import Database
from . import parse

MAX_TRIES = 3  # times an article is asked for before it is left as failed
THIN = 0.6  # a fetched page with less text than this share of the feed's own is not the article


def article_shell(title: str, body: str, url: str) -> str:
    """An article as the feed carries it, as a page ``extract.prepare`` can clean."""
    return (
        f"<html><head><title>{html_text.escape(title)}</title></head><body><article>"
        f"<h1>{html_text.escape(title)}</h1>{body}</article></body></html>"
    )


class FeedSync:
    def __init__(
        self,
        db: Database,
        feed: dict[str, Any],
        store: Path,
        stage: Stage,
        report: Reporter,
        workers: int = 4,
        rate: float = 0,
        proxy: str = "",
        solver: str = "",
        rebuild: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.db, self.feed, self.stage, self.report = db, feed, stage, report
        self.rebuild = rebuild
        self.store = store
        self.items_dir = store / "items"
        self.items_dir.mkdir(parents=True, exist_ok=True)
        self.sources = db.all(
            "SELECT * FROM feed_sources WHERE feed_id = ? ORDER BY id", (feed["id"],)
        )
        if not self.sources:
            raise BuildError("This collection has no feed addresses. Add one and sync again.")
        options = CrawlOptions(
            url=self.sources[0]["url"],
            scope="page",
            clean=feed["mode"] != "full",
            images=bool(feed["images"]),
            video=bool(feed["video"]),
            rate=rate,
            proxy=proxy,
            solver=solver,
        )
        self.crawler = Crawler(options, stage, store / "cache", report, workers, transport)
        self.fetcher = self.crawler.fetcher
        # An article does not change once published: what is in the cache is used as it is.
        self.fetcher.prefer_cache = True
        self.found = self.saved_now = self.failed_now = self.dropped = 0

    async def run(self) -> tuple[ZimMeta | None, dict[str, Any]]:
        """Sync; returns the ZIM to write (None when nothing changed) and what happened."""
        try:
            return await self._run()
        finally:
            await asyncio.gather(*self.crawler._met.values(), return_exceptions=True)
            await self.fetcher.close()

    async def _run(self) -> tuple[ZimMeta | None, dict[str, Any]]:
        feed, report = self.feed, self.report
        await self._check_sources()
        await self._fetch_articles()
        self._retire_old()
        total = self.db.scalar(
            "SELECT COUNT(*) FROM feed_items WHERE feed_id = ? AND state = 'saved'", (feed["id"],)
        )
        outcome = {
            "found": self.found,
            "new": self.saved_now,
            "articles": total,
            "dropped": self.dropped,
            "slowed": self.fetcher.slowed,
            "solved": self.fetcher.solved,
        }
        if not total:
            why = next((e["why"] for e in report.log if e["kind"] == "failed"), "")
            raise BuildError(
                "No article could be saved from "
                + ("these feeds" if len(self.sources) > 1 else "this feed")
                + (f": {why}" if why else ".")
            )
        has_book = bool(feed["book_id"]) and bool(
            self.db.one("SELECT id FROM books WHERE id = ?", (feed["book_id"],))
        )
        if has_book and not (self.saved_now or self.dropped or self.rebuild):
            return None, outcome
        meta = await self._assemble()
        return meta, outcome

    # -- what the feeds list ----------------------------------------------------------

    async def _check_sources(self) -> None:
        report = self.report
        report.phase("checking feeds", len(self.sources))
        for number, source in enumerate(self.sources, 1):
            await report.gate()
            error = None
            try:
                fetched = await self.fetcher.get(source["url"], 20 * MB, fresh=True)
                data = fetched.file.read_bytes()
                parsed = await asyncio.to_thread(parse.parse, data, fetched.url)
                self._remember(source, parsed)
            except (FetchError, parse.NotAFeed) as exc:
                error = str(exc)
                report.failed(source["url"], error)
            self.db.execute(
                "UPDATE feed_sources SET last_checked = unixepoch('subsec'), last_error = ? "
                "WHERE id = ?",
                (error, source["id"]),
            )
            report.progress(number)

    def _remember(self, source: dict[str, Any], parsed: parse.Feed) -> None:
        """Note the articles of a feed that were not seen before."""
        self.db.execute(
            "UPDATE feed_sources SET title = ?, site = ? WHERE id = ?",
            (parsed.title or source["title"], parsed.site or source["site"], source["id"]),
        )
        source.update(title=parsed.title or source["title"], site=parsed.site or source["site"])
        if not self.feed["title"] and parsed.title:
            self.feed["title"] = parsed.title
            self.db.execute(
                "UPDATE feeds SET title = ? WHERE id = ?", (parsed.title, self.feed["id"])
            )
        if not self.feed["language"] and parsed.language:
            self.feed["language"] = parsed.language
        now = time.time()
        for entry in parsed.entries:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO feed_items "
                "(feed_id, source_id, guid, url, title, author, published, content) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.feed["id"],
                    source["id"],
                    entry.guid,
                    entry.url,
                    entry.title,
                    entry.author,
                    entry.published or now,
                    entry.html,
                ),
            )
            self.found += cursor.rowcount

    # -- the new articles -------------------------------------------------------------

    async def _fetch_articles(self) -> None:
        feed, report = self.feed, self.report
        wanted = "state = 'new' OR state = 'failed'"
        if not self.rebuild:
            wanted = f"state = 'new' OR (state = 'failed' AND tries < {MAX_TRIES})"
        pending = self.db.all(
            f"SELECT * FROM feed_items WHERE feed_id = ? AND ({wanted}) ORDER BY published DESC",
            (feed["id"],),
        )
        if feed["keep"] and len(pending) > feed["keep"]:
            # More new articles than the collection keeps: the older ones would be saved
            # only to be dropped.
            for item in pending[feed["keep"] :]:
                self.db.execute(
                    "UPDATE feed_items SET state = 'dropped', content = '' WHERE id = ?",
                    (item["id"],),
                )
            pending = pending[: feed["keep"]]
        report.phase("fetching articles", len(pending))
        taken = {
            row["path"]
            for row in self.db.all(
                "SELECT path FROM feed_items WHERE feed_id = ? AND state = 'saved'", (feed["id"],)
            )
        }
        done = 0

        async def one(item: dict[str, Any]) -> None:
            nonlocal done
            try:
                await report.gate()
                await self._article(item, taken)
            finally:
                done += 1
                report.progress(done)

        await asyncio.gather(*(one(item) for item in pending))

    async def _article(self, item: dict[str, Any], taken: set[str]) -> None:
        feed = self.feed
        clean, images, video = feed["mode"] != "full", bool(feed["images"]), bool(feed["video"])
        prepared, final, why = None, item["url"], ""
        if feed["full_text"] and item["url"]:
            where = urlsplit(item["url"])
            await self.crawler._meet(where.scheme, where.netloc)
        if feed["full_text"] and item["url"] and not self.crawler.allowed(item["url"]):
            why = "the site's robots.txt does not allow the page to be fetched"
        elif feed["full_text"] and item["url"]:
            try:
                fetched = await self.fetcher.get(item["url"], PAGE_LIMIT_BYTES)
                if fetched.is_html:
                    final = fetched.url
                    prepared = await asyncio.to_thread(
                        lambda: extract.prepare(
                            _decode(fetched.file.read_bytes()), final, clean, images, video
                        )
                    )
                else:
                    why = "the address does not hold a web page"
            except FetchError as exc:
                why = str(exc)
        if item["content"].strip():
            base = item["url"] or self.sources[0]["url"]
            carried = await asyncio.to_thread(
                lambda: extract.prepare(
                    article_shell(item["title"], item["content"], base), base, True, images, video
                )
            )
            # A page with much less to say than the feed itself is a paywall or an empty shell.
            if prepared is None or len(prepared.text) < THIN * len(carried.text):
                if prepared is not None or why:
                    self.report.note(
                        "Kept the article as the feed carries it"
                        + (f": {why}" if why else ": the page held less than the feed."),
                        item["url"],
                    )
                prepared, final = carried, base
        if prepared is None or not prepared.text.strip():
            why = why or "the feed carries no text for this article and it has no address"
            self.failed_now += 1
            self.report.failed(item["url"] or item["title"], why)
            self.db.execute(
                "UPDATE feed_items SET state = 'failed', tries = tries + 1, error = ? WHERE id = ?",
                (why, item["id"]),
            )
            return
        path = extract.zim_path(final) if item["url"] else f"articles/{item['id']}"
        if path in taken:
            self.db.execute(
                "UPDATE feed_items SET state = 'skipped', content = '', "
                "error = 'the same article as another one in this collection' WHERE id = ?",
                (item["id"],),
            )
            self.report.skipped(item["url"], "the same article as another one")
            return
        taken.add(path)
        record = {
            "url": final,
            "asked": item["url"],
            "path": path,
            "html": prepared.html,
            "text": prepared.text,
            "resources": prepared.resources,
        }
        (self.items_dir / f"{item['id']}.json").write_text(json.dumps(record), "utf-8")
        self.db.execute(
            "UPDATE feed_items SET state = 'saved', path = ?, content = '', error = NULL "
            "WHERE id = ?",
            (path, item["id"]),
        )
        self.saved_now += 1

    def _retire_old(self) -> None:
        """Keep only the newest articles, when the collection has a limit."""
        keep = int(self.feed["keep"] or 0)
        if keep <= 0:
            return
        old = self.db.all(
            "SELECT id FROM feed_items WHERE feed_id = ? AND state = 'saved' "
            "ORDER BY published DESC, id DESC LIMIT -1 OFFSET ?",
            (self.feed["id"], keep),
        )
        for row in old:
            (self.items_dir / f"{row['id']}.json").unlink(missing_ok=True)
            self.db.execute(
                "UPDATE feed_items SET state = 'dropped', path = '' WHERE id = ?", (row["id"],)
            )
        self.dropped = len(old)

    # -- the ZIM ----------------------------------------------------------------------

    async def _assemble(self) -> ZimMeta:
        feed, report, crawler, stage = self.feed, self.report, self.crawler, self.stage
        items = self.db.all(
            "SELECT i.*, s.title AS source_title FROM feed_items i "
            "LEFT JOIN feed_sources s ON s.id = i.source_id "
            "WHERE i.feed_id = ? AND i.state = 'saved' ORDER BY i.published DESC, i.id DESC",
            (feed["id"],),
        )
        report.phase("assembling", len(items))
        missing_file = self.store / "missing.json"
        try:
            missing = set() if self.rebuild else set(json.loads(missing_file.read_text()))
        except (OSError, ValueError):
            missing = set()
        listed: list[dict[str, Any]] = []
        in_use: set[str] = {source["url"] for source in self.sources}
        for number, item in enumerate(items, 1):
            report.check()
            try:
                record = json.loads((self.items_dir / f"{item['id']}.json").read_text("utf-8"))
            except (OSError, ValueError):
                # The saved copy is gone: ask for the article again next time.
                self.db.execute(
                    "UPDATE feed_items SET state = 'new', tries = 0 WHERE id = ?", (item["id"],)
                )
                continue
            path, address = record["path"], record["url"]
            stage.add_page(path, item["title"], record["html"], record["text"])
            crawler.page_urls[path] = address
            for spelling in {address, record.get("asked") or address}:
                crawler.saved.setdefault(spelling, path)
                in_use.add(spelling)
            for resource, kind in record["resources"]:
                in_use.add(resource)
                if resource not in missing:
                    crawler.resources.setdefault(resource, kind)
            listed.append({**item, "address": address})
            if number % 50 == 0:
                report.progress(number)
        if not listed:
            raise BuildError("The saved articles of this collection could not be read back.")
        title = feed["title"] or self.sources[0]["title"] or "Feeds"
        stage.add_page("index", title, self._index_page(title, listed), title)
        stage.main = "index"
        site = next((s["site"] for s in self.sources if s["site"]), self.sources[0]["url"])
        crawler.icon_url = extract.normalise("/favicon.ico", site)
        wanted = set(crawler.resources)
        await crawler._resources()
        missing_file.write_text(json.dumps(sorted(missing | (wanted - set(crawler.saved)))))
        report.phase("converting", len(stage.pages))
        await crawler._localise()
        if self.dropped:
            self._prune_cache(in_use | {crawler.icon_url})
        names = [s["title"] or s["url"] for s in self.sources]
        return ZimMeta(
            name=f"custom_feed-{slugify(title)}",
            title=title,
            description=f"{len(listed)} articles from {', '.join(names)}"[:80],
            language=language_code(feed["language"]),
            creator=", ".join(names)[:100],
            source=self.sources[0]["url"],
        )

    def _index_page(self, title: str, items: list[dict[str, Any]]) -> str:
        """The first page: every article, newest first, under the month it came out."""
        parts, month = [], ""
        for item in items:
            when = time.gmtime(item["published"])
            heading = time.strftime("%B %Y", when)
            if heading != month:
                parts.append(f"{'</ul>' if month else ''}<h2>{heading}</h2><ul>")
                month = heading
            small = time.strftime("%d %b %Y", when)
            if len(self.sources) > 1 and item["source_title"]:
                small += " · " + html_text.escape(item["source_title"])
            parts.append(
                f'<li><a href="{html_text.escape(item["address"])}">'
                f"{html_text.escape(item['title'])}</a> <small>{small}</small></li>"
            )
        sources = ", ".join(
            f'<a href="{html_text.escape(s["site"] or s["url"])}">'
            f"{html_text.escape(s['title'] or s['url'])}</a>"
            for s in self.sources
        )
        return (
            f'<!doctype html><html lang="{html_text.escape(self.feed["language"] or "en")}">'
            '<head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{html_text.escape(title)}</title><style>{extract.PAGE_CSS}"
            "small{color:#71717a}li{margin:.3rem 0}</style></head><body>"
            f"<h1>{html_text.escape(title)}</h1>"
            f"<p>{len(items)} {'article' if len(items) == 1 else 'articles'} from {sources}. "
            f"Last synced {time.strftime('%d %b %Y, %H:%M UTC', time.gmtime())}.</p>"
            f"{''.join(parts)}</ul></body></html>"
        )

    def _prune_cache(self, in_use: set[str]) -> None:
        """Forget fetched files that only dropped articles used."""
        index = self.fetcher._index
        for address in [a for a in index if a not in in_use and index[a]["url"] not in in_use]:
            del index[address]
        keep = {self.fetcher.cache_file(address).name for address in index} | {"index.json"}
        for file in self.fetcher.cache.iterdir():
            if file.name not in keep:
                file.unlink(missing_ok=True)


def forget(store: Path) -> None:
    """Remove everything kept for a collection that was deleted."""
    shutil.rmtree(store, ignore_errors=True)
