"""The linear-time glob matcher against the regex translation it replaced.

``glob_to_regex`` (applied with IGNORECASE | DOTALL and ``fullmatch``) is the
reference oracle: redaction and preview parity depend on identical semantics.
"""

from __future__ import annotations

import os
import random
import re
import time
import warnings

import pytest

from diffbell.globs import glob_matches, glob_to_regex

# Pattern atoms: the task's alphabet, plus a lone "[" / "]" so that merged or
# unterminated classes (which check_pattern rejects) keep their error parity.
PATTERN_ATOMS = (
    "a", "b", "/", ".", "*", "**", "**/", "?", "[ab]", "[!a]", "A", "é", "\n", "[", "]",
)  # fmt: skip
PATH_CHARS = ("a", "b", "/", ".", "*", "?", "[", "]", "!", "A", "B", "é", "É", "\n")

FUZZ_CASES = int(os.environ.get("DIFFBELL_GLOB_FUZZ_CASES", "20000"))


def oracle(pattern: str, path: str) -> bool:
    regex = glob_to_regex(pattern)
    return re.fullmatch(regex, path, re.IGNORECASE | re.DOTALL) is not None


def outcome(fn: object, pattern: str, path: str) -> bool | str:
    assert callable(fn)
    try:
        return bool(fn(pattern, path))
    except re.error:
        return "re.error"


def test_equivalence_fuzz_against_the_regex_oracle() -> None:
    rng = random.Random(20261009)
    mismatches: list[tuple[str, str, bool | str, bool | str]] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # "[[" carries a FutureWarning in re
        for _ in range(FUZZ_CASES):
            pattern = "".join(rng.choices(PATTERN_ATOMS, k=rng.randint(0, 7)))
            path = "".join(rng.choices(PATH_CHARS, k=rng.randint(0, 10)))
            if rng.random() < 0.3:
                # Bias towards matches: derive the path from the pattern's text.
                path = pattern.replace("**/", "a/b/").replace("[!a]", "b").replace("[ab]", "A")
            expected = outcome(oracle, pattern, path)
            actual = outcome(glob_matches, pattern, path)
            if expected != actual:
                mismatches.append((pattern, path, expected, actual))
    assert mismatches == []


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("**/", "", True),
        ("**/", "a/", True),
        ("**/", "a", False),
        ("**/x", "x", True),
        ("**/x", "a/b/x", True),
        ("**/x", "ax", False),
        ("a**/x", "ax", True),  # "**/" after a literal still is "(?:.*/)?"
        ("**", "a/b\nc", True),
        ("*", "a\nb", True),
        ("*", "a/b", False),
        ("?", "/", False),
        ("[!a]", "/", True),  # a negated class matches "/" exactly like the regex
        ("[ab]", "B", True),
        ("É", "é", True),
        ("k", "\u212a", True),  # KELVIN SIGN folds to "k" under re.IGNORECASE
        ("s", "\u017f", True),  # LATIN SMALL LETTER LONG S
        ("[z-a]", "a", "re.error"),
        ("[]", "x", "re.error"),
        ("[]?", "/", True),  # merged class "[][^/]", rejected by check_pattern
        ("[]?", "a", False),
    ],
)
def test_edge_cases_match_the_oracle(pattern: str, path: str, expected: bool | str) -> None:
    assert outcome(oracle, pattern, path) == expected
    assert outcome(glob_matches, pattern, path) == expected


@pytest.mark.parametrize(
    ("pattern", "path"),
    [
        ("*a" * 12 + "b", "a" * 40),
        ("**/" * 10 + "*a" * 10 + "b", "a/" * 20 + "a" * 40),
        ("**" * 10 + "a" * 5 + "b", "a" * 200),
    ],
)
def test_pathological_patterns_run_in_linear_time(pattern: str, path: str) -> None:
    glob_matches(pattern, "warm the cache")
    start = time.perf_counter()
    assert glob_matches(pattern, path) is False
    assert time.perf_counter() - start < 0.05
