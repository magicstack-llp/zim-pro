import asyncio
import gzip
import io
import json
import threading
import time
import zipfile

import httpx
import pytest
from libzim.reader import Archive
from libzim.search import Query, Searcher

from zim_pro.builder import extract, sitemap
from zim_pro.builder.convert import epub, pdf
from zim_pro.builder.crawler import Crawler, CrawlOptions
from zim_pro.builder.jobs import BuildManager
from zim_pro.builder.report import Cancelled, Reporter
from zim_pro.builder.writer import BuildError, Stage, ZimMeta, verify, write_zim
from zim_pro.db import Database
from zim_pro.events import EventBus
from zim_pro.library.archive_pool import ArchivePool
from zim_pro.library.scanner import Scanner, read_book

PNG = b"\x89PNG\r\n\x1a\n" + b"picture-bytes"


def search(archive: Archive, words: str) -> list[str]:
    found = Searcher(archive).search(Query().set_query(words))
    return list(found.getResults(0, 10))


# -- writing --------------------------------------------------------------------------


def staged(tmp_path) -> Stage:
    stage = Stage(tmp_path / "stage")
    stage.add_page(
        "index", "Home", "<html><body><nav>MENUWORD</nav><h1>Home</h1></body></html>", "Home"
    )
    stage.add_page("a", "Solar still", "<html><body>x</body></html>", "A still distills water")
    stage.add_resource("img/a.png", "image/png", PNG)
    stage.add_resource("img/copy.png", "image/png", PNG)
    stage.add_redirect("old", "Old address", "a")
    stage.main = "index"
    return stage


def test_written_zim_holds_what_was_staged_and_is_a_valid_book(tmp_path):
    meta = ZimMeta("custom_test", "A test book", "About tests", source="https://example.org")
    target = tmp_path / meta.filename
    report = write_zim(staged(tmp_path), meta, target)
    assert report["pages"] == 2 and report["files"] == 2 and report["searchable"]
    assert report["deduplicated"] == 1  # the same picture twice is stored once
    assert not target.with_name(target.name + ".tmp").exists()

    archive = Archive(str(target))
    # Search matches the text given for indexing, not the page's markup or menus.
    assert search(archive, "distills") == ["a"] and search(archive, "MENUWORD") == []
    assert archive.article_count == 2  # pictures are not articles
    assert bytes(archive.get_entry_by_path("img/copy.png").get_item().content) == PNG
    assert archive.get_entry_by_path("old").get_redirect_entry().path == "a"
    assert archive.main_entry.get_redirect_entry().path == "index"

    book = read_book(target)
    assert book["id"] == "custom_test" and book["title"] == "A test book"
    assert book["has_illustration"] and book["has_ftindex"] and book["error"] is None
    assert "_category:custom" in book["tags"]


def test_nothing_to_write_is_an_error_and_leaves_no_file(tmp_path):
    target = tmp_path / "empty.zim"
    with pytest.raises(BuildError, match="nothing to put"):
        write_zim(Stage(tmp_path / "stage"), ZimMeta("custom_x", "X"), target)
    assert list(tmp_path.glob("*.zim*")) == []


def test_a_library_folder_that_cannot_be_written_to_is_named(tmp_path):
    stage = staged(tmp_path)
    library = tmp_path / "library"
    library.mkdir()
    library.chmod(0o555)
    try:
        with pytest.raises(BuildError, match="cannot be written to by the user"):
            write_zim(stage, ZimMeta("custom_test", "T"), library / "t.zim")
    finally:
        library.chmod(0o755)


def test_verify_notices_a_page_that_went_missing(tmp_path):
    stage = staged(tmp_path)
    meta = ZimMeta("custom_test", "T")
    write_zim(stage, meta, tmp_path / meta.filename)
    stage.add_page("b", "Late", "<html></html>", "added after the file was written")
    with pytest.raises(BuildError, match="2 pages but 3 were prepared"):
        verify(tmp_path / meta.filename, stage, "index")


# -- cleaning pages -------------------------------------------------------------------

PAGE = """<html lang="en-GB"><head><title>Solar stills</title>
<link rel="icon" href="/fav.png"><meta name="description" content="How stills work">
<script>track()</script></head><body>
<nav><a href="/">Home</a> <a href="/shop">Shop</a></nav>
<div class="cookie-banner">We use cookies</div>
<article><h1>Solar stills</h1><p onclick="x()">A still <a href="pit.html#dig">in a pit</a>
makes water. See <a href="https://other.org/x?utm_source=a">elsewhere</a>.</p>
<img src="/img/still.png" alt="A pit still" srcset="/img/still-2x.png 2x">
<video src="/v/still.mp4" poster="/img/poster.png"></video></article>
<footer>Copyright</footer></body></html>"""


