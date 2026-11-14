import asyncio
from types import SimpleNamespace

import pytest

from zim_pro import scheduler
from zim_pro.config import Config
from zim_pro.db import Database
from zim_pro.events import EventBus


def _ctx(tmp_path) -> SimpleNamespace:
    data = tmp_path / "data"
    state = tmp_path / "state"
    data.mkdir(exist_ok=True)
    state.mkdir(exist_ok=True)
    return SimpleNamespace(
        config=Config(
            data_dir=data,
            state_dir=state,
            catalog_url="http://127.0.0.1:0/catalog.xml",
        ),
        db=Database(":memory:"),
        events=EventBus(),
        downloads=None,
        builds=None,
        scanner=SimpleNamespace(scan=lambda: {"books": 0, "read": 0, "removed": 0}),
    )


def test_scheduler_queue_auto_updates_skips_pinned_and_active(tmp_path):
    ctx = _ctx(tmp_path)
    enqueued: list[str] = []
    ctx.downloads = SimpleNamespace(enqueue=lambda uuid: enqueued.append(uuid))

    # Local books: one auto, one pinned, one auto already downloading
    for bid, name, policy in (
        ("b_auto", "alpha_en", "auto"),
        ("b_pin", "beta_en", "pinned"),
        ("b_busy", "gamma_en", "auto"),
    ):
        ctx.db.execute(
            "INSERT INTO books (id, path, filename, size, mtime, name, date, policy) "
            "VALUES (?, ?, ?, 100, 1.0, ?, '2025-01-01', ?)",
            (bid, str(tmp_path / "data" / f"{bid}.zim"), f"{bid}.zim", name, policy),
        )
    for uuid, name in (
        ("u-auto", "alpha_en"),
        ("u-pin", "beta_en"),
        ("u-busy", "gamma_en"),
    ):
        ctx.db.execute(
            "INSERT INTO catalog (uuid, name, stem, title, issued, url) "
            "VALUES (?, ?, ?, ?, '2026-01-01', ?)",
            (uuid, name, name, name, f"https://example.org/{uuid}.zim"),
        )
    ctx.db.execute(
        "INSERT INTO downloads (catalog_uuid, book_id, url, state) "
        "VALUES ('u-busy', 'b_busy', 'https://example.org/u-busy.zim', 'downloading')"
    )

    count = scheduler.queue_auto_updates(ctx)
    assert count == 1
    assert enqueued == ["u-auto"]


def test_scheduler_rebuild_due_links_queues_once(tmp_path):
    ctx = _ctx(tmp_path)
    queued: list[tuple[str, int, str]] = []

    def fake_enqueue(kind: str, source_id: int, label: str):
        queued.append((kind, source_id, label))
        ctx.db.execute(
            "INSERT INTO builds (kind, source_id, title, state) VALUES (?, ?, ?, 'queued')",
            (kind, source_id, label),
        )

    ctx.builds = SimpleNamespace(enqueue=fake_enqueue)
    ctx.db.execute(
        "INSERT INTO links (url, title, book_id, rebuild_days, last_built) "
        "VALUES ('https://due.test', 'Due', 'due_book', 7, unixepoch() - 86400 * 10)"
    )
    ctx.db.execute(
        "INSERT INTO links (url, title, book_id, rebuild_days, last_built) "
        "VALUES ('https://fresh.test', 'Fresh', 'fresh_book', 7, unixepoch() - 60)"
    )

    scheduler._rebuild_due_links(ctx)
    assert queued == [("link", 1, "Due")]

    # Second call does not duplicate the queued build
    scheduler._rebuild_due_links(ctx)
    assert len(queued) == 1


async def test_scheduler_refresh_catalog_and_run_loop(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(ctx.events, "publish", lambda event, data: published.append((event, data)))

    async def fail_refresh(_db, _url):
        raise RuntimeError("catalog unreachable")

    monkeypatch.setattr("zim_pro.scheduler.opds.refresh", fail_refresh)
    await scheduler.refresh_catalog(ctx)
    assert ctx.db.get_meta("catalog_error") == "catalog unreachable"
    assert ctx.db.get_meta("catalog_retry_after") is not None

    async def ok_refresh(_db, _url):
        return 42

    monkeypatch.setattr("zim_pro.scheduler.opds.refresh", ok_refresh)
    monkeypatch.setattr("zim_pro.scheduler.queue_auto_updates", lambda _ctx: 0)
    await scheduler.refresh_catalog(ctx)
    assert ctx.db.get_meta("catalog_error") is None
    assert ("catalog", {"entries": 42}) in published

    # Exercise one iteration of scheduler.run() and clean cancellation
    ctx.db.set_meta("catalog_refreshed", 0)
    ctx.db.set_meta("catalog_retry_after", 0)
    monkeypatch.setattr("zim_pro.scheduler.RESCAN_SECONDS", 0)

    async def cancel_sleep(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr("zim_pro.scheduler.asyncio.sleep", cancel_sleep)
    with pytest.raises(asyncio.CancelledError):
        await scheduler.run(ctx)


def test_negative_api_paths_across_build_downloads_catalog_feeds_library(make_client, monkeypatch):
    client = make_client()
    ctx = client.app.state.ctx

    # api/build.py uploads: empty (422), wrong signature (415), oversize (413)
    assert (
        client.post(
            "/api/build/uploads",
            files={"file": ("empty.pdf", b"", "application/pdf")},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/build/uploads",
            files={"file": ("fake.pdf", b"not-a-pdf", "application/pdf")},
        ).status_code
        == 415
    )
    ctx.db.set_settings({"upload_limit_mb": 0})
    assert (
        client.post(
            "/api/build/uploads",
            files={"file": ("big.pdf", b"%PDF-1.4\n123456", "application/pdf")},
        ).status_code
        == 413
    )

    # api/downloads.py: unknown action / missing download -> 404
    assert client.post("/api/downloads/999/unknown").status_code == 404
    assert client.post("/api/downloads/999/pause").status_code == 404

    # api/catalog.py: start_update 404 for unknown book, 409 when already downloading
    assert client.post("/api/updates/nonexistent/start").status_code == 404
    ctx.db.execute(
        "INSERT INTO books (id, path, filename, size, mtime, name, date, policy) "
        "VALUES ('b1', '/tmp/b1.zim', 'b1.zim', 10, 1.0, 'b1_en', '2025-01-01', 'auto')"
    )
    ctx.db.execute(
        "INSERT INTO catalog (uuid, name, stem, title, issued, url) "
        "VALUES ('u1', 'b1_en', 'b1_en', 'B1', '2026-01-01', 'https://example.org/b1.zim')"
    )
    ctx.db.execute(
        "INSERT INTO downloads (catalog_uuid, book_id, url, state) "
        "VALUES ('u1', 'b1', 'https://example.org/b1.zim', 'downloading')"
    )
    assert client.post("/api/updates/b1/start").status_code == 409

    # api/feeds.py: 422 on invalid mode/interval, 404 on missing feed
    assert (
        client.post(
            "/api/feeds",
            json={"title": "Bad", "sources": "https://a.test/rss", "mode": "bogus"},
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/feeds/999",
            json={"title": "Nope", "sources": "https://a.test/rss"},
        ).status_code
        == 404
    )
    assert client.delete("/api/feeds/999").status_code == 404

    # api/library.py: 404 on missing book
    assert client.get("/api/books/nonexistent").status_code == 404
    assert client.delete("/api/books/nonexistent").status_code == 404
