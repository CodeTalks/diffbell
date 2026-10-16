from __future__ import annotations

from typing import Any

from diffbell.fingerprint import FIELDS, baseline, fingerprint, observe
from diffbell.models import ChangeKind

SHA = "6fcf9dfbd479ed82697fee719b9f8c610a11ff2a"


def entry(name: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": name,
        "exists": True,
        "new": False,
        "type": "f",
        "content.sha1hex": SHA,
        "symlink_target": None,
        "mtime_ms": 1,
        "size": 2,
    }
    base.update(kw)
    return base


def test_fields_include_fallback_inputs() -> None:
    assert {"mtime_ms", "size", "symlink_target", "content.sha1hex", "exists"} <= set(FIELDS)


def test_delete_pdu_has_no_fingerprint_despite_stale_fields() -> None:
    # Verified shape of a real delete PDU: null sha, stale type "f", old mtime/size.
    assert fingerprint(entry("a", exists=False, **{"content.sha1hex": None})) is None


def test_symlink_uses_target_even_without_sha() -> None:
    e = entry("l", type="l", symlink_target=".env.local", **{"content.sha1hex": None})
    assert fingerprint(e) == "link:.env.local"


def test_regular_file_uses_sha() -> None:
    assert fingerprint(entry("a")) == SHA


def test_error_object_falls_back_to_mtime_and_size() -> None:
    e = entry("a", mtime_ms=99, size=7, **{"content.sha1hex": {"error": "too big"}})
    assert fingerprint(e) == "err:99:7"


def test_observe_created_modified_noop() -> None:
    stored = {"same": SHA, "changed": "old"}
    obs = observe(
        stored,
        [entry("new"), entry("same"), entry("changed")],
        fresh=False,
    )
    assert obs.changes == [("new", ChangeKind.CREATED), ("changed", ChangeKind.MODIFIED)]
    assert obs.upserts == {"new": SHA, "changed": SHA}
    assert obs.deletes == []


def test_observe_ignores_new_flag() -> None:
    obs = observe({"a": SHA}, [entry("a", new=True)], fresh=True)
    assert obs.changes == []


def test_observe_delete_known_and_unknown() -> None:
    gone = {"content.sha1hex": None}
    obs = observe(
        {"known": SHA},
        [entry("known", exists=False, **gone), entry("unknown", exists=False, **gone)],
        fresh=False,
    )
    assert obs.changes == [("known", ChangeKind.DELETED)]
    assert obs.deletes == ["known"]


def test_fresh_instance_reports_missing_as_deleted() -> None:
    obs = observe({"a": SHA, "b": SHA}, [entry("a")], fresh=True)
    assert obs.changes == [("b", ChangeKind.DELETED)]
    assert obs.deletes == ["b"]


def test_delete_then_recreate_is_created_not_modified() -> None:
    stored = {"a": SHA}
    first = observe(stored, [entry("a", exists=False, **{"content.sha1hex": None})], fresh=False)
    for name in first.deletes:
        stored.pop(name)
    second = observe(stored, [entry("a")], fresh=False)
    assert first.changes == [("a", ChangeKind.DELETED)]
    assert second.changes == [("a", ChangeKind.CREATED)]


def test_baseline_skips_deleted_entries() -> None:
    files = [entry("a"), entry("b", exists=False, **{"content.sha1hex": None})]
    assert baseline(files) == {"a": SHA}
