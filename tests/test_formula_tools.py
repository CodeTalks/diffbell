"""`scripts/formula_resources.py` and `scripts/bump_formula.py` (ADR 0021): the
formula's resources come from `uv.lock`, and a release becomes one tap PR per
version, built only from what the release published."""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[1]
SHA_OLD = "a" * 64
SHA_NEW = "b" * 64


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up while defining
    spec.loader.exec_module(module)
    return module


resources = load("formula_resources")
bumper = load("bump_formula")


def lock(*packages: str) -> str:
    return "version = 1\n\n" + "\n".join(packages)


def package(
    name: str, version: str = "1.0", deps: str = "", extra: str = "", sdist: bool = True
) -> str:
    text = f'[[package]]\nname = "{name}"\nversion = "{version}"\n'
    if deps:
        text += f"dependencies = [{deps}]\n"
    if sdist:
        text += f'sdist = {{ url = "https://files/{name}-{version}.tar.gz", hash = "sha256:{name[0] * 64}" }}\n'
    return text + extra


PROJECT = '[[package]]\nname = "diffbell"\nversion = "0.1.2"\nsource = { editable = "." }\n'


# -- formula_resources ---------------------------------------------------------------


def test_the_closure_follows_markers_for_the_formulas_python() -> None:
    text = lock(
        PROJECT
        + 'dependencies = [{ name = "anyio" }, { name = "pywin", marker = "sys_platform == \'win32\'" }]\n',
        package(
            "anyio",
            deps='{ name = "idna" }, { name = "exceptiongroup", marker = "python_full_version < \'3.11\'" }',
        ),
        package("idna"),
        package("exceptiongroup"),
        package("pywin"),
    )
    names = [p["name"] for p in resources.runtime_packages(resources.tomllib.loads(text), "3.15")]
    assert names == ["anyio", "idna"]


def test_homebrew_provided_packages_and_extras() -> None:
    text = lock(
        PROJECT
        + 'dependencies = [{ name = "pydantic" }, { name = "uvicorn", extra = ["standard"] }]\n',
        package("pydantic", deps='{ name = "typing-extensions" }'),
        package("typing-extensions"),
        package(
            "uvicorn", extra='[package.optional-dependencies]\nstandard = [{ name = "h11" }]\n'
        ),
        package("h11"),
    )
    names = [p["name"] for p in resources.runtime_packages(resources.tomllib.loads(text), "3.15")]
    assert names == ["h11", "uvicorn"]  # pydantic and its dependency come from Homebrew


def test_a_block_names_the_locked_sdist() -> None:
    text = lock(PROJECT + 'dependencies = [{ name = "click" }]\n', package("click", "8.5.0"))
    assert resources.render(text, "3.15") == (
        '  resource "click" do\n'
        '    url "https://files/click-8.5.0.tar.gz"\n'
        f'    sha256 "{"c" * 64}"\n'
        "  end\n"
    )


def test_a_package_without_an_sdist_is_refused() -> None:
    text = lock(
        PROJECT + 'dependencies = [{ name = "wheelonly" }]\n', package("wheelonly", sdist=False)
    )
    with pytest.raises(resources.LockError, match=re.escape("wheelonly 1.0 has no sdist")):
        resources.render(text, "3.15")


def test_this_repositorys_lock_renders() -> None:
    blocks = resources.render((ROOT / "uv.lock").read_text(encoding="utf-8"), "3.15")
    names = [line.split('"')[1] for line in blocks.splitlines() if line.startswith("  resource ")]
    assert "pywatchman" in names and "fastapi" in names
    assert not set(names) & resources.HOMEBREW_PROVIDED


# -- bump_formula ----------------------------------------------------------------------

FORMULA = f"""class Diffbell < Formula
  include Language::Python::Virtualenv

  desc "Watch key files"
  homepage "https://github.com/CodeTalks/diffbell"
  url "https://github.com/CodeTalks/diffbell/releases/download/v0.1.1/diffbell-0.1.1.tar.gz"
  sha256 "{SHA_OLD}"
  license "MIT"

  depends_on "python@3.15"

  pypi_packages exclude_packages: ["certifi", "pydantic"]

  resource "click" do
    url "https://files/click-8.0.tar.gz"
    sha256 "{"c" * 64}"
  end

  def install
    virtualenv_install_with_resources
  end
end
"""
NEW_LOCK = lock(PROJECT + 'dependencies = [{ name = "click" }]\n', package("click", "8.5.0"))
SUMS = f"{SHA_NEW}  diffbell-0.1.2.tar.gz\n{'d' * 64}  diffbell-0.1.2-py3-none-any.whl\n"


