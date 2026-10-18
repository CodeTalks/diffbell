from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import pytest

from diffbell.config import DEFAULT_CONFIG_TEXT, Editor, load_config, write_default_config
from diffbell.web import config_edit
from diffbell.web.config_edit import (
    ConfigConflict,
    ConfigInvalid,
    ConfigUnreadable,
    ProjectInput,
    ProjectNotFound,
    SettingsInput,
)


@pytest.fixture
def cfg(tmp_dir: Path) -> Path:
    path = tmp_dir / "config.yaml"
    path.write_text(DEFAULT_CONFIG_TEXT)
    return path


def project(
    path: Path | str,
    *,
    name: str | None = None,
    patterns: tuple[str, ...] = (),
    ignore: tuple[str, ...] = (),
    use_defaults: bool = True,
) -> ProjectInput:
    return ProjectInput(str(path), name, list(patterns), list(ignore), use_defaults)


def test_config_version_hashes_the_file_bytes(cfg: Path) -> None:
    before = config_edit.config_version(cfg)
    cfg.write_text(DEFAULT_CONFIG_TEXT + "# a comment-only hand edit\n")
    assert config_edit.config_version(cfg) != before


def test_add_project_keeps_comments_and_returns_the_new_version(
    cfg: Path, tmp_project: Path
) -> None:
    version = config_edit.config_version(cfg)
    new = config_edit.add_project(cfg, version, project(tmp_project, patterns=("migrations/**",)))
    text = cfg.read_text()
    assert new == config_edit.config_version(cfg)
    assert "# diffbell configuration. See README.md" in text
    assert "quiet_seconds: 10        # flush a project's batch" in text
    assert f"  - path: {tmp_project}\n    patterns: [migrations/**]\n" in text
    (only,) = load_config(cfg).projects
    assert (only.path, only.patterns, only.use_defaults) == (
        str(tmp_project),
        ["migrations/**"],
        True,
    )


def test_edit_project_updates_in_place(cfg: Path, tmp_project: Path) -> None:
    v1 = config_edit.add_project(cfg, config_edit.config_version(cfg), project(tmp_project))
    v2 = config_edit.edit_project(
        cfg,
        v1,
        "proj",
        project(
            tmp_project, name="demo", ignore=("x/**",), use_defaults=False, patterns=("Makefile",)
        ),
    )
    (only,) = load_config(cfg).projects
    assert (only.name, only.patterns, only.ignore, only.use_defaults) == (
        "demo",
        ["Makefile"],
        ["x/**"],
        False,
    )
    config_edit.edit_project(cfg, v2, "demo", project(tmp_project))
    (only,) = load_config(cfg).projects
    assert (only.name, only.ignore, only.use_defaults) == (None, [], True)
    assert "use_defaults" not in cfg.read_text().split("projects:")[1].split("#")[0]


def test_remove_project(cfg: Path, tmp_project: Path) -> None:
    v1 = config_edit.add_project(cfg, config_edit.config_version(cfg), project(tmp_project))
    config_edit.remove_project(cfg, v1, "proj")
    assert load_config(cfg).projects == []


def test_unknown_project_is_not_found(cfg: Path) -> None:
    with pytest.raises(ProjectNotFound):
        config_edit.remove_project(cfg, config_edit.config_version(cfg), "nope")


def test_version_mismatch_is_a_conflict_and_leaves_the_file_alone(
    cfg: Path, tmp_project: Path
) -> None:
    stale = config_edit.config_version(cfg)
    cfg.write_text(DEFAULT_CONFIG_TEXT + "# hand edit\n")
    before = cfg.read_bytes()
    with pytest.raises(ConfigConflict):
        config_edit.add_project(cfg, stale, project(tmp_project))
    assert cfg.read_bytes() == before


