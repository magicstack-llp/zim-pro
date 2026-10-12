"""Ask AI: answer questions from the library with any OpenAI-compatible chat model.

The flow is retrieval-augmented rather than tool-calling, because small local models
are unreliable at calling tools: the model first turns the question into keyword
searches, the library is searched, the best passages are put in the prompt, and the
answer is streamed back with numbered citations.
"""

import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from .context import Context
from .library import search as zim_search
from .library.article import to_markdown
from .library.reader import read_article

log = logging.getLogger(__name__)

MAX_QUERIES = 3
CANDIDATES = 14
HISTORY_TURNS = 6
EXCERPT_CHARS = 2400

STOPWORDS = frozenset(
    "a an and are as at be but by can could did do does for from had has have how i if in is it "
    "its me my of on or should so than that the their them then there these they this to was "
    "we were what when where which who why will with would you your about into over tell "
    "explain describe give show please".split()
)

QUERY_PROMPT = """\
You write search queries for a full-text search engine over an offline library \
(Wikipedia, Stack Exchange, software documentation and similar).
Given the conversation, write up to 3 short keyword queries that would find articles \
answering the user's latest question. Use 1 to 4 plain keywords each: no quotes, no \
operators, no question words. Write one query per line and nothing else.
If the latest message is a greeting, thanks or small talk that needs no lookup, write only \
the word NONE."""

CHAT_PROMPT = """\
You are the assistant built into the user's offline library (Wikipedia, Stack Exchange, \
software documentation and similar). The user's message needed no library search. Reply \
briefly and naturally. If they ask what you can do, say you answer questions from the \
books in their library and cite the articles you used."""

_SMALL_TALK = re.compile(
    r"^(?:(?:hi|hii+|hello|hey|heya|yo|hola|namaste|howdy|greetings|sup)(?: there| again)?"
    r"|good (?:morning|afternoon|evening|night|day)"
    r"|(?:thanks|thank you|thx|ty|cheers)(?: a lot| so much| very much)?"
    r"|ok|okay|k|cool|nice|great|got it|i see|alright|sure|yes|no|yep|nope"
    r"|bye|goodbye|see you|see ya|good ?bye"
    r"|how are you(?: doing)?(?: today)?|what'?s up|who are you|what can you do)$"
)


def is_small_talk(message: str) -> bool:
    """Greetings and acknowledgements: answered directly, with no library search."""
    text = re.sub(r"[^\w\s']", " ", message.lower())
    return bool(_SMALL_TALK.match(" ".join(text.split())))


ANSWER_PROMPT = """\
You are a research assistant answering questions from the user's offline library.
Numbered sources from the library follow the question. Base your answer on them and cite \
the ones you use inline as [1], [2]. If the sources do not contain the answer, say so \
plainly, then answer from your own knowledge and say that you are doing so. Be direct and \
well organised, and use Markdown."""


class AIError(Exception):
    pass


@dataclass
class Source:
    n: int
    book: str
    book_title: str
    path: str
    title: str
    excerpt: str


def ai_settings(ctx: Context) -> dict[str, Any]:
    settings = ctx.db.get_settings()
    base = str(settings["ai_base_url"]).strip().rstrip("/")
    return {
        "base_url": base.removesuffix("/chat/completions"),
        "api_key": str(settings["ai_api_key"]).strip(),
        "model": str(settings["ai_model"]).strip(),
        "max_sources": max(1, min(12, int(settings["ai_max_sources"]))),
    }


def _client(settings: dict[str, Any]) -> httpx.AsyncClient:
    if not settings["base_url"]:
        raise AIError("No AI server is set up. Add one under Settings → Ask AI.")
    headers = {"Authorization": f"Bearer {settings['api_key']}"} if settings["api_key"] else {}
    return httpx.AsyncClient(
        base_url=settings["base_url"] + "/",
        headers=headers,
        timeout=httpx.Timeout(20, read=300),
        follow_redirects=True,
    )


async def _fail(response: httpx.Response) -> AIError:
    body = (await response.aread()).decode("utf-8", "replace")
    try:
        error = json.loads(body).get("error")
        body = error.get("message", body) if isinstance(error, dict) else str(error or body)
    except (ValueError, AttributeError):
        pass
    return AIError(f"The AI server answered HTTP {response.status_code}: {body.strip()[:300]}")


