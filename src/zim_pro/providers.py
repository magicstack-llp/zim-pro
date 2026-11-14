"""Model providers: chat, built-in web search and speech over their HTTP APIs.

Every provider speaks the OpenAI chat-completions dialect. ``kind`` records which
service it is, because the extras differ: how a model is told to search the web, how
it reports the pages it used, and how it turns text into audio.
"""

import asyncio
import base64
import json
import logging
import random
import re
import struct
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from .db import Database
from .monitor import metrics

log = logging.getLogger(__name__)

KINDS = (
    "openai", "gemini", "perplexity", "openrouter", "groq", "ollama", "lmstudio", "llamacpp",
    "ovh", "kilo", "custom",
)  # fmt: skip
# Kinds whose models can search the web themselves.
WEB_KINDS = ("perplexity", "openrouter", "openai", "gemini")

# Services that answer without an account or key, which is what makes Ask AI usable straight
# after installing. They are rate-limited per IP address and offer open-weight models.
FREE_PROVIDERS = (
    {
        "id": "free-ovh",
        "name": "OVHcloud AI Endpoints",
        "kind": "ovh",
        "base_url": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1",
        "model": "Meta-Llama-3_3-70B-Instruct",
    },
    {
        "id": "free-kilo",
        "name": "Kilo Gateway (free models)",
        "kind": "kilo",
        "base_url": "https://api.kilo.ai/api/gateway/v1",
        "model": "",
    },
)
FREE_KINDS = tuple(p["kind"] for p in FREE_PROVIDERS)
# Without a key OVH allows about two requests a minute per model, so the app must not spend
# one of them asking the model what to search for.
LOW_RATE_KINDS = ("ovh",)
# Where a kind lists its models, when that is not <base_url>/models.
_MODELS_URL = {"kilo": "https://api.kilo.ai/api/gateway/models"}
_NOT_CHAT = re.compile(
    r"diffusion|tts|whisper|embedding|bge-|guard|content-safety|image|rerank", re.I
)

GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
MODEL_CACHE_SECONDS = 300
DEFAULT_MAX_TOKENS = 2048
MAX_RETRIES = 2
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class AIError(Exception):
    pass


@dataclass
class Provider:
    id: str
    name: str
    kind: str
    base_url: str
    api_key: str
    # Off: kept with its key, but not offered in the model pickers or used for anything.
    enabled: bool = True

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "base_url": self.base_url,
            "has_key": bool(self.api_key),
            "enabled": self.enabled,
            "web": self.kind in WEB_KINDS,
            # Works with no account: rate-limited, and worth saying so in the interface.
            "free": self.kind in FREE_KINDS and not self.api_key,
        }


def guess_kind(base_url: str) -> str:
    host = urlsplit(base_url).netloc.lower()
    for needle, kind in (
        ("generativelanguage.googleapis.com", "gemini"),
        ("perplexity.ai", "perplexity"),
        ("openrouter.ai", "openrouter"),
        ("api.openai.com", "openai"),
        ("groq.com", "groq"),
        ("endpoints.kepler.ai.cloud.ovh.net", "ovh"),
        ("api.kilo.ai", "kilo"),
        (":11434", "ollama"),
        (":1234", "lmstudio"),
    ):
        if needle in host:
            return kind
    return "custom"


def clean_url(base_url: str) -> str:
    return base_url.strip().rstrip("/").removesuffix("/chat/completions")


def validate_url(base_url: str) -> str:
    """Clean and validate an HTTP(S) provider URL using ``httpx.URL``."""
    cleaned = clean_url(base_url)
    try:
        parsed = httpx.URL(cleaned)
    except httpx.InvalidURL as exc:
        raise ValueError(str(exc)) from exc
    if parsed.scheme not in ("http", "https") or not parsed.host:
        raise ValueError("URL must be an http:// or https:// address with a host")
    return cleaned


# -- storage --------------------------------------------------------------------------


def _row(row: dict[str, Any]) -> Provider:
    return Provider(
        row["id"], row["name"], row["kind"], row["base_url"], row["api_key"], bool(row["enabled"])
    )


def list_providers(db: Database) -> list[Provider]:
    return [_row(r) for r in db.all("SELECT * FROM ai_providers ORDER BY created, name")]