@pytest.mark.parametrize(
    ("patterns", "ignore", "use_defaults"),
    [
        (("/abs",), (), True),
        ((), ("a//b",), True),
        ((), (), False),  # nothing to watch
    ],
)
def test_invalid_edit_is_rejected_and_the_file_is_byte_identical(
    cfg: Path,
    tmp_project: Path,
    patterns: tuple[str, ...],
    ignore: tuple[str, ...],
    use_defaults: bool,
) -> None:
    before = cfg.read_bytes()
    bad = project(tmp_project, patterns=patterns, ignore=ignore, use_defaults=use_defaults)
    with pytest.raises(ConfigInvalid):
        config_edit.add_project(cfg, config_edit.config_version(cfg), bad)
    assert cfg.read_bytes() == before


def test_adding_a_missing_directory_is_invalid(cfg: Path, tmp_dir: Path) -> None:
    before = cfg.read_bytes()
    with pytest.raises(
        ConfigInvalid, match="does not exist \\(or is not a directory\\); fix the path"
    ):
        config_edit.add_project(cfg, config_edit.config_version(cfg), project(tmp_dir / "nope"))
    assert cfg.read_bytes() == before


def test_duplicate_project_is_invalid(cfg: Path, tmp_project: Path) -> None:
    v1 = config_edit.add_project(cfg, config_edit.config_version(cfg), project(tmp_project))
    with pytest.raises(ConfigInvalid, match="duplicate project path"):
        config_edit.add_project(cfg, v1, project(tmp_project))


def test_update_settings_writes_only_the_editable_keys(cfg: Path) -> None:
    settings = SettingsInput(
        quiet_seconds=5.0,
        max_wait_seconds=30.0,
        max_lines=40,
        defaults=["Procfile"],
        ignore=["vendor/**"],
        redact=["*.secret.json"],
    )
    config_edit.update_settings(cfg, config_edit.config_version(cfg), settings)
    text = cfg.read_text()
    assert "quiet_seconds: 5         # flush a project's batch" in text
    assert "max_lines: 40            # per file" in text
    config = load_config(cfg)
    assert (config.defaults, config.ignore, config.redact) == (
        ["Procfile"],
        ["vendor/**"],
        ["*.secret.json"],
    )
    assert config.web.port == 8765 and config.web.enabled is False


def test_invalid_settings_leave_the_file_untouched(cfg: Path) -> None:
    before = cfg.read_bytes()
    bad = SettingsInput(10.0, 5.0, 20, [], [], [])  # max_wait < quiet
    with pytest.raises(ConfigInvalid, match="max_wait_seconds"):
        config_edit.update_settings(cfg, config_edit.config_version(cfg), bad)
    assert cfg.read_bytes() == before


def test_set_web_enabled_toggles_and_reports_no_change(cfg: Path) -> None:
    assert config_edit.set_web_enabled(cfg, True) is True
    text = cfg.read_text()
    # A new last key of the web block: inside it, before the blank line.
    assert "  port: 8765\n  enabled: true\n\nnotify:\n" in text
    assert "  # enabled: false         # install turns the UI on" in text  # comment kept
    assert load_config(cfg).web.enabled is True
    before = cfg.read_bytes()
    assert config_edit.set_web_enabled(cfg, True) is False
    assert cfg.read_bytes() == before
    assert config_edit.set_web_enabled(cfg, False) is True
    assert load_config(cfg).web.enabled is False


def test_set_web_enabled_creates_the_web_section(tmp_dir: Path) -> None:
    path = tmp_dir / "config.yaml"
    path.write_text("projects: []\n")
    assert config_edit.set_web_enabled(path, True) is True
    assert load_config(path).web.enabled is True


def test_the_default_config_leaves_web_enabled_unstated(cfg: Path) -> None:
    """`install` turns the UI on only while the key is absent (bot onboarding §1)."""
    assert (
        "web:\n  # enabled: false         # install turns the UI on while Telegram isn't set up\n"
        "  port: 8765\n" in cfg.read_text()
    )
    assert config_edit.web_enabled_set(cfg) is False
    assert load_config(cfg).web.enabled is False


