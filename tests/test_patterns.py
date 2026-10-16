from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import pytest

from diffbell.config import BUILTIN_REDACT, ResolvedProject
from diffbell.patterns import (
    build_expression,
    expression_hash,
    glob_term,
    is_redacted,
    matches,
)


def project(watched: tuple[str, ...], excluded: tuple[str, ...] = ()) -> ResolvedProject:
    return ResolvedProject("p", Path("/x"), watched, excluded)


def test_glob_term_scope_depends_on_slash() -> None:
    assert glob_term("*.lock") == ["match", "*.lock", "basename", {"includedotfiles": True}]
    assert glob_term(".github/workflows/**")[2] == "wholename"


def test_expression_never_uses_dirname_and_excludes_at_any_depth() -> None:
    expr = build_expression(project(("package.json",)))
    text = json.dumps(expr)
    assert '"dirname"' not in text
    assert '"**/node_modules/**"' in text
    assert '"**/.worktrees/**"' in text


def test_expression_shape() -> None:
    expr = build_expression(project(("a.json", "x/**"), ("x/skip/**",)))
    assert expr[0] == "allof"
    assert expr[1] == ["anyof", ["type", "f"], ["type", "l"]]
    assert expr[2][0] == "anyof" and len(expr[2]) == 3
    assert expr[3][0] == "not"


def test_expression_hash_is_stable_and_sensitive() -> None:
    a = build_expression(project(("a",)))
    b = build_expression(project(("b",)))
    assert expression_hash(a) == expression_hash(build_expression(project(("a",))))
    assert expression_hash(a) != expression_hash(b)


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("packages/gw/.env.local", ".env*", True),
        ("x/uv.lock", "*.lock", True),
        ("package.json", "package.json", True),
        ("a/b/package.json", "package.json", True),
        (".github/workflows/ci.yml", ".github/workflows/**", True),
        (".github/workflows/sub/x.yml", ".github/workflows/**", True),
        ("x/.github/workflows/ci.yml", ".github/workflows/**", False),
        ("node_modules/k.js", "**/node_modules/**", True),
        ("a/b/node_modules/k.js", "**/node_modules/**", True),
        ("src/x/y.ts", "src/*/y.ts", True),
        ("src/x/z/y.ts", "src/*/y.ts", False),
        ("docker-compose.dev.yml", "docker-compose*.yml", True),
        ("Makefile.bak", "Makefile", False),
        (".Env.Local", ".env*", True),
        ("certs/Server.PEM", "*.pem", True),
        (".GitHub/Workflows/ci.yml", ".github/workflows/**", True),
    ],
)
def test_matches(path: str, pattern: str, expected: bool) -> None:
    assert matches(path, pattern) is expected


def test_builtin_redaction_and_documented_over_redaction() -> None:
    assert is_redacted("packages/gw/.env.local", BUILTIN_REDACT)
    assert is_redacted("certs/server.pem", BUILTIN_REDACT)
    assert is_redacted("src/diffbell/secrets.py", BUILTIN_REDACT)  # fail-closed by design
    assert not is_redacted("package.json", BUILTIN_REDACT)


@pytest.mark.parametrize("path", [".ENVRC", "AuthKey_X.P8", "pkg/.Env.Production"])
def test_redaction_is_case_insensitive(path: str) -> None:
    assert is_redacted(path, BUILTIN_REDACT)


@pytest.mark.parametrize(
    ("path_form", "pattern_form"), [("NFD", "NFC"), ("NFC", "NFD"), ("NFD", "NFD")]
)
def test_matching_ignores_unicode_normalization(path_form: str, pattern_form: str) -> None:
    path = unicodedata.normalize(path_form, "cfg/clé.yaml")  # type: ignore[arg-type]
    pattern = unicodedata.normalize(pattern_form, "cfg/clé.yaml")  # type: ignore[arg-type]
    assert matches(path, pattern)
    assert matches(unicodedata.normalize(path_form, "x/.énv"), ".énv")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("config/we\nird/db.yaml", "config/**/db.yaml", True),
        ("config/a/we\nird/db.yaml", "config/**", True),
        ("we\nird.pem", "*.pem", True),
        ("a\nb", "a?b", True),
        ("a/b", "a?b", False),
        ("a/b.pem", "a*.pem", False),
    ],
)
def test_globs_match_newlines_but_never_slash(path: str, pattern: str, expected: bool) -> None:
    assert matches(path, pattern) is expected


def test_uncompilable_pattern_fails_closed(caplog: pytest.LogCaptureFixture) -> None:
    # NFD splits "é" into e + accent, so this range is invalid in that form.
    assert matches("cfg/b.yaml", "cfg/[à-é]*.yaml") is True
    assert is_redacted("cfg/b.yaml", ["cfg/[à-é]*.yaml"]) is True
    assert "cannot compile" in caplog.text


@pytest.mark.parametrize(
    ("path", "pattern"),
    [
        ("config/secrets/db.yaml", "*secret*"),
        ("credentials/aws.json", "*credentials*"),
        ("a/My-Secrets/b/c.txt", "*secret*"),
        ("deploy/.env.d/app.conf", ".env*"),
        ("x/clés.p12/inner.txt", "clés.p12"),  # NFD directory, NFC pattern
    ],
)
def test_basename_redact_pattern_also_matches_a_directory_component(
    path: str, pattern: str
) -> None:
    assert is_redacted(path, [pattern])
    assert not matches(path, pattern)  # watch matching keeps basename semantics


def test_builtin_redaction_covers_secret_directories() -> None:
    assert is_redacted("config/secrets/db.yaml", BUILTIN_REDACT)
    assert is_redacted("credentials/aws.json", BUILTIN_REDACT)
    assert not is_redacted("config/app/db.yaml", BUILTIN_REDACT)


def test_anchored_redact_pattern_is_not_applied_per_component() -> None:
    assert is_redacted("config/prod.yaml", ["config/prod.yaml"])
    assert not is_redacted("x/config/prod.yaml", ["config/prod.yaml"])
    assert not is_redacted("prod.yaml/x.txt", ["config/prod.yaml"])
