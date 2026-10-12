import asyncio
import itertools
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from .. import ai, providers, retrieval, websearch
from ..context import Context, get_context, require_admin
from ..providers import AIError, Provider
from .library import SECRET_MASK

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ai", dependencies=[Depends(require_admin)])


# -- providers ------------------------------------------------------------------------


class ProviderBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    kind: str = "custom"
    base_url: str = Field(min_length=1, max_length=500)
    # Empty or the mask means "keep the stored key" when editing.
    api_key: str = ""


def _from_body(body: ProviderBody, existing: Provider | None, provider_id: str) -> Provider:
    base_url = providers.clean_url(body.base_url)
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(422, "The server address must start with http:// or https://")
    kind = body.kind if body.kind in providers.KINDS else providers.guess_kind(base_url)
    key = body.api_key.strip()
    if key == SECRET_MASK:
        key = existing.api_key if existing else ""
    return Provider(provider_id, body.name.strip(), kind, base_url, key)


@router.get("/providers")
def list_providers(ctx: Context = Depends(get_context)) -> list[dict[str, Any]]:
    return [p.public() for p in providers.list_providers(ctx.db)]


@router.post("/providers")
def create_provider(body: ProviderBody, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    provider = providers.save_provider(ctx.db, _from_body(body, None, providers.new_id()))
    if not ctx.db.get_setting("ai_provider"):
        ctx.db.set_settings({"ai_provider": provider.id})
    return provider.public()


@router.put("/providers/{provider_id}")
def update_provider(provider_id: str, body: ProviderBody, ctx: Context = Depends(get_context)):
    existing = providers.get_provider(ctx.db, provider_id)
    if existing is None:
        raise HTTPException(404, "No such provider")
    return providers.save_provider(ctx.db, _from_body(body, existing, provider_id)).public()


@router.delete("/providers/{provider_id}")
def delete_provider(provider_id: str, ctx: Context = Depends(get_context)) -> dict[str, bool]:
    providers.delete_provider(ctx.db, provider_id)
    # Anything that pointed at it falls back to "not set".
    settings = ctx.db.get_settings()
    cleared = {
        key: ""
        for key in ("ai_provider", "web_provider", "tts_provider")
        if settings[key] == provider_id
    }
    if "ai_provider" in cleared:
        remaining = providers.list_providers(ctx.db)
        cleared.update(ai_provider=remaining[0].id if remaining else "", ai_model="")
    if cleared:
        ctx.db.set_settings(cleared)
    return {"ok": True}


class ProviderTest(BaseModel):
    """Connection details to try. With ``id``, blanks fall back to that provider's saved ones."""

    id: str = ""
    kind: str = "custom"
    base_url: str = ""
    api_key: str = ""


@router.post("/providers/test")
async def test_provider(body: ProviderTest, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    saved = providers.get_provider(ctx.db, body.id) if body.id else None
    key = body.api_key.strip()
    if not key or key == SECRET_MASK:
        key = saved.api_key if saved else ""
    base_url = providers.clean_url(body.base_url) or (saved.base_url if saved else "")
    if not base_url:
        raise HTTPException(422, "Enter the server address first")
    probe = Provider("", (saved.name if saved else "the server"), body.kind, base_url, key)
    try:
        return {"models": await providers.list_models(probe)}
    except AIError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/models")
async def models(
    refresh: bool = False, ctx: Context = Depends(get_context)
) -> list[dict[str, Any]]:
    """Every provider with the models it offers, for the model pickers."""

    async def load(provider: Provider) -> dict[str, Any]:
        entry: dict[str, Any] = {"provider": provider.public(), "models": [], "error": None}
        try:
            entry["models"] = await providers.list_models(provider, use_cache=not refresh)
        except AIError as exc:
            entry["error"] = str(exc)
        return entry

    return list(await asyncio.gather(*(load(p) for p in providers.list_providers(ctx.db))))


# -- speech ---------------------------------------------------------------------------


class Speech(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    # Optional overrides, used by the "Test voice" button before settings are saved.
    provider: str = ""
    model: str = ""
    voice: str = ""


@router.post("/speech")
async def speech(body: Speech, ctx: Context = Depends(get_context)) -> Response:
    settings = ctx.db.get_settings()
    provider = providers.get_provider(ctx.db, body.provider or settings["tts_provider"])
    if provider is None:
        raise HTTPException(409, "Choose a provider for reading aloud under Settings → Read aloud.")
    try:
        audio, media_type = await providers.speech(
            provider,
            body.model or settings["tts_model"],
            body.voice or settings["tts_voice"],
            body.text,
        )
    except AIError as exc:
        raise HTTPException(502, str(exc)) from exc
    return Response(audio, media_type=media_type, headers={"Cache-Control": "no-store"})


# -- chats ----------------------------------------------------------------------------


class Question(BaseModel):
    question: str = Field(default="", max_length=4000)
    chat_id: str = ""
    # Answer the chat's last question again, replacing the answer it got.
    regenerate: bool = False
    book: str = ""
    category: str = ""
    # Which model answers; blank means the defaults from settings.
    provider: str = ""
    model: str = ""
    # "auto" searches the web only when the library has no good match; "on" always does.
    web: str = "auto"


def _event(name: str, data: Any) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def _title(question: str) -> str:
    words = " ".join(question.split())
    return words if len(words) <= 60 else words[:57].rsplit(" ", 1)[0] + "…"


def _message(row: dict[str, Any]) -> dict[str, Any]:
    content, reasoning = row["content"], row.get("reasoning") or ""
    if row["role"] == "assistant" and not reasoning:
        # Answers stored before thinking was separated may still carry it inline.
        reasoning, content = providers.split_reasoning(content)
    message = {"role": row["role"], "content": content}
    if reasoning:
        message["reasoning"] = reasoning
        message["thought_seconds"] = row.get("thought_seconds")
    if row["sources"] is not None:
        message["sources"] = json.loads(row["sources"])
        message["queries"] = json.loads(row["queries"] or "[]")
    if row.get("steps"):
        message["steps"] = json.loads(row["steps"])
    elif message.get("queries"):
        # Answers from before steps were recorded: rebuild them from what was kept.
        kept = message["sources"]
        message["steps"] = [
            {
                "tool": "web" if kind == "web" else "library",
                "title": "Searched the web" if kind == "web" else "Searched your library",
                "state": "done",
                "queries": message["queries"],
                "results": [
                    {"title": s["title"], "where": s.get("book_title") or s.get("site") or ""}
                    for s in kept
                    if s.get("kind", "library") == kind
                ],
            }
            for kind in ("library", "web")
            if kind == "library" or any(s.get("kind") == "web" for s in kept)
        ]
    if row["error"]:
        message["error"] = row["error"]
    if row.get("model"):
        message["model"] = row["model"]
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


def _pick_model(ctx: Context, body: Question, settings: dict[str, Any]) -> tuple[Provider, str]:
    provider = providers.get_provider(ctx.db, body.provider or settings["ai_provider"])
    if provider is None:
        available = providers.list_providers(ctx.db)
        if not available:
            raise AIError("No AI provider is set up. Add one under Settings → AI providers.")
        provider = available[0]
    model = body.model if body.provider else settings["ai_model"]
    if not model:
        raise AIError("No model is chosen. Pick one in the chat box or under Settings.")
    return provider, model


# Models to plan with on OVH, whose free limit (about two requests a minute) is counted per
# model: planning with a different model than the one answering costs the answer nothing,
# and taking turns among several spreads the planning itself.
_OVH_PLANNERS = ("Mistral-Small-3.2-24B-Instruct-2506", "Mistral-Nemo-Instruct-2407")
_planner_turn = itertools.count()


def planner_model(provider: Provider, model: str) -> str:
    """The model that plans the search: the answering one, unless its rate limit is tight."""
    if provider.kind in providers.LOW_RATE_KINDS and not provider.api_key:
        others = [m for m in _OVH_PLANNERS if m != model]
        return others[next(_planner_turn) % len(others)]
    return model


def _number_citations(text: str, urls: list[str]) -> str:
    """Rewrite the links a searching model wrote inline as [n] markers for our source list."""

    def swap(match: re.Match[str]) -> str:
        url = match.group(2).split("?utm_source=")[0]
        return f"[{urls.index(url) + 1}]" if url in urls else match.group(0)

    return re.sub(r"\(?\[([^\]]*)\]\((https?://[^)\s]+)\)\)?", swap, text)


@router.post("/ask")
async def ask(body: Question, ctx: Context = Depends(get_context)) -> StreamingResponse:
    """Answer a question, streamed as server-sent events.

    The exchange is stored in a chat: the one named by ``chat_id``, or a new one. Events:
    ``chat`` (the chat's id and title), ``status`` (what is happening), ``sources`` (the
    passages used; may be sent again as web pages are added), ``delta`` (answer text),
    then ``done`` or ``error``.
    """
    settings = ctx.db.get_settings()
    question = body.question.strip()
    chat = ctx.db.one("SELECT id, title FROM chats WHERE id = ?", (body.chat_id,))
    if body.chat_id and chat is None:
        raise HTTPException(404, "No such chat")
    if body.regenerate:
        if chat is None:
            raise HTTPException(422, "There is no answer to regenerate")
        last = ctx.db.one(
            "SELECT id, content FROM chat_messages WHERE chat_id = ? AND role = 'user' "
            "ORDER BY id DESC LIMIT 1",
            (chat["id"],),
        )
        if last is None:
            raise HTTPException(422, "There is no answer to regenerate")
        question = last["content"]
        ctx.db.execute(
            "DELETE FROM chat_messages WHERE chat_id = ? AND id >= ?", (chat["id"], last["id"])
        )
    if not question:
        raise HTTPException(422, "Ask a question")
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
        used = ""
        thinking: list[str] = []
        thought: dict[str, float | None] = {"start": None, "seconds": None}
        # What the app did to answer, in order: each search it ran, shown like a tool call.
        steps: list[dict[str, Any]] = []
        started: dict[int, float] = {}

        def begin(tool: str, title: str, **fields: Any) -> str:
            steps.append({"tool": tool, "title": title, "state": "running", **fields})
            started[len(steps) - 1] = time.monotonic()
            return _event("step", {"index": len(steps) - 1, **steps[-1]})

        def finish(index: int, title: str, state: str = "done", **fields: Any) -> str:
            seconds = round(time.monotonic() - started[index], 1)
            steps[index].update(title=title, state=state, seconds=seconds, **fields)
            return _event("step", {"index": index, **steps[index]})

        def results(sources: list[ai.Source]) -> list[dict[str, str]]:
            return [{"title": s.title, "where": s.book_title or s.site} for s in sources]

        def relay(kind: str, value: Any) -> list[str]:
            """Turn one piece of the model's output into events, keeping thinking apart."""
            if kind == "reasoning":
                if thought["start"] is None:
                    thought["start"] = time.monotonic()
                thinking.append(value)
                return [_event("thinking", value)]
            if kind == "retract":
                answer.clear()
                return [_event("replace", "")]
            if kind != "text":
                return []
            events = []
            if thought["start"] is not None and thought["seconds"] is None:
                thought["seconds"] = round(time.monotonic() - thought["start"], 1)
                events.append(_event("thought", thought["seconds"]))
            answer.append(value)
            return [*events, _event("delta", value)]

        try:
            yield _event("chat", chat)
            provider, model = _pick_model(ctx, body, settings)
            used = f"{provider.name} · {model}"
            small_talk = ai.is_small_talk(question)
            scope = retrieval.scope_books(ctx, body.book, body.category)
            if not small_talk:
                yield begin("plan", "Working out what to search for")
            plan = await retrieval.plan(
                provider, planner_model(provider, model), question, history, scope
            )
            queries = plan.queries
            focus = retrieval.choose_focus(plan, question, scope) if plan else []
            if not small_talk:
                if plan:
                    yield finish(
                        0,
                        "Planned the search",
                        queries=plan.queries,
                        keywords=plan.keywords,
                        books=[row["title"] for row in focus] if len(scope) > len(focus) else [],
                        detail=plan.note,
                    )
                else:
                    yield finish(0, "Decided no search was needed")

            sources: list[ai.Source] | None = None
            web_model: tuple[Provider, str] | None = None
            web_step = -1
            if plan:  # greetings and small talk are answered without any search
                candidates: retrieval.Candidates = {}
                focus_ids = {row["id"] for row in focus}
                rest = [row for row in scope if row["id"] not in focus_ids]
                # The relevant books get every query and every keyword, in depth.
                everything = retrieval.search_list(plan)
                label = "the book" if len(scope) == 1 else f"{len(focus)} relevant books"
                yield begin("library", f"Searching {label}", queries=[q for q, _ in everything])
                hits = await asyncio.to_thread(
                    retrieval.search_books, ctx, focus, everything, 8, candidates, True
                )
                yield finish(
                    len(steps) - 1,
                    f"Searched {label}",
                    detail=f"{len(candidates)} articles found in {hits} matches.",
                )
                if rest:
                    # The rest of the library gets the main queries, lightly.
                    main = [(q, 1.0) for q in plan.queries[:2]]
                    before = len(candidates)
                    yield begin(
                        "library",
                        f"Searching the other {len(rest)} books",
                        queries=[q for q, _ in main],
                    )
                    await asyncio.to_thread(retrieval.search_books, ctx, rest, main, 3, candidates)
                    yield finish(
                        len(steps) - 1,
                        f"Searched the other {len(rest)} books",
                        detail=f"{len(candidates) - before} more articles found.",
                    )
                yield begin("library", "Reading the most promising articles")
                passages, read = await asyncio.to_thread(
                    retrieval.rank,
                    ctx,
                    candidates,
                    plan,
                    focus_ids,
                    settings["ai_max_sources"],
                    len(scope) == 1,
                )
                sources = ai.library_sources(passages)
                enough = ai.library_is_enough(sources)
                detail = "" if enough else "No strong match for this question."
                yield finish(
                    len(steps) - 1,
                    f"Read {read} articles and kept the {len(sources)} best passages"
                    if sources
                    else "Found nothing relevant in the library",
                    results=results(sources),
                    detail=detail if sources else "",
                )
                web_mode = settings["web_mode"] if body.web != "off" else "off"
                wants_web = body.web == "on" or not enough
                if wants_web and web_mode == "api":
                    service = settings["web_service"]
                    via = {"tavily": "Tavily", "brave": "Brave Search", "serper": "Google (Serper)"}
                    via = f"via {via.get(service, 'SearXNG')}"
                    yield begin("web", "Searching the web", queries=[queries[0]], detail=via)
                    web_step = len(steps) - 1
                    try:
                        hits = await websearch.search(
                            service, settings["web_api_key"], settings["web_api_url"], queries[0]
                        )
                        pages = ai.web_sources(hits, len(sources) + 1)
                        sources += pages
                        yield finish(web_step, "Searched the web", results=results(pages))
                    except AIError as exc:
                        detail = str(exc).removeprefix("Web search failed: ")
                        yield finish(web_step, "Web search failed", "failed", detail=detail)
                elif wants_web and web_mode == "model":
                    searcher = providers.get_provider(ctx.db, settings["web_provider"])
                    if searcher and settings["web_model"]:
                        web_model = (searcher, settings["web_model"])
                    else:
                        yield _event(
                            "notice", "Web search is on, but no searching model is chosen."
                        )
                if web_model is None:
                    found = [ai.public_source(s) for s in sources]
                    yield _event("sources", {"queries": queries, "sources": found})

            if web_model:
                # A model with its own search answers by itself and reports the pages it used.
                searcher, search_model = web_model
                used = f"{searcher.name} · {search_model}"
                yield begin(
                    "web",
                    f"Asking {searcher.name} to search the web",
                    queries=[question],
                    detail=search_model,
                )
                web_step = len(steps) - 1
                cited: dict[str, str] = {}
                messages = ai.web_messages(question, history, settings["ai_instructions"])
                async for kind, value in providers.stream_chat(
                    searcher, search_model, messages, web=True
                ):
                    for event in relay(kind, value):
                        yield event
                    if kind == "citations":
                        for page in value:
                            cited.setdefault(page["url"], page["title"])
                        hits = [{"url": u, "title": t, "text": ""} for u, t in cited.items()]
                        found = [ai.public_source(s) for s in ai.web_sources(hits, 1)]
                        yield _event("sources", {"queries": queries, "sources": found})
                yield finish(
                    web_step,
                    f"{searcher.name} searched the web",
                    results=[{"title": t or u, "where": ""} for u, t in cited.items()],
                )
                if cited:
                    # Models that inline their links get them swapped for our numbered pills.
                    numbered = _number_citations("".join(answer), list(cited))
                    if numbered != "".join(answer):
                        answer = [numbered]
                        yield _event("replace", numbered)
                elif found is None:
                    found = []
                    yield _event("sources", {"queries": queries, "sources": found})
            else:
                yield _event("status", "Writing the answer…")
                messages = ai.build_messages(
                    question, history, sources, settings["ai_instructions"]
                )
                async for kind, value in providers.stream_chat(provider, model, messages):
                    for event in relay(kind, value):
                        yield event
            if not "".join(answer).strip():
                # Some free routes accept a request and send nothing back.
                raise AIError(
                    "The model returned an empty reply. Try again, or pick another model in the "
                    "chat box."
                )
            yield _event("done", {"model": used})
        except AIError as exc:
            error = str(exc)
            yield _event("error", error)
            if used:
                yield _event("done", {"model": used})
        except Exception as exc:
            log.exception("ask failed")
            error = f"Something went wrong: {exc or type(exc).__name__}"
            yield _event("error", error)
        finally:
            # Also runs when the reader presses Stop, so the partial answer is kept.
            for item in steps:
                if item["state"] == "running":
                    item["state"] = "failed" if error else "stopped"
            ctx.db.execute(
                "INSERT INTO chat_messages (chat_id, role, content, sources, queries, error, "
                "model, reasoning, thought_seconds, steps) "
                "VALUES (?, 'assistant', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chat["id"],
                    "".join(answer),
                    None if found is None else json.dumps(found),
                    json.dumps(queries),
                    error,
                    used or None,
                    "".join(thinking).strip() or None,
                    thought["seconds"]
                    if thought["seconds"] is not None or thought["start"] is None
                    else round(time.monotonic() - thought["start"], 1),
                    json.dumps(steps) if steps else None,
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
