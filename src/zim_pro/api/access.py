"""Keys for MCP clients, and what this server is: its version and where it keeps things."""

import platform
import sys
import time
from importlib import metadata
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import access
from ..context import Context, get_context, require_admin
from ..downloads.engine import USER_AGENT

router = APIRouter(prefix="/api")
STARTED = time.time()
REPOSITORY = "magicstack-llp/zim-pro"


def version() -> str:
    try:
        return metadata.version("zim-pro")
    except metadata.PackageNotFoundError:
        return "dev"


def _library_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return ""


class KeyBody(BaseModel):
    name: str = Field(default="", max_length=80)


def _keys(ctx: Context) -> dict[str, Any]:
    keys = access.list_keys(ctx.db)
    return {
        "keys": keys,
        # Who may call /mcp: anyone, or only a caller with a key (or the admin token).
        "locked": bool(keys) or bool(ctx.config.admin_token),
        "admin_token": bool(ctx.config.admin_token),
    }


@router.get("/mcp/keys", dependencies=[Depends(require_admin)])
def list_keys(ctx: Context = Depends(get_context)) -> dict[str, Any]:
    return _keys(ctx)


@router.post("/mcp/keys", dependencies=[Depends(require_admin)])
def create_key(body: KeyBody, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    """Make a key. The reply is the only time the key itself is given."""
    row, key = access.create_key(ctx.db, body.name)
    return {**_keys(ctx), "created": {**row, "key": key}}


@router.delete("/mcp/keys/{key_id}", dependencies=[Depends(require_admin)])
def delete_key(key_id: int, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    access.delete_key(ctx.db, key_id)
    return _keys(ctx)


@router.get("/about")
def about(ctx: Context = Depends(get_context)) -> dict[str, Any]:
    return {
        "name": "Zim Pro",
        "version": version(),
        "python": platform.python_version(),
        "libzim": _library_version("libzim"),
        "system": f"{platform.system()} {platform.machine()}",
        "packaged": bool(getattr(sys, "frozen", False)),
        "started": STARTED,
        "data_dir": str(ctx.config.data_dir),
        "state_dir": str(ctx.config.state_dir),
        "books": ctx.db.scalar("SELECT COUNT(*) FROM books"),
        "library_bytes": ctx.db.scalar("SELECT COALESCE(SUM(size), 0) FROM books"),
        "repository": f"https://github.com/{REPOSITORY}",
    }


def _numbers(text: str) -> tuple[int, ...]:
    parts = []
    for piece in text.lstrip("v").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


@router.get("/about/latest")
async def latest_release() -> dict[str, Any]:
    """Ask GitHub for the newest release; only when the user presses the button."""
    address = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
    try:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": USER_AGENT}) as client:
            reply = await client.get(address)
            reply.raise_for_status()
            data = reply.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            502, f"GitHub could not be asked ({exc or type(exc).__name__})."
        ) from exc
    latest = str(data.get("tag_name", "")).lstrip("v")
    mine = version()
    return {
        "latest": latest,
        "url": data.get("html_url", ""),
        "published": data.get("published_at", ""),
        "newer": mine != "dev" and _numbers(latest) > _numbers(mine),
    }
