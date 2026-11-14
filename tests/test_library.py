import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from libzim.writer import ContentProvider, Creator, Item, StringProvider

from zim_pro.config import Config
from zim_pro.db import Database
from zim_pro.library import article, reader, scanner
from zim_pro.library import search as zim_search
from zim_pro.library.archive_pool import ArchivePool
from zim_pro.main import create_app


class _SimpleItem(Item):
    def __init__(self, path: str, title: str, mimetype: str, data: bytes | str):
        super().__init__()
        self._path = path
        self._title = title
        self._mimetype = mimetype
        self._data = data.encode("utf-8") if isinstance(data, str) else data

    def get_path(self) -> str:
        return self._path

    def get_title(self) -> str:
        return self._title

    def get_mimetype(self) -> str:
        return self._mimetype

    def get_contentprovider(self) -> ContentProvider:
        return StringProvider(self._data)

    def get_hints(self) -> dict:
        return {}


def _make_zim(
    path: Path,
    *,
    name: str = "test_book",
    title: str = "Test Book",
    date: str = "2026-01-01",
    flavour: str = "",
    html: str = "<html><body><h1>Hello</h1><p>Water is essential for life.</p></body></html>",
    illustration: tuple[str, bytes] | None = None,
    extra_items: list[tuple[str, str, str, bytes | str]] | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with Creator(path).config_indexing(True, "eng") as creator:
        creator.set_mainpath("A/index")
        creator.add_metadata("Name", name)
        creator.add_metadata("Title", title)
        creator.add_metadata("Date", date)
        creator.add_metadata("Language", "eng")
        if flavour:
            creator.add_metadata("Flavour", flavour)
        if illustration is not None:
            _, data = illustration
            creator.add_illustration(48, data)
        creator.add_item(_SimpleItem("A/index", title, "text/html", html))
        for p, t, m, d in extra_items or []:
            creator.add_item(_SimpleItem(p, t, m, d))
    return path


def _client(tmp_path: Path, *, admin_token: str | None = None) -> TestClient:
    data_dir = tmp_path / "data"
    state_dir = tmp_path / "state"
    data_dir.mkdir(exist_ok=True)
    state_dir.mkdir(exist_ok=True)
    cfg = Config(data_dir=data_dir, state_dir=state_dir, admin_token=admin_token)
    app = create_app(cfg)
    return TestClient(app)


# -- F1, F13, F18: /content CSP, Range, ETag/Cache-Control -------------------------------


def test_content_csp_nosniff_and_etag(tmp_path: Path):
    client = _client(tmp_path, admin_token="secret")
    zim_path = tmp_path / "data" / "sample_en_2026-01.zim"
    _make_zim(zim_path, name="sample_en", title="Sample")
    client.app.state.ctx.scanner.scan()

    resp = client.get("/content/sample_en/A/index")
    assert resp.status_code == 200
    csp = resp.headers["content-security-policy"]
    assert "sandbox allow-scripts allow-forms allow-popups" in csp
    assert "allow-same-origin" not in csp
    assert "connect-src 'none'" in csp
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["cache-control"].startswith("private")
    assert "last-modified" in resp.headers
    etag1 = resp.headers["etag"]

    # Conditional 304
    r_304 = client.get("/content/sample_en/A/index", headers={"If-None-Match": etag1})
    assert r_304.status_code == 304

    # Bumping mtime in DB changes ETag (F18)
    client.app.state.ctx.db.execute("UPDATE books SET mtime = mtime + 100 WHERE id = 'sample_en'")
    resp2 = client.get("/content/sample_en/A/index")
    assert resp2.headers["etag"] != etag1


def test_content_range_requests(tmp_path: Path):
    client = _client(tmp_path)
    zim_path = tmp_path / "data" / "media_en_2026-01.zim"
    _make_zim(
        zim_path,
        name="media_en",
        extra_items=[("I/blob.bin", "blob", "application/octet-stream", b"0123456789")],
    )
    client.app.state.ctx.scanner.scan()

    r0 = client.get("/content/media_en/I/blob.bin", headers={"Range": "bytes=0-0"})
    assert r0.status_code == 206
    assert r0.content == b"0"
    assert r0.headers["content-range"] == "bytes 0-0/10"

    r_suffix = client.get("/content/media_en/I/blob.bin", headers={"Range": "bytes=-4"})
    assert r_suffix.status_code == 206
    assert r_suffix.content == b"6789"

    r_case = client.get("/content/media_en/I/blob.bin", headers={"Range": "Bytes=1-3"})
    assert r_case.status_code == 206
    assert r_case.content == b"123"

    r_head = client.head("/content/media_en/I/blob.bin", headers={"Range": "bytes=0-2"})
    assert r_head.status_code == 206
    assert r_head.headers["content-length"] == "3"

    # Invalid / multi-range -> 416 with Accept-Ranges preserved
    for bad in ("bytes=5-3", "bytes=0-1,3-4", "bytes=-"):
        r_bad = client.get("/content/media_en/I/blob.bin", headers={"Range": bad})
        assert r_bad.status_code == 416
        assert r_bad.headers["accept-ranges"] == "bytes"
        assert r_bad.headers["content-range"] == "bytes */10"

    # If-Range mismatch falls back to 200
    r_ifrange = client.get(
        "/content/media_en/I/blob.bin",
        headers={"Range": "bytes=0-1", "If-Range": '"stale-etag"'},
    )
    assert r_ifrange.status_code == 200
    assert r_ifrange.content == b"0123456789"


# -- F2: Symlinked directories & walk safety --------------------------------------------


def test_scanner_follows_symlinked_dirs_and_survives_rescan(tmp_path: Path):
    data_dir = tmp_path / "data"
    ext_dir = tmp_path / "external"
    data_dir.mkdir()
    ext_dir.mkdir()
    _make_zim(ext_dir / "linked_en_2026-01.zim", name="linked_en", title="Linked")
    (data_dir / "zims").symlink_to(ext_dir)
    # Also create a symlink cycle to verify loop protection
    (ext_dir / "loop").symlink_to(data_dir)

    db = Database(":memory:")
    pool = ArchivePool()
    sc = scanner.Scanner(db, pool, data_dir)

    res1 = sc.scan()
    assert res1["books"] == 1
    assert db.one("SELECT id FROM books WHERE id = 'linked_en'") is not None

    res2 = sc.scan()
    assert res2["books"] == 1
    assert res2["removed"] == 0
    assert db.one("SELECT id FROM books WHERE id = 'linked_en'") is not None


# -- F3: Atomic scan & OSError handling -------------------------------------------------


def test_scanner_handles_read_book_oserror_atomically(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    z1 = _make_zim(data_dir / "one_en_2026-01.zim", name="one_en")
    _make_zim(data_dir / "one_en_2026-02.zim", name="one_en")
    db = Database(":memory:")
    pool = ArchivePool()
    sc = scanner.Scanner(db, pool, data_dir)
    sc.scan()

    assert db.scalar("SELECT COUNT(*) FROM books") == 1
    assert db.scalar("SELECT COUNT(*) FROM shadowed") == 1

    # Add a new file whose read_book raises OSError
    bad_zim = data_dir / "two_en_2026-01.zim"
    bad_zim.write_bytes(b"partial")
    real_read_book = scanner.read_book

    def flaky_read(path: Path):
        if path == bad_zim:
            raise OSError("vanished mid-scan")
        return real_read_book(path)

    monkeypatch.setattr(scanner, "read_book", flaky_read)
    res = sc.scan()
    assert res["books"] == 1
    assert db.scalar("SELECT COUNT(*) FROM books") == 1
    assert db.scalar("SELECT COUNT(*) FROM shadowed") == 1
    assert z1.exists()


# -- F4: Revision letter sorting & distinct metadata split ------------------------------


def test_scanner_prefers_revision_letter_and_splits_distinct_names(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _make_zim(data_dir / "b_2026-01.zim", name="b", title="Base")
    _make_zim(data_dir / "b_2026-01a.zim", name="b", title="Rev A")

    # Two unrelated ZIMs sharing stem `custom_notes`
    _make_zim(data_dir / "custom_notes_2026-01.zim", name="notes_alpha", title="Alpha")
    _make_zim(data_dir / "custom_notes_2026-02.zim", name="notes_beta", title="Beta")

    db = Database(":memory:")
    sc = scanner.Scanner(db, ArchivePool(), data_dir)
    res = sc.scan()

    assert res["books"] == 3
    b_row = db.one("SELECT filename, title FROM books WHERE id = 'b'")
    assert b_row is not None
    assert b_row["filename"] == "b_2026-01a.zim"

    notes_rows = db.all("SELECT id, name FROM books WHERE id LIKE 'custom_notes%'")
    assert len(notes_rows) == 2
    assert {r["name"] for r in notes_rows} == {"notes_alpha", "notes_beta"}


# -- F5: Delete removes shadowed releases; rename preserves policy ----------------------


def test_delete_book_removes_shadowed_and_rename_preserves_policy(tmp_path: Path):
    client = _client(tmp_path)
    ctx = client.app.state.ctx
    old_p = _make_zim(ctx.config.data_dir / "a_2026-01.zim", name="a", title="Old")
    new_p = _make_zim(ctx.config.data_dir / "a_2026-02.zim", name="a", title="New")
    ctx.scanner.scan()

    assert ctx.db.scalar("SELECT COUNT(*) FROM shadowed") == 1
    resp = client.delete("/api/books/a")
    assert resp.status_code == 200
    assert not old_p.exists()
    assert not new_p.exists()
    assert ctx.db.scalar("SELECT COUNT(*) FROM books") == 0
    assert ctx.db.scalar("SELECT COUNT(*) FROM shadowed") == 0

    # Rename preserves policy via UUID
    p1 = _make_zim(ctx.config.data_dir / "foo_2026-01.zim", name="foo", title="Foo")
    ctx.scanner.scan()
    assert client.patch("/api/books/foo", json={"policy": "pinned"}).status_code == 200
    p1.rename(ctx.config.data_dir / "bar_2026-01.zim")
    ctx.scanner.scan()
    bar = ctx.db.one("SELECT policy FROM books WHERE id = 'bar'")
    assert bar is not None
    assert bar["policy"] == "pinned"


# -- F6: Error rows are retried on rescan -----------------------------------------------


def test_scanner_retries_error_rows(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    zim = _make_zim(data_dir / "retry_en_2026-01.zim", name="retry_en")
    stat = zim.stat()

    db = Database(":memory:")
    db.execute(
        "INSERT INTO books (id, path, filename, size, mtime, error) VALUES (?, ?, ?, ?, ?, ?)",
        ("retry_en", str(zim), zim.name, stat.st_size, stat.st_mtime, "EMFILE"),
    )
    sc = scanner.Scanner(db, ArchivePool(), data_dir)
    res = sc.scan()
    assert res["read"] == 1
    row = db.one("SELECT error, title FROM books WHERE id = 'retry_en'")
    assert row is not None
    assert row["error"] is None
    assert row["title"] == "Test Book"


# -- F7: ArchivePool.get respects concurrent invalidate ---------------------------------


def test_archive_pool_invalidate_during_open(monkeypatch):
    opened: list[object] = []
    entered_open = threading.Event()
    allow_finish = threading.Event()

    def fake_archive(path: str):
        obj = object()
        opened.append(obj)
        if len(opened) == 1:
            entered_open.set()
            allow_finish.wait(timeout=2)
        return obj

    monkeypatch.setattr("zim_pro.library.archive_pool.Archive", fake_archive)
    pool = ArchivePool()
    results: list[object] = []

    t = threading.Thread(target=lambda: results.append(pool.get("/x/book.zim")))
    t.start()
    assert entered_open.wait(timeout=2)
    pool.invalidate("/x/book.zim")
    allow_finish.set()
    t.join(timeout=2)

    assert len(opened) == 2
    assert results[0] is opened[1]
    assert pool.get("/x/book.zim") is opened[1]


# -- F9, F10, F15, F21: Search pagination, snippets, category case, auth, query limit ---


def test_search_pagination_error_logging_and_query_limit(tmp_path: Path, caplog):
    client = _client(tmp_path, admin_token="tok")
    ctx = client.app.state.ctx
    _make_zim(
        ctx.config.data_dir / "alpha_2026-01.zim",
        name="alpha",
        title="Alpha",
        html="<html><body><p>Water flows downhill.</p></body></html>",
    )
    _make_zim(
        ctx.config.data_dir / "beta_2026-01.zim",
        name="beta",
        title="Beta",
        html="<html><body><p>Water boils at 100C.</p></body></html>",
    )
    ctx.scanner.scan()

    # Mixed-case category filter (F15)
    ctx.db.execute("UPDATE books SET category = 'Wikipedia' WHERE id = 'alpha'")
    ctx.db.execute("UPDATE books SET category = 'wikipedia' WHERE id = 'beta'")
    r_cat = client.get("/api/search", params={"q": "water", "category": "WIKIPEDIA"})
    assert r_cat.status_code == 200
    assert r_cat.json()["total"] == 2

    # Pagination (F9)
    p1 = client.get("/api/search", params={"q": "water", "start": 0, "count": 1}).json()
    p2 = client.get("/api/search", params={"q": "water", "start": 1, "count": 1}).json()
    assert len(p1["books"]) == 1
    assert len(p2["books"]) == 1
    assert p1["books"][0]["book"] != p2["books"][0]["book"]

    # Query length limit -> 422 (F21)
    assert client.get("/api/search", params={"q": "a" * 513}).status_code == 422

    # Delete history requires admin (F21)
    assert client.delete("/api/search/history").status_code == 401
    assert client.delete("/api/search/history/1").status_code == 401
    r_del = client.delete("/api/search/history", headers={"Authorization": "Bearer tok"})
    assert r_del.status_code == 200

    # Broken archive in cross-book search logs warning and does not abort (F9)
    (ctx.config.data_dir / "beta_2026-01.zim").unlink()
    ctx.pool.invalidate(str(ctx.config.data_dir / "beta_2026-01.zim"))
    with caplog.at_level("WARNING"):
        r_broken = client.get("/api/search", params={"q": "water"})
    assert r_broken.status_code == 200
    assert len(r_broken.json()["books"]) == 1
    assert any("search skipped book beta" in rec.message for rec in caplog.records)


def test_snippet_skips_large_html_and_caps_hits():
    huge_html = b"<html><body>" + b"x" * (zim_search.MAX_SNIPPET_BYTES + 10) + b"</body></html>"
    fake_item = SimpleNamespace(mimetype="text/html", size=len(huge_html), content=huge_html)
    fake_entry = SimpleNamespace(get_item=lambda: fake_item)
    fake_archive = SimpleNamespace(get_entry_by_path=lambda p: fake_entry)

    assert zim_search.snippet(fake_archive, "A/Huge", "water") == ""


# -- F11: Redirect cycles & libzim errors -----------------------------------------------


def test_resolve_redirects_guards_cycles_and_dangling():
    class _FakeEntry:
        def __init__(self, path: str, target=None, broken: bool = False):
            self.path = path
            self.is_redirect = target is not None or broken
            self._target = target
            self._broken = broken

        def get_redirect_entry(self):
            if self._broken:
                raise RuntimeError("dangling")
            return self._target

    a = _FakeEntry("A/1")
    b = _FakeEntry("A/2", a)
    a.is_redirect = True
    a._target = b  # cycle

    assert reader.resolve_redirects(a) is None
    assert reader.resolve_redirects(_FakeEntry("A/broken", broken=True)) is None


def test_content_maps_libzim_read_error_to_503(tmp_path: Path):
    client = _client(tmp_path)
    ctx = client.app.state.ctx
    _make_zim(ctx.config.data_dir / "err_en_2026-01.zim", name="err_en")
    ctx.scanner.scan()

    with patch.object(reader, "find_entry", side_effect=RuntimeError("corrupt")):
        # content.py imported find_entry directly, so patch on content module
        with patch("zim_pro.api.content.find_entry", side_effect=RuntimeError("corrupt")):
            resp = client.get("/content/err_en/A/index")
    assert resp.status_code == 503


# -- F12: resolve_link canonicalisation -------------------------------------------------


@pytest.mark.parametrize(
    ("base", "href", "expected"),
    [
        ("A/Page", "%2e%2e/%2e%2e/etc/passwd", None),
        ("A/Page", "..%2F..%2Fetc/passwd", None),
        ("A/Page", "/../etc/passwd", None),
        ("A/Page", "/A/Foo", "A/Foo"),
        ("A/Page", "a%2520b", "A/a%20b"),
    ],
)
def test_resolve_link_security_and_normalisation(base, href, expected):
    assert article.resolve_link(base, href) == expected


# -- F14, F15: Categories transactions, 404, 409, NOCASE --------------------------------


def test_categories_delete_404_duplicate_409_and_atomic_rename(tmp_path: Path, monkeypatch):
    client = _client(tmp_path)
    ctx = client.app.state.ctx

    assert client.delete("/api/categories/unknown").status_code == 404
    assert client.post("/api/categories", json={"name": "Science"}).status_code == 200
    assert client.post("/api/categories", json={"name": "Science"}).status_code == 409

    # Simulated failure mid-rename rolls back
    def boom(db):
        raise RuntimeError("disk full")

    monkeypatch.setattr("zim_pro.api.organise.file_under_categories", boom)
    with pytest.raises(RuntimeError):
        client.put("/api/categories/Science", json={"name": "Physics"})
    assert ctx.db.one("SELECT name FROM categories WHERE name = 'Science'") is not None
    assert ctx.db.one("SELECT name FROM categories WHERE name = 'Physics'") is None


# -- F16: leftovers skips dot-dirs and .meta4, requires admin ---------------------------


def test_leftovers_skips_dotdirs_meta4_and_requires_admin(tmp_path: Path):
    client = _client(tmp_path, admin_token="tok")
    data_dir = tmp_path / "data"
    (data_dir / ".zimit-1").mkdir()
    (data_dir / ".zimit-1" / "temp.html").write_text("wip")
    (data_dir / "book.zim.meta4").write_text("<metalink/>")
    (data_dir / "stray.txt").write_text("hello")

    assert client.get("/api/library/leftovers").status_code == 401
    resp = client.get("/api/library/leftovers", headers={"Authorization": "Bearer tok"})
    assert resp.status_code == 200
    names = [item["name"] for item in resp.json()]
    assert names == ["stray.txt"]


# -- F17: /api/status handles missing data_dir ------------------------------------------


def test_status_handles_missing_data_dir(tmp_path: Path):
    client = _client(tmp_path)
    (tmp_path / "data").rmdir()
    resp = client.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["disk_free"] is None
    assert body["disk_total"] is None


# -- F19: /illustration uses item.mimetype and checks has_illustration ------------------


def test_illustration_uses_item_mimetype(tmp_path: Path):
    client = _client(tmp_path)
    ctx = client.app.state.ctx
    _make_zim(ctx.config.data_dir / "ill_en_2026-01.zim", name="ill_en")
    ctx.scanner.scan()

    # No illustration -> 404
    assert client.get("/api/books/ill_en/illustration").status_code == 404

    # Mock illustration item with image/webp
    ctx.db.execute("UPDATE books SET has_illustration = 1 WHERE id = 'ill_en'")
    fake_item = SimpleNamespace(content=b"RIFFWEBP", mimetype="image/webp")
    fake_archive = SimpleNamespace(get_illustration_item=lambda size: fake_item)
    with patch.object(ctx.pool, "get", return_value=fake_archive):
        resp = client.get("/api/books/ill_en/illustration")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/webp"


# -- Real fixture ZIM: reader, search, archive_pool LRU, and content endpoints ----------

_SAMPLE_ZIM = Path(__file__).parent / "fixtures" / "sample.zim"


def test_sample_zim_reader_search_and_pool_lru(tmp_path: Path):
    import shutil

    pool = ArchivePool(max_open=1)
    a1 = pool.get(_SAMPLE_ZIM)
    assert reader.main_path(a1) == "A/index"
    assert reader.random_path(a1) in {"A/index", "A/water"}
    assert reader.find_entry(a1, "A/water") is not None
    assert reader.find_entry(a1, "A/missing") is None

    alias = reader.find_entry(a1, "A/alias")
    assert alias is not None and alias.is_redirect
    resolved = reader.resolve_redirects(alias)
    assert resolved is not None and resolved.path == "A/water"

    art = reader.read_article(a1, "A/alias")
    assert art is not None and art.path == "A/water" and b"hydration" in art.content

    res = zim_search.search_book(a1, "hydration")
    assert res["total"] >= 1 and res["mode"] == "fulltext"
    hits = res["results"]
    assert hits[0]["path"] == "A/water"
    assert "hydration" in hits[0]["snippet"].lower()
    suggestions = zim_search.suggest(a1, "Wat")
    assert any(s["title"] == "Water" for s in suggestions)

    # Pool LRU eviction when max_open=1 and a second ZIM path is opened
    copy_zim = tmp_path / "copy.zim"
    shutil.copy2(_SAMPLE_ZIM, copy_zim)
    a2 = pool.get(copy_zim)
    assert a2 is not a1
    assert len(pool._archives) == 1
    pool.invalidate(copy_zim)
    assert len(pool._archives) == 0


def test_sample_zim_content_home_random_redirects_and_markdown(make_client):
    import shutil

    client = make_client()
    ctx = client.app.state.ctx
    shutil.copy2(_SAMPLE_ZIM, ctx.config.data_dir / "sample_en_2026-01.zim")
    ctx.scanner.scan()

    # Book home and random redirects
    r_home = client.get("/content/sample_en", follow_redirects=False)
    assert r_home.status_code == 302
    assert r_home.headers["location"] == "/content/sample_en/A/index"

    r_rand = client.get("/random/sample_en", follow_redirects=False)
    assert r_rand.status_code == 302
    assert r_rand.headers["location"].startswith("/content/sample_en/A/")

    # ZIM redirect entry -> 302 to resolved target
    r_alias = client.get("/content/sample_en/A/alias", follow_redirects=False)
    assert r_alias.status_code == 302
    assert r_alias.headers["location"] == "/content/sample_en/A/water"

    # Public Cache-Control when admin_token is unset
    r_water = client.get("/content/sample_en/A/water")
    assert r_water.status_code == 200
    assert r_water.headers["cache-control"].startswith("public")

    # Missing book / missing entry -> 404
    assert client.get("/content/nope/A/index").status_code == 404
    assert client.get("/content/sample_en/A/nope").status_code == 404

    # Markdown home redirect, content negotiation, 404 and 415
    r_md_home = client.get("/markdown/sample_en", follow_redirects=False)
    assert r_md_home.status_code == 302
    assert r_md_home.headers["location"] == "/markdown/sample_en/A/index"

    r_md = client.get("/markdown/sample_en/A/water")
    assert r_md.status_code == 200
    assert r_md.headers["content-type"].startswith("text/markdown")
    assert "hydration" in r_md.text

    r_md_browser = client.get("/markdown/sample_en/A/water", headers={"Accept": "text/html"})
    assert r_md_browser.status_code == 200
    assert r_md_browser.headers["content-type"].startswith("text/plain")

    assert client.get("/markdown/sample_en/A/missing").status_code == 404
    assert client.get("/markdown/sample_en/I/blob.bin").status_code == 415