@pytest.mark.parametrize("enabled", [True, False])
def test_web_and_disable_both_state_the_key(cfg: Path, enabled: bool) -> None:
    """`web` and `--disable` write an explicit key, even when the value is already in
    force: from then on it is the user's statement and `install` leaves it alone."""
    changed = config_edit.set_web_enabled(cfg, enabled)
    assert changed is enabled  # disabling an already-off UI changes nothing in force
    assert config_edit.web_enabled_set(cfg) is True
    assert f"  port: 8765\n  enabled: {str(enabled).lower()}\n\nnotify:\n" in cfg.read_text()
    assert load_config(cfg).web.enabled is enabled
    before = cfg.read_bytes()
    assert config_edit.set_web_enabled(cfg, enabled) is False
    assert cfg.read_bytes() == before  # stated and in force: untouched


@pytest.mark.parametrize(
    ("text", "stated"),
    [
        ("projects: []\n", False),
        ("web:\n  port: 8765\n", False),
        ("web:\n  # enabled: true\n  port: 8765\n", False),
        ("web:\n  enabled: false\n", True),
        ("web: {enabled: true, port: 8765}\n", True),
        ("", False),
    ],
)
def test_web_enabled_set_reads_key_presence(tmp_dir: Path, text: str, stated: bool) -> None:
    path = tmp_dir / "config.yaml"
    path.write_text(text)
    assert config_edit.web_enabled_set(path) is stated


def test_web_enabled_set_without_a_file_is_false(tmp_dir: Path) -> None:
    assert config_edit.web_enabled_set(tmp_dir / "missing.yaml") is False


def test_web_enabled_set_reports_an_unparsable_or_unreadable_file(tmp_dir: Path) -> None:
    path = tmp_dir / "config.yaml"
    path.write_text("web: [\n")
    with pytest.raises(ConfigInvalid, match="cannot parse"):
        config_edit.web_enabled_set(path)
    path.write_text("web:\n  enabled: true\n")
    path.chmod(0)
    try:
        with pytest.raises(ConfigUnreadable):
            config_edit.web_enabled_set(path)
    finally:
        path.chmod(0o600)


def test_write_is_atomic_and_keeps_the_file_mode(cfg: Path, tmp_project: Path) -> None:
    os.chmod(cfg, 0o600)
    inode = cfg.stat().st_ino
    config_edit.add_project(cfg, config_edit.config_version(cfg), project(tmp_project))
    assert cfg.stat().st_ino != inode  # replaced, not rewritten in place
    assert cfg.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in cfg.parent.iterdir() if p.name.endswith(".tmp")] == []


def test_read_rejects_an_invalid_file(cfg: Path) -> None:
    cfg.write_text("projects: 5\n")
    with pytest.raises(ConfigInvalid):
        config_edit.read(cfg)


def test_add_project_writes_through_a_symlinked_config(tmp_dir: Path, tmp_project: Path) -> None:
    real = tmp_dir / "real.yaml"
    real.write_text(DEFAULT_CONFIG_TEXT)
    link = tmp_dir / "lk.yaml"
    link.symlink_to(real)
    real.chmod(0o600)
    config_edit.add_project(link, config_edit.config_version(link), project(tmp_project))
    assert link.is_symlink()
    assert real.stat().st_mode & 0o777 == 0o600  # the target keeps its mode
    assert link.resolve() == real.resolve()
    assert f"path: {tmp_project}" in real.read_text()
    assert [p.path for p in load_config(link).projects] == [str(tmp_project)]


