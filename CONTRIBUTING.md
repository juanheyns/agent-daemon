# Contributing to blemeesd

Thanks for your interest. This document covers the dev loop, what CI
enforces, and how we shape commits and PRs.

## Dev setup

Python 3.11+. The package is stdlib-only at runtime; dev extras cover
tests and linting.

```bash
git clone https://github.com/blemees/blemees-daemon
cd blemees-daemon
uv pip install -e ".[dev]"     # or: pip install -e ".[dev]"
```

Run the daemon locally for smoke testing:

```bash
blemeesd --log-level debug
```

## Tests and lint

All four must pass locally before opening a PR — CI runs the same set:

```bash
ruff check .
ruff format --check .
pytest -q
uv build       # sanity-check sdist + wheel
```

Tests tagged `requires_claude` hit a real `claude` binary and are
skipped unless you pass `-m requires_claude` and have the CLI
authenticated.

## Scope discipline

Keep PRs tightly scoped. A bug fix does not need surrounding cleanup;
a new feature does not need a refactor. If you notice unrelated issues
while working, open a separate issue or PR rather than bundling them in.

## Commits and PRs

- Commit messages: short imperative subject, optional body explaining
  **why** (not **what** — the diff covers that). Conventional-commit
  prefixes (`fix:`, `feat:`, `ci:`, `docs:`, `packaging:`) are the norm
  in this repo, but not enforced.
- PRs: fill in the template (Summary + Test plan). Squash-merge is the
  default — a clean PR title becomes the `main` commit subject.
- Rebase on `main` before marking ready for review. Force-pushes on PR
  branches are fine.

## Protocol changes

If your change touches the wire protocol (any `claude.*` / `blemeesd.*`
frame shape, session lifecycle, error codes), also update:

- `README.md` §§ 3–9 (the spec lives here),
- the matching JSON Schema under `schemas/`,
- a test that exercises the new shape.

Breaking protocol changes require a protocol-version bump (`blemees/1`
→ `blemees/2`) and a changelog note in the release body.

## Releasing

Maintainers only. Tagging `vX.Y.Z` on `main` triggers `.github/workflows/release.yml`,
which builds and publishes to PyPI + GitHub Releases. `bump-tap.yml`
then pushes an updated formula to `blemees/homebrew-tap`.