class FakeTap:
    """A small model of the tap and the release, behind the `gh` calls the script makes."""

    def __init__(self, main: str = FORMULA) -> None:
        self.files = {"main": main}
        self.prs: list[dict[str, str]] = []
        self.writes: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        joined = " ".join(args)
        if args[:2] == ["release", "download"]:
            return SUMS
        if "contents/uv.lock?ref=v0.1.2" in joined:
            return NEW_LOCK
        if "contents/Formula/diffbell.rb?ref=" in joined and "--jq" not in args:
            return self.files[joined.split("?ref=")[1].split()[0]]
        if joined.endswith("branches --paginate --jq .[].name"):
            return "\n".join(self.files) + "\n"
        if "git/ref/heads/main" in joined:
            return "mainsha\n"
        if "-X POST" in joined and "git/refs" in joined:
            self.writes.append(args)
            branch = next(a for a in args if a.startswith("ref=refs/heads/")).removeprefix(
                "ref=refs/heads/"
            )
            self.files[branch] = self.files["main"]
            return "{}"
        if "--jq .sha" in joined:
            return "blobsha\n"
        if "-X PUT" in joined:
            self.writes.append(args)
            fields = dict(a.split("=", 1) for a in args if "=" in a and not a.startswith("repos/"))
            self.files[fields["branch"]] = base64.b64decode(fields["content"]).decode("utf-8")
            return "{}"
        if args[:2] == ["pr", "list"]:
            head = args[args.index("--head") + 1]
            return json.dumps([{"url": p["url"]} for p in self.prs if p["head"] == head])
        if args[:2] == ["pr", "create"]:
            head = args[args.index("--head") + 1]
            url = f"https://github.com/CodeTalks/homebrew-tap/pull/{len(self.prs) + 1}"
            self.prs.append({"head": head, "url": url, "body": args[args.index("--body") + 1]})
            return url + "\n"
        raise AssertionError(f"unexpected gh call: {joined}")


def test_update_formula_replaces_url_sha_and_resources_only() -> None:
    new = bumper.update_formula(
        FORMULA, "https://x/diffbell-0.1.2.tar.gz", SHA_NEW, '  resource "a" do\n  end\n'
    )
    assert '  url "https://x/diffbell-0.1.2.tar.gz"\n  sha256 "' + SHA_NEW + '"' in new
    assert 'resource "click"' not in new and '  resource "a" do\n  end\n\n  def install\n' in new
    assert new.startswith("class Diffbell < Formula\n") and new.endswith("  end\nend\n")
    assert 'depends_on "python@3.15"' in new


def test_sdist_sha256_reads_the_release_sums() -> None:
    assert bumper.sdist_sha256(SUMS, "0.1.2") == SHA_NEW
    with pytest.raises(bumper.BumpError, match=re.escape("no line for diffbell-0.1.3.tar.gz")):
        bumper.sdist_sha256(SUMS, "0.1.3")


def test_a_dry_run_prints_the_diff_and_writes_nothing() -> None:
    tap, printed = FakeTap(), list[str]()
    assert bumper.bump(tap, "v0.1.2", dry_run=True, out=printed.append) == 0
    diff = printed[0]
    assert (
        f'+  sha256 "{SHA_NEW}"' in diff and '+    url "https://files/click-8.5.0.tar.gz"' in diff
    )
    assert "releases/download/v0.1.2/diffbell-0.1.2.tar.gz" in diff
    assert tap.writes == [] and tap.prs == []


def test_a_bump_pushes_one_branch_and_opens_one_pr() -> None:
    tap, printed = FakeTap(), list[str]()
    assert bumper.bump(tap, "0.1.2", dry_run=False, out=printed.append) == 0
    assert printed == ["https://github.com/CodeTalks/homebrew-tap/pull/1"]
    assert "releases/download/v0.1.2/diffbell-0.1.2.tar.gz" in tap.files["diffbell-0.1.2"]
    assert tap.files["main"] == FORMULA  # main changes only when the PR is merged
    assert "releases/tag/v0.1.2" in tap.prs[0]["body"]
    # Run again: the same PR, and no second write of an unchanged branch.
    writes = len(tap.writes)
    assert bumper.bump(tap, "0.1.2", dry_run=False, out=printed.append) == 0
    assert printed[-1] == "https://github.com/CodeTalks/homebrew-tap/pull/1"
    assert len(tap.prs) == 1 and len(tap.writes) == writes


def test_a_formula_already_at_the_release_is_left_alone() -> None:
    tap = FakeTap()
    first = FakeTap()
    bumper.bump(first, "0.1.2", dry_run=False, out=lambda _: None)
    tap.files["main"] = first.files["diffbell-0.1.2"]  # as after merging the PR
    printed: list[str] = []
    assert bumper.bump(tap, "0.1.2", dry_run=False, out=printed.append) == 0
    assert printed == ["The tap's formula is already at diffbell 0.1.2: nothing to do."]
    assert tap.writes == [] and tap.prs == []


def test_a_bad_version_is_refused() -> None:
    with pytest.raises(bumper.BumpError, match=re.escape("not a version: 0.1")):
        bumper.bump(FakeTap(), "0.1", dry_run=True, out=lambda _: None)


def test_an_older_version_is_refused() -> None:
    with pytest.raises(bumper.BumpError, match=re.escape("0.1.0 is older than the tap's formula")):
        bumper.bump(FakeTap(), "v0.1.0", dry_run=True, out=lambda _: None)
