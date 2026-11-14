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

    app = create_app(
        Config(
            data_dir=tmp_path / "data",
            state_dir=tmp_path / "state",
            catalog_url="http://127.0.0.1:0/catalog.xml",
        )
    )
    with TestClient(app) as client:
        yield client


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


# -- r1 review fixes ---------------------------------------------------------------------


def _make_sync(tmp_path, site, *, keep: int = 0, full_text: bool = True, workers: int = 4):
    from zim_pro.feeds.sync import FeedSync

    db = Database(":memory:")
    db.execute(
        "INSERT INTO feeds (id, title, interval_minutes, keep, full_text) VALUES (1, '', 60, ?, ?)",
        (keep, int(full_text)),
    )
    db.execute("INSERT INTO feed_sources (feed_id, url) VALUES (1, 'https://news.test/feed.xml')")
    feed = db.one("SELECT * FROM feeds WHERE id = 1")
    stage = Stage(tmp_path / "stage")
    report = Reporter()
    sync = FeedSync(
        db,
        feed,
        tmp_path / "store",
        stage,
        report,
        workers=workers,
        transport=httpx.MockTransport(site),
    )
    return db, sync


def test_f1_f17_parse_malformed_url_long_guid_and_identityless():
    bad_rss = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
        "<item><title>Good</title><link>https://news.test/ok</link></item>"
        "<item><title>BadUrl</title><link>http://[bad/</link></item>"
        "<item><title>NoId</title></item>"
        f"<item><title>L1</title><guid>{'x' * 1005}a</guid></item>"
        f"<item><title>L2</title><guid>{'x' * 1005}b</guid></item>"
        "</channel></rss>"
    )
    parsed = parse.parse(bad_rss.encode(), "https://news.test/feed.xml")
    # BadUrl has no link (normalise -> "") and no id, so both BadUrl and NoId are skipped.
    assert [e.title for e in parsed.entries] == ["Good", "L1"]
    assert parsed.skipped == 2


def test_f1_opml_and_check_malformed_url_return_422(api):
    bad_opml = '<opml><body><outline text="Bad" xmlUrl="http://[bad/"/></body></opml>'
    assert api.post("/api/feeds/opml", json={"text": bad_opml}).status_code == 422
    assert api.post("/api/feeds/check", json={"url": "http://[bad/"}).status_code == 422


def test_f10_opml_namespace_nested_folders_utc_and_duplicate_export(api):
    ns_opml = (
        '<?xml version="1.0"?>'
        '<opml xmlns="http://opml.org/spec2" version="2.0">'
        "<body>"
        '<outline text="Tech">'
        '  <outline text="Linux" xmlUrl="https://linux.test/rss"/>'
        '  <outline text="AI">'
        '    <outline text="ML" xmlUrl="https://ml.test/rss"/>'
        "  </outline>"
        "</outline>"
        "</body></opml>"
    )
    folders = parse.read_opml(ns_opml)
    assert folders == [
        ("Tech", [("Linux", "https://linux.test/rss")]),
        ("AI", [("ML", "https://ml.test/rss")]),
    ]
    exported = parse.write_opml([])
    assert "GMT</dateCreated>" in exported

    # Duplicate collection titles get disambiguated on export.
    api.post("/api/feeds", json={"title": "Dup", "sources": "https://a.test/rss", "enabled": False})
    api.post("/api/feeds", json={"title": "Dup", "sources": "https://b.test/rss", "enabled": False})
    opml_out = api.get("/api/feeds/opml").text
    exported_folders = [name for name, _ in parse.read_opml(opml_out)]
    assert exported_folders == ["Dup", "Dup (2)"]


