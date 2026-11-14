import asyncio
import hashlib
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from zim_pro.catalog import opds
from zim_pro.catalog.updates import book_updates
from zim_pro.config import Config
from zim_pro.db import Database
from zim_pro.downloads.engine import Download
from zim_pro.downloads.manager import DownloadManager
from zim_pro.downloads.metalink import Metalink
from zim_pro.events import EventBus
from zim_pro.library.archive_pool import ArchivePool
from zim_pro.library.scanner import Scanner
from zim_pro.main import create_app


def _make_manager(tmp_path: Path) -> tuple[Database, EventBus, DownloadManager]:
    db = Database(":memory:")
    events = EventBus()
    pool = ArchivePool(db)
    scanner = MagicMock(spec=Scanner)
    mgr = DownloadManager(db, events, scanner, pool, tmp_path)
    return db, events, mgr


def _seed_catalog(
    db: Database,
    *,
    uuid: str = "u1",
    stem: str = "wiki_en_all",
    name: str = "wiki_en_all",
    flavour: str = "",
    title: str = "Wiki",
    issued: str = "2026-02-01T00:00:00Z",
    url: str = "https://mirror.example/wiki_en_all_2026-02.zim.meta4",
    size: int = 1000,
) -> None:
    db.execute(
        "INSERT INTO catalog (uuid, stem, name, flavour, title, issued, url, size) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (uuid, stem, name, flavour, title, issued, url, size),
    )


def _seed_book(
    db: Database,
    *,
    book_id: str = "wiki_en_all",
    path: str = "/data/wiki_en_all_2026-01.zim",
    name: str = "wiki_en_all",
    flavour: str = "",
    date: str = "2026-01-01",
    uuid: str = "local-u0",
) -> None:
    db.execute(
        "INSERT INTO books (id, path, filename, size, mtime, name, flavour, date, uuid) "
        "VALUES (?, ?, ?, 100, 0, ?, ?, ?, ?)",
        (book_id, path, Path(path).name, name, flavour, date, uuid),
    )


# -- F10 & F11: OPDS parsing & refresh ---------------------------------------


def test_opds_relative_href_and_torrent_filtering():
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/terms/">
      <entry>
        <id>urn:uuid:e1</id>
        <title>Entry 1</title>
        <updated>2026-10-10T00:00:00Z</updated>
        <dc:issued>2026-10-05T00:00:00Z</dc:issued>
        <link rel="http://opds-spec.org/acquisition/open-access"
              type="application/x-zim"
              href="/zim/a_2026-10.zim.meta4" />
        <link rel="http://opds-spec.org/acquisition/open-access"
              type="application/x-bittorrent"
              href="/zim/a_2026-10.zim.torrent" length="999" />
      </entry>
    </feed>"""
    rows = opds.parse_feed(xml, "https://library.kiwix.org/catalog/v2/entries")
    assert len(rows) == 1
    assert rows[0]["url"] == "https://library.kiwix.org/zim/a_2026-10.zim.meta4"
    assert rows[0]["size"] == 0
    assert rows[0]["issued"] == "2026-10-05T00:00:00Z"


def test_opds_idless_entries_do_not_collide_and_updated_not_used_as_issued():
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>No ID 1</title>
        <updated>2026-10-10T00:00:00Z</updated>
        <link rel="http://opds-spec.org/acquisition/open-access"
              href="https://example.com/a_2026-10.zim.meta4" length="100" />
      </entry>
      <entry>
        <title>No ID 2</title>
        <updated>2026-10-10T00:00:00Z</updated>
        <link rel="http://opds-spec.org/acquisition/open-access"
              href="https://example.com/b_2026-10.zim.meta4" length="200" />
      </entry>
    </feed>"""
    rows = opds.parse_feed(xml, "https://example.com/")
    assert len(rows) == 2
    assert rows[0]["uuid"] != rows[1]["uuid"]
    assert rows[0]["issued"] == ""


def test_opds_store_rollback_preserves_old_catalog():
    db = Database(":memory:")
    _seed_catalog(db, uuid="existing")
    with pytest.raises(sqlite3.ProgrammingError):
        opds.store(db, [{"uuid": "incomplete"}])
    assert db.scalar("SELECT COUNT(*) FROM catalog") == 1


