<p align="center">
  <img src="assets/logo.png" alt="Zim Pro logo: an elephant with a ball" width="180">
</p>

<h1 align="center">Zim Pro</h1>

**An offline AI knowledge base.** Zim Pro turns a folder of [ZIM](https://openzim.org) files
(offline Wikipedia, Stack Exchange, DevDocs, iFixit and the rest of the
[Kiwix catalog](https://library.kiwix.org)) into a library you can read, search and question,
on your own machine.

- **Knowledge that stays with you.** The books live on your disk and are read, searched and
  kept up to date by your own server.
- **AI that answers from them.** Ask a question and the answer is written from your books,
  with citations you can open and the pictures and videos from the pages it used.
- **Fully offline when you want it.** Point it at a local model (Ollama, LM Studio,
  llama.cpp) and nothing leaves your network: no internet is needed to read, search or ask.
  Hosted models and web search are there if you choose them, and the internet is otherwise
  only used to download and update books.
- **A knowledge source for your other AI tools.** The same library is served over MCP, so
  Claude and other LLM apps can search and read it.

It replaces `kiwix-serve`.

## What it does

- **Read** – a reader for every book, with in-book search and title suggestions. Entries are
  served at `/content/<book>/<path>` as `kiwix-serve` does, including `Range` for video.
- **Search** – full text within a book, or across the whole library with results grouped by
  book, with a history of past searches.
- **Ask AI** – ask a question and get an answer written from your books, with numbered
  citations, the source passages beside it, and the pictures and videos from those pages.
  Works with any OpenAI-compatible chat API, hosted or local, and with two free providers
  out of the box.
- **Markdown** – `/markdown/<book>/<path>` gives any article as Markdown: its text and
  pictures, with links leading to the Markdown of other articles. The reader's **Markdown**
  button shows it in place, with a copy button; videos and recordings play there too.
- **Updates** – compares each book with the Kiwix catalog and lists newer releases, with a
  policy per book: automatic, ask first, or pinned.
- **Downloads** – multi-connection across the Kiwix mirrors, SHA-1 checked per 4 MiB piece
  and SHA-256 checked at the end. Resumes after a pause, a restart or a crash.
- **Catalog** – browse the whole Kiwix library and add books.
- **MCP** – the library as tools for Claude and other LLM apps: streamable HTTP at `/mcp`,
  or stdio with `zim-pro mcp`.
- **Phone-friendly** – installable as a web app, with a layout made for small screens.

## Run with Docker

The image is published at `ghcr.io/magicstack-llp/zim-pro` for amd64 and arm64. It needs two
folders: your ZIM files, mounted at `/data`, and a folder for its database, mounted at
`/state`. The container runs as the user you give it, who must be able to write to both
(to `/data` because that is where downloads and updates go).

### docker run

```sh
mkdir -p zims state              # create them yourself, or Docker creates them as root
docker run -d --name zim-pro \
  --restart unless-stopped \
  --user "$(id -u):$(id -g)" \
  -p 8083:8080 \
  -v "$PWD/zims:/data" \
  -v "$PWD/state:/state" \
  --add-host host.docker.internal:host-gateway \
  ghcr.io/magicstack-llp/zim-pro:latest
```

Then open http://localhost:8083. `--add-host` is only needed for Ask AI to reach a model
server on the same machine (Ollama, LM Studio, llama.cpp).

### docker compose

```yaml
services:
  zim-pro:
    image: ghcr.io/magicstack-llp/zim-pro:latest
    container_name: zim-pro
    restart: unless-stopped
    user: "1000:1000"                # your own uid:gid, from `id -u` and `id -g`
    ports:
      - "8083:8080"
    extra_hosts:
      - "host.docker.internal:host-gateway"
    volumes:
      - ./zims:/data                 # your folder of .zim files
      - ./state:/state
    # environment:
    #   KIWIX_ADMIN_TOKEN: change-me # required for changes and /mcp when set
```

```sh
mkdir -p zims state
docker compose up -d
```

### Everyday commands

| To | docker compose | docker run |
|---|---|---|
| See the logs | `docker compose logs -f` | `docker logs -f zim-pro` |
| Stop | `docker compose stop` | `docker stop zim-pro` |
| Start again | `docker compose start` | `docker start zim-pro` |
| Update to the newest image | `docker compose pull && docker compose up -d` | `docker pull ghcr.io/magicstack-llp/zim-pro:latest`, then `docker rm -f zim-pro` and the `docker run` above |
| Remove the container | `docker compose down` | `docker rm -f zim-pro` |

Removing the container loses nothing: the books are in `zims` and the settings, chats and
download progress are in `state`. A download in progress when the container stops carries on
from where it was when it starts again.

To stay on one version, use a version tag in place of `latest`, such as `:0.2.0` or `:0.2`.

### Build the image yourself

The `compose.yml` in this repository builds from source:

```sh
mkdir -p state
docker compose up -d --build     # http://localhost:8083
```

It mounts `/mnt/storage/Media/Kiwix/data` as the library; change that path to your own
folder of ZIM files.

## Run without Docker

Each [release](https://github.com/magicstack-llp/zim-pro/releases) has two other ways to run
it, with the web app included in both.

**Standalone executable** for Linux (x86-64 and arm64). One file, no Python needed:

```sh
tar -xzf zim-pro-0.2.0-linux-x86_64.tar.gz
cd zim-pro-0.2.0-linux-x86_64
KIWIX_DATA_DIR=/path/to/zims KIWIX_STATE_DIR=./state ./zim-pro   # http://localhost:8080
```

**Python package**, for Python 3.13 on Linux or macOS:

```sh
pip install https://github.com/magicstack-llp/zim-pro/releases/download/v0.2.0/zim_pro-0.2.0-py3-none-any.whl
KIWIX_DATA_DIR=/path/to/zims KIWIX_STATE_DIR=./state zim-pro   # http://localhost:8080
```

`SHA256SUMS` in the release lists the checksum of every file.

## Run from source

```sh
(cd web && npm ci && npm run build)
KIWIX_DATA_DIR=/path/to/zims KIWIX_STATE_DIR=./state uv run zim-pro
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

The project was first called Kiwix Updater. The `KIWIX_*` variables and the database file
(`kiwix-updater.sqlite3`) keep that name so existing installs carry on unchanged, and the
`kiwix-updater` command still works alongside `zim-pro`.

## Ask AI

Add one or more providers under Settings → AI providers, then pick a default model. Any
server with the OpenAI `/chat/completions` API works: OpenAI, Google Gemini, Perplexity,
OpenRouter, Groq, Ollama, LM Studio, llama.cpp, vLLM. The model can be switched per message
in the chat box.

Each provider has a switch: turned off, it is kept with its key but not used or offered.

Two providers that need no account or key are added on first start, so Ask AI works straight
after installing: **OVHcloud AI Endpoints** (Llama 3.3 70B, Qwen, Mistral, gpt-oss; about two
requests a minute per model) and **Kilo Gateway**'s free models (about 200 requests an hour;
Kilo may log free-tier prompts for training). Both are rate-limited per IP address and can be
removed under Settings; a removed one is not added back.

For a model server on the Docker host, use `http://host.docker.internal:<port>/v1` (Ollama:
`11434`, LM Studio: `1234`), not `localhost`, and make the server listen on all interfaces
(for Ollama, `OLLAMA_HOST=0.0.0.0`).

How a question is answered (see `src/zim_pro/retrieval.py`):

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
question while the Internet button in the chat box is on. Either:

- a *search service* (Tavily, Brave, Serper for Google results, or your own SearXNG), whose
  results are added to the sources for your chat model, or
- a *searching model* that looks things up itself: Perplexity Sonar, OpenRouter's web add-on,
  an OpenAI search model, or Gemini with Google Search.

**Read aloud** adds a listen button to answers, using the browser's built-in voice or a
provider's speech model (OpenAI, Gemini, Groq, or a local `/audio/speech` server).

## MCP

```sh
claude mcp add --transport http zim-pro http://localhost:8083/mcp
```

Tools: `list_books`, `search`, `suggest`, `get_article`, `get_article_outline`,
`list_updates`, and `start_update` (off until enabled in Settings).

For a stdio client, run `zim-pro mcp` with `KIWIX_DATA_DIR` and `KIWIX_STATE_DIR` set.

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

## Built with

Python 3.13, FastAPI and [python-libzim](https://github.com/openzim/python-libzim) on the
server, SQLite for state, and React, Vite and Tailwind for the web app. Zim Pro is an
independent project and is not affiliated with Kiwix.

## Releasing

Pushing a tag of the form `v1.2.3` runs `.github/workflows/release.yml`:

1. Lint and tests. Nothing is published if they fail.
2. In parallel: the Docker image for amd64 and for arm64 (each on its own runner, then joined
   and published to GHCR as `1.2.3`, `1.2` and `latest`), the Python wheel and source
   archive, and the standalone executables.
3. Last, and only if all of those succeeded, the GitHub release, with the executables, the
   wheel, the source archive and their checksums attached.

The package version comes from the tag, and the release notes from that version's section of
`CHANGELOG.md`, so move the entries under "Unreleased" to a new version heading before
tagging.

```sh
git tag v0.2.0 && git push origin v0.2.0
```

Every push to `master` and every pull request runs `.github/workflows/ci.yml`: Python lint
(`ruff check`), formatting check and tests, and the web app's type check, tests and build.