async def test_f2_f3_keep_respects_saved_count_reports_dropped_and_revives(tmp_path, no_waiting):
    site = News()
    site.add("Two", "two", "Tue, 06 Oct 2026 10:00:00 GMT")
    site.add("Three", "three", "Wed, 07 Oct 2026 10:00:00 GMT")
    db, sync = _make_sync(tmp_path / "r1", site, keep=2)
    meta, out1 = await sync.run()
    assert meta is not None and out1["new"] == 2 and out1["dropped"] == 1

    # Add 3 more articles while 2 are already saved and keep=2: only 2 fetched, 3 dropped total.
    site.asked.clear()
    site.add("Four", "four", "Thu, 08 Oct 2026 10:00:00 GMT")
    site.add("Five", "five", "Fri, 09 Oct 2026 10:00:00 GMT")
    site.add("Six", "six", "Sat, 10 Oct 2026 10:00:00 GMT")
    db.execute("UPDATE feeds SET keep = 2")
    feed = db.one("SELECT * FROM feeds WHERE id = 1")
    from zim_pro.feeds.sync import FeedSync

    sync2 = FeedSync(
        db, feed, tmp_path / "r1" / "store", Stage(tmp_path / "s2"), Reporter(),
        transport=httpx.MockTransport(site),
    )
    _, out2 = await sync2.run()
    assert out2["new"] == 2 and out2["dropped"] == 3
    assert "/four" not in site.asked

    # Raising keep to 0 revives all previously dropped articles.
    db.execute("UPDATE feeds SET keep = 0")
    feed = db.one("SELECT * FROM feeds WHERE id = 1")
    sync3 = FeedSync(
        db, feed, tmp_path / "r1" / "store", Stage(tmp_path / "s3"), Reporter(),
        transport=httpx.MockTransport(site),
    )
    _, out3 = await sync3.run()
    assert out3["articles"] == 6 and out3["new"] == 4


async def test_f4_language_persists_when_declaring_source_fails(tmp_path, no_waiting):
    from zim_pro.feeds.sync import FeedSync

    fail_fr = False

    def site(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fr.xml":
            if fail_fr:
                return httpx.Response(503)
            rss = RSS.replace("<language>en</language>", "<language>fr</language>").format(
                items=ITEM.format(
                    title="Un", slug="un", date="Mon, 05 Oct 2026 10:00:00 GMT", extra=""
                )
            )
            return httpx.Response(200, content=rss, headers={"content-type": "application/rss+xml"})
        if request.url.path == "/en.xml":
            rss = RSS.format(
                items=ITEM.format(
                    title="Two", slug="two", date="Tue, 06 Oct 2026 10:00:00 GMT", extra=""
                )
            )
            return httpx.Response(200, content=rss, headers={"content-type": "application/rss+xml"})
        return httpx.Response(
            200, content=article("Art", "Body text " * 8), headers={"content-type": "text/html"}
        )

    db = Database(":memory:")
    db.execute("INSERT INTO feeds (id, title, interval_minutes) VALUES (1, '', 60)")
    db.execute("INSERT INTO feed_sources (feed_id, url) VALUES (1, 'https://news.test/fr.xml')")
    db.execute("INSERT INTO feed_sources (feed_id, url) VALUES (1, 'https://news.test/en.xml')")
    sync1 = FeedSync(
        db, db.one("SELECT * FROM feeds WHERE id = 1"), tmp_path / "store",
        Stage(tmp_path / "s1"), Reporter(), transport=httpx.MockTransport(site),
    )
    meta1, _ = await sync1.run()
    assert meta1 is not None and meta1.language == "fra"
    assert db.one("SELECT language FROM feeds WHERE id = 1")["language"] == "fr"

    fail_fr = True
    sync2 = FeedSync(
        db, db.one("SELECT * FROM feeds WHERE id = 1"), tmp_path / "store",
        Stage(tmp_path / "s2"), Reporter(), rebuild=True, transport=httpx.MockTransport(site),
    )
    meta2, _ = await sync2.run()
    assert meta2 is not None and meta2.language == "fra"


def test_f5_concurrent_sync_enqueues_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    feeds = manager(tmp_path, News())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: _try_sync(feeds, 1), range(4)))
    assert sum(1 for r in results if r is not None) == 1
    assert feeds.db.scalar("SELECT COUNT(*) FROM builds WHERE kind = 'feed' AND source_id = 1") == 1


def _try_sync(feeds: FeedManager, feed_id: int):
    from zim_pro.builder.writer import BuildError

    try:
        return feeds.sync(feed_id)
    except BuildError:
        return None


