from __future__ import annotations

import os
import subprocess
import threading
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from diffbell import diffing
from diffbell.config import BUILTIN_REDACT
from diffbell.diffing import build_reports, current_branch
from diffbell.models import Change, ChangeKind
from tests.helpers import (
    HAS_GIT,
    HOSTILE_AFTER,
    HOSTILE_BEFORE,
    HOSTILE_CHANGED,
    HOSTILE_LINES,
    git,
    init_repo,
)

pytestmark = pytest.mark.skipif(not HAS_GIT, reason="git not installed")


def ch(path: str, kind: ChangeKind) -> Change:
    return Change(1, "p", path, kind)


@pytest.fixture
def repo(tmp_project: Path) -> Path:
    init_repo(tmp_project)
    (tmp_project / "package.json").write_text('{\n  "hono": "^4.6.0"\n}\n')
    (tmp_project / "schema.sql").write_text("-- keep me\nSELECT 1;\n")
    git(tmp_project, "add", ".")
    git(tmp_project, "commit", "-q", "-m", "init")
    return tmp_project


def test_tracked_modified_shows_hunk_lines_vs_head(repo: Path) -> None:
    (repo / "package.json").write_text('{\n  "hono": "^4.7.2"\n}\n')
    (report,) = build_reports(repo, [ch("package.json", ChangeKind.MODIFIED)], BUILTIN_REDACT)
    assert report.note == "vs HEAD"
    assert report.lines == ('-  "hono": "^4.6.0"', '+  "hono": "^4.7.2"')


def test_removed_line_starting_with_dashes_is_not_dropped(repo: Path) -> None:
    (repo / "schema.sql").write_text("SELECT 1;\n")
    (report,) = build_reports(repo, [ch("schema.sql", ChangeKind.MODIFIED)], BUILTIN_REDACT)
    assert "--- keep me" in report.lines


def test_untracked_created_shows_head_of_file(repo: Path) -> None:
    (repo / "migrations").mkdir()
    (repo / "migrations" / "0042.sql").write_text("CREATE INDEX i ON t(c);\n")
    (report,) = build_reports(repo, [ch("migrations/0042.sql", ChangeKind.CREATED)], BUILTIN_REDACT)
    assert report.lines == ("+CREATE INDEX i ON t(c);",)
    assert report.note == "new file"


def test_binary_created_file(repo: Path) -> None:
    (repo / "logo.bin").write_bytes(b"\x89PNG\x00\x01")
    (report,) = build_reports(repo, [ch("logo.bin", ChangeKind.CREATED)], ())
    assert report.lines == ()
    assert report.note == "binary file"


def test_deleted_has_no_lines(repo: Path) -> None:
    (repo / "package.json").unlink()
    (report,) = build_reports(repo, [ch("package.json", ChangeKind.DELETED)], BUILTIN_REDACT)
    assert (report.lines, report.note, report.redacted) == ((), None, False)


def test_non_git_project_is_metadata_only(tmp_project: Path) -> None:
    (tmp_project / "package.json").write_text("{}")
    (report,) = build_reports(tmp_project, [ch("package.json", ChangeKind.CREATED)], ())
    assert (report.lines, report.note) == ((), None)


