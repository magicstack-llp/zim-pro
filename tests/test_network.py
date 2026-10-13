import asyncio
import io
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from zim_pro.builder import extract, icons
from zim_pro.builder.crawler import Fetcher
from zim_pro.catalog.updates import book_updates
from zim_pro.config import Config
from zim_pro.db import Database
from zim_pro.events import EventBus
from zim_pro.library.scanner import file_under_categories
from zim_pro.main import create_app
from zim_pro.network import Network, NetworkError


class Gluetun:
    """The control server of a VPN container: a status, an address, and a key to ask with."""

    ADDRESSES = [
        {"public_ip": "185.65.134.10", "country": "Netherlands", "city": "Amsterdam"},
        {"public_ip": "146.70.20.7", "country": "Sweden", "city": "Stockholm"},
        {"public_ip": "193.32.126.5", "country": "Switzerland", "city": "Zurich"},
    ]

    def __init__(self, key: str = "secret", route: str = "vpn"):
        self.key, self.route = key, route
        self.status = "running"
        self.connection = 0
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        basic = httpx.BasicAuth("me", self.key)._auth_header
        allowed = (request.headers.get("x-api-key"), request.headers.get("authorization"))
        if self.key and self.key not in allowed and basic not in allowed:
            return httpx.Response(401, text="Unauthorized")
        if path == f"/v1/{self.route}/status":
            if request.method == "PUT":
                wanted = json.loads(request.content)["status"]
                if wanted == "running" and self.status == "stopped":
                    self.connection += 1  # a new connection comes up on another server
                self.status = wanted
                return httpx.Response(200, json={"outcome": wanted})
            return httpx.Response(200, json={"status": self.status})
        if path == "/v1/publicip/ip":
            if self.status != "running":
                return httpx.Response(200, json={"public_ip": ""})
            return httpx.Response(200, json=self.ADDRESSES[self.connection % 3])
        return httpx.Response(404)


@pytest.fixture
def quick(monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _seconds: real_sleep(0))
    monkeypatch.setattr("zim_pro.builder.crawler.HOST_DELAY", 0)
    monkeypatch.setattr("zim_pro.builder.crawler.SLOW_DELAY_MIN", 0)
    monkeypatch.setattr("zim_pro.builder.crawler.RATE_WAIT", 0)


def network(gluetun, **settings) -> Network:
    db = Database(":memory:")
    db.set_settings(
        {
            "build_proxy": "http://gluetun:8888",
            "vpn_control_url": "http://gluetun:8000/",
            "vpn_control_key": "secret",
            **settings,
        }
    )
    return Network(db, EventBus(), httpx.MockTransport(gluetun))


async def test_the_vpn_says_where_requests_leave_from():
    net = network(Gluetun())
    vpn = (await net.status())["vpn"]
    assert (vpn["status"], vpn["public_ip"], vpn["country"]) == (
        "running",
        "185.65.134.10",
        "Netherlands",
    )
    assert vpn["enabled"] and vpn["control"] and not vpn["error"]
    assert net.proxy == "http://gluetun:8888"

    # Switched off, the proxy is kept but not used; the answer is current, not the cached one.
    net.db.set_settings({"build_proxy_enabled": False})
    assert net.proxy == "" and not (await net.status())["vpn"]["enabled"]


async def test_a_wrong_key_is_said_plainly_and_basic_auth_works_too():
    wrong = network(Gluetun(), vpn_control_key="nope")
    assert "API key" in (await wrong.status())["vpn"]["error"]
    basic = network(
        Gluetun(), vpn_control_key="", vpn_control_user="me", vpn_control_password="secret"
    )
    assert (await basic.status())["vpn"]["public_ip"] == "185.65.134.10"


async def test_older_gluetun_is_asked_on_its_own_route():
    old = Gluetun(route="openvpn")
    net = network(old)
    assert (await net.status())["vpn"]["status"] == "running"
    assert await net.change_ip(forced=True)
    assert "PUT /v1/openvpn/status" in old.calls


