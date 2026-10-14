import asyncio
import json
import time

import httpx
import pytest
from libzim.reader import Archive

from zim_pro.builder.crawler import Crawler, CrawlOptions, Fetcher, FetchError
from zim_pro.builder.report import Reporter
from zim_pro.builder.writer import Stage
from zim_pro.db import Database
from zim_pro.events import EventBus
from zim_pro.feeds import parse
from zim_pro.feeds.manager import FeedManager
from zim_pro.library.archive_pool import ArchivePool
from zim_pro.library.scanner import Scanner

RSS = """<?xml version="1.0"?><rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Knot News</title><link>https://news.test/</link><language>en</language>{items}</channel></rss>"""
ITEM = """<item><title>{title}</title><link>https://news.test/{slug}</link><guid>{slug}</guid>
<pubDate>{date}</pubDate><description>Short note on {title}.</description>{extra}</item>"""


def article(title: str, words: str) -> bytes:
    return (
        f"<html><head><title>{title} | Knot News</title></head><body><nav>Menu</nav><article>"
        f"<h1>{title}</h1><p>{words}</p><img src='/pic-{title.lower()}.png' alt='{title}'>"
        "</article></body></html>"
    ).encode()


class News:
    """A news site with a feed, served in-process."""

    def __init__(self):
        self.asked: list[str] = []
        self.items = [("Bowline", "bowline", "Mon, 05 Oct 2026 10:00:00 GMT", "")]
        self.pages = {"/bowline": article("Bowline", "The bowline makes a fixed loop. " * 8)}

    def add(self, title: str, slug: str, date: str, extra: str = "", page: bool = True) -> None:
        self.items.insert(0, (title, slug, date, extra))
        if page:
            self.pages[f"/{slug}"] = article(title, f"The {slug} is a useful knot. " * 8)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.asked.append(path)
        if path == "/feed.xml":
            items = "".join(
                ITEM.format(title=t, slug=s, date=d, extra=e) for t, s, d, e in self.items
            )
            return httpx.Response(
                200,
                content=RSS.format(items=items),
                headers={"content-type": "application/rss+xml"},
            )
        if path in self.pages:
            return httpx.Response(
                200, content=self.pages[path], headers={"content-type": "text/html"}
            )
        if path.startswith("/pic-"):
            return httpx.Response(
                200, content=b"\x89PNG" + path.encode(), headers={"content-type": "image/png"}
            )
        return httpx.Response(404)


@pytest.fixture
def no_waiting(monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _seconds: real_sleep(0))
    monkeypatch.setattr("zim_pro.builder.crawler.HOST_DELAY", 0)
    monkeypatch.setattr("zim_pro.builder.crawler.SLOW_DELAY_MIN", 0)
    monkeypatch.setattr("zim_pro.builder.crawler.RATE_WAIT", 0)


def manager(tmp_path, site) -> FeedManager:
    db = Database(":memory:")
    pool = ArchivePool()
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    feeds = FeedManager(db, EventBus(), Scanner(db, pool, data), pool, data, tmp_path / "state")
    feeds.transport = httpx.MockTransport(site)
    db.set_settings({"feed_auto_sync": False})  # these tests say when to sync
    db.execute("INSERT INTO feeds (id, title, interval_minutes) VALUES (1, '', 60)")
    db.execute("INSERT INTO feed_sources (feed_id, url) VALUES (1, 'https://news.test/feed.xml')")
    return feeds


async def synced(feeds: FeedManager, **options) -> dict:
    job = feeds.sync(1, **options)
    deadline = time.monotonic() + 30  # by the clock: sleeping is switched off here
    while time.monotonic() < deadline:
        job = feeds.get(job["id"])
        if job["state"] not in ("queued", "processing", "paused"):
            return job
        await asyncio.sleep(0.02)
    raise AssertionError("the sync did not finish")


def states(feeds: FeedManager) -> dict[str, str]:
    rows = feeds.db.all("SELECT guid, state FROM feed_items")
    return {row["guid"].rsplit("/", 1)[-1]: row["state"] for row in rows}


