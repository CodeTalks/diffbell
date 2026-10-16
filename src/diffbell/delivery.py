"""Shared classification and backoff of a failed Telegram send (health spec §4.3).

Both the notifier (events) and health (alerts) import this module; neither imports
the other. It only classifies: each caller applies its own policy. Events treat
AUTH and BAD_REQUEST as terminal (BAD_REQUEST with one plain-text retry in between);
alerts retry every kind on the backoff, because an alert that is never resent is
worse than a lost event, and alert texts are plain, so a 400 should not recur.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from diffbell.telegram import ErrorKind, TelegramError


def backoff(attempts: int) -> float:
    """Seconds before the next retry after ``attempts`` failed attempts (30 s … 30 min)."""
    return float(min(30 * 2**attempts, 1800))


@dataclass(frozen=True, slots=True)
class Failure:
    """What a failed send means: its classification and the wait before a retry."""

    kind: ErrorKind
    delay: float  # seconds


def classify(exc: TelegramError, attempts: int) -> Failure:
    """A 429's ``retry_after`` wins; otherwise ``backoff(attempts)``, where
    ``attempts`` counts the failed attempts before this one. A ``retry_after`` that
    is not a finite, non-negative number is ignored."""
    after = exc.retry_after
    if (
        exc.kind is ErrorKind.RETRYABLE
        and after is not None
        and math.isfinite(after)
        and after >= 0
    ):
        return Failure(exc.kind, after)
    return Failure(exc.kind, backoff(attempts))