async def test_changing_the_address_stops_and_starts_the_vpn(quick):
    gluetun = Gluetun()
    net = network(gluetun)
    assert await net.change_ip() is True
    puts = [c for c in gluetun.calls if c.startswith("PUT")]
    assert puts == ["PUT /v1/vpn/status", "PUT /v1/vpn/status"] and gluetun.status == "running"
    assert net.generation == 1 and net.changes == 1
    assert (await net.status())["vpn"]["public_ip"] == "146.70.20.7"

    # A request refused under the old address has its new one already.
    assert await net.change_ip(since=0) is True and net.changes == 1
    # The new address refused too, moments later: not changed again straight away.
    assert await net.change_ip(since=1) is False and net.changes == 1
    # Asked for by hand, it is.
    assert await net.change_ip(forced=True) is True and net.changes == 2


async def test_no_control_server_is_an_error_to_show():
    net = network(Gluetun(), vpn_control_url="")
    assert not net.can_change_ip
    with pytest.raises(NetworkError, match="control server"):
        await net.change_ip()


async def test_a_refusing_site_gets_a_new_address_when_that_is_switched_on(tmp_path, quick):
    gluetun = Gluetun()
    net = network(gluetun, vpn_change_ip=True)
    seen: list[int] = []

    def site(request: httpx.Request) -> httpx.Response:
        seen.append(gluetun.connection)
        if gluetun.connection == 0:  # the first address has asked too much
            return httpx.Response(429, headers={"retry-after": "3600"})
        return httpx.Response(200, content=b"<p>hello</p>", headers={"content-type": "text/html"})

    told: list[str] = []
    fetcher = Fetcher(
        tmp_path / "cache", 2, httpx.MockTransport(site), network=net, on_new_address=told.append
    )
    # ``proxy`` is what makes the VPN matter; the mock transport stands in for it here.
    fetcher._network = net
    try:
        got = await fetcher.get("https://big.test/page", 1_000_000)
    finally:
        await fetcher.close()
    assert got.file.read_bytes() == b"<p>hello</p>"
    assert seen == [0, 1] and fetcher.address_changes == 1 and told == ["big.test"]
    assert fetcher.slowed == 0  # the hour it asked for was not waited


async def test_without_the_switch_a_refusal_is_waited_for_as_before(tmp_path, quick):
    gluetun = Gluetun()
    net = network(gluetun)  # vpn_change_ip is off
    answers = iter([429, 200])

    def site(request: httpx.Request) -> httpx.Response:
        if next(answers) == 429:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})

    fetcher = Fetcher(tmp_path / "cache", 2, httpx.MockTransport(site), network=net)
    fetcher._network = net
    try:
        await fetcher.get("https://big.test/page", 1_000_000)
    finally:
        await fetcher.close()
    assert fetcher.slowed == 1 and fetcher.address_changes == 0 and gluetun.connection == 0


# -- categories and pictures ------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    (tmp_path / "data").mkdir()
    app = create_app(Config(data_dir=tmp_path / "data", state_dir=tmp_path / "state"))
    return TestClient(app)  # not entered: the background work is not what is tested here


def png(colour=(200, 30, 30), size=(64, 40)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, "PNG")
    return out.getvalue()


