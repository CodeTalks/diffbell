"""Glob semantics shared by Watchman expressions and local redaction checks (spec §4).

A pattern without "/" matches the basename at any depth; a pattern with "/" is
anchored at the project root, and "**" spans directories. Dotfiles always match.
For redaction only, a pattern without "/" also matches any directory component
(``*secret*`` redacts ``config/secrets/db.yaml``).
Matching is case-insensitive (Watchman's match is, on case-insensitive filesystems)
and normalization-insensitive (APFS stores names in the form they were created
with, NFC or NFD, while a user types patterns in NFC).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from collections.abc import Iterable
from typing import Any

from diffbell.config import ALWAYS_EXCLUDED_DIRS, ResolvedProject
from diffbell.globs import glob_matches

Expr = list[Any]

log = logging.getLogger(__name__)


def glob_term(pattern: str) -> Expr:
    scope = "wholename" if "/" in pattern else "basename"
    return ["match", pattern, scope, {"includedotfiles": True}]


def excluded_dir_term(directory: str) -> Expr:
    # Not ["dirname", d]: dirname compares the full dirname, so it only matches
    # a root-level directory (verified 2026-10-08).
    return ["match", f"**/{directory}/**", "wholename", {"includedotfiles": True}]


def build_expression(project: ResolvedProject) -> Expr:
    exclusions = [glob_term(p) for p in project.excluded]
    exclusions += [excluded_dir_term(d) for d in ALWAYS_EXCLUDED_DIRS]
    return [
        "allof",
        ["anyof", ["type", "f"], ["type", "l"]],
        ["anyof", *[glob_term(p) for p in project.watched]],
        ["not", ["anyof", *exclusions]],
    ]


def expression_hash(expr: Expr) -> str:
    canonical = json.dumps(expr, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _matches_exact(path: str, pattern: str) -> bool:
    target = path if "/" in pattern else path.rsplit("/", 1)[-1]
    try:
        return glob_matches(pattern, target)
    except re.error as exc:
        # Fail closed: an unusable pattern is treated as a match (redacted) rather
        # than raising and wedging every notification.
        log.warning("cannot compile pattern %r (%s); treating as matched", pattern, exc)
        return True


def matches(path: str, pattern: str) -> bool:
    """Local equivalent of the Watchman term built by ``glob_term``.

    True if the pattern matches with both sides in NFC or both in NFD, so an
    NFC-typed redact pattern still covers a file stored in NFD (fail closed).
    Note: NFD character classes (e.g. "[à-é]") may over-match, which is the safe
    direction for redaction.
    """
    return any(
        _matches_exact(unicodedata.normalize(form, path), unicodedata.normalize(form, pattern))
        for form in ("NFC", "NFD")
    )


def is_redacted(path: str, patterns: Iterable[str]) -> bool:
    """True if any pattern matches the path (as ``matches`` does) or, for a pattern
    without "/", any of its directory components.

    Watch matching (``matches`` / ``glob_term``) keeps basename semantics; only
    redaction widens, because over-redaction is the safe direction: a file inside
    ``secrets/`` or ``credentials/`` is likely secret whatever its own name is.
    """
    patterns = tuple(patterns)
    if any(matches(path, pattern) for pattern in patterns):
        return True
    directories = path.split("/")[:-1]
    return any(
        matches(directory, pattern)
        for pattern in patterns
        if "/" not in pattern
        for directory in directories
        if directory
    )
