# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/magicstack-llp/zim-pro/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/magicstack-llp/zim-pro/releases/tag/v0.1.0
