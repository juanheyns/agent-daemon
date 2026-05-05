# Contributing to blemees-agentd

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
blemees-agentd --log-level debug
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

If your change touches the wire protocol (any `claude.*` / `blemees-agentd.*`
frame shape, session lifecycle, error codes), also update:

- `README.md` §§ 3–9 (the spec lives here),
- the matching JSON Schema under `blemees/schemas/`,
- a test that exercises the new shape.

Breaking protocol changes require a protocol-version bump (`blemees/1`
→ `blemees/2`) and a changelog note in the release body.

## Releasing

Maintainers only. From a clean `main` in sync with `origin`, run:

```bash
./scripts/release.sh patch        # 0.5.0 → 0.5.1
./scripts/release.sh minor        # 0.5.0 → 0.6.0
./scripts/release.sh major        # 0.5.0 → 1.0.0
./scripts/release.sh 0.6.2        # explicit
```

The script bumps `pyproject.toml`, commits, tags `vX.Y.Z`, and pushes
both. That tag triggers `.github/workflows/release.yml` (build →
publish-pypi → gh-release); `bump-tap.yml` then updates
`blemees/homebrew-tap`. Watch progress with `gh run watch`.
