# Homebrew formula

`blemees.rb` is a self-contained Homebrew formula for installing
`blemeesd` on macOS (and Homebrew-on-Linux). The package has no runtime
dependencies outside the Python standard library, so the formula just
installs into a virtualenv at `#{libexec}` and exposes `blemeesd` under
`#{bin}`.

## Publishing the tap (one-time)

Homebrew reads formulas from a tap repo named `homebrew-<name>`.

```bash
gh repo create juanheyns/homebrew-blemees --public --clone
cd homebrew-blemees
mkdir -p Formula
cp ../agent-daemon/packaging/homebrew/blemees.rb Formula/
git add Formula/blemees.rb
git commit -m "Add blemees formula"
git push
```

End users then:

```bash
brew tap juanheyns/blemees
brew install blemees
brew services start blemees      # optional: run at login
```

## Releasing a new version

On every `vX.Y.Z` tag of this repo:

1. Copy the updated formula into the tap repo.
2. Update the `url` line to the new tag:
   `https://github.com/juanheyns/agent-daemon/archive/refs/tags/vX.Y.Z.tar.gz`
3. Recompute the sha256:

   ```bash
   curl -sL https://github.com/juanheyns/agent-daemon/archive/refs/tags/vX.Y.Z.tar.gz | shasum -a 256
   ```

4. Replace `REPLACE_ME_ON_RELEASE` with the new digest.
5. Commit and push the tap repo.

`brew bump-formula-pr` can automate steps 2–4 if you'd rather go that
route.
