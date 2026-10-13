# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-10-13

### Added

- **Categories for the ZIMs you make.** Links, uploaded books and feed collections have a
  category dropdown, and a list of categories you can add to, rename and remove. Your
  categories show as filters in the Library, Search and Ask AI. Links CSV has a `category`
  column.
- **A picture for every ZIM made here.** Choose your own for a link, a book or a feed
  collection; otherwise the site's best icon is used (touch icon, favicon, then its share
  picture). The Create and Feeds pages show each ZIM's picture.
- **VPN and captcha solver have their own Settings sections**, each with a switch to turn
  it off without losing its addresses, and a live status.
- **Gluetun control server.** With its address and key, Settings and the menu show whether
  the VPN is connected, its public IP address, country, region and network, and a
  **Change IP** button reconnects the VPN for another address.
- **Change IP when a site says too many requests.** A switch that reconnects the VPN when
  a site answers HTTP 429 and asks for the page again from the new address.
- The foot of the menu shows the VPN and the captcha solver once they are set up: a
  status light and a switch each, and an "i" that opens the details (the VPN's IP address,
  country and network, and Change IP).
- A `docker/` folder of ready-to-run compose files, one per setup (on its own, with the
  captcha solver, with a VPN, with a VPN that already runs elsewhere, everything through
  the VPN, with browser capture, and all of it), sharing one `.env`.
- README: a full `docker compose` example with the VPN, the captcha solver and browser
  capture together.
- The `compose.yml` at the top of the repository builds from source with the full setup:
  VPN, captcha solver and browser capture, with its values in `.env`.

- The category dropdown also lists the categories already in the library, so a ZIM made
  here can be filed beside the books Kiwix publishes.
- **Test connection** for the VPN and for the captcha solver: tries the addresses as typed,
  saved or not, and says what answered.
- **API keys for MCP.** Generate, name and revoke keys in Settings. A key is shown once and
  only its SHA-256 fingerprint is stored; once a key exists, `/mcp` answers only to a key.
- **About**, in Settings: version, who makes Zim Pro, what the server runs on, where it
  keeps its files, links, and a check for a newer release.

### Changed

- **Settings is split into sections** (General, AI, Create and Feeds, Network, MCP, About)
  with a menu down the side, and a row of tabs on a phone. Links into Settings open the
  right section.
- Small site icons are drawn to fill the ZIM's picture, not left as a speck in the middle.
- A ZIM made by browser capture is filed under My ZIMs, not Other.

### Fixed

- Making a ZIM when the library folder is not writable (Docker creates a missing folder
  as root) now says so, with the fix, instead of "Unable to write ZIM file".
- Escape closes only the top dialog when one dialog was opened from another.

## [0.3.0] - 2026-10-13

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

[Unreleased]: https://github.com/magicstack-llp/zim-pro/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/magicstack-llp/zim-pro/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/magicstack-llp/zim-pro/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/magicstack-llp/zim-pro/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/magicstack-llp/zim-pro/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/magicstack-llp/zim-pro/releases/tag/v0.1.0
