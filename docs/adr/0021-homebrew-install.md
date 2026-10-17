# 0021: Homebrew install

- Status: accepted (2026-10-17).

## Context
Diffbell is published in a Homebrew tap (`brew install codetalks/tap/diffbell`), next
to the existing install from a checkout (`uv tool install .`, then `diffbell
upgrade`). A virtualenv formula installs the tool under
`<prefix>/Cellar/diffbell/<version>/libexec` and links `<prefix>/bin/diffbell` to it.
`brew upgrade` installs the new version in a new Cellar folder and deletes the old
one; `<prefix>/opt/diffbell` always links the current version.

`diffbell install` wrote the agent's plist with the `diffbell` on PATH, resolved: for
a Homebrew install that is the versioned Cellar path, so the first `brew upgrade`
would leave launchd running a deleted program (a crash loop under `KeepAlive`).
`diffbell upgrade` builds and installs a checkout with `uv tool install`, which would
fight Homebrew over the same command, and `doctor`'s Install check knows only
`install-source.json`, which a Homebrew install never writes.

## Decision
- `launchd.find_executable` maps a resolved path inside
  `<prefix>/Cellar/diffbell/<version>/` to the same file under `<prefix>/opt/diffbell/`
  (`stable_executable`), when that exists. Every other path, including a uv tool's,
  is kept resolved, as before.
- A Homebrew install is recognised from the running interpreter
  (`launchd.running_from_homebrew`: `sys.prefix`, resolved, inside that Cellar).
  - `diffbell upgrade` refuses it first, with exit 2: "run `brew upgrade diffbell`,
    then `diffbell install` to restart the agent". It never builds a checkout over a
    Homebrew install.
  - `doctor`'s Install check is ok with "Homebrew (<prefix>), diffbell <version>";
    `install-source.json` is not consulted.
- The formula lives in the tap repository (`CodeTalks/homebrew-tap`), installs from the
  GitHub release's sdist, depends on `watchman` and Homebrew's `python@3.15`,
  `pydantic` and `certifi`, and builds the other dependencies from their sdists. Its
  caveats say to run `diffbell install` after installing and after every
  `brew upgrade` (the running daemon keeps the old code until it restarts). Its test
  runs only `diffbell --version` and `diffbell help`.

Updating the tap for a release is a pull request on the tap, opened by
`scripts/bump_formula.py <version>` (`--dry-run` first prints the diff):
- The published release is the source of truth: the formula's `url` is the
  release asset, its `sha256` comes from the release's `SHA256SUMS`, and the
  resources are rendered from `uv.lock` at the release's tag
  (`scripts/formula_resources.py`: the runtime closure, markers evaluated for macOS
  on the formula's Python, minus what Homebrew formulae provide).
- It is idempotent per version: one branch `diffbell-<version>`, one open PR (an
  existing one is updated), and nothing at all when the formula already matches.
  It never downgrades.
- It acts as `GH_TOKEN` or the `gh` login, so a CI job can later wrap it unchanged.
  It is run by hand for now: an automated job needs a credential that can write the
  tap, and a tampered formula runs code on every user's machine at `brew install`,
  so it would take a GitHub App installation token scoped to the tap (not a
  personal token), only in the tag-triggered release workflow, never in a job that
  runs on `pull_request`. Such a job depends on `release` (`needs: release`), gets
  `permissions: contents: read`, passes the token as `GH_TOKEN`, and skips with a
  notice when the secret is absent through a step-level `if` on an `env:` value
  (a job-level `if` on `secrets.*` is the fragile form).
- The tap's CI (`brew test-bot`) builds, installs and tests the PR; merging it
  publishes the version.

## Consequences
- The agent survives `brew upgrade`: launchd runs the `opt` path, whose script's
  interpreter is the new version's once Homebrew relinks.
- Until `diffbell install` runs after an upgrade, the old daemon keeps running from a
  deleted folder; the caveat says so, and `install` restarts it.
- Two installs at once (uv tool and Homebrew) are possible; whichever `diffbell`
  comes first on PATH is the one `install` writes into the plist.