def test_rss_and_atom_are_read_and_pages_say_where_their_feeds_are():
    rss = RSS.format(
        items=ITEM.format(
            title="A",
            slug="a",
            date="Mon, 05 Oct 2026 10:00:00 GMT",
            extra="<content:encoded><![CDATA[<p>The whole article.</p>]]></content:encoded>",
        )
    )
    feed = parse.parse(rss.encode(), "https://news.test/feed.xml")
    assert feed.title == "Knot News" and feed.site == "https://news.test/"
    assert [(e.url, e.title, e.html) for e in feed.entries] == [
        ("https://news.test/a", "A", "<p>The whole article.</p>")
    ]
    assert time.gmtime(feed.entries[0].published)[:3] == (2026, 10, 5)
    atom = (
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom News</title>'
        '<entry><title>B</title><link href="/b"/><id>tag:b</id>'
        "<updated>2026-10-06T00:00:00Z</updated>"
        "<summary>About B</summary></entry></feed>"
    )
    entry = parse.parse(atom.encode(), "https://atom.test/feed").entries[0]
    assert (entry.url, entry.guid, entry.html) == (
        "https://atom.test/b",
        "https://atom.test/b",
        "About B",
    )
    with pytest.raises(parse.NotAFeed):
        parse.parse(b"<html><body>Hello</body></html>", "https://news.test/")
    page = (
        '<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml">'
        "</head></html>"
    )
    assert parse.discover(page, "https://news.test/blog/") == ["https://news.test/feed.xml"]


def test_opml_goes_out_and_comes_back():
    text = parse.write_opml(
        [("Tech", [("Knot News", "https://news.test/feed.xml", "https://news.test/")])]
    )
    assert parse.read_opml(text) == [("Tech", [("Knot News", "https://news.test/feed.xml")])]
    loose = '<opml><body><outline text="X" xmlUrl="https://x.test/rss"/></body></opml>'
    assert parse.read_opml(loose) == [("", [("X", "https://x.test/rss")])]


async def test_a_sync_only_fetches_what_is_new_and_keeps_the_zim_current(tmp_path, no_waiting):
    site = News()
    feeds = manager(tmp_path, site)
    await feeds.start()
    try:
        first = await synced(feeds)
        assert first["state"] == "finished", first["error"]
        assert first["report"]["new"] == 1 and first["report"]["articles"] == 1
        feed = feeds.db.one("SELECT * FROM feeds WHERE id = 1")
        assert feed["title"] == "Knot News" and feed["book_id"] == "custom_feed-knot-news"
        book = feeds.db.one("SELECT * FROM books WHERE id = ?", (feed["book_id"],))
        assert book["category"] == "custom" and book["article_count"] == 2  # index + article

        # Nothing new: the feed is looked at, no article is asked for, the ZIM is left alone.
        site.asked.clear()
        written = (tmp_path / "data" / book["filename"]).stat().st_mtime_ns
        second = await synced(feeds)
        assert second["report"]["unchanged"] and second["report"]["new"] == 0
        assert site.asked == ["/feed.xml"]
        assert (tmp_path / "data" / book["filename"]).stat().st_mtime_ns == written

        # One new article: only it and its picture are fetched; the old one comes from disk.
        site.add("Clove", "clove", "Tue, 06 Oct 2026 10:00:00 GMT")
        site.asked.clear()
        third = await synced(feeds)
        assert third["report"]["new"] == 1 and third["report"]["articles"] == 2
        assert "/bowline" not in site.asked and "/pic-bowline.png" not in site.asked
        assert "/clove" in site.asked and "/pic-clove.png" in site.asked
        archive = Archive(str(tmp_path / "data" / book["filename"]))
        index = bytes(archive.get_entry_by_path("index").get_item().content).decode()
        assert index.index("Clove") < index.index("Bowline")  # newest first
        assert 'href="news.test/clove"' in index and "October 2026" in index
        page = bytes(archive.get_entry_by_path("news.test/clove").get_item().content).decode()
        assert "useful knot" in page and "Menu" not in page and 'src="pic-clove.png"' in page
        assert archive.has_entry_by_path("news.test/pic-bowline.png")
    finally:
        await feeds.stop()


