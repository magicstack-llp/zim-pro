---
name: release
description: Release a new version of Zim Pro (GitHub release, Docker image on GHCR, PyPI package, standalone executables). Use when asked to release, publish, tag or cut a version. Works through a checklist and stops at the first item that fails.
argument-hint: "[version, e.g. 0.4.0 — or patch | minor | major]"
---

# Release Zim Pro

A release is made by pushing a tag `vX.Y.Z`. `.github/workflows/release.yml` then lints and
tests, builds the Docker image (amd64 and arm64), the Python package (uploaded to PyPI) and
the executables, and creates the GitHub release last, only if all of those succeeded.

**A PyPI version can never be replaced or reused.** So every check below comes before the
tag, and a check that fails stops the release: report it, do not work around it.

Go through the checklist in order. Tick each item in your reply as it passes, with the
evidence (the command's result, not "should be fine"). End with the filled-in checklist.

## 1. Decide the version

- [ ] Read the current version (`pyproject.toml`) and the last tag (`git tag --sort=-v:refname | head -1`).
- [ ] Pick the new version. If the user gave one, use it. Otherwise, from the
      "Unreleased" section of `CHANGELOG.md`: a new feature means a minor bump, only fixes
      mean a patch bump. A change that breaks existing setups (settings, environment
      variables, URLs, database that cannot migrate) needs the user's word before a major bump.
- [ ] The tag does not exist yet, here or on GitHub (`git ls-remote --tags origin vX.Y.Z`),
      and the version is not on PyPI (`https://pypi.org/pypi/zim-pro/X.Y.Z/json` answers 404).

## 2. The working tree

- [ ] On `master`, and up to date with `origin/master` (`git fetch`, then `git status -sb`).
- [ ] No uncommitted changes other than the ones this release makes. If there is other
      work, ask whether it belongs in the release before going on.
- [ ] The last CI run on `master` passed (`gh run list --workflow CI --limit 1`).

## 3. The code

All of these must pass locally, on the commit that will be tagged:

- [ ] `uv run ruff check src tests`
- [ ] `uv run ruff format --check src tests`
- [ ] `uv run pytest`
- [ ] `cd web && npm test && npm run build`

## 4. What the release says

- [ ] `CHANGELOG.md`: "Unreleased" is not empty, and covers everything a user would notice
      since the last tag (compare with `git log vLAST..HEAD --oneline`). Add what is missing.
- [ ] Move those entries under a new heading `## [X.Y.Z] - YYYY-MM-DD` (today), leaving an
      empty "Unreleased" above it. The release notes are cut from this section.
- [ ] Update the links at the foot of the changelog: `[Unreleased]` compares from
      `vX.Y.Z`, and a new `[X.Y.Z]` line compares the last tag with it.
- [ ] `README.md`: every mention of the old version is updated (the `:X.Y.Z` / `:X.Y`
      image tags, the executable's file name, the tag example under "Releasing"), and new
      features, settings and environment variables are described.
- [ ] Screenshots in `screenshots/` still show the app as it is. If the menu or a page
      shown in them changed, retake them (never from the user's own library state, and
      never with builds written into their library).

## 5. What gets built

- [ ] `uv version X.Y.Z` (sets `pyproject.toml` and `uv.lock`).
- [ ] `uv.lock` is in step with `pyproject.toml` (`uv lock --check`).
- [ ] New Python dependencies since the last tag
      (`git diff vLAST..HEAD -- pyproject.toml`): each one is either found by PyInstaller
      on its own or named in the `pyinstaller` command in `release.yml`
      (`--collect-all` / `--collect-submodules`). Packages with data files or native
      libraries need `--collect-all`.
- [ ] New system packages or files the app needs are in the `Dockerfile`, and nothing it
      needs is shut out by `.dockerignore`.
- [ ] `compose.yml` still passes `docker compose config -q`.
- [ ] Database changes migrate an existing database: start the new code once on a copy of
      a state folder from the last release and check it comes up with its data.

## 6. Publish

Only now, and only if every box above is ticked:

- [ ] Commit as `Release X.Y.Z: <a few words on what is in it>`.
- [ ] `git push origin master`
- [ ] `git tag vX.Y.Z && git push origin vX.Y.Z`
- [ ] Watch the workflow to its end: `gh run watch <id> --exit-status`.

If the workflow fails **before** the PyPI job ran, fix the cause, delete the tag
(`git push origin :refs/tags/vX.Y.Z`, `git tag -d vX.Y.Z`) and tag again. If it fails
**after** PyPI accepted the upload, that version is spent: tell the user and release the
fix as the next patch version.

## 7. Confirm it is really out

Do not report success from the workflow's colour alone:

- [ ] GitHub release exists with five files: two executables (`linux-x86_64`,
      `linux-arm64`), the wheel, the source archive and `SHA256SUMS`
      (`gh release view vX.Y.Z --json assets -q '.assets[].name'`).
- [ ] The release notes are the changelog section, not "See CHANGELOG.md."
- [ ] PyPI has it: `https://pypi.org/pypi/zim-pro/X.Y.Z/json` answers 200.
- [ ] The image has both architectures:
      `docker manifest inspect ghcr.io/magicstack-llp/zim-pro:X.Y.Z`.

## 8. Report

Give the user the filled-in checklist, the release link, and plainly:

- anything that was not verified (for example, a feature never exercised in the
  standalone executable);
- that their own running container is not updated by a release. Redeploying it
  (`docker compose up -d --build`) is a separate step: offer it, and before doing it check
  that no download, build or feed sync is running (`/api/downloads`, `/api/build/jobs`,
  `/api/feeds/runs`).

## Never

- Never tag with a failing or skipped check, and never pass `--no-verify`.
- Never move or re-push an existing tag whose package reached PyPI.
- Never use `pkill -f kiwix-updater` or `pkill -f zim-pro` to stop a test server: it also
  kills the server inside the user's container. Stop test servers by their port.