def get_provider(db: Database, provider_id: str) -> Provider | None:
    row = db.one("SELECT * FROM ai_providers WHERE id = ?", (provider_id,))
    return _row(row) if row else None


def save_provider(db: Database, provider: Provider) -> Provider:
    db.execute(
        "INSERT INTO ai_providers (id, name, kind, base_url, api_key, enabled) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (id) DO UPDATE SET name = excluded.name, kind = excluded.kind, "
        "base_url = excluded.base_url, api_key = excluded.api_key, enabled = excluded.enabled",
        (
            provider.id,
            provider.name,
            provider.kind,
            provider.base_url,
            provider.api_key,
            int(provider.enabled),
        ),
    )
    _models.pop(provider.id, None)
    return provider


def usable_provider(db: Database, provider_id: str) -> Provider | None:
    """The provider, unless it is missing or turned off."""
    provider = get_provider(db, provider_id) if provider_id else None
    return provider if provider and provider.enabled else None


def delete_provider(db: Database, provider_id: str) -> None:
    db.execute("DELETE FROM ai_providers WHERE id = ?", (provider_id,))
    _models.pop(provider_id, None)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def migrate_single_provider(db: Database) -> None:
    """Carry the one server configured before providers existed over as the first provider."""
    if db.scalar("SELECT COUNT(*) FROM ai_providers"):
        return
    old = {
        row["key"]: json.loads(row["value"])
        for row in db.all(
            "SELECT key, value FROM settings WHERE key IN ('ai_base_url', 'ai_api_key')"
        )
    }
    base_url = clean_url(str(old.get("ai_base_url") or ""))
    if not base_url:
        return
    kind = guess_kind(base_url)
    names = {"gemini": "Google Gemini", "openai": "OpenAI", "perplexity": "Perplexity",
             "openrouter": "OpenRouter", "groq": "Groq", "ollama": "Ollama",
             "lmstudio": "LM Studio"}  # fmt: skip
    provider = Provider(
        new_id(), names.get(kind, "My server"), kind, base_url, str(old.get("ai_api_key") or "")
    )
    save_provider(db, provider)
    db.set_settings({"ai_provider": provider.id})
    db.execute("DELETE FROM settings WHERE key IN ('ai_base_url', 'ai_api_key')")


def seed_free_providers(db: Database) -> None:
    """Add the keyless providers once, so Ask AI works without any setup.

    Runs a single time per database: a provider the user later removes stays removed. An
    existing default model is left alone; only an install with none gets one.
    """
    if db.get_meta("free_providers_seeded"):
        return
    for entry in FREE_PROVIDERS:
        if get_provider(db, entry["id"]) is None:
            save_provider(
                db, Provider(entry["id"], entry["name"], entry["kind"], entry["base_url"], "")
            )
    if not db.get_setting("ai_provider") or not db.get_setting("ai_model"):
        first = FREE_PROVIDERS[0]
        db.set_settings({"ai_provider": first["id"], "ai_model": first["model"]})
    db.set_meta("free_providers_seeded", True)


def usable_models(provider: Provider, models: list[str]) -> list[str]:
    """The models of a provider that can be chatted with, and that its free tier covers."""
    if provider.kind == "kilo" and not provider.api_key:
        # Only the ":free" routes answer without a key; the rest of the catalogue is paid.
        # ("kilo-auto/free" is left out: tried without a key, it returned empty replies.)
        models = [m for m in models if m.endswith(":free")]
    if provider.kind in FREE_KINDS:
        models = [m for m in models if not _NOT_CHAT.search(m)]
    return models


# -- HTTP -----------------------------------------------------------------------------


def _client(provider: Provider, base_url: str | None = None) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {provider.api_key}"} if provider.api_key else {}
    return httpx.AsyncClient(
        base_url=(base_url or provider.base_url) + "/",
        headers=headers,
        timeout=httpx.Timeout(20, read=300),
        follow_redirects=True,
    )


