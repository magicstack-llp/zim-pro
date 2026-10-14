import asyncio
import json
import logging

import fakeredis
import pytest
import redis
from fastapi.testclient import TestClient

from zim_pro import logs
from zim_pro.config import Config
from zim_pro.db import Database
from zim_pro.main import create_app
from zim_pro.monitor import Metrics, route_group
from zim_pro.queues import LOG_STREAM, LocalBroker, RedisBroker, new_place

# -- logs -------------------------------------------------------------------------------


@pytest.fixture
def hub(tmp_path):
    fresh = logs.LogHub()
    fresh.attach_file(tmp_path / "logs")
    logger = logging.getLogger("zim_pro.feeds.test")
    logger.addHandler(fresh)
    logger.setLevel(logging.DEBUG)
    yield fresh
    logger.removeHandler(fresh)


def test_every_line_says_where_it_came_from_and_for_which_job(hub):
    log = logging.getLogger("zim_pro.feeds.test")
    log.info("outside any job")

    async def sync():
        with logs.job("feed 7"):
            log.warning("inside the job")
            # Tasks and threads the job starts carry its name too.
            await asyncio.gather(asyncio.to_thread(log.info, "on a thread"))

    asyncio.run(sync())
    lines = hub.query()
    assert [line["message"] for line in lines] == [
        "on a thread",
        "inside the job",
        "outside any job",
    ]
    assert [line["job"] for line in lines] == ["feed 7", "feed 7", ""]
    assert {line["area"] for line in lines} == {"feeds"}
    # The area of a job's line follows the job, whichever module wrote it.
    with logs.job("download 3"):
        log.info("from a feed module, for a download")
    assert hub.query()[0]["area"] == "downloads"
    assert [line["message"] for line in hub.query(level="WARNING")] == ["inside the job"]
    assert len(hub.query(job_name="feed 7")) == 2
    assert [line["message"] for line in hub.query(text="THREAD")] == ["on a thread"]
    newest = hub.newest
    log.info("later")
    assert [line["message"] for line in hub.query(after=newest)] == ["later"]


def test_secrets_never_reach_the_logs(hub, tmp_path):
    log = logging.getLogger("zim_pro.feeds.test")
    log.info("asked with Authorization: Bearer abcdef1234567890 and api_key=sk-live-123456789012")
    log.info("password: hunter22 token=xyz98765")
    text = " ".join(line["message"] for line in hub.query())
    for secret in ("abcdef1234567890", "sk-live-123456789012", "hunter22", "xyz98765"):
        assert secret not in text
    assert secret not in (tmp_path / "logs" / "zim-pro.log").read_text()


def test_the_lines_from_before_a_restart_are_shown_again(hub, tmp_path):
    logging.getLogger("zim_pro.feeds.test").error("before the restart")
    again = logs.LogHub()
    again.attach_file(tmp_path / "logs")
    assert again.query()[0]["message"] == "before the restart"
    assert again.query()[0]["level"] == "ERROR"


def test_areas_follow_the_logger():
    assert logs.area_of("zim_pro.downloads.manager") == "downloads"
    assert logs.area_of("zim_pro.builder.crawler") == "builds"
    assert logs.area_of("zim_pro.providers") == "ai"
    assert logs.area_of("httpx") == "web"
    assert logs.area_of("uvicorn.error") == "server"
    assert logs.area_of("something.else") == "system"


# -- the queues -------------------------------------------------------------------------


def test_a_job_can_be_moved_anywhere_in_the_queue():
    order = [(1, 1.0), (2, 2.0), (3, 3.0), (4, 4.0)]

    def moved(job_id, where):
        score = new_place(order, job_id, where)
        assert score is not None
        return [
            j
            for j, _ in sorted(
                [(j, score if j == job_id else s) for j, s in order], key=lambda x: x[1]
            )
        ]

    assert moved(3, "top") == [3, 1, 2, 4]
    assert moved(2, "bottom") == [1, 3, 4, 2]
    assert moved(3, "up") == [1, 3, 2, 4]
    assert moved(2, "up") == [2, 1, 3, 4]
    assert moved(2, "down") == [1, 3, 2, 4]
    assert moved(3, "down") == [1, 2, 4, 3]
    assert new_place(order, 1, "top") is None and new_place(order, 9, "up") is None


def test_redis_keeps_the_queue_and_has_the_last_word_on_its_order():
    db = Database(":memory:")
    server = fakeredis.FakeRedis(decode_responses=True)
    broker = RedisBroker(db, "redis://fake", client=server)
    jobs = [(1, 1.0), (2, 2.0), (3, 3.0)]
    assert [j for j, _ in broker.waiting("builds", jobs)] == [1, 2, 3]
    assert server.zrange("zimpro:queue:builds", 0, -1) == ["1", "2", "3"]

    # Changed from outside: Redis's order wins.
    server.zadd("zimpro:queue:builds", {"3": 0})
    assert [j for j, _ in broker.waiting("builds", jobs)] == [3, 1, 2]
    # A job that is no longer waiting leaves Redis; a new one joins.
    assert [j for j, _ in broker.waiting("builds", [(1, 1.0), (3, 3.0), (5, 5.0)])] == [3, 1, 5]
    assert server.zrange("zimpro:queue:builds", 0, -1) == ["3", "1", "5"]

    broker.set_paused("builds", True)
    assert broker.paused("builds") and server.get("zimpro:queue:builds:paused") == "1"
    assert LocalBroker(db).paused("builds")  # kept in the database too
    # Resumed from outside: the database follows, so it is still right if Redis is emptied.
    server.set("zimpro:queue:builds:paused", "0")
    assert not broker.paused("builds") and not LocalBroker(db).paused("builds")
    broker.set_paused("builds", True)
    server.flushall()
    assert broker.paused("builds") and server.get("zimpro:queue:builds:paused") == "1"

    broker.send_logs([{"id": 1, "level": "INFO", "message": "hello"}])
    assert server.xlen(LOG_STREAM) == 1
    info = broker.info()
    assert info["ok"] and info["kind"] == "redis" and info["log_lines"] == 1