def _unreachable(settings: dict[str, Any], exc: Exception) -> AIError:
    return AIError(
        f"Could not reach the AI server at {settings['base_url']}: {exc or type(exc).__name__}"
    )


async def list_models(settings: dict[str, Any]) -> list[str]:
    try:
        async with _client(settings) as client:
            response = await client.get("models")
            if response.status_code != 200:
                raise await _fail(response)
            data = response.json()
    except httpx.HTTPError as exc:
        raise _unreachable(settings, exc) from exc
    except ValueError as exc:
        raise AIError("The AI server did not return a model list") from exc
    items = data.get("data", data) if isinstance(data, dict) else data
    return sorted(str(m["id"]) for m in items if isinstance(m, dict) and m.get("id"))


async def stream_chat(
    settings: dict[str, Any], messages: list[dict[str, str]]
) -> AsyncIterator[str]:
    """Yield the reply text of a streamed ``/chat/completions`` call."""
    if not settings["base_url"]:
        raise AIError("No AI server is set up. Add one under Settings → Ask AI.")
    if not settings["model"]:
        raise AIError("No model is chosen. Pick one under Settings → Ask AI.")
    payload = {"model": settings["model"], "messages": messages, "stream": True}
    try:
        async with _client(settings) as client:
            async with client.stream("POST", "chat/completions", json=payload) as response:
                if response.status_code != 200:
                    raise await _fail(response)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        choices = json.loads(data).get("choices") or []
                    except ValueError:
                        continue
                    text = (choices[0].get("delta") or {}).get("content") if choices else None
                    if text:
                        yield text
    except httpx.HTTPError as exc:
        raise _unreachable(settings, exc) from exc


async def complete(settings: dict[str, Any], messages: list[dict[str, str]]) -> str:
    return "".join([part async for part in stream_chat(settings, messages)])


def keywords(text: str) -> list[str]:
    return [w for w in re.findall(r"\w+", text.lower()) if len(w) > 2 and w not in STOPWORDS]


def _strip_reasoning(reply: str) -> str:
    return re.sub(r"<think>.*?(</think>|$)", "", reply, flags=re.S).strip()


def parse_queries(reply: str) -> list[str]:
    """Pull the search queries out of the model's reply, tolerating chatter and markup."""
    reply = _strip_reasoning(reply)
    queries = []
    for line in reply.splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip().strip("\"'`")
        words = keywords(line)
        if words and len(words) <= 8 and not line.endswith(":"):
            queries.append(" ".join(words))
    return list(dict.fromkeys(queries))[:MAX_QUERIES]


async def plan_queries(
    settings: dict[str, Any], question: str, history: list[dict[str, str]]
) -> list[str]:
    """The searches to run for a question; an empty list means no search is needed."""
    if is_small_talk(question):
        return []
    fallback = " ".join(keywords(question)[:6]) or question
    reply = await complete(
        settings,
        [
            {"role": "system", "content": QUERY_PROMPT},
            *history,
            {"role": "user", "content": question},
        ],
    )
    if re.fullmatch(r"\W*none\W*", _strip_reasoning(reply), flags=re.I):
        return []
    queries = parse_queries(reply)
    if fallback not in queries:
        queries.append(fallback)
    return queries[: MAX_QUERIES + 1]