def test_the_home_directory_itself_is_refused(
    cfg: Path, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_dir / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    before = cfg.read_bytes()
    version = config_edit.config_version(cfg)
    for path in (home, "~"):
        with pytest.raises(ConfigInvalid, match="cannot be your home directory"):
            config_edit.add_project(cfg, version, project(path, name="h"))
    assert cfg.read_bytes() == before
    inside = home / "proj"
    inside.mkdir()
    config_edit.add_project(cfg, version, project(inside))


def test_invalid_config_messages_do_not_echo_values(tmp_dir: Path) -> None:
    path = tmp_dir / "config.yaml"
    path.write_text("debounce: {quiet_seconds: 'MARKER-x', max_wait_seconds: 1}\n")
    with pytest.raises(ConfigInvalid) as caught:
        config_edit.read(path)
    assert "MARKER-x" not in str(caught.value) and "debounce.quiet_seconds" in str(caught.value)
    path.write_text("projects: [\n  MARKER-y: [\n")
    with pytest.raises(ConfigInvalid) as caught:
        config_edit.read(path)
    assert "MARKER-y" not in str(caught.value) and "line" in str(caught.value)
    with pytest.raises(ConfigInvalid) as caught:
        config_edit.remove_project(path, config_edit.config_version(path), "x")
    assert "MARKER-y" not in str(caught.value)


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (None, "config file not found"),
        (b"\xff\xfe broken\n", "cannot read config"),
        (b"- a\n- b\n", "the top level must be a mapping"),
    ],
)
def test_config_errors_never_name_the_config_path(
    tmp_dir: Path, content: bytes | None, reason: str
) -> None:
    path = tmp_dir / "cfg-dir" / "config.yaml"
    path.parent.mkdir()
    if content is not None:
        path.write_bytes(content)
    with pytest.raises(ConfigInvalid) as caught:
        config_edit.read(path)
    assert reason in str(caught.value)
    assert str(path) not in str(caught.value) and str(tmp_dir) not in str(caught.value)
    version = config_edit.config_version(path)
    with pytest.raises(ConfigInvalid) as caught:
        config_edit.add_project(path, version, project(tmp_dir))
    assert str(tmp_dir) not in str(caught.value)
    with pytest.raises(ConfigInvalid) as caught:
        config_edit.remove_project(path, version, "x")
    assert str(tmp_dir) not in str(caught.value)
    if content is None:
        assert "config file not found" in str(caught.value)