async def test_opds_refresh_rejects_truncated_feed(monkeypatch):
    db = Database(":memory:")
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:dc="http://purl.org/dc/terms/"
          xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
      <opensearch:totalResults>100</opensearch:totalResults>
      <entry>
        <id>urn:uuid:e1</id>
        <title>One</title>
        <dc:issued>2026-10-05T00:00:00Z</dc:issued>
        <link rel="http://opds-spec.org/acquisition/open-access"
              href="https://example.com/a_2026-10.zim.meta4" length="100" />
      </entry>
    </feed>"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=xml)

    real_client = httpx.AsyncClient

    def mock_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    with pytest.raises(RuntimeError, match="truncated"):
        await opds.refresh(db, "https://example.com/catalog")


# -- F12: Update semantics ---------------------------------------------------


def test_book_updates_empty_catalog_is_unknown():
    db = Database(":memory:")
    _seed_book(db)
    items = book_updates(db)
    assert len(items) == 1
    assert items[0]["status"] == "unknown"


def test_book_updates_same_day_repack_and_failed_download_excluded():
    db = Database(":memory:")
    _seed_catalog(db, uuid="new-uuid", issued="2026-02-01T12:00:00Z")
    _seed_book(db, date="2026-02-01", uuid="old-uuid")
    db.execute(
        "INSERT INTO downloads (catalog_uuid, book_id, title, url, state) "
        "VALUES ('new-uuid', 'wiki_en_all', 'Wiki', 'https://x/a.meta4', 'failed')"
    )
    items = book_updates(db)
    assert items[0]["status"] == "update"
    assert items[0]["download"] is None


def test_book_updates_month_only_date():
    db = Database(":memory:")
    _seed_catalog(db, uuid="u2", issued="2026-02-01T00:00:00Z")
    _seed_book(db, date="2026-02", uuid="u1")
    items = book_updates(db)
    assert items[0]["status"] == "current"


# -- F3, F6, F13, F14, F18: DownloadManager ----------------------------------


def test_enqueue_rejects_duplicate_target_and_older_release(tmp_path):
    db, _events, mgr = _make_manager(tmp_path)
    _seed_catalog(db, uuid="u-new", issued="2026-02-01T00:00:00Z")
    _seed_catalog(db, uuid="u-old", issued="2025-12-01T00:00:00Z")
    _seed_catalog(db, uuid="u-alt", issued="2026-02-02T00:00:00Z")
    _seed_book(db, date="2026-01-01", uuid="local")

    with pytest.raises(ValueError, match="newer release"):
        mgr.enqueue("u-old")

    first = mgr.enqueue("u-new")
    assert first["state"] == "queued"

    with pytest.raises(ValueError, match="already active"):
        mgr.enqueue("u-alt")


async def test_resume_clears_finished_and_bulk_publishes_once(tmp_path):
    db, events, mgr = _make_manager(tmp_path)
    q = events.subscribe()
    _seed_catalog(db, uuid="u1", stem="b1", name="b1", url="https://x/b1.zim.meta4")
    _seed_catalog(db, uuid="u2", stem="b2", name="b2", url="https://x/b2.zim.meta4")
    d1 = mgr.enqueue("u1")
    d2 = mgr.enqueue("u2")
    db.execute(
        "UPDATE downloads SET state = 'failed', finished = 12345.0 WHERE id IN (?, ?)",
        (d1["id"], d2["id"]),
    )
    while not q.empty():
        q.get_nowait()

    assert mgr.retry_failed() == 2
    assert q.qsize() == 1
    assert db.one("SELECT finished FROM downloads WHERE id = ?", (d1["id"],))["finished"] is None


async def test_remove_cancels_active_and_deletes_row(tmp_path):
    db, _events, mgr = _make_manager(tmp_path)
    _seed_catalog(db, uuid="u1")
    row = mgr.enqueue("u1")
    started = asyncio.Event()

    async def slow_task():
        try:
            started.set()
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            mgr._set_state(row["id"], "cancelled")
            mgr._active.pop(row["id"], None)

    task = asyncio.create_task(slow_task())
    await started.wait()
    from zim_pro.downloads.manager import _Active

    mgr._active[row["id"]] = _Active(task)
    db.execute("UPDATE downloads SET state = 'downloading' WHERE id = ?", (row["id"],))

    outcome = await mgr.remove(row["id"])
    assert outcome == "deleted"
    assert mgr.get(row["id"]) is None