async def _fail(response: httpx.Response) -> AIError:
    body = (await response.aread()).decode("utf-8", "replace")
    try:
        data = json.loads(body)
        if isinstance(data, list) and data:
            data = data[0]
        error = data.get("error")
        body = error.get("message", body) if isinstance(error, dict) else str(error or body)
    except (ValueError, AttributeError):
        pass
    if response.status_code == 429:
        return AIError(
            "The provider's rate limit was reached. Wait a minute, or pick another model in the "
            f"chat box. ({body.strip()[:160]})"
        )
    return AIError(f"The AI server answered HTTP {response.status_code}: {body.strip()[:300]}")


def _unreachable(provider: Provider, exc: Exception) -> AIError:
    reason = str(exc) or type(exc).__name__
    return AIError(f"Could not reach {provider.name} at {provider.base_url}: {reason}")


_models: dict[str, tuple[float, list[str]]] = {}


async def list_models(provider: Provider, use_cache: bool = False) -> list[str]:
    cached = _models.get(provider.id)
    if use_cache and cached and time.monotonic() - cached[0] < MODEL_CACHE_SECONDS:
        return cached[1]
    try:
        async with _client(provider) as client:
            response = await client.get(_MODELS_URL.get(provider.kind, "models"))
            if response.status_code != 200:
                raise await _fail(response)
            data = response.json()
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise _unreachable(provider, exc) from exc
    except ValueError as exc:
        raise AIError(f"{provider.name} did not return a model list") from exc
    items = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise AIError(f"{provider.name} did not return a model list")
    models = sorted(str(m["id"]) for m in items if isinstance(m, dict) and m.get("id"))
    models = usable_models(provider, models)
    _models[provider.id] = (time.monotonic(), models)
    return models


# What a chat stream yields:
#   ("text", str)        a piece of the answer
#   ("reasoning", str)   a piece of the model's thinking, to show apart from the answer
#   ("retract", None)    the text so far turned out to be thinking; it follows as "reasoning"
#   ("citations", [...]) web pages a searching model used
Event = tuple[str, Any]

REASONING_TAGS = ("think", "thinking", "thought", "thoughts", "reasoning", "reflection")
_OPEN_TAG = re.compile(rf"<({'|'.join(REASONING_TAGS)})>", re.I)
_CLOSE_TAG = re.compile(rf"</({'|'.join(REASONING_TAGS)})>", re.I)
_TAG_START = re.compile(r"</?[a-z]{0,10}$", re.I)


class ReasoningSplitter:
    """Separates a model's thinking from its answer in a stream of text.

    Models that reason before answering either send the reasoning in a field of its own, or
    write it inline between tags such as ``<think>`` (DeepSeek, Qwen) or ``<thought>``
    (Gemma). This handles the inline form, including tags split across chunks and templates
    that only emit the closing tag.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._inside: str | None = None
        self._answer = ""  # answer text handed out so far, in case it must be retracted
        self._closed = False

    def feed(self, text: str) -> list[Event]:
        self._buffer += text
        out: list[Event] = []
        while True:
            if self._inside:
                close = f"</{self._inside}>"
                at = self._buffer.lower().find(close)
                if at >= 0:
                    out.append(("reasoning", self._buffer[:at]))
                    self._buffer = self._buffer[at + len(close) :].lstrip()
                    self._inside, self._closed = None, True
                    continue
                keep = self._held_back()
                out.append(("reasoning", self._buffer[: len(self._buffer) - keep]))
                self._buffer = self._buffer[len(self._buffer) - keep :]
                break
            opening = _OPEN_TAG.search(self._buffer)
            closing = _CLOSE_TAG.search(self._buffer)
            if closing and not self._closed and (not opening or closing.start() < opening.start()):
                # A closing tag that was never opened: all of it so far was thinking.
                out += [
                    ("retract", None),
                    ("reasoning", self._answer + self._buffer[: closing.start()]),
                ]
                self._answer, self._buffer = "", self._buffer[closing.end() :].lstrip()
                self._closed = True
                continue
            # Thinking comes before the answer; a tag later on is the model quoting markup.
            if opening and not (self._answer + self._buffer[: opening.start()]).strip():
                self._inside = opening.group(1).lower()
                self._buffer = self._buffer[opening.end() :]
                continue
            keep = self._held_back()
            piece = self._buffer[: len(self._buffer) - keep]
            self._buffer = self._buffer[len(self._buffer) - keep :]
            if not self._answer:
                piece = piece.lstrip()  # blank lines between the thinking and the answer
            if piece:
                self._answer += piece
                out.append(("text", piece))
            break
        return [event for event in out if event[1] or event[0] == "retract"]

    def _held_back(self) -> int:
        """How much of the buffer's end might be the start of a tag still arriving."""
        at = self._buffer.rfind("<")
        if at < 0 or not _TAG_START.fullmatch(self._buffer[at:]):
            return 0
        return len(self._buffer) - at

    def flush(self) -> list[Event]:
        rest, self._buffer = self._buffer, ""
        if not rest:
            return []
        return [("reasoning" if self._inside else "text", rest)]


