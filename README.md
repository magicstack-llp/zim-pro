# Kiwix Updater

A self-hosted replacement for `kiwix-serve`: browse, read and search a folder of ZIM files,
keep them updated from the Kiwix catalog, and expose the library to LLM apps over MCP.

- **Reader** – `/content/<book>/<path>` serves ZIM entries (redirects, `Range` for video).
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

Set the server address, API key and model under Settings → Ask AI. Any server with the
OpenAI `/chat/completions` API works: OpenAI, OpenRouter, Ollama, LM Studio, llama.cpp, vLLM.

For a model server on the Docker host, use `http://host.docker.internal:<port>/v1` (Ollama:
`11434`, LM Studio: `1234`), not `localhost`, and make the server listen on all interfaces
(for Ollama, `OLLAMA_HOST=0.0.0.0`).

Each question is turned into keyword searches by the model, the library is searched, and the
best passages are sent to the model with the question. Nothing else leaves the machine.

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
```