async def test_feed_text_stands_in_when_the_page_cannot_be_had(tmp_path, no_waiting):
    site = News()
    whole = (
        "<content:encoded><![CDATA[<p>"
        + "The sheet bend joins two ropes. " * 10
        + "</p>]]></content:encoded>"
    )
    site.add("Sheet bend", "sheet-bend", "Wed, 07 Oct 2026 10:00:00 GMT", whole, page=False)
    site.add("Ghost", "ghost", "Wed, 07 Oct 2026 11:00:00 GMT", page=False)
    site.pages["/ghost"] = b""  # placeholder removed below: the page is a 404
    del site.pages["/ghost"]
    feeds = manager(tmp_path, site)
    feeds.db.execute("UPDATE feeds SET keep = 2")
    await feeds.start()
    try:
        job = await synced(feeds)
        assert job["state"] == "finished", job["error"]
        # Three in the feed, two kept: the two newest. Both pages are 404, so the feed's own
        # text is what was saved, the full one and the one-line one alike.
        assert states(feeds) == {"ghost": "saved", "sheet-bend": "saved", "bowline": "dropped"}
        assert "/bowline" not in site.asked
        book = feeds.db.one("SELECT * FROM books")
        archive = Archive(book["path"])
        page = bytes(archive.get_entry_by_path("news.test/sheet-bend").get_item().content).decode()
        assert "joins two ropes" in page

        # A newer article pushes the oldest kept one out of the ZIM.
        site.add("Reef", "reef", "Thu, 08 Oct 2026 10:00:00 GMT")
        job = await synced(feeds)
        assert job["report"]["dropped"] == 1 and job["report"]["articles"] == 2
        assert states(feeds)["sheet-bend"] == "dropped" and states(feeds)["reef"] == "saved"
        archive = Archive(feeds.db.one("SELECT path FROM books")["path"])
        assert not archive.has_entry_by_path("news.test/sheet-bend")
    finally:
        await feeds.stop()


async def test_a_feed_that_is_not_one_fails_with_the_reason(tmp_path, no_waiting):
    feeds = manager(
        tmp_path,
        lambda request: httpx.Response(
            200, content=b"<html>hi</html>", headers={"content-type": "text/html"}
        ),
    )
    await feeds.start()
    try:
        job = await synced(feeds)
        assert job["state"] == "failed" and "RSS or Atom" in job["error"]
        assert "RSS or Atom" in feeds.db.one("SELECT last_error FROM feeds")["last_error"]
    finally:
        await feeds.stop()


def test_only_collections_whose_time_has_come_are_due(tmp_path):
    feeds = manager(tmp_path, News())
    assert [f["id"] for f in feeds.due()] == [1]  # never synced
    feeds.db.execute("UPDATE feeds SET last_attempt = unixepoch() - 600")
    assert feeds.due() == []  # ten minutes into an hour
    feeds.db.execute("UPDATE feeds SET last_attempt = unixepoch() - 3700")
    assert [f["id"] for f in feeds.due()] == [1]
    feeds.sync(1)
    assert feeds.due() == []  # already queued
    feeds.db.execute("DELETE FROM builds")
    feeds.db.execute("UPDATE feeds SET enabled = 0")
    assert feeds.due() == []
    feeds.db.execute("UPDATE feeds SET enabled = 1, interval_minutes = 0")
    assert feeds.due() == []  # only when asked


# -- what the fetcher gained ------------------------------------------------------------


async def test_a_rate_limit_spaces_requests_out(tmp_path):
    seen: list[float] = []

    def site(request: httpx.Request) -> httpx.Response:
        seen.append(time.monotonic())
        return httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})

    fetcher = Fetcher(tmp_path, 4, httpx.MockTransport(site), pace=1 / 20)  # 20 a second
    await asyncio.gather(*(fetcher.get(f"https://a.test/{n}", 1000) for n in range(5)))
    await fetcher.close()
    gaps = [later - earlier for earlier, later in zip(seen, seen[1:], strict=False)]
    assert len(seen) == 5 and min(gaps) >= 0.045


CHALLENGE = httpx.Response(
    403,
    content=b"<html><title>Just a moment...</title></html>",
    headers={"content-type": "text/html"},
)


async def test_a_browser_check_is_passed_to_the_solver_and_its_clearance_reused(
    tmp_path, no_waiting
):
    asked: list[tuple[str, str]] = []

    def web(request: httpx.Request) -> httpx.Response:
        if request.url.host == "solver.test":
            body = json.loads(request.content)
            asked.append(("solver", body["url"]))
            assert body["cmd"] == "request.get"
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "solution": {
                        "url": body["url"],
                        "status": 200,
                        "response": "<html><body>Solved page</body></html>",
                        "cookies": [
                            {"name": "cf_clearance", "value": "abc", "domain": "guarded.test"}
                        ],
                        "userAgent": "Mozilla/5.0 Solver",
                    },
                },
            )
        asked.append(("site", request.url.path))
        cleared = (
            "cf_clearance=abc" in request.headers.get("cookie", "")
            and request.headers["user-agent"] == "Mozilla/5.0 Solver"
        )
        if request.url.path == "/strict":  # checks every request, cleared or not
            return httpx.Response(403, headers={"cf-mitigated": "challenge"})
        if not cleared:
            return CHALLENGE
        return httpx.Response(200, content=b"real bytes", headers={"content-type": "image/png"})

    fetcher = Fetcher(tmp_path, 2, httpx.MockTransport(web), solver="http://solver.test:8191/")
    first = await fetcher.get("https://guarded.test/pic.png", 10000)
    assert first.file.read_bytes() == b"real bytes" and first.content_type == "image/png"
    await fetcher.get("https://guarded.test/other.png", 10000)
    assert [who for who, _ in asked].count("solver") == 1  # the clearance carried over
    strict = await fetcher.get("https://guarded.test/strict", 10000)
    assert strict.is_html and "Solved page" in strict.file.read_text()
    assert fetcher.solved == 2
    await fetcher.close()