def test_clean_page_keeps_the_article_and_lists_what_it_needs():
    page = extract.prepare(PAGE, "https://example.org/guide/still.html")
    assert page.title == "Solar stills" and page.language == "en-GB"
    assert page.description == "How stills work" and page.icon == "https://example.org/fav.png"
    for gone in ("track()", "cookies", "Shop", "Copyright", "onclick"):
        assert gone not in page.html
    assert "A still" in page.text and "Shop" not in page.text
    assert page.links == ["https://example.org/guide/pit.html", "https://other.org/x"]
    assert ("https://example.org/img/still-2x.png", "image") in page.resources
    assert ("https://example.org/v/still.mp4", "media") in page.resources
    assert '<link rel="canonical" href="https://example.org/guide/still.html">' in page.html


def test_media_can_be_left_out():
    page = extract.prepare(PAGE, "https://example.org/guide/still.html", images=False, video=False)
    assert page.resources == []
    assert "[A pit still]" in page.html and "<img" not in page.html
    assert "<video" not in page.html and "https://example.org/v/still.mp4" in page.html


def test_whole_body_is_kept_when_the_article_guess_would_lose_text():
    filler = "<p>" + "Real content that matters. " * 40 + "</p>"
    html = f"<html><body><article><p>Tiny teaser.</p></article><div>{filler}</div></body></html>"
    page = extract.prepare(html, "https://example.org/")
    assert page.fell_back and "Real content that matters" in page.text


def test_addresses_are_normalised_and_mapped_to_zim_paths():
    assert extract.normalise("/a//b?utm_source=x&id=2#top", "https://Example.org:443/") == (
        "https://example.org/a/b?id=2"
    )
    assert extract.normalise("mailto:me@example.org") == ""
    assert extract.zim_path("https://example.org/docs/") == "example.org/docs/index"
    first = extract.zim_path("https://example.org/p.html?id=1")
    assert first != extract.zim_path("https://example.org/p.html?id=2")
    assert first.startswith("example.org/p_") and first.endswith(".html")
    assert extract.relative("example.org/guide/still.html", "example.org/img/a b.png") == (
        "../img/a%20b.png"
    )


def test_localise_points_at_saved_files_and_leaves_the_rest_on_the_web():
    page = extract.prepare(PAGE, "https://example.org/guide/still.html")
    saved = {
        "https://example.org/guide/pit.html": "example.org/guide/pit.html",
        "https://example.org/img/still-2x.png": "example.org/img/still-2x.png",
    }
    html = extract.localise(page.html, "example.org/guide/still.html", saved)
    assert 'href="pit.html#dig"' in html and 'src="../img/still-2x.png"' in html
    assert 'href="https://other.org/x"' in html
    assert "<video" not in html and "Media (not saved)" in html  # the video was not saved


# -- crawling -------------------------------------------------------------------------


