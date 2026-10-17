"""Open (or update) the tap pull request that moves the formula to a release (ADR 0021).

    uv run python scripts/bump_formula.py 0.1.2 --dry-run   # print the formula diff
    uv run python scripts/bump_formula.py 0.1.2             # push a branch, open the PR

The published release is the source of truth: the sdist URL is its asset, the
sha256 comes from its `SHA256SUMS`, and the resources are rendered from `uv.lock`
at the release's tag (`formula_resources.render`, for the formula's Python). Per
version it is idempotent: one branch `diffbell-<version>` on the tap, one open PR
(an existing one is updated, never duplicated), and nothing happens when the
formula on the tap's main or on that branch already matches.

Every GitHub call goes through `gh`, so it acts as `GH_TOKEN` when set and as the
`gh auth login` account otherwise. The tap's own CI (`brew test-bot`) builds and
tests the PR; merging it publishes the new version.
"""

from __future__ import annotations

import argparse
import base64
import difflib
import importlib.util
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

REPO = "CodeTalks/diffbell"
TAP = "CodeTalks/homebrew-tap"
FORMULA = "Formula/diffbell.rb"
VERSION = re.compile(r"^v?(\d+\.\d+\.\d+)$")
URL_LINE = re.compile(r'^  url "[^"]*"$', re.MULTILINE)
SHA_LINE = re.compile(r'^  sha256 "[0-9a-f]{64}"$', re.MULTILINE)
PYTHON_DEP = re.compile(r'^  depends_on "python@(\d+\.\d+)"$', re.MULTILINE)
RESOURCES_START = re.compile(r"^  pypi_packages [^\n]*\n\n", re.MULTILINE)
INSTALL = "  def install\n"


class BumpError(Exception):
    pass


# args -> stdout; raises BumpError on a non-zero exit. Tests inject a fake.
Gh = Callable[[list[str]], str]


def real_gh(args: list[str]) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise BumpError(f"gh {args[0]} failed: {result.stderr.strip() or result.returncode}")
    return result.stdout


def _resources_module() -> ModuleType:
    # Loaded by path: `python -I` keeps scripts/ off sys.path.
    path = Path(__file__).with_name("formula_resources.py")
    spec = importlib.util.spec_from_file_location("formula_resources", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["formula_resources"] = module
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True, slots=True)
class Release:
    version: str
    tag: str
    url: str
    sha256: str
    lock: str


def sdist_sha256(sums: str, version: str) -> str:
    name = f"diffbell-{version}.tar.gz"
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == name and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            return parts[0]
    raise BumpError(f"SHA256SUMS of the release has no line for {name}")


def read_release(gh: Gh, version: str, repo: str = REPO) -> Release:
    tag = f"v{version}"
    sums = gh(
        ["release", "download", tag, "--repo", repo, "--pattern", "SHA256SUMS", "--output", "-"]
    )
    lock = gh(
        [
            "api",
            "-H",
            "Accept: application/vnd.github.raw",
            f"repos/{repo}/contents/uv.lock?ref={tag}",
        ]
    )
    url = f"https://github.com/{repo}/releases/download/{tag}/diffbell-{version}.tar.gz"
    return Release(version, tag, url, sdist_sha256(sums, version), lock)


def formula_python(formula: str) -> str:
    match = PYTHON_DEP.search(formula)
    if match is None:
        raise BumpError('the formula has no depends_on "python@X.Y"')
    return match.group(1)


def update_formula(formula: str, url: str, sha256: str, resources: str) -> str:
    """The formula with its url, sha256 and resource blocks replaced; the rest kept."""
    if len(URL_LINE.findall(formula)) != 1 or len(SHA_LINE.findall(formula)) != 1:
        raise BumpError("expected exactly one top-level url and sha256 line in the formula")
    start = RESOURCES_START.search(formula)
    end = formula.find(INSTALL)
    if start is None or end < start.end():
        raise BumpError("expected the resources between pypi_packages and def install")
    formula = formula[: start.end()] + resources + "\n" + formula[end:]
    formula = URL_LINE.sub(f'  url "{url}"', formula)
    return SHA_LINE.sub(f'  sha256 "{sha256}"', formula)