async def test_a_browser_check_without_a_solver_says_what_to_do(tmp_path, no_waiting):
    fetcher = Fetcher(tmp_path, 2, httpx.MockTransport(lambda request: CHALLENGE))
    with pytest.raises(FetchError, match="captcha solver in Settings"):
        await fetcher.get("https://guarded.test/", 10000)
    await fetcher.close()


async def test_given_sitemaps_and_robots_are_used_in_place_of_the_usual_ones(tmp_path, no_waiting):
    asked: list[str] = []
    pages = {
        "/rules.txt": ("text/plain", b"User-agent: *\nDisallow: /no"),
        "/maps/one.xml": (
            "application/xml",
            b"<urlset><url><loc>https://s.test/a</loc></url><url><loc>https://s.test/no</loc></url></urlset>",
        ),
        "/maps/two.xml": (
            "application/xml",
            b"<urlset><url><loc>https://s.test/b</loc></url><url><loc>https://s.test/a</loc></url></urlset>",
        ),
        "/": (
            "text/html",
            b"<html><body><article><h1>Home</h1><p>Home page.</p></article></body></html>",
        ),
        "/a": (
            "text/html",
            b"<html><body><article><h1>A</h1><p>Page A.</p></article></body></html>",
        ),
        "/b": (
            "text/html",
            b"<html><body><article><h1>B</h1><p>Page B.</p></article></body></html>",
        ),
    }

    def site(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        if request.url.path not in pages:
            return httpx.Response(404)
        kind, body = pages[request.url.path]
        return httpx.Response(200, content=body, headers={"content-type": kind})

    options = CrawlOptions(
        "https://s.test/",
        scope="site",
        sitemap_urls=("https://s.test/maps/one.xml", "https://s.test/maps/two.xml"),
        robots_url="https://s.test/rules.txt",
    )
    stage, report = Stage(tmp_path / "stage"), Reporter()
    crawler = Crawler(options, stage, tmp_path / "cache", report, 3, httpx.MockTransport(site))
    await crawler.run()
    assert sorted(stage.pages) == ["s.test/a", "s.test/b", "s.test/index"]
    assert asked[:3] == ["/rules.txt", "/maps/one.xml", "/maps/two.xml"]
    assert "/robots.txt" not in asked and "/sitemap.xml" not in asked and "/no" not in asked
    assert asked.count("/a") == 1 and report.skipped_count == 1


# -- a site's own feeds, on a link ------------------------------------------------------


async def test_a_links_feeds_add_new_articles_without_crawling_the_site_again(tmp_path, no_waiting):
    from zim_pro.builder.jobs import BuildManager

    asked: list[str] = []
    posts = ["one", "two"]

    def page(name: str, body: str = "") -> bytes:
        text = f"<p>All about {name}, at some length for the reader.</p>"
        return f"<html><body><article><h1>{name}</h1>{text}{body}</article></body></html>".encode()

    def site(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        asked.append(path)
        if path == "/":
            return httpx.Response(
                200,
                content=page("Home", '<a href="/one">One</a><img src="/logo.png" alt="Logo">'),
                headers={"content-type": "text/html"},
            )
        if path == "/rss":
            items = "".join(
                ITEM.format(title=p, slug=p, date="Mon, 05 Oct 2026 10:00:00 GMT", extra="")
                for p in posts
            )
            feed = RSS.format(items=items).replace("news.test", "blog.test")
            return httpx.Response(200, content=feed, headers={"content-type": "text/xml"})
        if path.strip("/") in posts:
            return httpx.Response(
                200, content=page(path.strip("/")), headers={"content-type": "text/html"}
            )
        if path == "/logo.png":
            return httpx.Response(
                200, content=b"\x89PNGlogo", headers={"content-type": "image/png"}
            )
        return httpx.Response(404)

    db = Database(":memory:")
    pool = ArchivePool()
    data = tmp_path / "data"
    data.mkdir()
    builds = BuildManager(db, EventBus(), Scanner(db, pool, data), pool, data, tmp_path / "state")
    builds.transport = httpx.MockTransport(site)
    db.execute(
        "INSERT INTO links (id, url, scope, feeds, feed_minutes) "
        "VALUES (1, 'https://blog.test/', 'site', 'https://blog.test/rss', 30)"
    )

    async def built(**params) -> dict:
        job = builds.enqueue("link", 1, "Blog", params)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            job = builds.get(job["id"])
            if job["state"] not in ("queued", "processing", "paused"):
                return job
            await asyncio.sleep(0.02)
        raise AssertionError("the build did not finish")

    def pages() -> list[str]:
        archive = Archive(db.one("SELECT path FROM books")["path"])
        return sorted(
            p
            for p in ("index", "one", "two", "three")
            if archive.has_entry_by_path(f"blog.test/{p}")
        )

    await builds.start()
    try:
        # The full build: "two" is linked from nowhere, and is saved because the feed lists it.
        first = await built()
        assert first["state"] == "finished", first["error"]
        assert pages() == ["index", "one", "two"] and first["report"]["from_feeds"] == 2
        built_at = db.one("SELECT last_built FROM links")["last_built"]

        # Nothing new in the feed: only the feed is looked at, and the ZIM is left alone.
        asked.clear()
        quiet = await built(rss=True)
        assert quiet["report"]["unchanged"] and set(asked) <= {"/rss", "/robots.txt"}

        # A new article: it is fetched; the pages saved before come from the cache.
        posts.insert(0, "three")
        asked.clear()
        update = await built(rss=True)
        assert update["state"] == "finished", update["error"]
        assert update["report"]["rss"] and update["report"]["from_feeds"] == 1
        assert pages() == ["index", "one", "three", "two"]
        assert "/three" in asked and "/one" not in asked and "/two" not in asked
        assert "/logo.png" not in asked and "/sitemap.xml" not in asked
        # The full rebuild is still due when it was: the site itself was not crawled again.
        assert db.one("SELECT last_built FROM links")["last_built"] == built_at
    finally:
        await builds.stop()


OPML = """<?xml version="1.0"?><opml version="2.0"><body>
<outline text="Loose" xmlUrl="https://loose.test/rss"/>
<outline text="Tech"><outline text="Knots" xmlUrl="https://knots.test/feed"/>
  <outline text="Ropes" xmlUrl="https://ropes.test/feed"/></outline>
<outline text="News"><outline text="Daily" xmlUrl="https://daily.test/atom"/></outline>
</body></opml>"""


@pytest.fixture
def api(tmp_path):
    from fastapi.testclient import TestClient

    from zim_pro.config import Config
    from zim_pro.main import create_app

    return TestClient(create_app(Config(data_dir=tmp_path / "data", state_dir=tmp_path / "state")))


def test_opml_preview_shows_folders_and_adds_nothing(api):
    reply = api.post("/api/feeds/opml", json={"text": OPML, "preview": True}).json()
    assert [(f["name"], len(f["feeds"])) for f in reply["folders"]] == [
        ("Imported feeds", 1),
        ("Tech", 2),
        ("News", 1),
    ]
    assert api.get("/api/feeds").json()["feeds"] == []


def test_opml_import_takes_the_chosen_folders_with_a_category(api):
    reply = api.post(
        "/api/feeds/opml", json={"text": OPML, "folders": [1, 2], "category": "Reading"}
    ).json()
    assert reply["added"] == 3 and reply["collections"] == 2 and reply["syncing"] == 0
    feeds = {f["title"]: f for f in reply["feeds"]}
    assert set(feeds) == {"Tech", "News"} and feeds["Tech"]["category"] == "Reading"
    # Seen again, the feeds are known, and importing them twice adds nothing.
    again = api.post("/api/feeds/opml", json={"text": OPML, "preview": True}).json()
    assert [feed["known"] for feed in again["folders"][1]["feeds"]] == [True, True]
    assert again["folders"][1]["exists"]
    assert api.post("/api/feeds/opml", json={"text": OPML, "folders": [1]}).json()["added"] == 0


def test_opml_import_can_put_everything_in_one_collection(api):
    reply = api.post("/api/feeds/opml", json={"text": OPML, "single": "Everything"}).json()
    assert [(f["title"], len(f["sources"])) for f in reply["feeds"]] == [("Everything", 4)]
    assert api.post("/api/feeds/opml", json={"text": OPML, "folders": []}).status_code == 422
    assert api.post("/api/feeds/opml", json={"text": "<opml/>"}).status_code == 422