def test_f6_f14_f18_f19_api_delete_retry_next_sync_and_like_escape(api):
    created = api.post(
        "/api/feeds",
        json={"title": "News", "sources": "https://news.test/feed.xml", "enabled": True},
        params={"hold": "true"},
    ).json()
    feed_id = created["id"]
    # F18: never-synced feed has next_sync <= now (due immediately).
    assert created["feeds"][0]["next_sync"] <= time.time() + 1

    # F14: unknown feed -> 404; busy feed -> 409 with failed item unchanged.
    assert api.post("/api/feeds/9999/retry-failed").status_code == 404

    ctx = api.app.state.ctx
    ctx.db.execute(
        "INSERT INTO feed_items (feed_id, guid, title, state, tries) "
        "VALUES (?, 'g1', '100% Real', 'failed', 3)",
        (feed_id,),
    )
    ctx.db.execute(
        "INSERT INTO feed_items (feed_id, guid, title, state) VALUES (?, 'g2', 'Other', 'saved')",
        (feed_id,),
    )
    # F19: LIKE wildcard % is escaped.
    items = api.get(f"/api/feeds/{feed_id}/items", params={"q": "%"}).json()["items"]
    assert [i["title"] for i in items] == ["100% Real"]

    # Active build -> retry-failed returns 409 without mutating item; delete returns 409.
    ctx.db.execute(
        "INSERT INTO builds (kind, source_id, title, state) VALUES ('feed', ?, 'News', 'queued')",
        (feed_id,),
    )
    assert api.post(f"/api/feeds/{feed_id}/retry-failed").status_code == 409
    assert ctx.db.one("SELECT state FROM feed_items WHERE guid = 'g1'")["state"] == "failed"
    assert api.delete(f"/api/feeds/{feed_id}").status_code == 409

    # Clear active build -> delete removes feed and cascades items/sources.
    ctx.db.execute("DELETE FROM builds WHERE source_id = ?", (feed_id,))
    assert api.delete(f"/api/feeds/{feed_id}").status_code == 200
    assert ctx.db.scalar("SELECT COUNT(*) FROM feed_items WHERE feed_id = ?", (feed_id,)) == 0
    assert ctx.db.scalar("SELECT COUNT(*) FROM feed_sources WHERE feed_id = ?", (feed_id,)) == 0


async def test_f7_article_write_oserror_and_sibling_cancellation(tmp_path, no_waiting, monkeypatch):
    from pathlib import Path

    site = News()
    site.add("Two", "two", "Tue, 06 Oct 2026 10:00:00 GMT")
    site.add("Three", "three", "Wed, 07 Oct 2026 10:00:00 GMT")
    _, sync = _make_sync(tmp_path, site)
    real_write_text = Path.write_text

    def fail_one_write(self: Path, *args, **kwargs):
        if self.name == "2.json":
            raise OSError("disk full")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_one_write)
    _, outcome = await sync.run()
    assert outcome["new"] == 2
    assert sync.db.one("SELECT state, error FROM feed_items WHERE id = 2")["state"] == "failed"


async def test_f8_missing_item_record_refetched_instead_of_unchanged(tmp_path, no_waiting):
    from zim_pro.feeds.sync import FeedSync

    site = News()
    db, sync = _make_sync(tmp_path, site)
    await sync.run()
    db.execute(
        "INSERT INTO books (id, title, filename, path, size, mtime) "
        "VALUES ('b1', 'Knot News', 'b1.zim', ?, 100, 0)",
        (str(tmp_path / "b1.zim"),),
    )
    db.execute("UPDATE feeds SET book_id = 'b1'")
    # Delete the item JSON on disk.
    (tmp_path / "store" / "items" / "1.json").unlink()
    site.asked.clear()
    sync2 = FeedSync(
        db, db.one("SELECT * FROM feeds WHERE id = 1"), tmp_path / "store",
        Stage(tmp_path / "s2"), Reporter(), transport=httpx.MockTransport(site),
    )
    meta, outcome = await sync2.run()
    assert meta is not None and outcome["new"] == 1
    assert (tmp_path / "store" / "items" / "1.json").is_file()
    assert "news.test/bowline" in sync2.stage.pages


def test_f9_scheduler_respects_feed_auto_sync(tmp_path):
    from types import SimpleNamespace

    from zim_pro import scheduler
    from zim_pro.builder.jobs import BuildManager

    db = Database(":memory:")
    pool = ArchivePool()
    data = tmp_path / "data"
    data.mkdir()
    builds = BuildManager(db, EventBus(), Scanner(db, pool, data), pool, data, tmp_path / "state")
    db.execute(
        "INSERT INTO links (id, url, scope, feeds, feed_minutes, engine, book_id, last_built) "
        "VALUES (1, 'https://b.test/', 'site', 'https://b.test/rss', 30, 'builtin', 'b1', 0)"
    )
    ctx = SimpleNamespace(db=db, builds=builds)

    db.set_settings({"feed_auto_sync": False})
    scheduler._check_due_feeds(ctx)
    assert db.scalar("SELECT COUNT(*) FROM builds") == 0

    db.set_settings({"feed_auto_sync": True})
    scheduler._check_due_feeds(ctx)
    assert db.scalar("SELECT COUNT(*) FROM builds") == 1