def test_categories_are_kept_as_a_list_and_follow_their_books(client):
    db = client.app.state.ctx.db
    assert client.post("/api/categories", json={"name": "  Dev   docs "}).status_code == 200
    assert client.post("/api/categories", json={"name": "dev docs"}).status_code == 409
    assert client.post("/api/categories", json={"name": "Wikipedia"}).status_code == 422
    assert client.post("/api/categories", json={"name": "a;b"}).status_code == 422

    # A link filed under a category that is not in the list yet adds it.
    link = client.post(
        "/api/build/links", json={"url": "https://example.org/guide", "category": "Cooking"}
    ).json()
    names = [c["name"] for c in client.get("/api/categories").json()["categories"]]
    assert names == ["Cooking", "Dev docs"]

    # The book made from the link is filed where the link is, and moves when it is renamed.
    db.execute("UPDATE links SET book_id = 'custom_guide' WHERE id = ?", (link["id"],))
    db.execute(
        "INSERT INTO books (id, path, filename, size, mtime, name, title, tags) "
        "VALUES ('custom_guide', '/x.zim', 'x.zim', 1, 1, 'custom_guide', 'Guide', "
        "'_category:custom')"
    )
    file_under_categories(db)
    assert db.one("SELECT category FROM books")["category"] == "Cooking"
    assert book_updates(db) == []  # made here: not something the Kiwix catalog updates

    renamed = client.put("/api/categories/cooking", json={"name": "Food"}).json()
    assert {"name": "Food", "used": 1} in renamed["categories"]
    assert db.one("SELECT category FROM books")["category"] == "Food"
    assert db.one("SELECT category FROM links")["category"] == "Food"

    client.delete("/api/categories/Food")
    assert db.one("SELECT category FROM books")["category"] == "custom"
    assert db.one("SELECT category FROM links")["category"] == ""


def test_a_picture_can_be_chosen_for_a_source_and_taken_away(client, tmp_path):
    link = client.post("/api/build/links", json={"url": "https://example.org/a"}).json()
    address = f"/api/icons/link/{link['id']}"
    assert client.get(address).status_code == 404
    refused = client.post(address, files={"file": ("x.png", b"not a picture", "image/png")})
    assert refused.status_code == 415
    assert client.post(address, files={"file": ("x.png", png(), "image/png")}).status_code == 200
    kept = Image.open(io.BytesIO(client.get(address).content))
    assert kept.size == (64, 64) and kept.format == "PNG"  # squared, not stretched
    assert client.get("/api/build/links").json()["links"][0]["icon"]
    assert icons.load(tmp_path / "state", "link", link["id"])

    assert client.post("/api/icons/link/999", files={"file": ("x.png", png())}).status_code == 404
    client.delete(address)
    assert client.get(address).status_code == 404
    assert client.get("/api/build/links").json()["links"][0]["icon"] is None


def test_the_sites_own_icons_are_tried_best_first():
    html = """<html><head><title>T</title>
      <link rel="icon" href="/favicon.svg" type="image/svg+xml">
      <link rel="icon" sizes="32x32" href="/icon-32.png">
      <link rel="icon" sizes="192x192" href="/icon-192.png">
      <link rel="apple-touch-icon" href="/touch.png">
      <meta property="og:image" content="/share.jpg">
      </head><body><article><p>Text enough to be a page.</p></article></body></html>"""
    prepared = extract.prepare(html, "https://site.test/")
    assert [a.rsplit("/", 1)[1] for a in prepared.icons] == [
        "touch.png",
        "icon-192.png",
        "icon-32.png",
        "favicon.svg",
    ]
    assert prepared.share_picture == "https://site.test/share.jpg"
    assert icons.readable(png()) and not icons.readable(
        b"<svg xmlns='http://www.w3.org/2000/svg'/>"
    )


def test_settings_keep_the_control_servers_secrets_to_the_server(client):
    saved = client.put(
        "/api/settings",
        json={"vpn_control_url": "http://gluetun:8000", "vpn_control_key": "k", "vpn_change_ip": 1},
    ).json()
    assert saved["vpn_control_key"] == "••••••••" and saved["vpn_change_ip"] is True
    # Saving the form again, mask and all, does not overwrite the key with the mask.
    client.put("/api/settings", json=saved)
    assert client.app.state.ctx.db.get_setting("vpn_control_key") == "k"
    assert client.put("/api/settings", json={"vpn_control_url": "gluetun"}).status_code == 422


