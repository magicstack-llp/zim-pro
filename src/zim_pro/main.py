import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import access, mcp_server, providers, scheduler
from .api import access as access_api
from .api import ai, build, catalog, content, downloads, library, organise
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
from .network import Network

log = logging.getLogger(__name__)


def build_context(config: Config) -> Context:
    db = Database(config.db_path)
    providers.migrate_single_provider(db)
    providers.seed_free_providers(db)
    pool = ArchivePool()
    scanner = Scanner(db, pool, config.data_dir)
    events = EventBus()
    manager = DownloadManager(db, events, scanner, pool, config.data_dir)
    builds = BuildManager(db, events, scanner, pool, config.data_dir, config.state_dir)
    feeds = FeedManager(db, events, scanner, pool, config.data_dir, config.state_dir)
    network = Network(db, events)
    builds.network = feeds.network = network
    return Context(config, db, pool, scanner, events, manager, builds, feeds, network)


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
        result = await asyncio.to_thread(ctx.scanner.scan)
        log.info("library scanned: %s", result)
        await ctx.downloads.start()
        await ctx.builds.start()
        await ctx.feeds.start()
        background = asyncio.create_task(scheduler.run(ctx))
        async with mcp.session_manager.run():
            yield
        background.cancel()
        await ctx.feeds.stop()
        await ctx.builds.stop()
        await ctx.downloads.stop()
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