def test_a_home_project_cannot_be_edited_until_its_path_changes(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_dir / "home"
    (home / "proj").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    path = tmp_dir / "config.yaml"
    path.write_text("projects:\n  - path: '~'\n    name: h\n")
    before = path.read_bytes()
    version = config_edit.config_version(path)
    for change in (
        project("~", name="h", patterns=("*.md",)),
        project("~", name="h", ignore=("tmp/**",)),
        project("~", name="h", patterns=("*.md",), use_defaults=False),
        project("~", name="renamed"),
        project(str(home), name="h"),
    ):
        with pytest.raises(ConfigInvalid, match="cannot be your home directory"):
            config_edit.edit_project(path, version, "h", change)
    assert path.read_bytes() == before
    config_edit.edit_project(path, version, "h", project(home / "proj", name="h"))
    assert [p.path for p in load_config(path).projects] == [str(home / "proj")]


COMMENTED = """\
# my config
debounce:
  quiet_seconds: 10        # flush soon
  max_wait_seconds: 60     # ...or later

diff:
  max_lines: 20            # per file

redact: []                 # extra redaction patterns
defaults: []
ignore: []                 # excluded everywhere

projects: []
# projects:
#   - path: ~/projects/myapp
"""


def quiet(window: str | None) -> SettingsInput:
    return SettingsInput(10.0, 60.0, 20, [], [], [], config_edit.NotifyInput(window))


def test_quiet_hours_on_then_off_restores_the_file_byte_for_byte(tmp_dir: Path) -> None:
    path = tmp_dir / "config.yaml"
    path.write_text(COMMENTED)
    config_edit.update_settings(path, config_edit.config_version(path), quiet("23:00-08:00"))
    assert load_config(path).notify.quiet_hours == "23:00-08:00"
    config_edit.update_settings(path, config_edit.config_version(path), quiet(None))
    assert path.read_text() == COMMENTED  # no empty `notify:\n  quiet_hours:` left behind


def test_quiet_hours_off_without_a_notify_block_is_a_no_op(tmp_dir: Path) -> None:
    path = tmp_dir / "config.yaml"
    path.write_text(COMMENTED)
    config_edit.update_settings(path, config_edit.config_version(path), quiet(None))
    assert path.read_text() == COMMENTED


def test_quiet_hours_already_off_keep_the_default_configs_placeholder(cfg: Path) -> None:
    config_edit.update_settings(cfg, config_edit.config_version(cfg), quiet(None))
    # Only on → off removes the key; the documented placeholder and its comment stay.
    assert '\nnotify:\n  quiet_hours:             # e.g. "23:00-08:00"' in cfg.read_text()
    assert load_config(cfg).notify.quiet_hours is None


# -- web.editor (Plan 7 spec §2.4) ----------------------------------------------------


def editor(value: Editor | config_edit.Keep) -> SettingsInput:
    return SettingsInput(10.0, 60.0, 20, [], [], [], editor=value)


def test_the_editor_round_trips_and_writes_only_what_changes(cfg: Path) -> None:
    config_edit.update_settings(cfg, config_edit.config_version(cfg), editor("cursor"))
    assert "editor" not in cfg.read_text()  # the default is already in force: not written
    config_edit.update_settings(cfg, config_edit.config_version(cfg), editor("vscode"))
    text = cfg.read_text()
    assert '  port: 8765\n  editor: "vscode"\n\nnotify:\n' in text  # inside the web block
    assert load_config(cfg).web.editor == "vscode"
    config_edit.update_settings(cfg, config_edit.config_version(cfg), editor("none"))
    assert '  editor: "none"\n' in cfg.read_text()  # the string, never YAML null
    assert load_config(cfg).web.editor == "none"
    config_edit.update_settings(cfg, config_edit.config_version(cfg), editor("cursor"))
    assert load_config(cfg).web.editor == "cursor"


def test_the_last_keys_own_comment_stays_on_its_line(tmp_dir: Path) -> None:
    path = tmp_dir / "config.yaml"
    path.write_text("web:\n  port: 8765   # the port\n\nprojects: []\n")
    config_edit.update_settings(path, config_edit.config_version(path), editor("none"))
    assert path.read_text().startswith(
        'web:\n  port: 8765   # the port\n  editor: "none"\n\nprojects: []\n'
    )


def test_a_flow_style_web_block_stays_on_one_line(tmp_dir: Path) -> None:
    path = tmp_dir / "config.yaml"
    path.write_text("web: {port: 8765}\n\nprojects: []\n")
    config_edit.update_settings(path, config_edit.config_version(path), editor("vscode"))
    assert path.read_text().startswith('web: {port: 8765, editor: "vscode"}\n\nprojects: []\n')
    assert load_config(path).web.editor == "vscode"


def test_a_comment_below_the_last_key_stays_and_the_blank_line_moves(tmp_dir: Path) -> None:
    """The comment stays below the key it followed; the new key comes after it,
    still inside the block, and the blank line still separates the next block."""
    path = tmp_dir / "config.yaml"
    path.write_text("web:\n  port: 8765\n  # note\n\nprojects: []\n")
    config_edit.update_settings(path, config_edit.config_version(path), editor("vscode"))
    assert path.read_text().startswith(
        'web:\n  port: 8765\n  # note\n  editor: "vscode"\n\nprojects: []\n'
    )
    assert load_config(path).web.editor == "vscode"


def machine_name(value: str | config_edit.Keep | None) -> SettingsInput:
    notify = config_edit.NotifyInput(None, machine=value)
    return SettingsInput(10.0, 60.0, 20, [], [], [], notify)


def test_naming_the_mac_and_clearing_it_restores_the_layout(tmp_dir: Path) -> None:
    """ADR 0023: the name takes the block's blank lines as its new last key, and gives
    them back when it is removed, so name → Computer Name leaves the file as it was."""
    path = tmp_dir / "config.yaml"
    write_default_config(path)
    config_edit.update_settings(
        path, config_edit.config_version(path), machine_name(config_edit.KEEP)
    )
    baseline = path.read_text()
    config_edit.update_settings(path, config_edit.config_version(path), machine_name("Work MBP"))
    assert 'machine: "Work MBP"' in path.read_text()
    config_edit.update_settings(path, config_edit.config_version(path), machine_name(None))
    assert path.read_text() == baseline
    assert load_config(path).notify.machine is None


def test_an_existing_editor_is_changed_in_place(tmp_dir: Path) -> None:
    """Not the last key of the block: the blank line after the block stays put."""
    path = tmp_dir / "config.yaml"
    path.write_text('web:\n  editor: "vscode"\n  port: 8765\n\nprojects: []\n')
    config_edit.update_settings(path, config_edit.config_version(path), editor("none"))
    assert path.read_text().startswith('web:\n  editor: "none"\n  port: 8765\n\nprojects: []\n')


def test_an_absent_editor_is_left_as_it_is(cfg: Path) -> None:
    config_edit.update_settings(cfg, config_edit.config_version(cfg), editor("vscode"))
    config_edit.update_settings(cfg, config_edit.config_version(cfg), editor(config_edit.KEEP))
    assert load_config(cfg).web.editor == "vscode"


def test_the_editor_creates_the_web_section_before_the_projects(tmp_dir: Path) -> None:
    path = tmp_dir / "config.yaml"
    path.write_text("projects: []\n")
    config_edit.update_settings(path, config_edit.config_version(path), editor("vscode"))
    text = path.read_text()
    assert text.index("web:") < text.index("projects:")
    assert load_config(path).web.editor == "vscode"


def test_an_unknown_editor_is_invalid_and_the_file_untouched(cfg: Path) -> None:
    before = cfg.read_bytes()
    with pytest.raises(ConfigInvalid, match=r"web\.editor"):
        config_edit.update_settings(
            cfg, config_edit.config_version(cfg), editor(cast(Editor, "vim"))
        )
    assert cfg.read_bytes() == before


def test_settings_still_never_touch_the_web_port(cfg: Path) -> None:
    config_edit.update_settings(cfg, config_edit.config_version(cfg), editor("vscode"))
    config = load_config(cfg)
    assert (config.web.port, config.web.enabled) == (8765, False)


def test_describe_config_error_words_the_cause_and_keeps_other_texts(tmp_path: Path) -> None:
    from diffbell.config import ConfigError

    path = tmp_path / "config.yaml"
    path.write_text("projects: []\nnope: 1\n")
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    assert config_edit.describe_config_error(caught.value) == "nope: unknown setting"
    missing = ConfigError("config file not found: x (run `diffbell setup`)")
    assert config_edit.describe_config_error(missing) == str(missing)


def test_move_project_path_rewrites_only_that_entry(tmp_path: Path) -> None:
    home = os.path.realpath(Path.home())
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "# keep me\nprojects:\n  - path: ~/projects/old-co\n    name: repo\n  - path: /srv/other\n"
    )
    old = f"{home}/projects/old-co"
    new = f"{home}/projects/new-co"
    assert config_edit.move_project_path(cfg, old, new) is True
    text = cfg.read_text()
    assert "path: ~/projects/new-co" in text and "/srv/other" in text and "# keep me" in text
    assert config_edit.move_project_path(cfg, old, new) is False  # already done


def test_move_project_path_without_a_match_writes_nothing(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("projects:\n  - path: /srv/other\n")
    before = cfg.stat().st_mtime_ns
    assert config_edit.move_project_path(cfg, "/a", "/b") is False
    assert cfg.stat().st_mtime_ns == before