def test_without_redis_the_database_order_is_used_until_it_is_back():
    db = Database(":memory:")

    class Down(fakeredis.FakeRedis):
        broken = True

        def zrange(self, *args, **kwargs):
            if self.broken:
                raise redis.ConnectionError("refused")
            return super().zrange(*args, **kwargs)

    server = Down(decode_responses=True)
    broker = RedisBroker(db, "redis://:secret@fake:6379", client=server)
    assert [j for j, _ in broker.waiting("feeds", [(2, 2.0), (1, 1.0)])] == [1, 2]
    assert broker.error == "refused" and "secret" not in broker.info()["url"]

    server.broken = False
    broker._down_until = 0  # the pause after a failure is over
    assert [j for j, _ in broker.waiting("feeds", [(2, 2.0), (1, 1.0)])] == [1, 2]
    assert broker.error == "" and server.zrange("zimpro:queue:feeds", 0, -1) == ["1", "2"]


# -- monitoring -------------------------------------------------------------------------


def test_requests_and_fetches_are_counted():
    metrics = Metrics()
    metrics.request("/api/feeds", 200, 0.05)
    metrics.request("/api/feeds", 500, 0.2)
    metrics.fetched("a.test", True, 1000, 0.1)
    metrics.fetched("a.test", False, 0, 0.3, "HTTP 404")
    view = metrics.view()
    assert view["requests"]["/api/feeds"]["count"] == 2
    assert view["requests"]["/api/feeds"]["errors"] == 1
    assert view["fetches"]["bytes"] == 1000 and view["fetches"]["errors"] == 1
    assert view["hosts"][0]["host"] == "a.test" and view["hosts"][0]["last_error"] == "HTTP 404"
    assert route_group("/api/feeds/3/sync") == "/api/feeds"
    assert route_group("/content/wikipedia/A/Foo") == "/content"
    assert route_group("/library") == "pages"


# -- through the API --------------------------------------------------------------------


@pytest.fixture
def app_client(tmp_path):
    (tmp_path / "data").mkdir()
    app = create_app(Config(data_dir=tmp_path / "data", state_dir=tmp_path / "state"))
    with TestClient(app) as client:
        yield client


def test_settings_show_the_logs_monitoring_and_queues(app_client):
    app_client.get("/api/settings")
    logging.getLogger("zim_pro.downloads").warning("a warning to find")
    found = app_client.get("/api/logs", params={"level": "WARNING", "q": "to find"}).json()
    assert found["lines"][0]["message"] == "a warning to find"
    assert "downloads" in found["areas"]

    seen = app_client.get("/api/monitor").json()
    names = {check["name"]: check["status"] for check in seen["checks"]}
    assert names["Database"] == "ok" and names["Redis"] == "off"
    assert seen["process"]["rss"] > 0 and seen["disks"]

    queues = app_client.get("/api/queues").json()
    assert queues["broker"]["kind"] == "database"
    assert [q["name"] for q in queues["queues"]] == ["downloads", "builds", "feeds"]

    download = app_client.get("/api/logs/download")
    assert download.status_code == 200
    assert any(
        json.loads(line)["message"] == "a warning to find" for line in download.text.splitlines()
    )


def test_a_queue_can_be_paused_reordered_and_its_limit_changed(app_client):
    ctx = app_client.app.state.ctx
    assert app_client.post("/api/queues/builds/pause").json()["queue"]["paused"]
    for n in (1, 2, 3):
        ctx.builds.enqueue("link", n, f"Site {n}")
    queue = app_client.get("/api/queues").json()["queues"][1]
    assert [w["title"] for w in queue["waiting"]] == ["Site 1", "Site 2", "Site 3"]
    assert queue["running"] == []  # paused: nothing starts

    third = queue["waiting"][2]["id"]
    queue = app_client.post(f"/api/queues/builds/jobs/{third}/top").json()
    assert [w["title"] for w in queue["waiting"]] == ["Site 3", "Site 1", "Site 2"]

    queue = app_client.put("/api/queues/builds/concurrency", json={"value": 99}).json()
    assert queue["concurrency"] == 4  # held to what the queue allows

    queue = app_client.post("/api/queues/builds/cancel-waiting").json()["queue"]
    assert queue["waiting"] == [] and queue["counts"]["cancelled"] == 3
    assert app_client.post("/api/queues/nope/pause").status_code == 404