def test_a_zim_can_be_filed_under_a_category_the_library_already_has(client):
    db = client.app.state.ctx.db
    db.execute(
        "INSERT INTO books (id, path, filename, size, mtime, name, title, tags, category) "
        "VALUES ('wikipedia_en_all', '/w.zim', 'w.zim', 1, 1, 'w', 'Wikipedia', '', 'wikipedia')"
    )
    listing = client.get("/api/categories").json()
    assert listing["library"] == [{"name": "wikipedia", "books": 1}]
    link = client.post(
        "/api/build/links", json={"url": "https://example.org/wiki", "category": "Wikipedia"}
    ).json()
    # Filed under the library's own spelling, and not added to the user's list.
    assert client.get("/api/build/links").json()["links"][0]["category"] == "wikipedia"
    assert client.get("/api/categories").json()["categories"] == []
    assert link["id"]


def test_mcp_is_open_until_a_key_is_made_and_keys_are_kept_only_as_hashes(tmp_path):
    (tmp_path / "data").mkdir()
    app = create_app(Config(data_dir=tmp_path / "data", state_dir=tmp_path / "state"))
    client = TestClient(app, raise_server_exceptions=False)
    db = app.state.ctx.db
    assert client.get("/api/mcp/keys").json() == {"keys": [], "locked": False, "admin_token": False}
    assert client.post("/mcp", json={}).status_code != 401  # no lock yet

    made = client.post("/api/mcp/keys", json={"name": "Claude Code"}).json()
    key = made["created"]["key"]
    assert key.startswith("zp_mcp_") and len(key) > 40 and made["locked"]
    # The key itself is nowhere in the database, nor in what is listed afterwards.
    stored = db.one("SELECT * FROM mcp_keys")
    assert key not in str(stored) and len(stored["digest"]) == 64
    assert "key" not in client.get("/api/mcp/keys").json()["keys"][0]

    assert client.post("/mcp", json={}).status_code == 401
    wrong = {"Authorization": "Bearer zp_mcp_" + "x" * 43}
    assert client.post("/mcp", json={}, headers=wrong).status_code == 401
    right = {"Authorization": f"Bearer {key}"}
    assert client.post("/mcp", json={}, headers=right).status_code != 401
    assert db.one("SELECT last_used FROM mcp_keys")["last_used"]

    client.delete(f"/api/mcp/keys/{stored['id']}")
    assert client.post("/mcp", json={}).status_code != 401  # the last key gone: open again


async def test_addresses_can_be_tried_before_they_are_saved(quick):
    gluetun = Gluetun()

    def world(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.cloudflare.com":  # what the proxy would have fetched
            return httpx.Response(200, text="fl=1\nip=185.65.134.10\nloc=NL\n")
        if request.url.host == "solver":
            return httpx.Response(200, json={"msg": "FlareSolverr is ready!", "version": "3.3.25"})
        return gluetun(request)

    net = Network(Database(":memory:"), EventBus(), httpx.MockTransport(world))
    typed = {
        "build_proxy": "http://gluetun:8888",
        "vpn_control_url": "http://gluetun:8000",
        "vpn_control_key": "secret",
        "vpn_control_user": "",
        "vpn_control_password": "",
    }
    tried = await net.try_vpn(typed)
    assert tried["control"]["ok"] and tried["control"]["country"] == "Netherlands"
    assert tried["proxy"] == {**tried["proxy"], "ok": True, "ip": "185.65.134.10", "country": "NL"}
    assert tried["same_address"] is True

    refused = await net.try_vpn({**typed, "vpn_control_key": "wrong"})
    assert not refused["control"]["ok"] and "API key" in refused["control"]["error"]
    assert refused["same_address"] is None

    assert (await net._look_at_solver("http://solver:8191"))["ready"]
    assert not net.db.get_setting("vpn_control_url")  # trying saved nothing


def test_about_says_what_is_running(client):
    about = client.get("/api/about").json()
    assert about["name"] == "Zim Pro" and about["version"] and about["python"]
    assert about["repository"].endswith("magicstack-llp/zim-pro")
