"""Logs, monitoring and the queues, as the Settings sections show them."""

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from .. import logs
from ..builder.writer import BuildError
from ..context import Context, get_context, require_admin

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

# -- logs -------------------------------------------------------------------------------


@router.get("/logs", dependencies=[Depends(require_admin)])
def read_logs(
    level: str = "",
    area: str = "",
    q: str = "",
    job: str = "",
    after: int = 0,
    before: int = 0,
    limit: int = 300,
    ctx: Context = Depends(get_context),
) -> dict[str, Any]:
    """Log lines, newest first. ``after`` gives only those newer than a line already seen."""
    hub = logs.hub
    lines = hub.query(level.upper(), area, q.strip(), job, after, before, max(1, min(limit, 2000)))
    return {
        "lines": lines,
        "newest": hub.newest,
        "areas": hub.areas(),
        "jobs": hub.jobs(),
        "level": ctx.db.get_setting("log_level"),
        "file": str(hub.path) if hub.path else "",
        "redis": ctx.broker.kind == "redis",
    }


@router.get("/logs/download", dependencies=[Depends(require_admin)])
def download_logs() -> Response:
    """Every line still in the log files (or in memory), one JSON object a line."""
    files = logs.hub.files()
    if files:
        body = b"".join(file.read_bytes() for file in files)
    else:
        import json

        lines = reversed(logs.hub.query(limit=logs.KEEP))
        body = "".join(json.dumps(line) + "\n" for line in lines).encode()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return Response(
        body,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="zim-pro-logs-{stamp}.jsonl"'},
    )


@router.post("/logs/clear", dependencies=[Depends(require_admin)])
def clear_logs() -> dict[str, bool]:
    """Empty the list shown in Settings; the files keep what they have."""
    logs.hub.clear()
    log.info("the log list was cleared")
    return {"ok": True}


# -- monitoring ---------------------------------------------------------------------------


@router.get("/monitor")
async def monitor(span: str = "1h", ctx: Context = Depends(get_context)) -> dict[str, Any]:
    assert ctx.monitor is not None
    network = await ctx.network.status()
    return ctx.monitor.snapshot(network, "24h" if span == "24h" else "1h")


# -- queues -------------------------------------------------------------------------------

QUEUES = {
    "downloads": {
        "label": "Downloads",
        "about": "ZIM files from the Kiwix catalog",
        "table": "downloads",
        "where": "1",
        "running": ("downloading", "verifying"),
        "done": "done",
        "setting": "max_concurrent_downloads",
        "limit": 8,
        "job": "download",
    },
    "builds": {
        "label": "Create",
        "about": "ZIMs made from links and uploaded books",
        "table": "builds",
        "where": "kind IN ('link', 'upload')",
        "running": ("processing",),
        "done": "finished",
        "setting": "max_concurrent_builds",
        "limit": 4,
        "job": "build",
    },
    "feeds": {
        "label": "Feed syncs",
        "about": "Collections kept up to date from RSS",
        "table": "builds",
        "where": "kind = 'feed'",
        "running": ("processing",),
        "done": "finished",
        "setting": "max_concurrent_feed_syncs",
        "limit": 4,
        "job": "feed",
    },
}
DAY = 86400


def _manager(ctx: Context, name: str) -> Any:
    if name not in QUEUES:
        raise HTTPException(404, "No such queue.")
    return {"downloads": ctx.downloads, "builds": ctx.builds, "feeds": ctx.feeds}[name]


def _quantile(values: list[float], share: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(share * len(ordered)))]