def _passages(text: str, terms: set[str]) -> tuple[float, str]:
    """Score an article against the query terms and cut its most relevant passages."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if len(b.strip()) > 40]
    if not blocks:
        return 0.0, text[:EXCERPT_CHARS]
    scored = []
    for index, block in enumerate(blocks):
        words = set(re.findall(r"\w+", block.lower()))
        scored.append((len(terms & words), -index, index))
    covered = set()
    for block in blocks:
        covered |= terms & set(re.findall(r"\w+", block.lower()))
    chosen, used = [], 0
    # The opening paragraph defines the topic, so it always goes in.
    for _, _, index in [(0, 0, 0), *sorted(scored, reverse=True)]:
        if index in chosen:
            continue
        block = blocks[index][:1200]
        if used + len(block) > EXCERPT_CHARS and chosen:
            break
        chosen.append(index)
        used += len(block)
    excerpt = "\n\n".join(blocks[i][:1200] for i in sorted(chosen))
    return len(covered) / max(1, len(terms)), excerpt


def retrieve(
    ctx: Context, queries: list[str], book: str, category: str, max_sources: int
) -> list[Source]:
    """Search the library and return the best passages to answer from."""
    if book:
        books = [ctx.book(book)]
        if books[0] is None:
            raise AIError(f"No book named {book!r}")
    else:
        books = ctx.db.all(
            "SELECT * FROM books WHERE error IS NULL AND (? = '' OR category = ?)",
            (category, category),
        )
    terms = {word for query in queries for word in keywords(query)}
    per_book = 8 if book else 3

    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for row in books:
        try:
            archive = ctx.pool.get(row["path"])
        except Exception:
            continue
        for query in queries:
            try:
                found = zim_search.search_book(archive, query, 0, per_book, snippets=False)
            except Exception:
                continue
            for rank, hit in enumerate(found["results"]):
                title_words = set(re.findall(r"\w+", hit["title"].lower()))
                score = 2 * len(terms & title_words) / max(1, len(terms)) + 1 / (2 + rank)
                if found["mode"] != "fulltext":
                    score *= 0.5
                key = (row["id"], hit["path"])
                if key not in candidates or candidates[key]["score"] < score:
                    candidates[key] = {"row": row, "score": score, **hit}

    ranked = sorted(candidates.values(), key=lambda c: -c["score"])[:CANDIDATES]
    scored = []
    for candidate in ranked:
        row = candidate["row"]
        try:
            article = read_article(ctx.pool.get(row["path"]), candidate["path"])
        except Exception:
            continue
        if article is None or not article.mimetype.startswith("text/html"):
            continue
        coverage, excerpt = _passages(to_markdown(article), terms)
        if excerpt.strip():
            scored.append((candidate["score"] + 2 * coverage, candidate, article, excerpt))

    sources: list[Source] = []
    seen: set[str] = set()
    for _, candidate, article, excerpt in sorted(scored, key=lambda item: -item[0]):
        # One article per title: editions of a book (maxi, nopic) and mirrors of Wikipedia
        # carry the same text, and repeats would crowd out other sources.
        fingerprint = (article.title or candidate["title"]).strip().lower()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        row = candidate["row"]
        sources.append(
            Source(
                n=len(sources) + 1,
                book=row["id"],
                book_title=row["title"],
                path=article.path,
                title=article.title or candidate["title"],
                excerpt=excerpt,
            )
        )
        if len(sources) >= max_sources:
            break
    return sources


def build_messages(
    question: str, history: list[dict[str, str]], sources: list[Source] | None
) -> list[dict[str, str]]:
    """The prompt for the answer; ``sources`` is None when no search was made."""
    if sources is None:
        return [
            {"role": "system", "content": CHAT_PROMPT},
            *history,
            {"role": "user", "content": question},
        ]
    if sources:
        listing = "\n\n".join(f"[{s.n}] {s.title} — {s.book_title}\n{s.excerpt}" for s in sources)
    else:
        listing = "(The library search found nothing relevant.)"
    return [
        {"role": "system", "content": ANSWER_PROMPT},
        *history,
        {"role": "user", "content": f"{question}\n\nSources from the library:\n\n{listing}"},
    ]


def clean_history(history: list[dict[str, Any]]) -> list[dict[str, str]]:
    turns = [
        {"role": m["role"], "content": str(m["content"])[:6000]}
        for m in history
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    return turns[-HISTORY_TURNS * 2 :]


def public_source(source: Source) -> dict[str, Any]:
    data = asdict(source)
    # For display only: drop Markdown markup and the title the excerpt usually opens with.
    plain = re.sub(r"[*_`#>|]+|!?\[([^\]]*)\]\([^)]*\)", r"\1", source.excerpt)
    plain = " ".join(plain.split()).removeprefix(source.title).strip()
    data["excerpt"] = plain[:240]
    return data
