import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import ai
from ..context import Context, get_context, require_admin
from .library import API_KEY_MASK

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ai", dependencies=[Depends(require_admin)])


class Probe(BaseModel):
    """Connection details to try before they are saved; blanks fall back to the saved ones."""

    base_url: str = ""
    api_key: str = ""


@router.post("/models")
async def models(probe: Probe, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    settings = ai.ai_settings(ctx)
    if probe.base_url.strip():
        settings["base_url"] = probe.base_url.strip().rstrip("/").removesuffix("/chat/completions")
    if probe.api_key and probe.api_key != API_KEY_MASK:
        settings["api_key"] = probe.api_key.strip()
    try:
        return {"models": await ai.list_models(settings)}
    except ai.AIError as exc:
        raise HTTPException(502, str(exc)) from exc


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    chat_id: str = ""
    book: str = ""
    category: str = ""


def _event(name: str, data: Any) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def _title(question: str) -> str:
    words = " ".join(question.split())
    return words if len(words) <= 60 else words[:57].rsplit(" ", 1)[0] + "…"


def _message(row: dict[str, Any]) -> dict[str, Any]:
    message = {"role": row["role"], "content": row["content"]}
    if row["sources"] is not None:
        message["sources"] = json.loads(row["sources"])
        message["queries"] = json.loads(row["queries"] or "[]")
    if row["error"]:
        message["error"] = row["error"]
    return message


@router.get("/chats")
def list_chats(ctx: Context = Depends(get_context)) -> list[dict[str, Any]]:
    return ctx.db.all(
        "SELECT id, title, created, updated FROM chats ORDER BY updated DESC LIMIT 500"
    )


@router.get("/chats/{chat_id}")
def get_chat(chat_id: str, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    chat = ctx.db.one("SELECT id, title, created, updated FROM chats WHERE id = ?", (chat_id,))
    if chat is None:
        raise HTTPException(404, "No such chat")
    rows = ctx.db.all("SELECT * FROM chat_messages WHERE chat_id = ? ORDER BY id", (chat_id,))
    return {**chat, "messages": [_message(row) for row in rows]}


class ChatPatch(BaseModel):
    title: str = Field(min_length=1, max_length=200)


@router.patch("/chats/{chat_id}")
def rename_chat(chat_id: str, patch: ChatPatch, ctx: Context = Depends(get_context)):
    changed = ctx.db.execute(
        "UPDATE chats SET title = ? WHERE id = ?", (patch.title.strip(), chat_id)
    ).rowcount
    if not changed:
        raise HTTPException(404, "No such chat")
    return {"id": chat_id, "title": patch.title.strip()}


@router.delete("/chats/{chat_id}")
def delete_chat(chat_id: str, ctx: Context = Depends(get_context)) -> dict[str, bool]:
    ctx.db.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
    ctx.db.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
    return {"ok": True}


@router.delete("/chats")
def delete_all_chats(ctx: Context = Depends(get_context)) -> dict[str, bool]:
    ctx.db.execute("DELETE FROM chat_messages")
    ctx.db.execute("DELETE FROM chats")
    return {"ok": True}


@router.post("/ask")
async def ask(body: Question, ctx: Context = Depends(get_context)) -> StreamingResponse:
    """Answer a question from the library, streamed as server-sent events.

    The exchange is stored in a chat: the one named by ``chat_id``, or a new one. Events:
    ``chat`` (the chat's id and title), ``status`` (what is happening), ``sources`` (the
    passages used), ``delta`` (answer text), then ``done`` or ``error``.
    """
    settings = ai.ai_settings(ctx)
    question = body.question.strip()
    chat = ctx.db.one("SELECT id, title FROM chats WHERE id = ?", (body.chat_id,))
    if body.chat_id and chat is None:
        raise HTTPException(404, "No such chat")
    if chat is None:
        chat = {"id": uuid.uuid4().hex, "title": _title(question)}
        ctx.db.execute("INSERT INTO chats (id, title) VALUES (?, ?)", (chat["id"], chat["title"]))
    earlier = ctx.db.all(
        "SELECT role, content FROM chat_messages WHERE chat_id = ? ORDER BY id", (chat["id"],)
    )
    history = ai.clean_history(earlier)
    ctx.db.execute(
        "INSERT INTO chat_messages (chat_id, role, content) VALUES (?, 'user', ?)",
        (chat["id"], question),
    )
    ctx.db.execute("UPDATE chats SET updated = unixepoch('subsec') WHERE id = ?", (chat["id"],))

    async def stream() -> AsyncIterator[str]:
        answer: list[str] = []
        found: list[dict[str, Any]] | None = None
        queries: list[str] = []
        error: str | None = None
        try:
            yield _event("chat", chat)
            if not ai.is_small_talk(question):
                yield _event("status", "Working out what to search for…")
            queries = await ai.plan_queries(settings, question, history)
            sources = None
            if queries:  # greetings and small talk are answered without touching the library
                searching = ", ".join(f"“{q}”" for q in queries)
                yield _event("status", f"Searching the library for {searching}")
                sources = await asyncio.to_thread(
                    ai.retrieve, ctx, queries, body.book, body.category, settings["max_sources"]
                )
                found = [ai.public_source(s) for s in sources]
                yield _event("sources", {"queries": queries, "sources": found})
            yield _event("status", "Writing the answer…")
            messages = ai.build_messages(question, history, sources)
            async for text in ai.stream_chat(settings, messages):
                answer.append(text)
                yield _event("delta", text)
            yield _event("done", {})
        except ai.AIError as exc:
            error = str(exc)
            yield _event("error", error)
        except Exception as exc:
            log.exception("ask failed")
            error = f"Something went wrong: {exc or type(exc).__name__}"
            yield _event("error", error)
        finally:
            # Also runs when the reader presses Stop, so the partial answer is kept.
            ctx.db.execute(
                "INSERT INTO chat_messages (chat_id, role, content, sources, queries, error) "
                "VALUES (?, 'assistant', ?, ?, ?, ?)",
                (
                    chat["id"],
                    "".join(answer),
                    None if found is None else json.dumps(found),
                    json.dumps(queries),
                    error,
                ),
            )
            ctx.db.execute(
                "UPDATE chats SET updated = unixepoch('subsec') WHERE id = ?", (chat["id"],)
            )

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
