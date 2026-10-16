"""``install-source.json``: where the installed tool was built from (Plan 6 spec §4).

Written by ``diffbell upgrade`` after a successful upgrade, read by ``upgrade``
(the default ``--from``) and by ``doctor`` (its Install check). A 0600 file in the
state directory: ``path``, ``commit``, ``branch``, ``installed_at``.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

SOURCE_FILE = "install-source.json"


@dataclass(frozen=True, slots=True)
class Source:
    path: str
    commit: str
    branch: str
    installed_at: float


def read_source(state_dir: Path) -> Source | None:
    """The recorded source, or None (absent, unreadable or malformed)."""
    try:
        data = json.loads((state_dir / SOURCE_FILE).read_text(encoding="utf-8"))
        source = Source(
            path=data["path"],
            commit=data["commit"],
            branch=data["branch"],
            installed_at=float(data["installed_at"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None
    fields = (source.path, source.commit, source.branch)
    return source if all(isinstance(v, str) and v for v in fields) else None


def write_source(state_dir: Path, source: Source) -> None:
    """Atomically replace the file, owner-only (0600) from the first byte."""
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = state_dir / f"{SOURCE_FILE}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)  # a leftover tmp may be wider: narrow before writing
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(source), indent=2))
    os.replace(tmp, state_dir / SOURCE_FILE)
