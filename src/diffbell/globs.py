"""Glob matching shared by the local matcher and config validation.

``glob_to_regex`` defines the semantics; ``glob_matches`` implements them in
linear time (an NFA simulation, no backtracking), because a pattern such as
``"*a" * 12 + "b"`` makes the regex backtrack for hours on a 40-character name.
``config.check_pattern`` still compiles the regex to validate classes and ranges.

Kept free of imports from ``config`` and ``patterns`` so that ``config`` can
validate a pattern by compiling it exactly as the matcher will.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache


def glob_to_regex(pattern: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")  # zero or more whole directories
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[" and (end := pattern.find("]", i + 1)) != -1:
            body = pattern[i + 1 : end]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            i = end + 1
            continue
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


@lru_cache(maxsize=1024)
def compile_glob(pattern: str) -> re.Pattern[str]:
    # IGNORECASE: Watchman's match is case-insensitive on APFS, and redaction must
    # cover at least everything Watchman watches. On a case-sensitive volume this
    # over-redacts, which is the safe direction. DOTALL: file names may contain
    # newlines, and "**" must span them as Watchman does ("*", "?" and classes
    # are "[^/]"-style and already match a newline; only "/" is excluded).
    return re.compile(glob_to_regex(pattern), re.IGNORECASE | re.DOTALL)


_FLAGS = re.IGNORECASE | re.DOTALL


class _Kind(Enum):
    ONE = 1  # one character satisfying ``atom``
    STAR = 2  # "[^/]*"
    GLOBSTAR = 3  # ".*"
    GLOBSTAR_SLASH = 4  # "(?:.*/)?"


@dataclass(frozen=True, slots=True)
class _Token:
    kind: _Kind
    # For ONE: the single-character regex, compiled with the same flags as
    # compile_glob, so case folding is exactly re.IGNORECASE's (never hand-rolled).
    atom: re.Pattern[str] | None = None


_NOT_SLASH = re.compile("[^/]", _FLAGS)


@lru_cache(maxsize=1024)
def _tokenize(pattern: str) -> tuple[_Token, ...] | None:
    """The pattern as tokens, mirroring ``glob_to_regex`` step by step.

    None if the regex is not a plain sequence of the atoms: an empty class body
    ("[]", "[!]", "[^]") makes re read on past the "]" into the following atoms.
    ``check_pattern`` rejects those; ``glob_matches`` falls back to the regex for
    them so its result stays identical. Raises re.error where compile_glob would.
    """
    compile_glob(pattern)  # same errors and warnings as before, in the same cases
    tokens: list[_Token] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            tokens.append(_Token(_Kind.GLOBSTAR_SLASH))
            i += 3
            continue
        if pattern.startswith("**", i):
            tokens.append(_Token(_Kind.GLOBSTAR))
            i += 2
            continue
        if c == "*":
            tokens.append(_Token(_Kind.STAR))
        elif c == "?":
            tokens.append(_Token(_Kind.ONE, _NOT_SLASH))
        elif c == "[" and (end := pattern.find("]", i + 1)) != -1:
            body = pattern[i + 1 : end]
            if body in ("", "!", "^"):
                return None
            if body.startswith("!"):
                body = "^" + body[1:]
            atom = "[" + body.replace("\\", "\\\\") + "]"
            tokens.append(_Token(_Kind.ONE, re.compile(atom, _FLAGS)))
            i = end + 1
            continue
        else:
            tokens.append(_Token(_Kind.ONE, re.compile(re.escape(c), _FLAGS)))
        i += 1
    return tuple(tokens)


def glob_matches(pattern: str, text: str) -> bool:
    """``compile_glob(pattern).fullmatch(text) is not None``, in O(len(text) * len(pattern)).

    States: index i means "before token i" (len(tokens) accepts); ~i means "inside
    the '.*' of the GLOBSTAR_SLASH at i", which leaves to i + 1 on a "/".
    Raises re.error for a pattern compile_glob can't compile.
    """
    tokens = _tokenize(pattern)
    if tokens is None:
        return compile_glob(pattern).fullmatch(text) is not None
    end = len(tokens)

    def closure(states: set[int]) -> set[int]:
        # Every optional token can be skipped; following the chain in index order
        # visits each state once.
        for i in range(end):
            if i in states and tokens[i].kind is not _Kind.ONE:
                states.add(i + 1)
        return states

    states = closure({0})
    for ch in text:
        nxt: set[int] = set()
        for state in states:
            if state < 0:  # inside a GLOBSTAR_SLASH
                i = ~state
                nxt.add(state)
                if ch == "/":
                    nxt.add(i + 1)
                continue
            if state == end:
                continue
            token = tokens[state]
            if token.kind is _Kind.ONE:
                assert token.atom is not None
                if token.atom.fullmatch(ch):
                    nxt.add(state + 1)
            elif token.kind is _Kind.STAR:
                if ch != "/":
                    nxt.add(state)
            elif token.kind is _Kind.GLOBSTAR:
                nxt.add(state)
            else:  # GLOBSTAR_SLASH: start its ".*", or end it right here on a "/"
                nxt.add(~state)
                if ch == "/":
                    nxt.add(state + 1)
        if not nxt:
            return False
        states = closure(nxt)
    return end in states
