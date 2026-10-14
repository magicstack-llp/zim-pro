# Contributing to Zim Pro

Thank you for helping. Bug reports, ideas, documentation and code are all welcome.

By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Reporting a bug

Open an [issue](https://github.com/magicstack-llp/zim-pro/issues/new/choose) and say:

- what you did, what you expected and what happened instead;
- the version (Settings → About) and how you run it (Docker image,
  `pip`, executable or source);
- anything in the log that looks related (`docker compose logs zim-pro`).

A security problem is not a bug report: see [SECURITY.md](SECURITY.md).

## Suggesting a feature

Open an issue describing the problem it solves before writing much code, so that we can
agree on the shape of it first.

## Setting up

You need [uv](https://docs.astral.sh/uv/) and Node.js 24.

```sh
git clone https://github.com/magicstack-llp/zim-pro.git
cd zim-pro
uv sync
(cd web && npm ci && npm run build)
KIWIX_DATA_DIR=./zims KIWIX_STATE_DIR=./state uv run zim-pro    # http://localhost:8080
```

For the web app with hot reload, run `npm run dev` in `web/` alongside the server.

Never point a development server at a library you care about: making ZIMs and downloads
write into `KIWIX_DATA_DIR`.

## Before opening a pull request

All of these must pass; CI runs the same checks:

```sh
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest
cd web && npx tsc --noEmit -p . && npm test && npm run build
```

- Keep a pull request to one change, and add tests for what it changes.
- Add a line under "Unreleased" in [CHANGELOG.md](CHANGELOG.md) for anything a user would
  notice.
- Update the README when you add or change a setting, an environment variable or a page.
- Write code that reads like the code around it, and user-facing text in plain words.
- New dependencies need a reason, and a licence compatible with the GPL v3.

## Licence

Zim Pro is licensed under the [GNU General Public License v3.0](LICENSE). By contributing,
you agree that your contributions are licensed under the same terms.
