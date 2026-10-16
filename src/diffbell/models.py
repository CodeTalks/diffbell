"""Plain data types shared across the pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ChangeKind(StrEnum):
    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"


@dataclass(frozen=True, slots=True)
class Change:
    """One journaled change to one key file. ``id`` is the journal (``changes``) row id."""

    id: int
    project: str
    path: str
    kind: ChangeKind


def collapse(changes: list[Change]) -> tuple[list[Change], list[int]]:
    """Reduce the changes for each path to their net effect (§5 step 7).

    The net kind depends only on whether the file existed before the first change
    and after the last one. Returns ``(kept, superseded_ids)``: a kept change reuses
    the id of the last change for its path, and every other id is superseded and
    must be deleted from the journal.
    """
    by_path: dict[str, list[Change]] = {}
    for change in changes:
        by_path.setdefault(change.path, []).append(change)

    kept: list[Change] = []
    superseded: list[int] = []
    for path, seq in by_path.items():
        existed_before = seq[0].kind is not ChangeKind.CREATED
        exists_after = seq[-1].kind is not ChangeKind.DELETED
        last = seq[-1]
        superseded.extend(c.id for c in seq[:-1])
        if existed_before and exists_after:
            kept.append(Change(last.id, last.project, path, ChangeKind.MODIFIED))
        elif existed_before:
            kept.append(Change(last.id, last.project, path, ChangeKind.DELETED))
        elif exists_after:
            kept.append(Change(last.id, last.project, path, ChangeKind.CREATED))
        else:
            superseded.append(last.id)
    return kept, superseded
