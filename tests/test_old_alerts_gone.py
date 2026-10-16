"""The hourly-repeating alert machinery is deleted, not bypassed (health spec §4.3):
no path can send a problem twice."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import diffbell
from diffbell import store

SRC = Path(diffbell.__file__).parent
GONE = (
    "Alerter",
    "alert_due",
    "record_alert",
    "_pending_alerts",
    "_queue_alert",
    "_flush_alerts",
    "_down_since",
    "_down_alerted",
    "_alert",
    "_check_down",
    "DOWN_ALERT_AFTER",
)


def test_alerts_py_is_gone() -> None:
    assert not (SRC / "alerts.py").exists()
    assert not list(SRC.rglob("alerts.py"))


@pytest.mark.parametrize("name", GONE)
def test_no_source_file_mentions(name: str) -> None:
    word = re.compile(rf"\b{re.escape(name)}\b")
    hits = [
        str(path.relative_to(SRC))
        for path in sorted(SRC.rglob("*.py"))
        if word.search(path.read_text(encoding="utf-8"))
    ]
    assert hits == [], f"{name} is still in {hits}"


def test_nothing_imports_the_old_module() -> None:
    pattern = re.compile(
        r"^\s*(from|import)\s+diffbell\.alerts\b|from diffbell import alerts\b", re.M
    )
    assert [
        p.name for p in SRC.rglob("*.py") if pattern.search(p.read_text(encoding="utf-8"))
    ] == []


def test_the_schema_has_no_alerts_table() -> None:
    assert re.search(r"CREATE TABLE (IF NOT EXISTS )?alerts\b", store.SCHEMA) is None