class Site:
    """A small website served in-process, counting what is asked of it."""

    def __init__(self):
        self.asked: list[str] = []
        self.conditional = 0
        self.pages = {
            "/robots.txt": ("text/plain", b"User-agent: *\nDisallow: /guide/private"),
            "/guide/": (
                "text/html",
                b"<html><head><title>Guide</title></head><body><article><h1>Guide</h1>"
                b'<a href="one.html">One</a> <a href="broken.html">Broken</a>'
                b'<a href="private/x.html">Private</a> <a href="/other/far.html">Far</a>'
                b'<a href="https://elsewhere.org/">Elsewhere</a>'
                b'<img src="pic.png" alt="A picture"></article></body></html>',
            ),
            "/guide/one.html": (
                "text/html",
                b"<html><head><title>One</title></head><body><article><h1>One</h1>"
                b'<p>The bowline is a knot.</p><a href="./">Back</a></article></body></html>',
            ),
            "/guide/pic.png": ("image/png", PNG),
            "/other/far.html": ("text/html", b"<html><body>Out of scope</body></html>"),
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.asked.append(path)
        if path not in self.pages:
            return httpx.Response(404 if path != "/guide/broken.html" else 500)
        if request.headers.get("if-none-match") == f'"{path}"':
            self.conditional += 1
            return httpx.Response(304)
        kind, body = self.pages[path]
        return httpx.Response(
            200, content=body, headers={"content-type": kind, "etag": f'"{path}"'}
        )


@pytest.fixture
def no_waiting(monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _seconds: real_sleep(0))
    monkeypatch.setattr("zim_pro.builder.crawler.HOST_DELAY", 0)
    monkeypatch.setattr("zim_pro.builder.crawler.SLOW_DELAY_MIN", 0)
    monkeypatch.setattr("zim_pro.builder.crawler.RATE_WAIT", 0)


async def crawl(tmp_path, site, url="https://site.test/guide/", **options):
    stage, report = Stage(tmp_path / f"stage-{len(list(tmp_path.glob('stage-*')))}"), Reporter()
    crawler = Crawler(
        CrawlOptions(url, **options),
        stage,
        tmp_path / "cache",
        report,
        workers=3,
        transport=httpx.MockTransport(site),
    )
    meta = await crawler.run()
    return stage, report, meta, crawler


async def test_crawl_saves_the_section_and_accounts_for_every_address(tmp_path, no_waiting):
    site = Site()
    stage, report, meta, _ = await crawl(tmp_path, site, scope="path")
    assert sorted(stage.pages) == ["site.test/guide/index", "site.test/guide/one.html"]
    assert stage.has("site.test/guide/pic.png") and stage.main == "site.test/guide/index"
    assert meta.name == "custom_guide" and meta.source == "https://site.test/guide/"
    # Out of scope and off-site pages were never asked for; the private one was refused.
    assert "/other/far.html" not in site.asked and "/guide/private/x.html" not in site.asked
    log = {(entry["kind"], entry["target"].rsplit("/", 1)[-1]) for entry in report.log}
    assert ("failed", "broken.html") in log and ("skipped", "x.html") in log
    assert report.failed_count == 1 and report.skipped_count == 1
    # Links between saved pages are relative; the rest still point at the web.
    index = stage.page_html("site.test/guide/index")
    assert 'href="one.html"' in index and 'src="pic.png"' in index
    assert 'href="https://site.test/other/far.html"' in index
    assert 'href="index"' in stage.page_html("site.test/guide/one.html")

    target = tmp_path / meta.filename
    write_zim(stage, meta, target)
    assert search(Archive(str(target)), "bowline") == ["site.test/guide/one.html"]


async def test_page_scope_and_media_toggle(tmp_path, no_waiting):
    site = Site()
    stage, _, _, _ = await crawl(tmp_path, site, scope="page", images=False)
    assert stage.pages == ["site.test/guide/index"] and "/guide/one.html" not in site.asked
    assert "/guide/pic.png" not in site.asked
    assert "[A picture]" in stage.page_html("site.test/guide/index")


async def test_rebuild_only_downloads_what_changed(tmp_path, no_waiting):
    site = Site()
    _, _, _, first = await crawl(tmp_path, site, scope="path")
    assert first.fetcher.reused == 0 and first.fetcher.downloaded > 0
    stage, _, _, second = await crawl(tmp_path, site, scope="path")
    # Two pages, a picture and robots.txt: all four are unchanged.
    assert second.fetcher.reused == 4 and second.fetcher.downloaded == 0 and site.conditional == 4
    assert len(stage.pages) == 2  # the same ZIM comes out of the cache


async def test_page_limit_is_respected_and_reported(tmp_path, no_waiting):
    site = Site()
    stage, report, _, _ = await crawl(tmp_path, site, scope="path", max_pages=1)
    assert len(stage.pages) == 1
    assert any("limit of 1 pages" in entry["why"] for entry in report.log)


def page(title: str, body: str = "") -> tuple[str, bytes]:
    html = f"<html><head><title>{title}</title></head><body><article><h1>{title}</h1>"
    return "text/html", f"{html}<p>About {title}.</p>{body}</article></body></html>".encode()


async def test_a_site_that_says_too_many_requests_is_waited_for(tmp_path, no_waiting):
    site = Site()
    refusals = {"/guide/one.html": 2, "/guide/pic.png": 1}

    def busy(request: httpx.Request) -> httpx.Response:
        if refusals.get(request.url.path):
            refusals[request.url.path] -= 1
            return httpx.Response(429, headers={"retry-after": "0"})
        return site(request)

    stage, report, _, crawler = await crawl(tmp_path, busy, scope="path")
    # Nothing was lost to the refusals: both pages and the picture are there.
    assert len(stage.pages) == 2 and stage.has("site.test/guide/pic.png")
    assert crawler.fetcher.slowed >= 1
    assert any("too many requests" in entry["why"] for entry in report.log)


async def test_a_site_that_never_stops_refusing_is_a_failed_page_not_a_hang(tmp_path, no_waiting):
    site = Site()

    def stubborn(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/guide/one.html":
            return httpx.Response(429)
        return site(request)

    stage, report, _, _ = await crawl(tmp_path, stubborn, scope="path")
    assert stage.pages == ["site.test/guide/index"]
    assert any(e["kind"] == "failed" and "HTTP 429" in e["why"] for e in report.log)


def test_retry_after_is_seconds_or_a_date():
    from email.utils import formatdate

    from zim_pro.builder.crawler import _retry_after

    assert _retry_after("12") == 12 and _retry_after("") is None
    assert _retry_after("soon") is None
    assert 50 < _retry_after(formatdate(time.time() + 60, usegmt=True)) <= 60


def test_sitemaps_are_read_in_every_shape():
    urlset = b"<urlset><url><loc> https://a.test/x?a=1&amp;b=2 </loc></url></urlset>"
    assert sitemap.parse(urlset) == ([], ["https://a.test/x?a=1&b=2"])
    assert sitemap.parse(gzip.compress(urlset)) == ([], ["https://a.test/x?a=1&b=2"])
    index = b"<sitemapindex><sitemap><loc>https://a.test/s1.xml</loc></sitemap></sitemapindex>"
    assert sitemap.parse(index) == (["https://a.test/s1.xml"], [])
    assert sitemap.parse(b"https://a.test/one\nnot an address\nhttps://a.test/two\n") == (
        [],
        ["https://a.test/one", "https://a.test/two"],
    )
    assert sitemap.parse(b"<!doctype html><html><body>Not found</body></html>") == ([], [])
    robots = "User-agent: *\nSitemap: https://a.test/s.xml\nsitemap:https://a.test/t.xml # x"
    assert sitemap.listed_in_robots(robots) == ["https://a.test/s.xml", "https://a.test/t.xml"]


class MappedSite(Site):
    """A site whose sitemaps list pages no page links to, and repeat ones that are linked."""

    def __init__(self):
        super().__init__()
        self.pages = {
            "/robots.txt": (
                "text/plain",
                b"User-agent: *\nDisallow: /secret\nSitemap: https://site.test/maps/index.xml",
            ),
            "/maps/index.xml": (
                "application/xml",
                b"<sitemapindex><sitemap><loc>https://site.test/maps/a.xml.gz</loc></sitemap>"
                b"<sitemap><loc>https://site.test/maps/a.xml.gz</loc></sitemap></sitemapindex>",
            ),
            "/maps/a.xml.gz": (
                "application/gzip",
                gzip.compress(
                    b"<urlset><url><loc>https://site.test/</loc></url>"
                    b"<url><loc>http://www.site.test/linked</loc></url>"
                    b"<url><loc>https://site.test/orphan</loc></url>"
                    b"<url><loc>https://site.test/orphan#top</loc></url>"
                    b"<url><loc>https://site.test/secret</loc></url>"
                    b"<url><loc>https://elsewhere.org/x</loc></url></urlset>"
                ),
            ),
            "/": page(
                "Home", '<a href="/linked">Linked</a> <a href="/linked?utm_source=x">Again</a>'
            ),
            "/linked": page("Linked", '<a href="/">Home</a>'),
            "/orphan": page("Orphan"),
            "/secret": page("Secret"),
        }


async def test_sitemaps_fill_one_queue_that_holds_each_address_once(tmp_path, no_waiting):
    site = MappedSite()
    stage, report, _, crawler = await crawl(tmp_path, site, "https://site.test/", scope="site")
    # The page nothing links to was found through the sitemap.
    # The sitemap spells one page with http and www; it is still the same page, saved once.
    assert sorted(p.split("/", 1)[1] for p in stage.pages) == ["index", "linked", "orphan"]
    # robots.txt and the sitemap came before any page, and each address was asked for once.
    assert site.asked[:3] == ["/robots.txt", "/maps/index.xml", "/maps/a.xml.gz"]
    assert len(site.asked) == len(set(site.asked)) and "/secret" not in site.asked
    assert "/sitemap.xml" not in site.asked  # robots.txt named the sitemap: no guessing
    assert crawler.from_sitemaps == 2 and report.skipped_count == 1
    assert any("sitemap" in entry["why"] for entry in report.log)


async def test_sitemap_is_looked_for_in_the_usual_places(tmp_path, no_waiting):
    site = MappedSite()
    site.pages["/robots.txt"] = ("text/plain", b"User-agent: *\nDisallow:")
    site.pages["/sitemap_index.xml"] = site.pages.pop("/maps/index.xml")
    stage, _, _, _ = await crawl(tmp_path, site, "https://site.test/", scope="site")
    assert "site.test/orphan" in stage.pages
    assert "/sitemap.xml" in site.asked and "/wp-sitemap.xml" not in site.asked


async def test_subdomains_are_part_of_the_site_only_when_asked(tmp_path, no_waiting):
    def site(request: httpx.Request) -> httpx.Response:
        pages = {
            ("site.test", "/"): page(
                "Home",
                '<a href="https://blog.site.test/post">Post</a>'
                '<a href="https://notsite.test/">Other</a>',
            ),
            ("blog.site.test", "/post"): page("Post", '<a href="/hidden">Hidden</a>'),
            ("blog.site.test", "/hidden"): page("Hidden"),
            ("blog.site.test", "/robots.txt"): ("text/plain", b"User-agent: *\nDisallow: /hidden"),
        }
        found = pages.get((request.url.host, request.url.path))
        if found is None:
            return httpx.Response(404)
        return httpx.Response(200, content=found[1], headers={"content-type": found[0]})

    stage, _, _, _ = await crawl(tmp_path, site, "https://site.test/", scope="site")
    assert stage.pages == ["site.test/index"]
    stage, report, _, _ = await crawl(
        tmp_path / "wide", site, "https://site.test/", scope="site", subdomains=True
    )
    # The subdomain is saved under its own name, and its own robots.txt is obeyed.
    assert sorted(stage.pages) == ["blog.site.test/post", "site.test/index"]
    assert [e["target"] for e in report.log if e["kind"] == "skipped"] == [
        "https://blog.site.test/hidden"
    ]
    assert 'href="../blog.site.test/post"' in stage.page_html("site.test/index")


async def test_paused_crawl_waits_and_carries_on_when_resumed(tmp_path, no_waiting):
    site = Site()
    stage, report = Stage(tmp_path / "stage"), Reporter()
    report.paused.set()
    crawler = Crawler(
        CrawlOptions("https://site.test/guide/", scope="path"),
        stage,
        tmp_path / "cache",
        report,
        transport=httpx.MockTransport(site),
    )
    running = asyncio.create_task(crawler.run())
    for _ in range(20):
        await asyncio.sleep(0)
    assert site.asked == [] and not running.done()  # nothing is asked for while paused
    report.paused.clear()
    await running
    assert len(stage.pages) == 2


def test_paused_work_on_a_thread_waits_and_can_still_be_cancelled():
    report = Reporter()
    report.paused.set()
    threading.Timer(0.05, report.paused.clear).start()
    started = time.monotonic()
    report.check()
    assert time.monotonic() - started >= 0.04
    report.paused.set()
    threading.Timer(0.05, report.cancelled.set).start()
    with pytest.raises(Cancelled):
        report.check()


def test_cancelled_build_stops_at_the_next_step():
    report = Reporter()
    report.cancelled.set()
    with pytest.raises(Cancelled):
        report.phase("fetching")


# -- books ----------------------------------------------------------------------------


def make_epub(path) -> None:
    chapter = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>c</title></head><body>'
        '<h1>{title}</h1><p>{text}</p><img src="../img/cover.png" alt="Cover art"/>'
        '<a href="two.xhtml">next</a><script>bad()</script></body></html>'
    )
    with zipfile.ZipFile(path, "w") as book:
        book.writestr("mimetype", "application/epub+zip")
        book.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
        )
        book.writestr(
            "OEBPS/content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<metadata><dc:title>Knots at Sea</dc:title><dc:creator>A. Sailor</dc:creator>"
            '<dc:language>en</dc:language><meta name="cover" content="cover"/></metadata><manifest>'
            '<item id="one" href="text/one.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="two" href="text/two.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="cover" href="img/cover.png" media-type="image/png"/>'
            '</manifest><spine><itemref idref="one"/><itemref idref="two"/></spine></package>',
        )
        book.writestr("OEBPS/text/one.xhtml", chapter.format(title="Bowline", text="A loop knot."))
        book.writestr("OEBPS/text/two.xhtml", chapter.format(title="Clove hitch", text="A hitch."))
        book.writestr("OEBPS/img/cover.png", PNG)


