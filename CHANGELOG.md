# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Create your own ZIMs.** A Create page with a directory of links, uploads, and a job list.
  - Links: save one page, a section or a whole site; clean-article or full-page mode;
    pictures and video on or off; CSV import with a preview, and export; Create and Update
    per link; optional automatic rebuild every N days.
  - Two capture engines per link: a built-in multithreaded crawler, and browser capture with
    Zimit for sites that need JavaScript (opt-in: it needs the Docker socket).
  - Uploads: PDF, EPUB, MOBI and AZW3 books are converted to readable, searchable pages with
    the original file kept inside the ZIM; ZIM files are checked and added as they are.
  - The crawler reads `robots.txt` (rules and Crawl-delay) and the site's sitemaps before
    any page, wherever the sitemaps are kept, and feeds them and followed links through one
    queue that holds each address once. An option includes a site's subdomains.
  - A site that answers "too many requests" (HTTP 429) is waited for as long as it asks and
    fetched more slowly afterwards, so no page is lost to it.
  - Builds can be paused and resumed; paused builds have their own tab.
  - Builds run as jobs (queued, processing, finished, failed, cancelled) with live progress
    and a list of anything that failed or was skipped. Every ZIM is verified against what
    was prepared before it enters the library.
- **Feeds.** A Feeds page that keeps ZIMs up to date from RSS and Atom feeds, with its own
  engine, queue, schedule and settings.
  - A collection holds any number of feeds and makes one ZIM, with a first page listing
    every article by month.
  - Only new articles are fetched; the ZIM is rewritten from what is kept on disk, and left
    alone when nothing is new.
  - A schedule per collection (minutes, hours or days), a limit on articles kept, full
    article or feed text, feed discovery from a site's address, OPML import and export,
    retry of failed articles, and pause and resume of syncs.
- Links: a rate limit (requests per second or per minute), sitemap addresses to read, and
  the address of the `robots.txt` to obey.
- Links: RSS feeds of the site with a schedule of their own. New articles are added to the
  ZIM without crawling the site again; the full rebuild keeps its own schedule.
- A proxy setting for everything fetched for links and feeds, and a captcha solver setting
  (FlareSolverr) for sites that answer with a browser check. `compose.yml` has optional
  Gluetun (VPN) and FlareSolverr services, commented out.
- Ask AI sources from books made from links show a link to the original page.
- A "My ZIMs" category for books made here; they are left out of the catalog update list.

### Fixed

- Builds in progress when the server is restarted are queued again, not marked cancelled.
- The library scan no longer picks up ZIM files inside hidden folders.

## [0.2.1] - 2026-10-12

### Added

- Published on PyPI: `pip install zim-pro`.

## [0.2.0] - 2026-10-12

### Added

- **Theme setting.** Light, dark or automatic, at the bottom of the menu; remembered per
  browser and applied before the page is drawn.
- **Search history.** A panel on the Search page, opened with the history button, lists past
  searches to run again or remove. A setting turns it off, and it can be cleared.
- **Turn providers on and off.** A switch on each AI provider stops it being used without
  removing it or its key.
- **Internet search toggle** in the chat box, always shown; it points to Settings when web
  search is not set up yet.
- **Standalone executables** for Linux x86-64 and arm64 in each release, which run without
  Python installed, and a checksum file for the release files.
- A project logo, used as the app icon, the favicon and the installed web-app icon.

### Changed

- The app opens on Ask AI. The Library moved from `/` to `/library`.
- Search comes before Library in the menu.
- The Python package is named `zim_pro` (was `kiwix_updater`).
- Releases: the Docker image is built for each architecture on its own runner at the same
  time as the package and executables, and the GitHub release is created only after all of
  them have succeeded.

### Fixed

- The MCP `get_article` tool no longer prints the article title twice.


## [0.1.0] - 2026-10-12

First release.

### Added

- **Library and reader.** Serves a folder of ZIM files at `/content/<book>/<path>` as
  `kiwix-serve` does, including redirects and `Range` requests for video. The reader has
  in-book search, title suggestions, a random-article button and a bookmarkable address.
- **Search.** Full text within a book, and across the whole library with results grouped by
  book; books without a full-text index fall back to title search.
- **Updates.** Compares each book with the Kiwix catalog and lists newer releases, with a
  policy per book (automatic, ask first, or pinned) and a history of past updates.
- **Downloads.** Multi-connection downloads across the Kiwix mirrors, checked with SHA-1 per
  4 MiB piece and SHA-256 at the end, resuming after a pause, a restart or a crash.
- **Catalog.** Browse and filter the whole Kiwix library and add books from it.
- **Ask AI.** Answers written from your books, with numbered citations and the source
  passages beside them. The model plans queries, keywords and the books to search; the
  relevant books are searched in depth and the rest lightly; the steps are shown above each
  answer. Chats are kept, and the model can be switched per message.
- **AI providers.** Any OpenAI-compatible chat API (OpenAI, Gemini, Perplexity, OpenRouter,
  Groq, Ollama, LM Studio, llama.cpp), with two free keyless providers added on first start.
  A model's reasoning is shown separately from its answer.
- **Web search** as an optional fallback, through a search service (Tavily, Brave, Serper,
  SearXNG) or a model that searches itself.
- **Read aloud** for answers, with the browser's voice or a provider's speech model.
- **Markdown pages.** `/markdown/<book>/<path>` gives any article as Markdown with its
  pictures, and the reader has a Markdown view with a copy button.
- **Source media.** Pictures, videos and recordings from source pages appear under each
  source and in a gallery below the answer, with a full-screen viewer.
- **MCP server** at `/mcp` (streamable HTTP) and over stdio, with tools to list books,
  search, suggest titles, read an article as Markdown, outline an article, and list or start
  updates.
- **Phone layout.** A bottom tab bar, full-screen pickers, and an installable web app.
- **Packaging.** A Docker image for amd64 and arm64 on GHCR, and a Python wheel with the web
  app inside, both published by a release workflow when a version tag is pushed.

[Unreleased]: https://github.com/magicstack-llp/zim-pro/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/magicstack-llp/zim-pro/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/magicstack-llp/zim-pro/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/magicstack-llp/zim-pro/releases/tag/v0.1.0