def _queue(ctx: Context, name: str) -> dict[str, Any]:
    spec = QUEUES[name]
    manager = _manager(ctx, name)
    table, where = spec["table"], spec["where"]
    db, now = ctx.db, time.time()
    counts = {
        row["state"]: row["n"]
        for row in db.all(f"SELECT state, COUNT(*) AS n FROM {table} WHERE {where} GROUP BY state")
    }
    done = spec["done"]
    running_states = spec["running"]
    title = "title"
    rows = db.all(
        f"SELECT id, {title} AS title, state, created, started, finished, error "
        f"FROM {table} WHERE {where} AND (state IN ('queued', 'paused', "
        + ", ".join(f"'{s}'" for s in running_states)
        + ") OR finished > ?) ORDER BY id DESC",
        (now - DAY,),
    )
    by_id = {row["id"]: row for row in rows}
    live = {item["id"]: item for item in manager.list() if item["id"] in by_id}

    running = []
    for row in rows:
        if row["state"] not in running_states and not (
            row["state"] == "paused" and row["id"] in manager._active
        ):
            continue
        item = live.get(row["id"], {})
        key = f"{spec['job']} {row['id']}"
        quiet = ctx.monitor.quiet_for(key) if ctx.monitor else None
        entry = {
            "id": row["id"],
            "title": row["title"],
            "state": row["state"],
            "started": row["started"],
            "running_for": now - row["started"] if row["started"] else None,
            "quiet_for": quiet,
            "stalled": bool(ctx.monitor and key in ctx.monitor.stalled),
        }
        if name == "downloads":
            entry.update(
                done=item.get("bytes_done", 0),
                total=item.get("size", 0),
                speed=item.get("speed", 0),
                unit="bytes",
                phase=row["state"],
            )
        else:
            entry.update(
                done=item.get("done", 0),
                total=item.get("total", 0),
                phase=item.get("phase", ""),
                detail=item.get("detail", ""),
                failed=item.get("failed", 0),
                unit="items",
            )
        running.append(entry)

    order = manager.waiting()
    waiting = [
        {
            "id": job_id,
            "title": by_id[job_id]["title"] if job_id in by_id else str(job_id),
            "place": place,
            "waited": now - by_id[job_id]["created"] if job_id in by_id else None,
        }
        for job_id, place in order
    ]
    paused = [
        {"id": row["id"], "title": row["title"], "created": row["created"]}
        for row in rows
        if row["state"] == "paused" and row["id"] not in manager._active
    ]

    recent = [row for row in rows if row["finished"] and row["finished"] > now - DAY]
    runs = [r["finished"] - r["started"] for r in recent if r["started"] and r["state"] == done]
    waits = [
        r["started"] - r["created"]
        for r in rows
        if r["started"] and r["started"] > now - DAY and r["created"]
    ]
    hours = []
    for hour in range(24):
        start = now - (24 - hour) * 3600
        inside = [r for r in recent if start <= r["finished"] < start + 3600]
        hours.append(
            {
                "t": start,
                "finished": sum(r["state"] == done for r in inside),
                "failed": sum(r["state"] == "failed" for r in inside),
                "cancelled": sum(r["state"] == "cancelled" for r in inside),
            }
        )
    failures = [
        {"id": r["id"], "title": r["title"], "error": r["error"], "finished": r["finished"]}
        for r in recent
        if r["state"] == "failed"
    ][:8]
    return {
        "name": name,
        "label": spec["label"],
        "about": spec["about"],
        "paused": manager.broker.paused(name),
        "concurrency": int(db.get_setting(spec["setting"])),
        "concurrency_max": spec["limit"],
        "counts": {
            "waiting": counts.get("queued", 0),
            "running": sum(counts.get(s, 0) for s in running_states),
            "paused": counts.get("paused", 0),
            "finished": counts.get(done, 0),
            "failed": counts.get("failed", 0),
            "cancelled": counts.get("cancelled", 0),
        },
        "day": {
            "finished": sum(r["state"] == done for r in recent),
            "failed": sum(r["state"] == "failed" for r in recent),
            "cancelled": sum(r["state"] == "cancelled" for r in recent),
            "average_wait": sum(waits) / len(waits) if waits else None,
            "average_run": sum(runs) / len(runs) if runs else None,
            "p95_run": _quantile(runs, 0.95),
        },
        "oldest_wait": max((w["waited"] or 0 for w in waiting), default=None),
        "running": running,
        "waiting": waiting,
        "paused_jobs": paused,
        "hours": hours,
        "failures": failures,
    }


@router.get("/queues")
def queues(ctx: Context = Depends(get_context)) -> dict[str, Any]:
    return {
        "broker": ctx.broker.info(),
        "queues": [_queue(ctx, name) for name in QUEUES],
    }


@router.post("/queues/{name}/{action}", dependencies=[Depends(require_admin)])
def queue_action(name: str, action: str, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    manager = _manager(ctx, name)
    label = QUEUES[name]["label"]
    if action in ("pause", "resume"):
        manager.broker.set_paused(name, action == "pause")
        log.info("queue %s %s", label, "paused" if action == "pause" else "resumed")
        count = 0
    elif action == "retry-failed":
        count = manager.retry_failed()
        log.info("queue %s: %d failed jobs queued again", label, count)
    elif action == "cancel-waiting":
        count = manager.cancel_waiting()
        log.info("queue %s: %d waiting jobs cancelled", label, count)
    elif action == "clear-finished":
        manager.clear_finished()
        count = 0
    else:
        raise HTTPException(404, "No such action.")
    manager._changed()
    return {"ok": True, "count": count, "queue": _queue(ctx, name)}


class Concurrency(BaseModel):
    value: int


@router.put("/queues/{name}/concurrency", dependencies=[Depends(require_admin)])
def set_concurrency(
    name: str, body: Concurrency, ctx: Context = Depends(get_context)
) -> dict[str, Any]:
    manager = _manager(ctx, name)
    spec = QUEUES[name]
    value = max(1, min(spec["limit"], body.value))
    ctx.db.set_settings({spec["setting"]: value})
    log.info("queue %s runs %d at a time", spec["label"], value)
    manager._changed()
    return _queue(ctx, name)


JOB_ACTIONS = ("top", "up", "down", "bottom", "pause", "resume", "cancel", "retry")


@router.post("/queues/{name}/jobs/{job_id}/{action}", dependencies=[Depends(require_admin)])
def job_action(
    name: str, job_id: int, action: str, ctx: Context = Depends(get_context)
) -> dict[str, Any]:
    manager = _manager(ctx, name)
    if action not in JOB_ACTIONS:
        raise HTTPException(404, "No such action.")
    try:
        if action in ("top", "up", "down", "bottom"):
            manager.place(job_id, action)
        elif action == "retry" and name == "downloads":
            manager.resume(job_id)
        else:
            getattr(manager, action)(job_id)
    except BuildError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _queue(ctx, name)