def test_epub_becomes_chapters_with_the_original_kept(tmp_path):
    source = tmp_path / "knots.epub"
    make_epub(source)
    stage, report = Stage(tmp_path / "stage"), Reporter()
    meta = epub.convert(source, stage, report, "knots.epub")
    assert meta.title == "Knots at Sea" and meta.creator == "A. Sailor" and meta.language == "eng"
    assert stage.pages == ["book/OEBPS/text/one.xhtml", "book/OEBPS/text/two.xhtml", "index"]
    assert stage.main == "index" and stage.icon == PNG
    one = stage.page_html("book/OEBPS/text/one.xhtml")
    # The chapter's own links and pictures still resolve; scripts are gone.
    assert 'src="../img/cover.png"' in one and 'href="two.xhtml"' in one and "bad()" not in one
    assert "Next →" in one and "← Previous" not in one
    index = stage.page_html("index")
    assert "Bowline" in index and "Clove hitch" in index and 'href="original/knots.epub"' in index
    assert stage.entries["original/knots.epub"].file.read_bytes() == source.read_bytes()

    target = tmp_path / meta.filename
    write_zim(stage, meta, target)
    assert search(Archive(str(target)), "hitch")[0] == "book/OEBPS/text/two.xhtml"


def test_epub_without_pictures(tmp_path):
    source = tmp_path / "knots.epub"
    make_epub(source)
    stage = Stage(tmp_path / "stage")
    epub.convert(source, stage, Reporter(), "knots.epub", images=False)
    assert not stage.has("book/OEBPS/img/cover.png")
    assert "[Cover art]" in stage.page_html("book/OEBPS/text/one.xhtml")