def split_reasoning(text: str) -> tuple[str, str]:
    """Split finished text into ``(reasoning, answer)``."""
    splitter = ReasoningSplitter()
    reasoning: list[str] = []
    answer: list[str] = []
    for kind, value in splitter.feed(text) + splitter.flush():
        if kind == "retract":
            answer.clear()
        elif kind == "reasoning":
            reasoning.append(value)
        elif kind == "text":
            answer.append(value)
    return "".join(reasoning).strip(), "".join(answer).strip()


def _citations(chunk: dict[str, Any], delta: dict[str, Any]) -> list[dict[str, str]]:
    """The web pages a model says it used, in whichever of three shapes it reports them."""
    found: list[dict[str, str]] = []
    for result in chunk.get("search_results") or []:  # Perplexity
        if isinstance(result, dict) and result.get("url"):
            found.append({"url": result["url"], "title": result.get("title") or ""})
    if not found:
        for url in chunk.get("citations") or []:  # Perplexity, older shape
            if isinstance(url, str):
                found.append({"url": url, "title": ""})
    for note in delta.get("annotations") or []:  # OpenAI, OpenRouter
        cite = note.get("url_citation") if isinstance(note, dict) else None
        if isinstance(cite, dict) and cite.get("url"):
            found.append({"url": cite["url"], "title": cite.get("title") or ""})
    return found


def _retry_delay(attempt: int, response: httpx.Response | None = None) -> float:
    """Seconds to wait before retrying, honouring ``Retry-After`` up to 10 s."""
    if response is not None:
        raw = response.headers.get("retry-after", "").strip()
        if raw:
            try:
                return max(0.0, min(float(raw), 10.0))
            except ValueError:
                pass
    return 0.5 * (2**attempt) + random.uniform(0, 0.3)


def _chunk_error(chunk: dict[str, Any], provider: Provider) -> AIError | None:
    err = chunk.get("error")
    if not err:
        return None
    if isinstance(err, dict):
        msg = err.get("message") or str(err)
    else:
        msg = str(err)
    return AIError(f"{provider.name} error: {str(msg).strip()[:300]}")


async def _iter_sse_json(
    response: httpx.Response, provider: Provider
) -> AsyncIterator[dict[str, Any]]:
    """Yield parsed JSON objects from an SSE stream, reassembling multi-line data frames."""
    buf: list[str] = []

    def _flush() -> tuple[bool, dict[str, Any] | None]:
        if not buf:
            return False, None
        payload = "\n".join(buf).strip()
        buf.clear()
        if not payload:
            return False, None
        if payload == "[DONE]":
            return True, None
        try:
            chunk = json.loads(payload)
        except ValueError:
            return False, None
        if not isinstance(chunk, dict):
            return False, None
        err = _chunk_error(chunk, provider)
        if err is not None:
            raise err
        return False, chunk

    async for line in response.aiter_lines():
        if line == "":
            done, chunk = _flush()
            if done:
                return
            if chunk is not None:
                yield chunk
        elif line.startswith("data:"):
            val = line[5:]
            if val.startswith(" "):
                val = val[1:]
            buf.append(val)
    done, chunk = _flush()
    if not done and chunk is not None:
        yield chunk