def test_f11_check_rejects_oversized_response(api, monkeypatch):
    from zim_pro.api import feeds as api_feeds

    monkeypatch.setattr(api_feeds, "CHECK_MAX_BYTES", 100)

    def big_transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 500, headers={"content-type": "text/xml"})

    real_client = httpx.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(big_transport)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_client)
    res = api.post("/api/feeds/check", json={"url": "https://big.test/feed.xml"})
    assert res.status_code == 422


async def test_f13_skips_carried_prepare_when_page_is_long(
    tmp_path, no_waiting, monkeypatch
):
    from zim_pro.builder import extract

    calls = 0
    real_prepare = extract.prepare

    def counting_prepare(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_prepare(*args, **kwargs)

    monkeypatch.setattr(extract, "prepare", counting_prepare)
    site = News()
    _, sync = _make_sync(tmp_path, site)
    await sync.run()
    assert calls == 1  # only the fetched page, not the short feed description


async def test_f15_schedule_continues_after_deleted_due_feed(tmp_path):
    feeds = manager(tmp_path, News())
    feeds.db.execute("INSERT INTO feeds (id, title, interval_minutes) VALUES (2, 'Two', 60)")
    feeds.db.execute("INSERT INTO feed_sources (feed_id, url) VALUES (2, 'https://two.test/rss')")
    due = feeds.due()
    assert [f["id"] for f in due] == [1, 2]
    # Simulate feed 1 being deleted right after due() returned it.
    feeds.db.execute("DELETE FROM feeds WHERE id = 1")
    from zim_pro.builder.writer import BuildError

    for feed in due:
        try:
            feeds.sync(feed["id"], trigger="schedule")
        except BuildError:
            continue
    assert feeds.db.scalar("SELECT COUNT(*) FROM builds WHERE source_id = 2") == 1


async def test_f16_skipped_duplicate_url_revives_when_original_retires(tmp_path, no_waiting):
    def site(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/feed.xml":
            rss = RSS.format(
                items=(
                    "<item><title>B</title><link>https://news.test/same</link>"
                    "<pubDate>Tue, 06 Oct 2026 10:00:00 GMT</pubDate></item>"
                    "<item><title>A</title><link>http://news.test/same</link>"
                    "<pubDate>Mon, 05 Oct 2026 10:00:00 GMT</pubDate></item>"
                )
            )
            return httpx.Response(200, content=rss, headers={"content-type": "application/rss+xml"})
        return httpx.Response(
            200, content=article("Same", "Enough text " * 8), headers={"content-type": "text/html"}
        )

    db2, sync2 = _make_sync(tmp_path / "k2", site, keep=2, workers=1)
    await sync2.run()
    row = db2.one("SELECT state FROM feed_items WHERE guid = 'http://news.test/same'")
    assert row["state"] == "skipped"
    # Insert a newer saved item and retire the https item with keep=1.
    db2.execute(
        "INSERT INTO feed_items (feed_id, guid, title, published, state, path) "
        "VALUES (1, 'https://news.test/newer', 'Newer', 9999999999, 'saved', 'news.test/newer')"
    )
    db2.execute("UPDATE feeds SET keep = 1")
    sync2.feed = db2.one("SELECT * FROM feeds WHERE id = 1")
    sync2._retire_old()
    row = db2.one("SELECT state FROM feed_items WHERE guid = 'http://news.test/same'")
    assert row["state"] == "new"


async def test_f18_total_outage_with_book_raises_build_error(tmp_path, no_waiting):
    from zim_pro.builder.writer import BuildError

    db, sync = _make_sync(tmp_path, lambda _req: httpx.Response(500))
    db.execute(
        "INSERT INTO books (id, title, filename, path, size, mtime) "
        "VALUES ('b1', 'T', 'b1.zim', '', 100, 0)"
    )
    db.execute("UPDATE feeds SET book_id = 'b1'")
    db.execute(
        "INSERT INTO feed_items (feed_id, guid, title, state, path) "
        "VALUES (1, 'g1', 'Old', 'saved', 'news.test/old')"
    )
    sync.feed = db.one("SELECT * FROM feeds WHERE id = 1")
    with pytest.raises(BuildError, match="Could not read"):
        await sync.run()
