#!/usr/bin/env bash
# Bump pyproject version, commit, tag, push.
#
# Usage:
#   scripts/release.sh patch         # 0.5.0 -> 0.5.1
#   scripts/release.sh minor         # 0.5.0 -> 0.6.0
#   scripts/release.sh major         # 0.5.0 -> 1.0.0
#   scripts/release.sh 0.6.2         # explicit version
#
# Pre-flight: working tree clean, on main, in sync with origin/main, the
# target tag does not exist locally or on origin. You'll be prompted to
# confirm before anything is committed or pushed.
#
# After the push, .github/workflows/release.yml runs build → publish-pypi
# → gh-release; bump-tap.yml then updates blemees/homebrew-tap. Watch
# with `gh run watch` or open https://github.com/blemees/blemees-daemon/actions.

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

# ---- arg validation (no side effects) -----------------------------------

bump="${1:-}"
if [[ -z "$bump" ]]; then
    echo "usage: $(basename "$0") <major|minor|patch|X.Y.Z>" >&2
    exit 64
fi

case "$bump" in
    major | minor | patch) ;;
    *)
        if [[ ! "$bump" =~ ^[0-9]+\.[0-9]+\.[0-9]+([a-zA-Z0-9.+-]*)?$ ]]; then
            echo "invalid version: '$bump' (want major|minor|patch or X.Y.Z)" >&2
            exit 64
        fi
        ;;
esac

# ---- read current version + compute next --------------------------------

# Stdlib regex instead of tomllib so this works on any python3 (the
# project itself needs 3.11+, but a release script that runs on whatever
# the user's shell happens to point at avoids a needless friction).
current="$(python3 - <<'PY'
import pathlib, re, sys
text = pathlib.Path("pyproject.toml").read_text()
m = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
if not m:
    sys.exit("could not find a version line in pyproject.toml")
print(m.group(1))
PY
)"

case "$bump" in
    major | minor | patch)
        next="$(python3 - "$current" "$bump" <<'PY'
import sys
cur, kind = sys.argv[1], sys.argv[2]
parts = cur.split(".")
if len(parts) < 3 or not all(p.isdigit() for p in parts[:3]):
    sys.exit(f"current version {cur!r} is not plain X.Y.Z; pass an explicit version instead")
M, m, p = (int(x) for x in parts[:3])
if kind == "major":
    M, m, p = M + 1, 0, 0
elif kind == "minor":
    m, p = m + 1, 0
elif kind == "patch":
    p = p + 1
print(f"{M}.{m}.{p}")
PY
)"
        ;;
    *)
        next="$bump"
        ;;
esac

# ---- pre-flight checks --------------------------------------------------

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "working tree is dirty — commit or stash first" >&2
    exit 1
fi

branch="$(git symbolic-ref --short HEAD)"
if [[ "$branch" != "main" ]]; then
    echo "expected branch 'main', got '$branch'" >&2
    exit 1
fi

git fetch --quiet origin main
local_sha="$(git rev-parse HEAD)"
remote_sha="$(git rev-parse origin/main)"
if [[ "$local_sha" != "$remote_sha" ]]; then
    echo "local main is not in sync with origin/main:" >&2
    echo "  local:  ${local_sha:0:12}" >&2
    echo "  origin: ${remote_sha:0:12}" >&2
    echo "rebase or pull first" >&2
    exit 1
fi

if git rev-parse --verify --quiet "v$next" >/dev/null; then
    echo "tag v$next already exists locally" >&2
    exit 1
fi
if git ls-remote --tags origin "refs/tags/v$next" 2>/dev/null | grep -q .; then
    echo "tag v$next already exists on origin" >&2
    exit 1
fi

# ---- confirm ------------------------------------------------------------

echo "About to release: v$current → v$next"
echo "  Will commit a version bump on main and push tag v$next."
read -r -p "Continue? [y/N] " ans
case "$ans" in
    y | Y | yes | YES) ;;
    *)
        echo "aborted"
        exit 1
        ;;
esac

# ---- bump + commit + tag + push -----------------------------------------

python3 - "$next" <<'PY'
import pathlib, re, sys
new = sys.argv[1]
path = pathlib.Path("pyproject.toml")
text = path.read_text()
# Anchor on '^version = "..."' so we don't accidentally rewrite
# tool.ruff's `target-version = "py311"` or anything similar.
text, n = re.subn(
    r'^version\s*=\s*"[^"]+"',
    f'version = "{new}"',
    text,
    count=1,
    flags=re.MULTILINE,
)
if n != 1:
    raise SystemExit("could not find the [project] version line in pyproject.toml")
path.write_text(text)
PY

git add pyproject.toml
git commit -m "release: v$next"
git tag -a "v$next" -m "Release v$next"
git push origin main "v$next"

cat <<EOF

✓ pushed v$next.
  Workflow:  gh run watch    (or https://github.com/blemees/blemees-daemon/actions)
  Release:   https://github.com/blemees/blemees-daemon/releases/tag/v$next
  PyPI:      https://pypi.org/project/blemees/$next/
EOF