async def stream_chat(
    provider: Provider,
    model: str,
    messages: list[dict[str, str]],
    web: bool = False,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> AsyncIterator[Event]:
    """Stream a chat reply. With ``web`` the model is asked to search the internet itself."""
    started = time.monotonic()
    ok = False
    log.info("asking %s (%s)%s", provider.name, model, ", with web search" if web else "")
    try:
        async for event in _stream_chat(provider, model, messages, web, max_tokens):
            yield event
        ok = True
    except (GeneratorExit, asyncio.CancelledError):
        ok = True  # the reader stopped listening: not the provider's failure
        raise
    except AIError as exc:
        log.warning("%s (%s) failed: %s", provider.name, model, exc)
        raise
    finally:
        seconds = time.monotonic() - started
        metrics.ai_call(provider.name, ok, seconds)
        if ok:
            log.info("%s (%s) answered in %.1f s", provider.name, model, seconds)


async def _stream_chat(
    provider: Provider,
    model: str,
    messages: list[dict[str, str]],
    web: bool,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> AsyncIterator[Event]:
    if not model:
        raise AIError("No model is chosen. Pick one in the chat box or under Settings.")
    if web and provider.kind == "gemini":
        async for event in _gemini_grounded(provider, model, messages, max_tokens):
            yield event
        return
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "max_tokens": max_tokens,
    }
    if web and provider.kind == "openrouter":
        payload["plugins"] = [{"id": "web"}]
    elif web and provider.kind == "openai":
        payload["web_search_options"] = {}

    for attempt in range(MAX_RETRIES + 1):
        splitter = ReasoningSplitter()
        yielded = False
        try:
            async with _client(provider) as client:
                async with client.stream("POST", "chat/completions", json=payload) as response:
                    if response.status_code != 200:
                        if response.status_code in _RETRYABLE_STATUSES and attempt < MAX_RETRIES:
                            await response.aread()
                            await asyncio.sleep(_retry_delay(attempt, response))
                            continue
                        raise await _fail(response)
                    async for chunk in _iter_sse_json(response, provider):
                        choices = chunk.get("choices")
                        first = choices[0] if isinstance(choices, list) and choices else None
                        delta = first.get("delta") if isinstance(first, dict) else None
                        if not isinstance(delta, dict):
                            delta = {}
                        # Thinking sent in its own field: DeepSeek and vLLM call it
                        # reasoning_content, OpenRouter and Ollama call it reasoning.
                        thinking = delta.get("reasoning_content") or delta.get("reasoning")
                        if isinstance(thinking, str) and thinking:
                            yielded = True
                            yield "reasoning", thinking
                        content = delta.get("content")
                        if isinstance(content, str) and content:
                            for event in splitter.feed(content):
                                yielded = True
                                yield event
                        if web:
                            cited = _citations(chunk, delta)
                            if cited:
                                yielded = True
                                yield "citations", cited
                    for event in splitter.flush():
                        yielded = True
                        yield event
                    return
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            if not yielded and attempt < MAX_RETRIES:
                await asyncio.sleep(_retry_delay(attempt))
                continue
            raise _unreachable(provider, exc) from exc
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise _unreachable(provider, exc) from exc


def _gemini_root(provider: Provider) -> str:
    return provider.base_url.removesuffix("/openai") or GEMINI_API


async def _gemini_grounded(
    provider: Provider,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> AsyncIterator[Event]:
    """Gemini's Google Search grounding, which only its native API offers."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    payload: dict[str, Any] = {
        "contents": [
            {
                "role": "model" if m["role"] == "assistant" else "user",
                "parts": [{"text": m["content"]}],
            }
            for m in messages
            if m["role"] != "system"
        ],
        "tools": [{"google_search": {}}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    path = f"models/{model.removeprefix('models/')}:streamGenerateContent"
    for attempt in range(MAX_RETRIES + 1):
        yielded = False
        try:
            async with httpx.AsyncClient(
                base_url=_gemini_root(provider) + "/",
                headers={"x-goog-api-key": provider.api_key},
                timeout=httpx.Timeout(20, read=300),
            ) as client:
                async with client.stream(
                    "POST", path, params={"alt": "sse"}, json=payload
                ) as response:
                    if response.status_code != 200:
                        if response.status_code in _RETRYABLE_STATUSES and attempt < MAX_RETRIES:
                            await response.aread()
                            await asyncio.sleep(_retry_delay(attempt, response))
                            continue
                        raise await _fail(response)
                    async for chunk in _iter_sse_json(response, provider):
                        for candidate in chunk.get("candidates") or []:
                            if not isinstance(candidate, dict):
                                continue
                            content_obj = candidate.get("content") or {}
                            for part in (
                                content_obj.get("parts") if isinstance(content_obj, dict) else []
                            ) or []:
                                if (
                                    isinstance(part, dict)
                                    and isinstance(part.get("text"), str)
                                    and part["text"]
                                ):
                                    yielded = True
                                    yield (
                                        ("reasoning" if part.get("thought") else "text"),
                                        part["text"],
                                    )
                            grounding = candidate.get("groundingMetadata") or {}
                            if isinstance(grounding, dict):
                                cited = [
                                    {
                                        "url": c["web"]["uri"],
                                        "title": c["web"].get("title") or "",
                                    }
                                    for c in grounding.get("groundingChunks") or []
                                    if isinstance(c, dict)
                                    and isinstance(c.get("web"), dict)
                                    and c["web"].get("uri")
                                ]
                                if cited:
                                    yielded = True
                                    yield "citations", cited
                    return
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            if not yielded and attempt < MAX_RETRIES:
                await asyncio.sleep(_retry_delay(attempt))
                continue
            raise _unreachable(provider, exc) from exc
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise _unreachable(provider, exc) from exc


async def complete(provider: Provider, model: str, messages: list[dict[str, str]]) -> str:
    """The whole reply as text, without any of the model's thinking."""
    parts: list[str] = []
    async for kind, value in stream_chat(provider, model, messages):
        if kind == "retract":
            parts.clear()
        elif kind == "text":
            parts.append(value)
    return "".join(parts)


# -- speech ---------------------------------------------------------------------------

TTS_DEFAULTS = {
    "openai": ("gpt-4o-mini-tts", "alloy"),
    "gemini": ("gemini-2.5-flash-preview-tts", "Kore"),
    "groq": ("playai-tts", "Fritz-PlayAI"),
}


def _wav(pcm: bytes, rate: int = 24000) -> bytes:
    """Wrap raw 16-bit mono PCM in a WAV header so a browser can play it."""
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ", 16, 1, 1, rate, rate * 2, 2, 16,
        b"data", len(pcm),
    )  # fmt: skip
    return header + pcm


async def speech(provider: Provider, model: str, voice: str, text: str) -> tuple[bytes, str]:
    """Turn text into audio. Returns the audio bytes and their media type."""
    default_model, default_voice = TTS_DEFAULTS.get(provider.kind, ("tts-1", "alloy"))
    model, voice = model or default_model, voice or default_voice
    try:
        if provider.kind == "gemini":
            payload = {
                "contents": [{"parts": [{"text": text}]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
                },
            }
            async with httpx.AsyncClient(
                base_url=_gemini_root(provider) + "/",
                headers={"x-goog-api-key": provider.api_key},
                timeout=120,
            ) as client:
                path = f"models/{model.removeprefix('models/')}:generateContent"
                response = await client.post(path, json=payload)
                if response.status_code != 200:
                    raise await _fail(response)
                parts = response.json()["candidates"][0]["content"]["parts"]
                data = next(p["inlineData"] for p in parts if "inlineData" in p)
                rate = 24000
                if "rate=" in data.get("mimeType", ""):
                    rate = int(data["mimeType"].split("rate=")[1].split(";")[0])
                return _wav(base64.b64decode(data["data"]), rate), "audio/wav"
        async with _client(provider) as client:
            client.timeout = httpx.Timeout(120)
            response = await client.post(
                "audio/speech",
                json={"model": model, "voice": voice, "input": text, "response_format": "mp3"},
            )
            if response.status_code != 200:
                raise await _fail(response)
            media_type = response.headers.get("content-type", "audio/mpeg").split(";")[0]
            return response.content, media_type if media_type.startswith("audio/") else "audio/mpeg"
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise _unreachable(provider, exc) from exc
    except (KeyError, IndexError, StopIteration, ValueError) as exc:
        raise AIError(f"{provider.name} did not return audio for model {model!r}") from exc
