import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..context import Context, get_context, require_admin

router = APIRouter(prefix="/api")


@router.get("/downloads")
def list_downloads(ctx: Context = Depends(get_context)) -> list[dict[str, Any]]:
    return ctx.downloads.list()


class NewDownload(BaseModel):
    catalog_uuid: str


@router.post("/downloads", dependencies=[Depends(require_admin)])
async def create_download(body: NewDownload, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    try:
        return ctx.downloads.enqueue(body.catalog_uuid)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/downloads/clear-finished", dependencies=[Depends(require_admin)])
async def clear_finished(ctx: Context = Depends(get_context)) -> list[dict[str, Any]]:
    ctx.downloads.clear_finished()
    return ctx.downloads.list()


@router.post("/downloads/{download_id}/{action}", dependencies=[Depends(require_admin)])
async def act(download_id: int, action: str, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    if ctx.downloads.get(download_id) is None:
        raise HTTPException(404, "No such download")
    match action:
        case "pause":
            ctx.downloads.pause(download_id)
        case "resume":
            ctx.downloads.resume(download_id)
        case "cancel":
            ctx.downloads.cancel(download_id)
        case "top":
            ctx.downloads.move(download_id, to_top=True)
        case "bottom":
            ctx.downloads.move(download_id, to_top=False)
        case _:
            raise HTTPException(404, "Unknown action")
    return ctx.downloads.get(download_id) or {}


@router.delete("/downloads/{download_id}", dependencies=[Depends(require_admin)])
async def delete_download(download_id: int, ctx: Context = Depends(get_context)) -> dict[str, bool]:
    ctx.downloads.remove(download_id)
    return {"ok": True}


@router.get("/events")
async def events(request: Request, ctx: Context = Depends(get_context)) -> StreamingResponse:
    """Server-sent events: download progress and library or catalog changes."""
    queue = ctx.events.subscribe()

    async def stream():
        try:
            yield ": connected\n\n"
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=20)
                except TimeoutError:
                    yield ": keepalive\n\n"
                if await request.is_disconnected():
                    break
        finally:
            ctx.events.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