def test_redacted_file_is_never_read_or_diffed(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / ".env.local").write_text("API_KEY=SECRET-VALUE\n")
    seen: list[list[str]] = []
    real_run = subprocess.run

    def spy(args: list[str], **kw: Any) -> Any:
        seen.append(args)
        return real_run(args, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    (report,) = build_reports(repo, [ch(".env.local", ChangeKind.CREATED)], BUILTIN_REDACT)
    assert report.redacted is True
    assert report.lines == ()
    assert not any(".env.local" in " ".join(a) for a in seen)


def test_current_branch_and_detached_head(repo: Path) -> None:
    assert current_branch(repo) == "main"
    sha = git(repo, "rev-parse", "--short", "HEAD").strip()
    git(repo, "checkout", "-q", "--detach")
    assert current_branch(repo) == sha


def test_current_branch_none_outside_git(tmp_project: Path) -> None:
    assert current_branch(tmp_project) is None
    assert current_branch(None) is None


# --- pathspec magic / textconv must never leak redacted content ---------------


def _all_text(reports: list[Any]) -> str:
    return "\n".join(line for r in reports for line in r.lines)


def test_glob_named_file_does_not_pull_in_redacted_sibling(repo: Path) -> None:
    mig = repo / "migrations"
    mig.mkdir()
    (mig / ".env.prod").write_text("TOKEN=OLD\n")
    (mig / "*").write_text("one\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "mig")
    (mig / ".env.prod").write_text("TOKEN=NEWSECRET\n")
    (mig / "*").write_text("two\n")
    reports = build_reports(repo, [ch("migrations/*", ChangeKind.MODIFIED)], BUILTIN_REDACT)
    text = _all_text(reports)
    assert "NEWSECRET" not in text and "TOKEN" not in text
    assert not any(line.startswith(("--- a/", "+++ b/")) for r in reports for line in r.lines)
    assert reports[0].lines == ("-one", "+two")


def test_untracked_bracket_name_does_not_match_tracked_redacted(repo: Path) -> None:
    mig = repo / "migrations"
    mig.mkdir()
    (mig / ".env.prod").write_text("TOKEN=OLD\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "mig")
    (mig / ".env.prod").write_text("TOKEN=NEWSECRET\n")
    (mig / "[.]env.prod").write_text("harmless\n")
    reports = build_reports(
        repo, [ch("migrations/[.]env.prod", ChangeKind.CREATED)], BUILTIN_REDACT
    )
    text = _all_text(reports)
    assert "NEWSECRET" not in text and "TOKEN" not in text
    assert reports[0].lines == ("+harmless",)


def test_textconv_driver_is_not_run(repo: Path) -> None:
    (repo / ".gitattributes").write_text("*.cfg diff=dec\n")
    (repo / "a.cfg").write_text("enc-1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "cfg")
    # Prefixes each converted line, so a textconv-produced diff would show it.
    git(repo, "config", "diff.dec.textconv", "sh -c 'echo DECRYPTED-SECRET-$(cat \"$0\")'")
    (repo / "a.cfg").write_text("enc-2\n")
    reports = build_reports(repo, [ch("a.cfg", ChangeKind.MODIFIED)], BUILTIN_REDACT)
    assert "DECRYPTED-SECRET" not in _all_text(reports)
    assert reports[0].lines == ("-enc-1", "+enc-2")


# --- new-file reads never follow symlinks or block on special files ----------


def _call_with_deadline(fn: Any, seconds: float = 5.0) -> Any:
    """Run fn in a daemon thread; fail (instead of hanging the suite) on timeout."""
    box: list[Any] = []
    worker = threading.Thread(target=lambda: box.append(fn()), daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        pytest.fail(f"call did not return within {seconds}s (blocked on open?)")
    return box[0]


def test_symlink_to_file_outside_project_is_not_read(repo: Path, tmp_dir: Path) -> None:
    secret = tmp_dir / "secret"
    secret.write_text("-----BEGIN PRIVATE KEY-----\nMIIE\n")
    (repo / "deps.lock").symlink_to(secret)
    (report,) = build_reports(repo, [ch("deps.lock", ChangeKind.CREATED)], BUILTIN_REDACT)
    assert report.lines == ()
    assert report.note is not None and report.note.startswith("symlink")


def test_symlink_to_redacted_file_inside_project_is_not_read(repo: Path) -> None:
    (repo / ".env.local").write_text("API_KEY=SECRET-VALUE\n")
    (repo / "yarn.lock").symlink_to(".env.local")
    (report,) = build_reports(repo, [ch("yarn.lock", ChangeKind.CREATED)], BUILTIN_REDACT)
    assert report.lines == ()
    assert "API_KEY" not in (report.note or "")


def test_symlink_to_fifo_does_not_hang(repo: Path, tmp_dir: Path) -> None:
    fifo = tmp_dir / "pipe"
    os.mkfifo(fifo)
    (repo / "deps.lock").symlink_to(fifo)
    (report,) = _call_with_deadline(
        lambda: build_reports(repo, [ch("deps.lock", ChangeKind.CREATED)], BUILTIN_REDACT)
    )
    assert report.lines == ()


def test_fifo_new_file_does_not_hang(repo: Path) -> None:
    os.mkfifo(repo / "deps.lock")
    (report,) = _call_with_deadline(
        lambda: build_reports(repo, [ch("deps.lock", ChangeKind.CREATED)], BUILTIN_REDACT)
    )
    assert (report.lines, report.note) == ((), "not a regular file")


def test_file_under_symlinked_dir_pointing_outside(repo: Path, tmp_dir: Path) -> None:
    outside = tmp_dir / "outside"
    outside.mkdir()
    (outside / "x.lock").write_text("PRIVATE-STUFF\n")
    (repo / "vendor").symlink_to(outside)
    (report,) = build_reports(repo, [ch("vendor/x.lock", ChangeKind.CREATED)], BUILTIN_REDACT)
    assert report.lines == ()
    assert report.note == "outside project"


def test_file_under_symlinked_dir_resolving_to_redacted(repo: Path) -> None:
    (repo / "secrets").mkdir()
    (repo / "secrets" / "prod").write_text("API_KEY=SECRET-VALUE\n")
    (repo / "conf").symlink_to("secrets")
    redact = (*BUILTIN_REDACT, "secrets/**")
    (report,) = build_reports(repo, [ch("conf/prod", ChangeKind.CREATED)], redact)
    assert report.lines == ()
    assert report.redacted is True


# --- hard links and post-open verification ------------------------------------


def test_hard_link_to_redacted_file_is_not_read(repo: Path) -> None:
    env_file = repo / ".env.local"
    env_file.write_text("API_KEY=SECRET-VALUE\n")
    os.link(env_file, repo / "yarn.lock")
    (report,) = build_reports(repo, [ch("yarn.lock", ChangeKind.CREATED)], BUILTIN_REDACT)
    assert report.lines == ()
    assert report.note == "hard-linked file"


def test_post_open_verification_rejects_swapped_inode(repo: Path) -> None:
    (repo / "a.lock").write_text("x\n")
    (repo / "b.lock").write_text("y\n")
    before = os.lstat(repo / "a.lock")
    fd = os.open(repo / "b.lock", os.O_RDONLY)
    try:
        problem = diffing._verify_opened(
            fd, before, os.path.realpath(repo), BUILTIN_REDACT, str(repo / "b.lock")
        )
    finally:
        os.close(fd)
    assert problem == "changed while reading"


def test_post_open_verification_checks_fd_real_path(repo: Path, tmp_dir: Path) -> None:
    outside = tmp_dir / "elsewhere"
    outside.mkdir()
    (outside / "x.lock").write_text("PRIVATE-STUFF\n")
    before = os.lstat(outside / "x.lock")
    fd = os.open(outside / "x.lock", os.O_RDONLY)
    try:
        problem = diffing._verify_opened(
            fd, before, os.path.realpath(repo), BUILTIN_REDACT, str(outside / "x.lock")
        )
    finally:
        os.close(fd)
    assert problem == "outside project"


def test_post_open_verification_accepts_plain_file(repo: Path) -> None:
    (repo / "a.lock").write_text("x\n")
    before = os.lstat(repo / "a.lock")
    fd = os.open(repo / "a.lock", os.O_RDONLY)
    try:
        real_root = os.path.realpath(repo)
        path = str(repo / "a.lock")
        assert diffing._verify_opened(fd, before, real_root, BUILTIN_REDACT, path) is None
    finally:
        os.close(fd)


# --- tracked files: hard links and special files are never diffed --------------


def test_tracked_file_hard_linked_to_redacted_is_not_diffed(repo: Path) -> None:
    env_file = repo / ".env"
    env_file.write_text("TOKEN=SECRET-VALUE\n")
    (repo / "package.json").unlink()
    os.link(env_file, repo / "package.json")
    (report,) = build_reports(repo, [ch("package.json", ChangeKind.MODIFIED)], BUILTIN_REDACT)
    assert "SECRET-VALUE" not in _all_text([report])
    assert (report.lines, report.note) == ((), "hard-linked file")


def test_tracked_file_replaced_by_fifo_is_not_diffed(repo: Path) -> None:
    (repo / "package.json").unlink()
    os.mkfifo(repo / "package.json")
    (report,) = _call_with_deadline(
        lambda: build_reports(repo, [ch("package.json", ChangeKind.MODIFIED)], BUILTIN_REDACT)
    )
    assert (report.lines, report.note) == ((), "not a regular file")


def test_tracked_symlink_still_shows_target_string(repo: Path) -> None:
    (repo / "link").symlink_to("package.json")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "link")
    (repo / "link").unlink()
    (repo / "link").symlink_to("schema.sql")
    (report,) = build_reports(repo, [ch("link", ChangeKind.MODIFIED)], BUILTIN_REDACT)
    assert report.lines == ("-package.json", "+schema.sql")


# --- unicode normalization: NFC pattern vs NFD name on disk --------------------


def test_nfd_file_matching_nfc_pattern_is_redacted(repo: Path) -> None:
    nfc = unicodedata.normalize("NFC", "cfg/clé.yaml")
    nfd = unicodedata.normalize("NFD", nfc)
    (repo / "cfg").mkdir()
    (repo / nfd).write_text("k=SECRET-VALUE\n")
    reports = build_reports(repo, [ch(nfd, ChangeKind.CREATED)], (*BUILTIN_REDACT, nfc))
    assert "SECRET-VALUE" not in _all_text(reports)
    assert reports[0].redacted is True


def test_file_under_nfd_symlinked_dir_resolving_to_nfc_redacted(repo: Path) -> None:
    nfc_dir = unicodedata.normalize("NFC", "sécrets")
    (repo / unicodedata.normalize("NFD", nfc_dir)).mkdir()
    (repo / unicodedata.normalize("NFD", nfc_dir) / "prod").write_text("SECRET-VALUE\n")
    (repo / "conf").symlink_to(unicodedata.normalize("NFD", nfc_dir))
    redact = (*BUILTIN_REDACT, f"{nfc_dir}/**")
    (report,) = build_reports(repo, [ch("conf/prod", ChangeKind.CREATED)], redact)
    assert report.lines == ()
    assert report.redacted is True


# --- project root typed in a different case / normalization than on disk -------


@pytest.mark.parametrize(
    ("on_disk", "configured"),
    [
        ("MyProj", "myproj"),
        (unicodedata.normalize("NFD", "Projé"), unicodedata.normalize("NFC", "Projé")),
        (unicodedata.normalize("NFC", "Projé"), unicodedata.normalize("NFD", "Projé")),
    ],
)
def test_new_file_shown_when_root_spelled_differently(
    tmp_dir: Path, on_disk: str, configured: str
) -> None:
    (tmp_dir / on_disk).mkdir()
    root = tmp_dir / configured
    if not root.is_dir():
        pytest.skip("case-sensitive filesystem")
    init_repo(root)
    (root / "new.json").write_text("hello\n")
    (report,) = build_reports(root, [ch("new.json", ChangeKind.CREATED)], BUILTIN_REDACT)
    assert (report.lines, report.note) == (("+hello",), "new file")


def test_file_outside_differently_spelled_root_still_refused(tmp_dir: Path) -> None:
    (tmp_dir / "MyProj").mkdir()
    (tmp_dir / "MyProjX").mkdir()
    root = tmp_dir / "myproj"
    if not root.is_dir():
        pytest.skip("case-sensitive filesystem")
    init_repo(root)
    (tmp_dir / "MyProjX" / "x.lock").write_text("PRIVATE\n")
    (root / "vendor").symlink_to(tmp_dir / "MyProjX")
    (report,) = build_reports(root, [ch("vendor/x.lock", ChangeKind.CREATED)], BUILTIN_REDACT)
    assert (report.lines, report.note) == ((), "outside project")


def test_git_never_inherits_stdin(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    seen: list[dict[str, Any]] = []
    real_run = subprocess.run

    def spy(args: list[str], **kw: Any) -> Any:
        seen.append(kw)
        return real_run(args, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    (repo / "package.json").write_text("{}\n")
    build_reports(repo, [ch("package.json", ChangeKind.MODIFIED)], BUILTIN_REDACT)
    assert seen and all(kw.get("stdin") is subprocess.DEVNULL for kw in seen)


# -- first_line (Plan 7 spec §2.1) ----------------------------------------------------


@pytest.mark.parametrize(
    ("header", "start"),
    [
        ("@@ -3,4 +5,6 @@ def f():", 5),
        ("@@ -17,7 +17,7 @@", 17),
        ("@@ -0,0 +1 @@", 1),  # a new one-line file: the comma-less form
        ("@@ -1 +1 @@", 1),
        ("@@ -2,0 +3 @@", 3),
        ("@@ -1 +0,0 @@", 0),  # a deletion's new side, as written
        ("+@@ -1 +5 @@ an added line that looks like a header", None),
        ("@@@ -1,2 -1,2 +1,3 @@@", None),  # a combined diff: never produced here
        ("not a header", None),
    ],
)
def test_hunk_new_start_reads_both_header_forms(header: str, start: int | None) -> None:
    assert diffing.hunk_new_start(header) == start


HEADERS = ["diff --git a/f b/f", "index 1..2 100644", "--- a/f", "+++ b/f"]


@pytest.mark.parametrize(
    ("hunks", "line"),
    [
        (["@@ -17,7 +17,7 @@", " a", " b", " c", "-old", "+new", " d"], 20),  # context counted
        (["@@ -17,6 +17,7 @@", " a", " b", " c", "+added", " d"], 20),
        (["@@ -17,7 +17,6 @@", " a", " b", " c", "-gone", " d", " e"], 20),  # deletion only
        (["@@ -1,3 +1,2 @@", "-gone", " a", " b"], 1),  # deleted at the top
        (["@@ -1,2 +0,0 @@", "-a", "-b"], 1),  # an emptied file: at least 1
        (["@@ -0,0 +1 @@", "+only"], 1),  # a new one-line file (comma-less)
        (["@@ -5 +5 @@", "-x", "\\ No newline at end of file", "+y"], 5),
        (["@@ -3,4 +3,4 @@", " a", "-b", "+B", " c", "@@ -30,3 +30,3 @@", "-z", "+Z"], 4),
        ([], None),  # no hunk: binary, mode change
    ],
)
def test_first_changed_line_counts_from_the_header(hunks: list[str], line: int | None) -> None:
    assert diffing.first_changed_line(HEADERS + hunks) == line


def test_first_line_is_the_first_changed_line(repo: Path) -> None:
    lines = [f"line {i}" for i in range(1, 41)]
    (repo / "long.txt").write_text("\n".join(lines) + "\n")
    git(repo, "add", "long.txt")
    git(repo, "commit", "-q", "-m", "long")
    lines[19] = "line 20 changed"
    lines[34] = "line 35 changed"  # a second hunk: only the first one counts
    (repo / "long.txt").write_text("\n".join(lines) + "\n")
    (report,) = build_reports(repo, [ch("long.txt", ChangeKind.MODIFIED)], BUILTIN_REDACT)
    assert report.first_line == 20  # the changed line, not the hunk's start (17)
    assert "+line 20 changed" in report.lines and "+line 35 changed" in report.lines


def test_a_new_one_line_file_in_the_index_starts_at_line_1(repo: Path) -> None:
    (repo / "VERSION").write_text("1.0\n")
    git(repo, "add", "VERSION")  # tracked, not committed: `git diff HEAD` says "+1"
    (report,) = build_reports(repo, [ch("VERSION", ChangeKind.CREATED)], BUILTIN_REDACT)
    assert (report.lines, report.first_line) == (("+1.0",), 1)


def test_a_deleted_line_opens_where_it_was(repo: Path) -> None:
    lines = [f"line {i}" for i in range(1, 41)]
    (repo / "long.txt").write_text("\n".join(lines) + "\n")
    git(repo, "add", "long.txt")
    git(repo, "commit", "-q", "-m", "long")
    del lines[19]  # line 20 goes: "@@ -17,7 +17,6 @@"
    (repo / "long.txt").write_text("\n".join(lines) + "\n")
    (report,) = build_reports(repo, [ch("long.txt", ChangeKind.MODIFIED)], BUILTIN_REDACT)
    assert (report.lines, report.first_line) == (("-line 20",), 20)


def test_an_emptied_file_opens_at_line_1(repo: Path) -> None:
    (repo / "schema.sql").write_text("")  # "@@ -1,2 +0,0 @@"
    (report,) = build_reports(repo, [ch("schema.sql", ChangeKind.MODIFIED)], BUILTIN_REDACT)
    assert report.lines == ("--- keep me", "-SELECT 1;")
    assert report.first_line == 1


def test_an_untracked_new_file_starts_at_line_1_unless_it_is_empty(repo: Path) -> None:
    (repo / "new.sql").write_text("SELECT 2;\n")
    (repo / "empty.sql").write_text("")
    new, empty = build_reports(
        repo,
        [ch("new.sql", ChangeKind.CREATED), ch("empty.sql", ChangeKind.CREATED)],
        BUILTIN_REDACT,
    )
    assert (new.lines, new.first_line) == (("+SELECT 2;",), 1)
    assert (empty.lines, empty.first_line) == ((), None)


def test_files_without_a_hunk_have_no_first_line(repo: Path) -> None:
    (repo / "logo.bin").write_bytes(b"\x89PNG\x00\x01")
    git(repo, "add", "logo.bin")
    git(repo, "commit", "-q", "-m", "logo")
    (repo / "logo.bin").write_bytes(b"\x89PNG\x00\x02")
    (repo / ".env").write_text("TOKEN=x\n")
    (repo / "package.json").unlink()
    reports = build_reports(
        repo,
        [
            ch("logo.bin", ChangeKind.MODIFIED),
            ch(".env", ChangeKind.CREATED),
            ch("package.json", ChangeKind.DELETED),
        ],
        BUILTIN_REDACT,
    )
    assert [r.note for r in reports] == ["binary file changed", None, None]
    assert reports[1].redacted is True
    assert [r.first_line for r in reports] == [None, None, None]


# -- git output is LF-delimited: no other line separator splits a line ---------------

# HOSTILE_* (tests/helpers.py): lines that str.splitlines() would break into a fake
# file header, hunk header or "+"/"-" line.


def _commit_hostile(repo: Path) -> None:
    (repo / "notes.txt").write_bytes(HOSTILE_BEFORE)
    git(repo, "add", "notes.txt")
    git(repo, "commit", "-q", "-m", "notes")
    (repo / "notes.txt").write_bytes(HOSTILE_AFTER)


def test_parse_diff_splits_a_real_git_diff_on_newlines_only(repo: Path) -> None:
    _commit_hostile(repo)
    raw = subprocess.run(
        ["git", "diff", "--no-color", "--no-ext-diff", "--no-textconv", "HEAD", "--", "notes.txt"],
        cwd=repo,
        capture_output=True,
        check=True,
    ).stdout
    parsed = diffing.parse_diff(raw.decode("utf-8"))
    assert parsed.lines == HOSTILE_CHANGED
    assert (parsed.first_line, parsed.binary) == (2, False)


def test_the_head_fallback_keeps_line_separators_inside_a_line(repo: Path) -> None:
    _commit_hostile(repo)
    (report,) = build_reports(repo, [ch("notes.txt", ChangeKind.MODIFIED)], BUILTIN_REDACT)
    assert report.note == "vs HEAD"
    assert report.lines == HOSTILE_CHANGED
    assert report.first_line == 2


def test_a_new_files_first_lines_split_on_newlines_only(repo: Path) -> None:
    (repo / "fresh.txt").write_bytes(HOSTILE_AFTER)
    (report,) = build_reports(repo, [ch("fresh.txt", ChangeKind.CREATED)], BUILTIN_REDACT)
    assert report.note == "new file"
    assert report.lines == ("+head", *(f"+{line}" for line in HOSTILE_LINES), "+tail")
    assert report.first_line == 1


@pytest.mark.parametrize(
    ("text", "lines"),
    [
        ("", []),
        ("a", ["a"]),
        ("a\n", ["a"]),
        ("a\n\n", ["a", ""]),
        ("a\r\nb\u2028c\x85\n", ["a\r", "b\u2028c\x85"]),
    ],
)
def test_split_lines_breaks_only_on_newline(text: str, lines: list[str]) -> None:
    assert diffing.split_lines(text) == lines