def test_broken_book_files_are_refused_with_a_reason(tmp_path):
    bad = tmp_path / "bad.epub"
    bad.write_bytes(b"not a zip")
    with pytest.raises(BuildError, match="not a readable EPUB"):
        epub.convert(bad, Stage(tmp_path / "a"), Reporter(), "bad.epub")
    bad_pdf = tmp_path / "bad.pdf"
    bad_pdf.write_bytes(b"%PDF-1.4 nothing here")
    with pytest.raises(BuildError, match="not a readable PDF"):
        pdf.convert(bad_pdf, Stage(tmp_path / "b"), Reporter(), "bad.pdf")


def make_pdf(pages: list[str]) -> bytes:
    """A minimal PDF with one line of text on each page."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + 2 * n} 0 R" for n in range(len(pages)))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    font = 3 + 2 * len(pages)
    for number, text in enumerate(pages):
        stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode()
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * number} 0 R "
            f"/Resources << /Font << /F1 {font} 0 R >> >> >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n%s\nendobj\n" % (index, body))
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objects) + 1, xref)
    )
    return out.getvalue()


def test_pdf_becomes_a_page_per_page_with_text_and_pictures(tmp_path):
    source = tmp_path / "manual.pdf"
    source.write_bytes(make_pdf(["Tying a bowline", "Splicing rope"]))
    stage, report = Stage(tmp_path / "stage"), Reporter()
    meta = pdf.convert(source, stage, report, "manual.pdf")
    assert meta.title == "manual" and stage.pages == ["page/1", "page/2", "index"]
    assert stage.has("img/page-1.jpg") and stage.has("original/manual.pdf")
    first = stage.page_html("page/1")
    assert "Tying a bowline" in first and 'src="../img/page-1.jpg"' in first and 'href="2"' in first
    target = tmp_path / meta.filename
    write_zim(stage, meta, target)
    assert search(Archive(str(target)), "splicing") == ["page/2"]

    text_only = Stage(tmp_path / "text-only")
    pdf.convert(source, text_only, Reporter(), "manual.pdf", images=False)
    assert not text_only.has("img/page-1.jpg") and "<img" not in text_only.page_html("page/1")


def test_pdf_without_a_text_layer_is_flagged(tmp_path):
    source = tmp_path / "scan.pdf"
    source.write_bytes(make_pdf(["", ""]))
    stage, report = Stage(tmp_path / "stage"), Reporter()
    pdf.convert(source, stage, report, "scan.pdf")
    assert any("cannot" in e["why"] or "not searched" in e["why"] for e in report.log)
    assert "read but not searched" in stage.page_html("index")


# -- links from CSV -------------------------------------------------------------------


def test_csv_rows_are_new_duplicate_or_invalid_with_a_reason():
    from types import SimpleNamespace

    from zim_pro.api.build import _parse_csv

    db = Database(":memory:")
    db.execute("INSERT INTO links (url) VALUES ('https://example.org/known')")
    text = (
        "﻿URL;Title;Scope;Images\n"
        "example.org/guide/;The guide;path;no\n"
        "https://example.org/known;;;\n"
        "https://example.org/guide/?utm_source=x;;;\n"
        "not a web address;;;\n"
        "https://example.org/b;;galaxy;perhaps\n"
    )
    rows = _parse_csv(SimpleNamespace(db=db), text)
    assert [r["status"] for r in rows] == ["new", "duplicate", "duplicate", "invalid", "invalid"]
    assert rows[0] | {"why": ""} == rows[0] and rows[0]["url"] == "https://example.org/guide/"
    assert rows[0]["scope"] == "path" and rows[0]["images"] is False and rows[0]["video"] is False
    assert rows[1]["why"] == "already in your links" and rows[2]["why"] == "repeated in this file"
    assert "scope must be" in rows[4]["why"] and "images must be yes or no" in rows[4]["why"]
    # Without a header row, the first column is the address.
    plain = _parse_csv(SimpleNamespace(db=db), "https://example.org/x,My page\n")
    assert plain[0]["status"] == "new" and plain[0]["title"] == "My page"


# -- the build queue ------------------------------------------------------------------


def manager(tmp_path) -> BuildManager:
    db = Database(":memory:")
    pool = ArchivePool()
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return BuildManager(db, EventBus(), Scanner(db, pool, data), pool, data, tmp_path / "state")


async def finished(builds: BuildManager, build_id: int) -> dict:
    for _ in range(400):
        job = builds.get(build_id)
        if job["state"] not in ("queued", "processing", "paused"):
            return job
        await asyncio.sleep(0.02)
    raise AssertionError("the build did not finish")


async def test_upload_build_goes_from_queued_to_finished_and_into_the_library(tmp_path):
    builds = manager(tmp_path)
    source = tmp_path / "knots.epub"
    make_epub(source)
    builds.db.execute(
        "INSERT INTO uploads (id, filename, kind, size, path) "
        "VALUES (1, 'knots.epub', 'epub', 1, ?)",
        (str(source),),
    )
    await builds.start()
    try:
        queued = builds.enqueue("upload", 1, "knots.epub")
        assert queued["state"] == "queued"
        with pytest.raises(BuildError, match="already being built"):
            builds.enqueue("upload", 1, "knots.epub")
        job = await finished(builds, queued["id"])
        assert (
            job["state"] == "finished" and job["error"] is None and job["title"] == "Knots at Sea"
        )
        assert job["book_id"] == "custom_knots-at-sea" and job["report"]["pages"] == 3
        book = builds.db.one("SELECT * FROM books WHERE id = ?", (job["book_id"],))
        assert book["category"] == "custom" and book["article_count"] == 3
        assert builds.db.one("SELECT book_id FROM uploads")["book_id"] == job["book_id"]
        assert not list((tmp_path / "state" / "builds" / "stage").glob("*"))  # tidied up

        # Converting again replaces the same book instead of adding a second one.
        again = await finished(builds, builds.enqueue("upload", 1, "knots.epub")["id"])
        assert again["book_id"] == job["book_id"]
        assert len(list((tmp_path / "data").glob("*.zim"))) == 1
    finally:
        await builds.stop()


async def test_failed_and_cancelled_builds_say_so_and_can_be_retried(tmp_path):
    builds = manager(tmp_path)
    bad = tmp_path / "bad.epub"
    bad.write_bytes(b"not a zip")
    builds.db.execute(
        "INSERT INTO uploads (id, filename, kind, size, path) VALUES (1, 'bad.epub', 'epub', 1, ?)",
        (str(bad),),
    )
    # Queued while nothing is running: cancelling it needs no worker.
    waiting = builds.enqueue("upload", 1, "bad.epub")
    builds.cancel(waiting["id"])
    assert builds.get(waiting["id"])["state"] == "cancelled"
    await builds.start()
    try:
        builds.retry(waiting["id"])
        job = await finished(builds, waiting["id"])
        assert job["state"] == "failed" and "not a readable EPUB" in job["error"]
        assert list((tmp_path / "data").glob("*")) == []
    finally:
        await builds.stop()


async def test_paused_builds_keep_their_place_and_resume(tmp_path):
    builds = manager(tmp_path)
    make_epub(tmp_path / "book.epub")
    builds.db.execute(
        "INSERT INTO uploads (id, filename, kind, size, path) "
        "VALUES (1, 'book.epub', 'epub', 1, ?)",
        (str(tmp_path / "book.epub"),),
    )
    job = builds.enqueue("upload", 1, "book.epub")
    builds.pause(job["id"])
    assert builds.get(job["id"])["state"] == "paused"
    with pytest.raises(BuildError):
        builds.enqueue("upload", 1, "book.epub")  # still counts as being built
    await builds.start()
    try:
        await asyncio.sleep(0.1)
        assert builds.get(job["id"])["state"] == "paused"  # the queue leaves it alone
        builds.resume(job["id"])
        assert (await finished(builds, job["id"]))["state"] == "finished"
        # A paused build can be cancelled without ever starting.
        again = builds.enqueue("upload", 1, "book.epub")
        builds.pause(again["id"])
        builds.cancel(again["id"])
        assert builds.get(again["id"])["state"] == "cancelled"
    finally:
        await builds.stop()


async def test_builds_interrupted_by_a_restart_are_queued_again(tmp_path):
    builds = manager(tmp_path)
    builds.db.execute(
        "INSERT INTO builds (kind, source_id, title, state, phase) "
        "VALUES ('upload', 99, 'x', 'processing', 'converting')"
    )
    await builds.start()
    try:
        job = await finished(builds, 1)
        # Re-run from the queue; its upload is gone, so it fails with a clear reason.
        assert job["state"] == "failed" and "has been removed" in job["error"]
    finally:
        await builds.stop()


def test_job_log_is_json(tmp_path):
    report = Reporter()
    report.failed("https://example.org/a", "HTTP 500")
    report.skipped("https://example.org/b", "not allowed by robots.txt")
    assert json.loads(json.dumps(report.log))[0]["kind"] == "failed"
    assert report.snapshot()["failed"] == 1 and report.snapshot()["skipped"] == 1


def test_scanner_ignores_work_in_progress_folders(tmp_path):
    db, pool = Database(":memory:"), ArchivePool()
    data = tmp_path / "data"
    stage = staged(tmp_path)
    meta = ZimMeta("custom_test", "T")
    (data / ".zimit-7").mkdir(parents=True)
    write_zim(stage, meta, data / ".zimit-7" / meta.filename)
    scanner = Scanner(db, pool, data)
    scanner.scan()
    assert db.all("SELECT id FROM books") == []
    write_zim(stage, meta, data / meta.filename)
    scanner.scan()
    assert [b["path"] for b in db.all("SELECT path FROM books")] == [str(data / meta.filename)]


async def test_a_new_zim_is_filed_under_its_category_and_carries_the_chosen_picture(tmp_path):
    import io

    from PIL import Image

    from zim_pro.builder import icons

    builds = manager(tmp_path)
    source = tmp_path / "knots.epub"
    make_epub(source)
    builds.db.execute(
        "INSERT INTO uploads (id, filename, kind, size, path, category) "
        "VALUES (1, 'knots.epub', 'epub', 1, ?, 'Sailing')",
        (str(source),),
    )
    out = io.BytesIO()
    Image.new("RGB", (16, 16), (200, 30, 30)).save(out, "PNG")
    picture = icons.path(tmp_path / "state", "upload", 1)
    picture.parent.mkdir(parents=True)
    picture.write_bytes(icons.normalise(out.getvalue()))
    await builds.start()
    try:
        job = await finished(builds, builds.enqueue("upload", 1, "knots.epub")["id"])
        book = builds.db.one("SELECT * FROM books WHERE id = ?", (job["book_id"],))
        assert book["category"] == "Sailing"  # from its very first build
        shown = Image.open(
            io.BytesIO(bytes(Archive(book["path"]).get_illustration_item(48).content))
        )
        # The chosen picture, drawn to fill the tile though it was only 16 pixels wide.
        assert shown.size == (48, 48)
        assert shown.convert("RGB").getpixel((2, 2)) == (200, 30, 30)
    finally:
        await builds.stop()
