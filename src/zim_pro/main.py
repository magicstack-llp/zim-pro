import asyncio
import hmac
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import access, logs, mcp_server, providers, scheduler
from .api import access as access_api
from .api import ai, build, catalog, content, downloads, library, organise, system
from .api import feeds as feeds_api
from .api import network as network_api
from .builder.jobs import BuildManager
from .config import Config
from .context import Context
from .db import Database
from .downloads.manager import DownloadManager
from .events import EventBus
from .feeds.manager import FeedManager
from .library.archive_pool import ArchivePool
from .library.scanner import Scanner
from .monitor import Monitor, metrics, route_group
from .network import Network
from .queues import make_broker

log = logging.getLogger(__name__)
http_log = logging.getLogger("zim_pro.http")

SLOW_REQUEST = 3.0  # seconds; a request slower than this is logged as a warning
UNTIMED = ("/api/events", "/api/health")  # long-lived, or asked every few seconds
LOG_FLUSH_SECONDS = 1


def build_context(config: Config) -> Context:
    db = Database(config.db_path)
    logs.setup(str(db.get_setting("log_level")), config.state_dir / "logs")
    broker = make_broker(db, config.redis_url)
    providers.migrate_single_provider(db)
    providers.seed_free_providers(db)
    pool = ArchivePool()
    scanner = Scanner(db, pool, config.data_dir)
    events = EventBus()
    manager = DownloadManager(db, events, scanner, pool, config.data_dir, broker)
    builds = BuildManager(db, events, scanner, pool, config.data_dir, config.state_dir, broker)
    feeds = FeedManager(db, events, scanner, pool, config.data_dir, config.state_dir, broker)
    network = Network(db, events)
    builds.network = feeds.network = network
    ctx = Context(config, db, pool, scanner, events, manager, builds, feeds, network, broker)
    ctx.monitor = Monitor(ctx)
    return ctx


async def send_logs(ctx: Context) -> None:
    """Copy new log lines to Redis, when the queues are kept there."""
    while True:
        await asyncio.sleep(LOG_FLUSH_SECONDS)
        lines = logs.hub.take_outbox()
        if lines and ctx.broker.kind == "redis":
            await asyncio.to_thread(ctx.broker.send_logs, lines)


def find_web_dir(config: Config) -> Path | None:
    here = Path(__file__).resolve()
    # Set explicitly, bundled inside an installed package, or built in a source checkout.
    candidates = [config.web_dir, here.parent / "web", here.parents[2] / "web" / "dist"]
    return next((p for p in candidates if p and (p / "index.html").is_file()), None)


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config()
    ctx = build_context(config)
    mcp = mcp_server.build_mcp(ctx)
    mcp_app = mcp_server.http_app(mcp)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx.events.bind(asyncio.get_running_loop())
        log.info("Zim Pro is starting: library %s, state %s", config.data_dir, config.state_dir)
        result = await asyncio.to_thread(ctx.scanner.scan)
        log.info("library scanned: %s", result)
        await ctx.downloads.start()
        await ctx.builds.start()
        await ctx.feeds.start()
        assert ctx.monitor is not None
        await ctx.monitor.start()
        background = asyncio.create_task(scheduler.run(ctx))
        shipping = asyncio.create_task(send_logs(ctx))
        async with mcp.session_manager.run():
            yield
        log.info("Zim Pro is stopping")
        background.cancel()
        shipping.cancel()
        await ctx.monitor.stop()
        await ctx.feeds.stop()
        await ctx.builds.stop()
        await ctx.downloads.stop()
        ctx.broker.close()
        ctx.db.close()

    app = FastAPI(title="Zim Pro", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.state.ctx = ctx

    @app.middleware("http")
    async def mcp_auth(request: Request, call_next):
        # /mcp is open until it is given a lock: the admin token, or a key made in Settings.
        if request.url.path.startswith("/mcp"):
            locked = bool(config.admin_token) or access.has_keys(ctx.db)
            supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            is_admin = bool(config.admin_token) and hmac.compare_digest(
                supplied.encode(), config.admin_token.encode()
            )
            if locked and not is_admin and not access.check_key(ctx.db, supplied):
                return JSONResponse(
                    {"detail": "An MCP key is required: Authorization: Bearer <key>"},
                    401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
        return await call_next(request)

    @app.middleware("http")
    async def timed(request: Request, call_next):
        """Count every request and its time; log the failed and slow ones. Added last, it
        is the outermost, so it sees the answers of the other middleware too."""
        path = request.url.path
        if path.startswith(UNTIMED):
            return await call_next(request)
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            seconds = time.perf_counter() - started
            metrics.request(route_group(path), status, seconds)
            if status >= 500:
                level = logging.ERROR
            elif status in (401, 403) or seconds >= SLOW_REQUEST:
                level = logging.WARNING
            else:
                level = logging.DEBUG
            if http_log.isEnabledFor(level):
                http_log.log(
                    level,
                    "%s %s answered %d in %.0f ms",
                    request.method,
                    path,
                    status,
                    seconds * 1000,
                )

    for module in (
        content,
        library,
        catalog,
        downloads,
        ai,
        build,
        feeds_api,
        network_api,
        organise,
        access_api,
        system,
    ):
        app.include_router(module.router)
    app.router.routes.extend(mcp_app.routes)

    web_dir = find_web_dir(config)
    if web_dir:
        app.mount("/assets", StaticFiles(directory=web_dir / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith(("api/", "content/", "mcp")):
                raise HTTPException(404)
            file = (web_dir / path).resolve()
            if path and file.is_file() and web_dir.resolve() in file.parents:
                return FileResponse(file)
            return FileResponse(web_dir / "index.html", headers={"Cache-Control": "no-cache"})

    return app