async def test_bookkeeping_error_after_install_keeps_done(tmp_path, monkeypatch):
    db, _events, mgr = _make_manager(tmp_path)
    _seed_catalog(db, uuid="u1")
    row = mgr.enqueue("u1")
    data = b"hello"
    link = Metalink(
        "wiki_en_all_2026-02.zim",
        len(data),
        ["https://mirror.example/wiki.zim"],
        hashlib.sha256(data).hexdigest(),
        len(data),
        [hashlib.sha1(data).hexdigest()],
    )

    async def fake_fetch(_url):
        return link

    async def fake_run(self):
        self.part.write_bytes(data)

    def fake_install(_row, dl):
        dl.part.replace(dl.target)

    def broken_history(*_args, **_kwargs):
        raise RuntimeError("disk hiccup")

    monkeypatch.setattr(mgr, "_fetch_metalink", fake_fetch)
    monkeypatch.setattr(Download, "run", fake_run)
    monkeypatch.setattr(mgr, "_install", fake_install)
    monkeypatch.setattr(mgr, "_history", broken_history)

    from zim_pro.downloads.manager import _Active

    mgr._active[row["id"]] = _Active(asyncio.current_task())  # type: ignore[arg-type]
    await mgr._download(row["id"])
    assert mgr.get(row["id"])["state"] == "done"


async def test_fetch_metalink_preserves_query_and_retries_503(tmp_path, monkeypatch):
    _db, _events, mgr = _make_manager(tmp_path)
    seen_urls: list[str] = []
    data = b"zim"
    xml = (
        b'<metalink xmlns="urn:ietf:params:xml:ns:metalink">'
        b'<file name="a.zim"><size>3</size>'
        b'<hash type="sha-256">' + hashlib.sha256(data).hexdigest().encode() + b"</hash>"
        b"<url>https://mirror.example/a.zim</url></file></metalink>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if len(seen_urls) == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=xml)

    real_client = httpx.AsyncClient
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _s: real_sleep(0))

    def mock_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    link = await mgr._fetch_metalink("https://example.com/a.zim?token=1")
    assert seen_urls == [
        "https://example.com/a.zim.meta4?token=1",
        "https://example.com/a.zim.meta4?token=1",
    ]
    assert link.name == "a.zim"


async def test_loops_survive_single_exception(tmp_path, monkeypatch):
    db, _events, mgr = _make_manager(tmp_path)
    calls = 0

    def flaky_settings():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient db error")
        return db.get_settings()

    monkeypatch.setattr(db, "get_settings", flaky_settings)
    task = asyncio.create_task(mgr._run_queue())
    await asyncio.sleep(0.05)
    mgr._wake.set()
    await asyncio.sleep(0.05)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# -- F15 & F16: API endpoints ------------------------------------------------


def test_catalog_and_downloads_api_fixes(tmp_path):
    cfg = Config(data_dir=tmp_path / "data", state_dir=tmp_path / "state")
    app = create_app(cfg)
    with TestClient(app) as client:
        ctx = app.state.ctx
        _seed_catalog(ctx.db, uuid="u1", title="Python 100% Guide")
        _seed_catalog(ctx.db, uuid="u2", title="Bash Docs", stem="bash", name="bash")
        _seed_book(
            ctx.db, book_id="bash", path="/data/bash.zim", name="bash", date="", uuid="other"
        )

        res = client.get("/api/catalog", params={"q": "%"})
        assert res.status_code == 200
        body = res.json()
        assert body["limit"] == 48
        assert [r["uuid"] for r in body["results"]] == ["u1"]

        bash = client.get("/api/catalog", params={"q": "Bash"}).json()["results"][0]
        assert bash["local"] == "current"  # empty local date is not marked outdated

        assert client.delete("/api/downloads/999999").status_code == 404
        assert client.post("/api/downloads/999999/pause").status_code == 404
