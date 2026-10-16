"""Turn Watchman file entries into fingerprints and classified changes (spec §5)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from diffbell.models import ChangeKind

# Watchman returns only the fields requested; mtime_ms/size feed the error fallback.
FIELDS = [
    "name",
    "exists",
    "new",
    "type",
    "content.sha1hex",
    "symlink_target",
    "mtime_ms",
    "size",
]


def fingerprint(entry: Mapping[str, Any]) -> str | None:
    """Content fingerprint, or None for a deletion. The order of checks matters.

    1. exists == false → deletion. Checked first because a delete PDU still carries a
       stale ``type`` and pre-deletion ``mtime_ms``/``size``.
    2. type == "l" → symlink target. Branch on type: regular files also have
       ``symlink_target: null``.
    3. sha1 string → the sha1.
    4. otherwise (error object for an unreadable or oversized file) → mtime/size.
    """
    if not entry.get("exists", True):
        return None
    if entry.get("type") == "l":
        return f"link:{entry.get('symlink_target')}"
    sha = entry.get("content.sha1hex")
    if isinstance(sha, str):
        return sha
    return f"err:{entry.get('mtime_ms')}:{entry.get('size')}"


@dataclass
class Observation:
    """What one PDU means for the store: hash upserts/deletes plus journaled changes."""

    upserts: dict[str, str] = field(default_factory=dict)
    deletes: list[str] = field(default_factory=list)
    changes: list[tuple[str, ChangeKind]] = field(default_factory=list)


def observe(
    stored: Mapping[str, str], files: Sequence[Mapping[str, Any]], *, fresh: bool
) -> Observation:
    """Classify entries against stored fingerprints. On a fresh instance, stored paths
    missing from the full listing were deleted while we weren't looking."""
    obs = Observation()
    seen: set[str] = set()
    for entry in files:
        name = str(entry["name"])
        seen.add(name)
        new_fp = fingerprint(entry)
        old_fp = stored.get(name)
        if new_fp is None:
            if old_fp is not None:
                obs.deletes.append(name)
                obs.changes.append((name, ChangeKind.DELETED))
        elif old_fp is None:
            obs.upserts[name] = new_fp
            obs.changes.append((name, ChangeKind.CREATED))
        elif old_fp != new_fp:
            obs.upserts[name] = new_fp
            obs.changes.append((name, ChangeKind.MODIFIED))
    if fresh:
        for name in sorted(set(stored) - seen):
            obs.deletes.append(name)
            obs.changes.append((name, ChangeKind.DELETED))
    return obs


def baseline(files: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Fingerprints of every existing file, used for a silent re-baseline."""
    result: dict[str, str] = {}
    for entry in files:
        fp = fingerprint(entry)
        if fp is not None:
            result[str(entry["name"])] = fp
    return result
