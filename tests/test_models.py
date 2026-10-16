from __future__ import annotations

from diffbell.models import Change, ChangeKind, collapse

C, M, D = ChangeKind.CREATED, ChangeKind.MODIFIED, ChangeKind.DELETED


def ch(id_: int, path: str, kind: ChangeKind) -> Change:
    return Change(id=id_, project="p", path=path, kind=kind)


def test_single_change_is_kept_as_is() -> None:
    kept, superseded = collapse([ch(1, "a", M)])
    assert kept == [ch(1, "a", M)]
    assert superseded == []


def test_created_then_modified_is_created_with_last_id() -> None:
    kept, superseded = collapse([ch(1, "a", C), ch(2, "a", M)])
    assert kept == [ch(2, "a", C)]
    assert superseded == [1]


def test_created_then_deleted_is_dropped_entirely() -> None:
    kept, superseded = collapse([ch(1, "a", C), ch(2, "a", D)])
    assert kept == []
    assert sorted(superseded) == [1, 2]


def test_deleted_then_created_is_modified() -> None:
    kept, _ = collapse([ch(1, "a", D), ch(2, "a", C)])
    assert kept == [ch(2, "a", M)]


def test_modified_then_deleted_is_deleted() -> None:
    kept, _ = collapse([ch(1, "a", M), ch(2, "a", D)])
    assert kept == [ch(2, "a", D)]


def test_paths_are_independent_and_keep_first_seen_order() -> None:
    kept, superseded = collapse([ch(1, "b", M), ch(2, "a", C), ch(3, "b", M)])
    assert [c.path for c in kept] == ["b", "a"]
    assert superseded == [1]
