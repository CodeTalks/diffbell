"""This Mac's name in messages (ADR 0023): several Macs can share one bot and chat,
so every message, alert and heartbeat names the Mac that sent it.

`notify.machine` absent (or null): macOS's Computer Name; ``""``: no name; any other
string: that name. The daemon resolves it at start and at every reload; nothing here
raises. Tests never run `scutil`: the conftest belt replaces `computer_name`.
"""

from __future__ import annotations

import socket
import subprocess
import unicodedata
from collections.abc import Callable
from typing import Any

from diffbell import BRAND

MAX_NAME = 40  # characters of a name in messages (config.NotifyConfig rejects longer)
SCUTIL = "/usr/sbin/scutil"
SCUTIL_TIMEOUT = 2

Runner = Callable[..., "subprocess.CompletedProcess[Any]"]


def computer_name(run: Runner = subprocess.run) -> str | None:
    """macOS's Computer Name (`scutil --get ComputerName`), else the host name without
    a trailing ``.local``; None if neither is there. Never raises."""
    try:
        result = run(
            [SCUTIL, "--get", "ComputerName"],
            capture_output=True,
            text=True,
            timeout=SCUTIL_TIMEOUT,
            check=False,
        )
        if result.returncode == 0 and isinstance(result.stdout, str) and result.stdout.strip():
            return str(result.stdout.strip())
    except Exception:
        pass
    try:
        host = socket.gethostname().strip().removesuffix(".local")
    except Exception:
        return None
    return host or None


def removed(ch: str) -> bool:
    """A control or format character (bidi controls are format characters): never
    shown in a name, and refused in `notify.machine`."""
    return unicodedata.category(ch) in ("Cc", "Cf")


def clean(text: str) -> str:
    """``text`` stripped, without control, format and bidi characters, inner whitespace
    collapsed to one space, cut to MAX_NAME characters (no ellipsis)."""
    kept = "".join(ch for ch in text if not removed(ch))
    return " ".join(kept.split())[:MAX_NAME].strip()


def default_name() -> str | None:
    """The detected Computer Name as messages would show it, or None."""
    detected = computer_name()
    return (clean(detected) or None) if detected is not None else None


def resolve(configured: str | None) -> str | None:
    """The name messages carry: None (not configured) is the Computer Name; ``""``, or
    a text of only removed characters, is no name (None); else the cleaned text."""
    if configured is None:
        return default_name()
    return clean(configured) or None


def signature(name: str | None) -> str:
    """How alerts and the heartbeat sign: "Diffbell", or "Diffbell (Work MBP)"."""
    return BRAND if name is None else f"{BRAND} ({name})"
