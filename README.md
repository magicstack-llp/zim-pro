# Kiwix Updater

A self-hosted replacement for `kiwix-serve`: browse, read and search a folder of ZIM files,
keep them updated from the Kiwix catalog, and expose the library to LLM apps over MCP.

- **Reader** – `/content/<book>/<path>` serves ZIM entries (redirects, `Range` for video).
- **Markdown** – `/markdown/<book>/<path>` gives the same article as Markdown: its text and
  pictures, with links leading to the Markdown of other articles. The reader's **Markdown**
  button shows it in place, with a copy button; videos and recordings play there too.
- **Search** – full text per book, grouped results across all books, title suggestions.
- **Updates** – compares each local book with `library.kiwix.org` and lists newer releases;
  per-book policy: automatic, ask first, or pinned.
- **Downloader** – multi-connection across the Kiwix mirrors, SHA-1 checked per 4 MiB piece,
  SHA-256 checked at the end, resumes after a pause, restart or crash.
- **Ask AI** – ask a question and get an answer written from your books, with sources cited.
  Works with any OpenAI-compatible chat API, hosted or local.
- **MCP** – streamable HTTP at `/mcp`, or stdio with `kiwix-updater mcp`.

## Run with Docker

```sh
mkdir -p state                   # must exist and belong to you, or Docker creates it as root
docker compose up -d --build     # http://localhost:8083
```

`compose.yml` mounts `/mnt/storage/Media/Kiwix/data` as the library and `./state` for the
SQLite database. The container runs as uid 1000, which must be able to write to both.

## Run from source

```sh
(cd web && npm ci && npm run build)
KIWIX_DATA_DIR=/path/to/zims KIWIX_STATE_DIR=./state uv run kiwix-updater
```

For UI work, run `npm run dev` in `web/` (set `KIWIX_BACKEND` if the server is not on
`127.0.0.1:8080`).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `KIWIX_DATA_DIR` | `/data` | Folder of `.zim` files (scanned recursively) |
| `KIWIX_STATE_DIR` | `/state` | Where the SQLite database lives |
| `KIWIX_HOST` / `KIWIX_PORT` | `0.0.0.0` / `8080` | Listen address |
| `KIWIX_ADMIN_TOKEN` | unset | If set, changes and `/mcp` need `Authorization: Bearer <token>` |
| `KIWIX_CATALOG_URL` | Kiwix OPDS feed | Catalog to check for updates |

Download limits, the update schedule and policies are set in the web UI under Settings.

## Ask AI

Add one or more providers under Settings → AI providers, then pick a default model. Any
server with the OpenAI `/chat/completions` API works: OpenAI, Google Gemini, Perplexity,
OpenRouter, Groq, Ollama, LM Studio, llama.cpp, vLLM. The model can be switched per message
in the chat box.

Two providers that need no account or key are added on first start, so Ask AI works straight
after installing: **OVHcloud AI Endpoints** (Llama 3.3 70B, Qwen, Mistral, gpt-oss; about two
requests a minute per model) and **Kilo Gateway**'s free models (about 200 requests an hour;
Kilo may log free-tier prompts for training). Both are rate-limited per IP address and can be
removed under Settings; a removed one is not added back.

For a model server on the Docker host, use `http://host.docker.internal:<port>/v1` (Ollama:
`11434`, LM Studio: `1234`), not `localhost`, and make the server listen on all interfaces
(for Ollama, `OLLAMA_HOST=0.0.0.0`).

How a question is answered (see `src/kiwix_updater/retrieval.py`):

1. **Plan.** The model is shown the list of books and returns several short queries from
   different angles, extra keywords a good article would contain, and the books most likely
   to help. If it cannot be asked in time, the question's own words are used.
2. **Search the relevant books in depth** with every query and keyword. These are the model's
   picks, books named after the subject, and always the main encyclopedia.
3. **Search every other book lightly** with the main queries.
4. **Rank.** Articles found by several searches rise (reciprocal rank fusion); the best 24 are
   read and scored on how much of the subject one passage covers, and only passages that fit
   are sent to the model with the question.

Articles reach the model as Markdown (`library/article.py`), not HTML: navigation, pictures
and their captions, citation markers, reference lists, "See also" and "External links"
sections and licence footers are removed and links are flattened to their text, so the tokens
go on prose. Each passage is sent under the heading of the section it came from.

The pictures, videos and recordings on each source's page are listed with the source and
shown as thumbnails under it; they are not sent to the model.

Chats are stored in the database.

Every prompt carries formatting rules, and answers are passed through a repair step before
they are shown (unclosed code blocks, leaked reasoning, malformed lists, tables and citations),
since small models often get Markdown wrong. Your own standing instructions can be added
under Settings → Ask AI.

**Web search** (off by default) is used when the library has no good match, or for every
question while the globe button in the chat box is on. Either:

- a *search service* (Tavily, Brave, Serper for Google results, or your own SearXNG), whose
  results are added to the sources for your chat model, or
- a *searching model* that looks things up itself: Perplexity Sonar, OpenRouter's web add-on,
  an OpenAI search model, or Gemini with Google Search.

**Read aloud** adds a listen button to answers, using the browser's built-in voice or a
provider's speech model (OpenAI, Gemini, Groq, or a local `/audio/speech` server).

## MCP

```sh
claude mcp add --transport http kiwix http://localhost:8083/mcp
```

Tools: `list_books`, `search`, `suggest`, `get_article`, `get_article_outline`,
`list_updates`, and `start_update` (off until enabled in Settings).

For a stdio client, run `kiwix-updater mcp` with `KIWIX_DATA_DIR` and `KIWIX_STATE_DIR` set.

## Notes

- Articles are served from the same origin as the admin UI, as with `kiwix-serve`. Scripts
  inside a ZIM could therefore call the API; set `KIWIX_ADMIN_TOKEN` if you load ZIMs you do
  not trust or expose the port beyond your LAN.
- libzim holds Python's GIL, so a slow full-text search briefly delays other requests.
- libzim does not expose relevance scores, so cross-book search groups results by book
  instead of ranking them together.

## Tests

```sh
uv run pytest
(cd web && npm test)
```