def read_formula(gh: Gh, ref: str, tap: str = TAP) -> str:
    return gh(
        [
            "api",
            "-H",
            "Accept: application/vnd.github.raw",
            f"repos/{tap}/contents/{FORMULA}?ref={ref}",
        ]
    )


def branch_exists(gh: Gh, branch: str, tap: str = TAP) -> bool:
    out = gh(["api", f"repos/{tap}/branches", "--paginate", "--jq", ".[].name"])
    return branch in out.split()


def write_branch(gh: Gh, branch: str, formula: str, message: str, tap: str = TAP) -> None:
    if not branch_exists(gh, branch, tap):
        main = gh(["api", f"repos/{tap}/git/ref/heads/main", "--jq", ".object.sha"]).strip()
        gh(
            [
                "api",
                "-X",
                "POST",
                f"repos/{tap}/git/refs",
                "-f",
                f"ref=refs/heads/{branch}",
                "-f",
                f"sha={main}",
            ]
        )
    blob = gh(["api", f"repos/{tap}/contents/{FORMULA}?ref={branch}", "--jq", ".sha"]).strip()
    content = base64.b64encode(formula.encode("utf-8")).decode("ascii")
    gh(
        [
            "api",
            "-X",
            "PUT",
            f"repos/{tap}/contents/{FORMULA}",
            "-f",
            f"message={message}",
            "-f",
            f"content={content}",
            "-f",
            f"sha={blob}",
            "-f",
            f"branch={branch}",
        ]
    )


def open_pr(gh: Gh, release: Release, branch: str, tap: str = TAP, repo: str = REPO) -> str:
    found = json.loads(
        gh(["pr", "list", "--repo", tap, "--head", branch, "--state", "open", "--json", "url"])
    )
    if found:
        return str(found[0]["url"])
    body = (
        f"Moves the formula to [Diffbell {release.version}]"
        f"(https://github.com/{repo}/releases/tag/{release.tag}): the release's sdist and its "
        "SHA256SUMS, with the resources rendered from the tag's `uv.lock`.\n\n"
        "Opened by `scripts/bump_formula.py`. Merge once `brew test-bot` passes."
    )
    title = f"diffbell {release.version}"
    out = gh(
        [
            "pr",
            "create",
            "--repo",
            tap,
            "--base",
            "main",
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
        ]
    )
    return out.strip().splitlines()[-1]


def formula_version(formula: str) -> tuple[int, ...]:
    match = re.search(r'^  url "[^"]*/diffbell-(\d+\.\d+\.\d+)\.tar\.gz"$', formula, re.MULTILINE)
    if match is None:
        raise BumpError("the formula's url names no diffbell-X.Y.Z.tar.gz")
    return tuple(int(part) for part in match.group(1).split("."))


def bump(gh: Gh, version: str, dry_run: bool, out: Callable[[str], None] = print) -> int:
    match = VERSION.match(version)
    if match is None:
        raise BumpError(f"not a version: {version} (expected X.Y.Z or vX.Y.Z)")
    current = read_formula(gh, "main")
    target = tuple(int(part) for part in match.group(1).split("."))
    if target < formula_version(current):
        raise BumpError(f"{match.group(1)} is older than the tap's formula: never downgrade")
    release = read_release(gh, match.group(1))
    resources = _resources_module().render(release.lock, formula_python(current))
    wanted = update_formula(current, release.url, release.sha256, resources)
    if wanted == current:
        out(f"The tap's formula is already at diffbell {release.version}: nothing to do.")
        return 0
    if dry_run:
        out(
            "".join(
                difflib.unified_diff(
                    current.splitlines(keepends=True),
                    wanted.splitlines(keepends=True),
                    f"a/{FORMULA}",
                    f"b/{FORMULA}",
                )
            )
        )
        return 0
    branch = f"diffbell-{release.version}"
    if not branch_exists(gh, branch) or read_formula(gh, branch) != wanted:
        write_branch(gh, branch, wanted, f"diffbell {release.version}")
    out(open_pr(gh, release, branch))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="the released version, X.Y.Z or vX.Y.Z")
    parser.add_argument("--dry-run", action="store_true", help="print the formula diff only")
    args = parser.parse_args(argv)
    try:
        return bump(real_gh, args.version, args.dry_run)
    except BumpError as exc:
        print(f"bump_formula: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
