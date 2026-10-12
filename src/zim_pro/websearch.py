"""Internet search through a search API, for questions the library cannot answer."""

from typing import Any
from urllib.parse import urlsplit

import httpx

from .providers import AIError

SERVICES = ("tavily", "brave", "serper", "searxng")
SNIPPET_CHARS = 1200


def _host(url: str) -> str:
    return urlsplit(url).netloc.removeprefix("www.")


def _result(title: Any, url: Any, text: Any) -> dict[str, str] | None:
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    return {
        "title": str(title or _host(url)).strip(),
        "url": url,
        "site": _host(url),
        "text": " ".join(str(text or "").split())[:SNIPPET_CHARS],
    }


async def search(
    service: str, api_key: str, base_url: str, query: str, count: int = 5
) -> list[dict[str, str]]:
    """Search the web and return ``title``, ``url``, ``site`` and ``text`` for each hit."""
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            if service == "tavily":
                response = await client.post(
                    "https://api.tavily.com/search",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"query": query, "max_results": count, "search_depth": "basic"},
                )
                rows = [("title", "url", "content"), "results"]
            elif service == "brave":
                response = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
                    params={"q": query, "count": count},
                )
                rows = [("title", "url", "description"), "web.results"]
            elif service == "serper":
                response = await client.post(
                    "https://google.serper.dev/search",
                    headers={"X-API-KEY": api_key},
                    json={"q": query, "num": count},
                )
                rows = [("title", "link", "snippet"), "organic"]
            elif service == "searxng":
                if not base_url.strip():
                    raise AIError(
                        "Set the address of your SearXNG server under Settings → Web search."
                    )
                response = await client.get(
                    base_url.strip().rstrip("/") + "/search",
                    params={"q": query, "format": "json"},
                )
                rows = [("title", "url", "content"), "results"]
            else:
                raise AIError("No web search service is set up.")
    except httpx.HTTPError as exc:
        raise AIError(f"Web search failed: {exc or type(exc).__name__}") from exc
    if response.status_code != 200:
        hint = " (SearXNG must have the JSON format enabled)" if service == "searxng" else ""
        raise AIError(
            f"Web search answered HTTP {response.status_code}{hint}: {response.text[:200]}"
        )
    try:
        data: Any = response.json()
    except ValueError as exc:
        raise AIError("Web search did not return JSON") from exc
    (title, url, text), path = rows
    for key in path.split("."):
        data = data.get(key) if isinstance(data, dict) else None
    hits = [
        _result(r.get(title), r.get(url), r.get(text)) for r in data or [] if isinstance(r, dict)
    ]
    return [hit for hit in hits if hit][:count]
