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

## Consequences
- The agent survives `brew upgrade`: launchd runs the `opt` path, whose script's
  interpreter is the new version's once Homebrew relinks.
- Until `diffbell install` runs after an upgrade, the old daemon keeps running from a
  deleted folder; the caveat says so, and `install` restarts it.
- Two installs at once (uv tool and Homebrew) are possible; whichever `diffbell`
  comes first on PATH is the one `install` writes into the plist.
