import asyncio
import os
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from zim_pro import access
from zim_pro.config import Config
from zim_pro.db import Database, StartupError
from zim_pro.events import EventBus
from zim_pro.main import build_context, create_app


def test_f1_lifespan_awaits_scan_thread_before_closing_db(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    state_dir = tmp_path / "state"
    data_dir.mkdir()
    state_dir.mkdir()
    app = create_app(Config(data_dir=data_dir, state_dir=state_dir))
    ctx = app.state.ctx

    scan_started = threading.Event()
    allow_scan_finish = threading.Event()
    scan_finished = threading.Event()
    close_order: list[str] = []
    original_close = ctx.db.close

    calls = 0

    def slow_scan():
        nonlocal calls
        calls += 1
        if calls == 2:
            scan_started.set()
            allow_scan_finish.wait(timeout=5)
            ctx.db.scalar("SELECT 1")
            scan_finished.set()
        return {"added": 0, "updated": 0, "removed": 0}

    def tracked_close():
        close_order.append("db_closed_after_scan" if scan_finished.is_set() else "db_closed_early")
        original_close()

    async def fake_run(_ctx):
        fut = asyncio.ensure_future(asyncio.to_thread(ctx.scanner.scan))
        try:
            await asyncio.shield(fut)
        except asyncio.CancelledError:
            await fut
            raise

    async def failing_stop():
        raise RuntimeError("boom during stop")

    monkeypatch.setattr(ctx.scanner, "scan", slow_scan)
    monkeypatch.setattr(ctx.db, "close", tracked_close)
    monkeypatch.setattr("zim_pro.scheduler.run", fake_run)
    monkeypatch.setattr(ctx.monitor, "stop", failing_stop)

    with TestClient(app):
        assert scan_started.wait(timeout=5)
        allow_scan_finish.set()

    assert close_order == ["db_closed_after_scan"]


def test_f2_partial_unique_indexes_block_duplicate_active_jobs():
    db = Database(":memory:")
    db.execute(
        "INSERT INTO downloads (catalog_uuid, book_id, url, state) "
        "VALUES ('u1', 'b1', 'https://x/1.zim', 'queued')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO downloads (catalog_uuid, book_id, url, state) "
            "VALUES ('u1', 'b1', 'https://x/1.zim', 'downloading')"
        )
    # Inactive states are allowed to have multiple historical rows.
    db.execute(
        "INSERT INTO downloads (catalog_uuid, book_id, url, state) "
        "VALUES ('u1', 'b1', 'https://x/1.zim', 'done')"
    )

    db.execute(
        "INSERT INTO builds (kind, source_id, title, state) VALUES ('link', 1, 't', 'queued')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO builds (kind, source_id, title, state) "
            "VALUES ('link', 1, 't', 'processing')"
        )
    db.execute(
        "INSERT INTO builds (kind, source_id, title, state) VALUES ('link', 1, 't', 'finished')"
    )
    db.close()


def test_f3_feed_foreign_keys_cascade_and_set_null():
    db = Database(":memory:")
    db.execute("INSERT INTO feeds (id, title) VALUES (1, 'News')")
    db.execute("INSERT INTO feed_sources (id, feed_id, url) VALUES (10, 1, 'https://a.test/rss')")
    db.execute("INSERT INTO feed_items (id, feed_id, source_id, guid) VALUES (100, 1, 10, 'g1')")

    db.execute("DELETE FROM feed_sources WHERE id = 10")
    item = db.one("SELECT source_id FROM feed_items WHERE id = 100")
    assert item == {"source_id": None}

    db.execute("DELETE FROM feeds WHERE id = 1")
    assert db.all("SELECT * FROM feed_items") == []
    assert db.all("SELECT * FROM feed_sources") == []
    db.close()


def test_f4_startup_errors_raise_startup_error(tmp_path):
    ro_dir = tmp_path / "ro"
    ro_dir.mkdir()
    ro_dir.chmod(0o500)
    try:
        if os.geteuid() != 0:
            with pytest.raises(StartupError, match="Cannot open the database"):
                Database(ro_dir / "kiwix.sqlite3")
    finally:
        ro_dir.chmod(0o700)

    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a sqlite file at all")
    with pytest.raises(StartupError, match="Cannot open the database"):
        Database(corrupt)

    bad_data = tmp_path / "not_a_dir"
    bad_data.write_text("file")
    with pytest.raises(StartupError, match="Cannot use library directory"):
        build_context(Config(data_dir=bad_data, state_dir=tmp_path / "state"))


def test_f5_corrupt_json_in_settings_falls_back_to_default(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO settings (key, value) VALUES ('log_level', 'INFO')")
    conn.execute("INSERT INTO settings (key, value) VALUES ('_broken', '{bad')")
    conn.commit()
    conn.close()

    db = Database(path)
    assert db.get_setting("log_level") == "INFO"
    assert db.get_meta("broken") is None
    db.close()


def test_f6_mcp_auth_does_not_block_event_loop(tmp_path, monkeypatch):
    app = create_app(Config(data_dir=tmp_path / "data", state_dir=tmp_path / "state"))
    ctx = app.state.ctx
    access.create_key(ctx.db, "test")

    entered = threading.Event()
    release = threading.Event()

    def slow_check(_db, _key):
        entered.set()
        release.wait(timeout=5)
        return False

    monkeypatch.setattr(access, "check_key", slow_check)

    with TestClient(app) as client:
        mcp_result: list[int] = []

        def call_mcp():
            resp = client.get("/mcp", headers={"Authorization": "Bearer zp_mcp_wrong"})
            mcp_result.append(resp.status_code)

        t = threading.Thread(target=call_mcp)
        t.start()
        assert entered.wait(timeout=5)
        health = client.get("/api/health")
        assert health.status_code == 200
        release.set()
        t.join(timeout=5)
        assert mcp_result == [401]


def test_f7_check_constraints_reject_invalid_values():
    db = Database(":memory:")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO downloads (catalog_uuid, book_id, url, state) "
            "VALUES ('u', 'b', 'https://x', 'finished')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO builds (kind, source_id, state) VALUES ('whatever', 1, 'queued')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO books (id, path, filename, size, mtime, policy) "
            "VALUES ('b', '/b.zim', 'b.zim', 1, 1.0, 'bogus')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO settings (key, value) VALUES ('k', 'not-json')")
    db.close()


def test_f8_admin_token_validation_and_case_insensitive_bearer(tmp_path):
    with pytest.raises(ValidationError):
        Config(admin_token="")

    app = create_app(
        Config(
            data_dir=tmp_path / "data",
            state_dir=tmp_path / "state",
            admin_token="secret-token",
        )
    )
    with TestClient(app) as client:
        assert client.get("/api/mcp/keys").status_code == 401
        assert (
            client.get("/api/mcp/keys", headers={"Authorization": "Bearer wrong"}).status_code
            == 401
        )
        assert (
            client.get(
                "/api/mcp/keys", headers={"authorization": "bearer secret-token"}
            ).status_code
            == 200
        )
        assert client.get("/mcp", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert (
            client.get("/mcp", headers={"authorization": "bearer secret-token"}).status_code != 401
        )


def test_f10_busy_timeout_waits_for_concurrent_writer(tmp_path):
    db_path = tmp_path / "shared.sqlite3"
    db1 = Database(db_path)
    db2 = Database(db_path)
    assert db1.scalar("PRAGMA busy_timeout") == 15000

    db1.execute("BEGIN IMMEDIATE")
    db1.execute("INSERT INTO categories (name) VALUES ('one')")

    def release_later():
        time.sleep(0.2)
        db1.execute("COMMIT")

    t = threading.Thread(target=release_later)
    t.start()
    db2.execute("INSERT INTO categories (name) VALUES ('two')")
    t.join(timeout=5)
    assert len(db2.all("SELECT name FROM categories")) == 2
    db1.close()
    db2.close()


def test_f11_event_bus_safe_before_bind_and_after_close():
    bus = EventBus()

    async def setup_and_close():
        loop = asyncio.get_running_loop()
        q = bus.subscribe()
        bus.bind(loop)
        bus.publish("ping", {"ok": True})
        await asyncio.sleep(0)
        assert not q.empty()
        return loop

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(setup_and_close())
    finally:
        loop.close()

    # Publishing from a thread after the loop is closed must not raise.
    bus.publish("after_close", {"ok": False})
    bus.unbind()
    bus.publish("after_unbind", {"ok": False})


def test_f12_migration_from_040_schema_is_transactional_and_idempotent(tmp_path):
    db_path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE books (
            id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, filename TEXT NOT NULL,
            size INTEGER NOT NULL, mtime REAL NOT NULL
        );
        CREATE TABLE downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT, catalog_uuid TEXT NOT NULL,
            book_id TEXT NOT NULL, url TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued'
        );
        CREATE TABLE links (
            id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL UNIQUE
        );
        CREATE TABLE uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL,
            kind TEXT NOT NULL, path TEXT NOT NULL
        );
        CREATE TABLE builds (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
            source_id INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'queued'
        );
        CREATE TABLE feeds (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL DEFAULT '',
            updated REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
            role TEXT NOT NULL, content TEXT NOT NULL
        );
        CREATE TABLE ai_providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            kind TEXT NOT NULL, url TEXT NOT NULL, api_key TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO chats (id, title) VALUES (1, 'Saved Chat');
        INSERT INTO chat_messages (chat_id, role, content) VALUES (1, 'user', 'Hello');
        INSERT INTO ai_providers (name, kind, url) VALUES ('Local', 'openai', 'http://localhost:11434');
        """
    )
    conn.commit()
    conn.close()

    db1 = Database(db_path)
    assert db1.scalar("PRAGMA user_version") == Database.SCHEMA_VERSION
    build_cols = {r["name"] for r in db1.all("PRAGMA table_info(builds)")}
    assert {"params", "position"}.issubset(build_cols)
    dl_cols = {r["name"] for r in db1.all("PRAGMA table_info(downloads)")}
    assert {"started", "finished"}.issubset(dl_cols)
    msg_cols = {r["name"] for r in db1.all("PRAGMA table_info(chat_messages)")}
    assert {"model", "reasoning", "thought_seconds", "steps"}.issubset(msg_cols)
    prov_cols = {r["name"] for r in db1.all("PRAGMA table_info(ai_providers)")}
    assert "enabled" in prov_cols
    assert db1.scalar("SELECT content FROM chat_messages WHERE chat_id = 1") == "Hello"
    assert db1.scalar("SELECT enabled FROM ai_providers WHERE name = 'Local'") == 1
    db1.close()

    db2 = Database(db_path)
    assert db2.scalar("PRAGMA user_version") == Database.SCHEMA_VERSION
    db2.close()


def test_f14_nested_transaction_and_rollback():
    db = Database(":memory:")
    with db.transaction():
        db.execute("INSERT INTO categories (name) VALUES ('outer')")
        with pytest.raises(RuntimeError):
            with db.transaction():
                db.execute("INSERT INTO categories (name) VALUES ('inner')")
                raise RuntimeError("inner fail")
        db.set_settings({"log_level": "DEBUG"})
    assert [r["name"] for r in db.all("SELECT name FROM categories")] == ["outer"]
    assert db.get_setting("log_level") == "DEBUG"
    db.close()


def test_f15_access_check_key_survives_write_error_and_corrupt_digest(monkeypatch):
    db = Database(":memory:")
    row, key = access.create_key(db, "ci")
    db.execute("UPDATE mcp_keys SET last_used = 0 WHERE id = ?", (row["id"],))
    db.execute(
        "INSERT INTO mcp_keys (name, hint, digest) VALUES ('bad', 'x', ?)",
        ("non-ascii-\u00e9",),
    )

    real_execute = db.execute

    def failing_update(sql, params=()):
        if sql.startswith("UPDATE mcp_keys"):
            raise sqlite3.OperationalError("database is locked")
        return real_execute(sql, params)

    monkeypatch.setattr(db, "execute", failing_update)
    assert access.check_key(db, key) is True
    db.close()


def test_f16_set_settings_rejects_unknown_keys():
    db = Database(":memory:")
    with pytest.raises(KeyError):
        db.set_settings({"nonexistent_setting": 1})
    db.close()


def test_f17_missing_zim_file_returns_404_not_500(tmp_path):
    app = create_app(Config(data_dir=tmp_path / "data", state_dir=tmp_path / "state"))
    ctx = app.state.ctx
    ctx.db.execute(
        "INSERT INTO books (id, path, filename, size, mtime) "
        "VALUES ('missing_book', ?, 'missing.zim', 100, 1.0)",
        (str(tmp_path / "data" / "missing.zim"),),
    )
    with pytest.raises(LookupError):
        ctx.archive("missing_book")

    with TestClient(app) as client:
        resp = client.get("/content/missing_book/A/index.html")
        assert resp.status_code == 404


def test_f18_web_dir_without_assets_and_spa_404s(tmp_path):
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "index.html").write_text("<html>ok</html>")

    app = create_app(
        Config(data_dir=tmp_path / "data", state_dir=tmp_path / "state", web_dir=web_dir)
    )
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api").status_code == 404
        assert client.get("/content").status_code == 404
        assert client.get("/favicon.ico").status_code == 404
