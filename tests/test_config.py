from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from diffbell.config import (
    BUILTIN_DEFAULTS,
    BUILTIN_REDACT,
    NOTIFY_KINDS,
    Config,
    ConfigError,
    MissingProject,
    ProjectConfig,
    canonical_path,
    check_pattern,
    effective_lists,
    load_and_resolve,
    load_config,
    machine_name_ok,
    project_identities,
    project_name,
    redact_patterns,
    resolve_projects,
    write_default_config,
)
from diffbell.patterns import is_redacted


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_empty_file_gives_defaults(tmp_dir: Path) -> None:
    cfg = load_config(write(tmp_dir / "c.yaml", ""))
    assert cfg.debounce.quiet_seconds == 10
    assert cfg.debounce.max_wait_seconds == 60
    assert cfg.diff.max_lines == 20
    assert cfg.projects == []


def test_defaults_are_additive_and_ignore_subtracts(tmp_dir: Path) -> None:
    proj = tmp_dir / "proj"
    proj.mkdir()
    cfg_path = write(
        tmp_dir / "c.yaml",
        f"""
defaults: ["composer.lock"]
ignore: ["app.json"]
projects:
  - path: {proj}
    patterns: ["migrations/**"]
    ignore: ["examples/**"]
""",
    )
    _, projects, _ = load_and_resolve(cfg_path)
    (p,) = projects
    assert p.name == "proj"
    assert set(BUILTIN_DEFAULTS) <= set(p.watched)  # built-ins never removed
    assert "composer.lock" in p.watched
    assert "migrations/**" in p.watched
    assert p.excluded == ("app.json", "examples/**")


def test_use_defaults_false_watches_only_project_patterns(tmp_dir: Path) -> None:
    proj = tmp_dir / "site"
    proj.mkdir()
    cfg_path = write(
        tmp_dir / "c.yaml",
        f"projects:\n  - path: {proj}\n    use_defaults: false\n    patterns: ['src/x/**']\n",
    )
    _, (p,), _ = load_and_resolve(cfg_path)
    assert p.watched == ("src/x/**",)


def test_nothing_to_watch_is_an_error(tmp_dir: Path) -> None:
    proj = tmp_dir / "p"
    proj.mkdir()
    cfg_path = write(tmp_dir / "c.yaml", f"projects:\n  - path: {proj}\n    use_defaults: false\n")
    with pytest.raises(ConfigError, match="nothing to watch"):
        load_and_resolve(cfg_path)


def test_redaction_builtins_always_present(tmp_dir: Path) -> None:
    cfg = load_config(write(tmp_dir / "c.yaml", "redact: ['config/prod.yaml']\n"))
    pats = redact_patterns(cfg)
    assert set(BUILTIN_REDACT) <= set(pats)
    assert "config/prod.yaml" in pats


def test_unknown_key_rejected(tmp_dir: Path) -> None:
    with pytest.raises(ConfigError, match="redcat"):
        load_config(write(tmp_dir / "c.yaml", "redcat: []\n"))


def test_max_wait_below_quiet_rejected(tmp_dir: Path) -> None:
    text = "debounce:\n  quiet_seconds: 30\n  max_wait_seconds: 5\n"
    with pytest.raises(ConfigError, match="max_wait_seconds"):
        load_config(write(tmp_dir / "c.yaml", text))


def test_missing_project_path_is_skipped_not_rejected(tmp_dir: Path) -> None:
    ok = tmp_dir / "ok"
    ok.mkdir()
    cfg_path = write(
        tmp_dir / "c.yaml",
        f"projects:\n  - path: {ok}\n  - path: {tmp_dir / 'nope'}\n"
        f"  - path: {tmp_dir / 'gone'}\n    name: named\n",
    )
    _, projects, missing = load_and_resolve(cfg_path)
    assert [p.name for p in projects] == ["ok"]
    assert missing == [
        MissingProject("nope", tmp_dir / "nope"),
        MissingProject("named", tmp_dir / "gone"),
    ]


def test_missing_project_path_is_expanded(tmp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_dir))
    cfg_path = write(tmp_dir / "c.yaml", "projects:\n  - path: ~/later\n")
    _, projects, missing = load_and_resolve(cfg_path)
    assert projects == []
    assert missing == [MissingProject("later", tmp_dir / "later")]


def test_project_path_that_is_a_file_counts_as_missing(tmp_dir: Path) -> None:
    write(tmp_dir / "afile", "")
    cfg_path = write(tmp_dir / "c.yaml", f"projects:\n  - path: {tmp_dir / 'afile'}\n")
    _, projects, missing = load_and_resolve(cfg_path)
    assert projects == [] and [m.name for m in missing] == ["afile"]


def test_missing_project_still_gets_other_validation(tmp_dir: Path) -> None:
    ok = tmp_dir / "ok"
    ok.mkdir()
    cfg_path = write(
        tmp_dir / "c.yaml",
        f"projects:\n  - path: {ok}\n    name: x\n  - path: {tmp_dir / 'nope'}\n    name: x\n",
    )
    with pytest.raises(ConfigError, match="duplicate project name"):
        load_and_resolve(cfg_path)
    cfg_path = write(
        tmp_dir / "c.yaml",
        f"projects:\n  - path: {tmp_dir / 'nope'}\n    use_defaults: false\n",
    )
    with pytest.raises(ConfigError, match="nothing to watch"):
        load_and_resolve(cfg_path)


def test_duplicate_by_realpath_rejected(tmp_dir: Path) -> None:
    real = tmp_dir / "real"
    real.mkdir()
    (tmp_dir / "alias").symlink_to(real)
    cfg_path = write(
        tmp_dir / "c.yaml",
        f"projects:\n  - path: {real}\n  - path: {tmp_dir / 'alias'}\n    name: other\n",
    )
    with pytest.raises(
        ConfigError, match=r"duplicate project path: entries 1 and 2 \(projects real and other\)"
    ):
        load_and_resolve(cfg_path)


def test_duplicate_name_rejected(tmp_dir: Path) -> None:
    a, b = tmp_dir / "a", tmp_dir / "b"
    a.mkdir()
    b.mkdir()
    cfg_path = write(
        tmp_dir / "c.yaml",
        f"projects:\n  - path: {a}\n    name: x\n  - path: {b}\n    name: x\n",
    )
    with pytest.raises(ConfigError, match="duplicate project name"):
        load_and_resolve(cfg_path)


@pytest.mark.parametrize("bad", ["/abs/path", "../up", " spaced", ""])
def test_bad_patterns_rejected(tmp_dir: Path, bad: str) -> None:
    proj = tmp_dir / "p"
    proj.mkdir()
    cfg_path = write(tmp_dir / "c.yaml", f"projects:\n  - path: {proj}\n    patterns: ['{bad}']\n")
    with pytest.raises(ConfigError, match="invalid pattern"):
        load_and_resolve(cfg_path)


@pytest.mark.parametrize(
    "quoted",
    ['"src/main.py"', "'src/main.py'", "“src/main.py”", "‘main.py’", "\"main.py'"],
)
def test_quoted_pattern_rejected(quoted: str) -> None:
    # Typed into the web form, the quotes would be part of the glob and match nothing.
    msg = check_pattern(quoted)
    assert msg is not None and "remove the quotes" in msg


@pytest.mark.parametrize("fine", ["it's.txt", '"odd name.txt', "notes'", 'a"b.py'])
def test_pattern_with_a_quote_inside_or_on_one_side_is_accepted(fine: str) -> None:
    assert check_pattern(fine) is None


def test_invalid_yaml_rejected(tmp_dir: Path) -> None:
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(write(tmp_dir / "c.yaml", "projects: [\n"))


def test_missing_file_mentions_setup(tmp_dir: Path) -> None:
    with pytest.raises(ConfigError, match="diffbell setup"):
        load_config(tmp_dir / "missing.yaml")


def test_write_default_config_is_valid_and_idempotent(tmp_dir: Path) -> None:
    path = tmp_dir / "sub" / "config.yaml"
    assert write_default_config(path) is True
    assert write_default_config(path) is False
    cfg = load_config(path)
    assert cfg.projects == []


@pytest.mark.parametrize(
    "name",
    [".env", ".env.local", ".envrc", ".env-prod", ".env_backup", "pkg/api/.env.production"],
)
def test_every_builtin_watched_env_file_is_redacted(name: str) -> None:
    assert any(p.startswith(".env") for p in BUILTIN_DEFAULTS)
    assert is_redacted(name, BUILTIN_REDACT)


@pytest.mark.parametrize(
    "name",
    [
        ".dev.vars",
        ".dev.vars.staging",
        "AuthKey_ABC123.p8",
        ".pypirc",
        "gcp-service-account-prod.json",
    ],
)
def test_more_secret_files_redacted(name: str) -> None:
    assert is_redacted(name, BUILTIN_REDACT)


def test_package_json_not_redacted() -> None:
    assert not is_redacted("package.json", BUILTIN_REDACT)


def test_relative_project_path_error_does_not_depend_on_the_cwd(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``path: .`` is named "(unnamed)", not after whatever directory the CLI runs in."""
    monkeypatch.chdir(tmp_dir)
    cfg_path = write(tmp_dir / "c.yaml", "projects:\n  - path: .\n")
    with pytest.raises(ConfigError) as exc:
        load_and_resolve(cfg_path)
    assert "project (unnamed): path must be absolute or start with ~" in str(exc.value)
    assert tmp_dir.name not in str(exc.value)


def test_config_path_is_directory_is_config_error(tmp_dir: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read config"):
        load_config(tmp_dir)


def test_relative_project_path_rejected(tmp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_dir / "rel").mkdir()
    monkeypatch.chdir(tmp_dir)
    cfg_path = write(tmp_dir / "c.yaml", "projects:\n  - path: rel\n")
    with pytest.raises(ConfigError, match="project rel: path must be absolute or start with ~"):
        load_and_resolve(cfg_path)


@pytest.mark.parametrize(
    "bad",
    [
        r"foo\*",
        r"a\\b",
        "[[:alpha:]]x",
        "x[[:digit:]]",
        "[]a]",
        "[!]a]",
        "[^]a]",
        "foo[abc",
        "[",
        "a[b]c[d",
        "[z-a]x",
        "x[9-0]",
        "[a--b]",
        "[[a]",
        "[a&&b]",
    ],
)
def test_unsupported_redact_globs_rejected(tmp_dir: Path, bad: str) -> None:
    proj = tmp_dir / "p"
    proj.mkdir()
    cfg_path = write(tmp_dir / "c.yaml", f"redact: ['{bad}']\nprojects:\n  - path: {proj}\n")
    with pytest.raises(ConfigError, match="invalid pattern"):
        load_and_resolve(cfg_path)


@pytest.mark.parametrize("pattern", BUILTIN_DEFAULTS + BUILTIN_REDACT)
def test_builtin_patterns_pass_check(pattern: str) -> None:
    assert check_pattern(pattern) is None


@pytest.mark.parametrize("bad", ["config//prod.yaml", "a//b/.env", "config/", "**/secrets/"])
def test_empty_path_segment_rejected(bad: str) -> None:
    assert check_pattern(bad) == f"invalid pattern {bad!r}: empty path segment"


def test_decomposed_form_of_pattern_is_validated(tmp_dir: Path) -> None:
    # "é" in NFD is "e" + combining accent, which turns "[à-é]" into a bad range.
    proj = tmp_dir / "p"
    proj.mkdir()
    cfg_path = write(
        tmp_dir / "c.yaml",
        f"redact: ['cfg/[à-é]*.yaml']\nprojects:\n  - path: {proj}\n    patterns: ['*.md']\n",
    )
    with pytest.raises(ConfigError, match="invalid pattern"):
        load_and_resolve(cfg_path)


def test_decomposed_form_failure_is_explained() -> None:
    msg = check_pattern("cfg/[à-é]*.yaml")
    assert msg is not None
    assert msg.endswith("(range is invalid after Unicode decomposition)")


def test_plain_bad_range_has_no_decomposition_hint() -> None:
    msg = check_pattern("[z-a]")
    assert msg is not None and "unsupported character class" in msg
    assert "decomposition" not in msg


def test_unreadable_project_path_counts_as_missing(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locked = tmp_dir / "locked"
    real_is_dir = Path.is_dir

    def is_dir(self: Path, **kwargs: bool) -> bool:
        if self == locked:
            raise PermissionError(13, "Permission denied", str(self))
        return real_is_dir(self, **kwargs)

    monkeypatch.setattr(Path, "is_dir", is_dir)
    cfg_path = write(tmp_dir / "c.yaml", f"projects:\n  - path: {locked}\n")
    _, projects, missing = load_and_resolve(cfg_path)
    assert projects == []
    assert missing == [MissingProject("locked", locked)]


@pytest.mark.parametrize(
    "name", ["terraform.tfstate", "terraform.tfstate.backup", "infra/prod.tfstate"]
)
def test_terraform_state_is_redacted(name: str) -> None:
    assert "*.tfstate*" in BUILTIN_REDACT
    assert is_redacted(name, BUILTIN_REDACT)


def test_effective_lists_and_project_name_match_resolve_projects(tmp_dir: Path) -> None:
    proj = tmp_dir / "proj"
    proj.mkdir()
    config = Config.model_validate(
        {
            "defaults": ["Procfile"],
            "ignore": ["vendor/**"],
            "projects": [{"path": str(proj), "patterns": ["x.txt"], "ignore": ["a/**"]}],
        }
    )
    (project_config,) = config.projects
    watched, excluded = effective_lists(config, project_config)
    assert watched == (*BUILTIN_DEFAULTS, "Procfile", "x.txt")
    assert excluded == ("vendor/**", "a/**")
    assert project_name(project_config) == "proj"
    (resolved,), _ = resolve_projects(config)
    assert (resolved.name, resolved.watched, resolved.excluded) == ("proj", watched, excluded)
    no_defaults = project_config.model_copy(update={"use_defaults": False, "name": "n"})
    assert effective_lists(config, no_defaults)[0] == ("x.txt",)
    assert project_name(no_defaults) == "n"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "a/b",
        "a\nb",
        "a\x00b",
        "\ttab",
        "\u200b",  # zero-width space (Cf)
        "\ufeff",  # BOM / zero-width no-break space (Cf)
        "app\u202e",  # right-to-left override (Cf)
        "a\u200bb",
        "a\u2028b",  # line separator (Zl)
        "a\u2029b",  # paragraph separator (Zp)
        "a\ue000b",  # private use (Co)
        "a\U000e0001b",  # language tag (Cf)
    ],
)
def test_project_name_rejects_blank_slash_and_control_chars(tmp_dir: Path, bad: str) -> None:
    cfg = tmp_dir / "c.yaml"
    cfg.write_text(f"projects:\n  - path: {tmp_dir}\n    name: {json.dumps(bad)}\n")
    with pytest.raises(ConfigError):
        load_config(cfg)


@pytest.mark.parametrize("bad", [".", "..", " .. "])
def test_project_name_rejects_dot_and_dotdot(bad: str) -> None:
    # The name ends up in /api/projects/<name> URLs, where "." and ".." are path
    # segments a browser or proxy may collapse.
    with pytest.raises(ValidationError):
        ProjectConfig(path="/x", name=bad)


@pytest.mark.parametrize("good", [".app", "...", "a..b"])
def test_project_name_with_dots_passes(good: str) -> None:
    assert ProjectConfig(path="/x", name=good).name == good


def test_project_name_normal_passes(tmp_dir: Path) -> None:
    cfg = tmp_dir / "c.yaml"
    cfg.write_text(f"projects:\n  - path: {tmp_dir}\n    name: My App 2\n")
    assert load_config(cfg).projects[0].name == "My App 2"


def test_project_name_rejects_a_lone_surrogate() -> None:
    # YAML cannot carry one, but the model is also built directly (web config edits).
    with pytest.raises(ValidationError):
        ProjectConfig(path="/x", name="a\ud800b")


@pytest.mark.parametrize("good", ["caf\u00e9", "my-proj 2", "\u9879\u76ee"])
def test_project_name_accepts_visible_unicode(tmp_dir: Path, good: str) -> None:
    cfg = tmp_dir / "c.yaml"
    cfg.write_text(f"projects:\n  - path: {tmp_dir}\n    name: {json.dumps(good)}\n")
    assert load_config(cfg).projects[0].name == good


def test_relative_dotdot_project_path_is_unnamed(tmp_dir: Path) -> None:
    cfg_path = write(tmp_dir / "c.yaml", "projects:\n  - path: ..\n")
    with pytest.raises(ConfigError) as exc:
        load_and_resolve(cfg_path)
    assert "project (unnamed): path must be absolute or start with ~" in str(exc.value)


# -- notification controls (notify controls spec §3.1) --------------------------------


def test_notify_defaults(tmp_dir: Path) -> None:
    proj = tmp_dir / "p"
    proj.mkdir()
    config, projects, _ = load_and_resolve(
        write(tmp_dir / "c.yaml", f"projects:\n  - path: {proj}\n")
    )
    assert config.notify.quiet_hours is None
    assert config.projects[0].notify_on == ["created", "modified", "deleted"]
    assert projects[0].notify_on == NOTIFY_KINDS == ("created", "modified", "deleted")


def test_notify_on_and_quiet_hours_are_read(tmp_dir: Path) -> None:
    proj = tmp_dir / "p"
    proj.mkdir()
    text = (
        'notify:\n  quiet_hours: "23:00-08:00"\n'
        f"projects:\n  - path: {proj}\n    notify_on: [deleted, created]\n"
    )
    config, projects, _ = load_and_resolve(write(tmp_dir / "c.yaml", text))
    assert config.notify.quiet_hours == "23:00-08:00"
    assert projects[0].notify_on == ("deleted", "created")


def test_unquoted_quiet_hours_are_a_string(tmp_dir: Path) -> None:
    config = load_config(write(tmp_dir / "c.yaml", "notify:\n  quiet_hours: 23:00-08:00\n"))
    assert config.notify.quiet_hours == "23:00-08:00"


@pytest.mark.parametrize("good", ["23:00-08:00", "00:00-23:59", "12:30-13:45", "08:00-23:00"])
def test_valid_quiet_hours(good: str) -> None:
    assert Config.model_validate({"notify": {"quiet_hours": good}}).notify.quiet_hours == good


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ("23:00-23:00", "quiet hours start and end must differ"),
        ("24:00-08:00", "24-hour clock"),
        ("23:60-08:00", "24-hour clock"),
        ("9:00-17:00", "24-hour clock"),
        ("23:00 - 08:00", "24-hour clock"),
        ("23:00-08:00\n", "24-hour clock"),
        ("", "24-hour clock"),
        ("1١:00-08:00", "24-hour clock"),  # an Arabic-Indic digit: ASCII only
        ("23:00-0８:00", "24-hour clock"),  # a fullwidth digit
    ],
)
def test_invalid_quiet_hours(bad: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Config.model_validate({"notify": {"quiet_hours": bad}})


def test_notify_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError, match="extra"):
        Config.model_validate({"notify": {"quiet": "23:00-08:00"}})


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ([], "notify_on needs at least one kind; use snooze to silence a project"),
        (["created", "created"], "notify_on lists a kind more than once"),
        (["renamed"], "Input should be 'created', 'modified' or 'deleted'"),
    ],
)
def test_invalid_notify_on(bad: list[str], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ProjectConfig.model_validate({"path": "/p", "notify_on": bad})


def test_the_default_config_mentions_the_new_keys(tmp_dir: Path) -> None:
    path = tmp_dir / "config.yaml"
    write_default_config(path)
    text = path.read_text()
    assert "quiet_hours: null" in text and "notify_on: [created, deleted]" in text
    assert load_config(path).notify.quiet_hours is None


# -- the daily heartbeat (health spec §5.2) ----------------------------------------------


def test_the_heartbeat_is_on_at_nine_by_default() -> None:
    assert Config().notify.heartbeat == "09:00"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("notify:\n  heartbeat: 07:30\n", "07:30"),  # unquoted: still a string (YAML 1.2)
        ('notify:\n  heartbeat: "23:59"\n', "23:59"),
        ("notify:\n  heartbeat: null\n", None),
        ("notify:\n  heartbeat:\n", None),
        ("notify:\n  quiet_hours: null\n", "09:00"),
    ],
)
def test_the_heartbeat_is_read(tmp_dir: Path, text: str, expected: str | None) -> None:
    assert load_config(write(tmp_dir / "c.yaml", text)).notify.heartbeat == expected


@pytest.mark.parametrize(
    "bad",
    ["24:00", "9:00", "09:60", "09:00 ", "0９:00", "1١:00", "", "09:00-10:00"],
)
def test_an_invalid_heartbeat_is_refused(bad: str) -> None:
    with pytest.raises(ValidationError, match="24-hour clock"):
        Config.model_validate({"notify": {"heartbeat": bad}})


def test_the_default_config_mentions_the_heartbeat(tmp_dir: Path) -> None:
    path = tmp_dir / "config.yaml"
    write_default_config(path)
    assert 'heartbeat: "09:00"' in path.read_text()
    assert load_config(path).notify.heartbeat == "09:00"


# -- web.editor (Plan 7 spec §2.4) ----------------------------------------------------


def test_the_editor_defaults_to_cursor(tmp_dir: Path) -> None:
    assert load_config(write(tmp_dir / "c.yaml", "")).web.editor == "cursor"
    assert load_config(write(tmp_dir / "c.yaml", "web:\n  port: 8765\n")).web.editor == "cursor"


@pytest.mark.parametrize("value", ["cursor", "vscode", "none", '"none"'])
def test_the_known_editors_load(tmp_dir: Path, value: str) -> None:
    config = load_config(write(tmp_dir / "c.yaml", f"web:\n  editor: {value}\n"))
    assert config.web.editor == value.strip('"')


@pytest.mark.parametrize("value", ["", "null", "~", "Cursor", "vim", "1", "[cursor]", "true"])
def test_any_other_editor_is_an_error_naming_the_key(tmp_dir: Path, value: str) -> None:
    with pytest.raises(ConfigError) as caught:
        load_config(write(tmp_dir / "c.yaml", f"web:\n  editor: {value}\n"))
    message = str(caught.value)
    assert "web.editor" in message
    assert 'editor must be "cursor", "vscode" or "none"' in message


def test_project_identities_are_canonical_folders(tmp_dir: Path) -> None:
    """Stable project ids spec §1: the folder a project's id keys on is its realpath,
    without a trailing slash; a missing folder is included."""
    real = tmp_dir / "real"
    real.mkdir()
    (tmp_dir / "link").symlink_to(real)
    config = Config.model_validate(
        {
            "projects": [
                {"path": f"{tmp_dir / 'link'}/"},
                {"path": str(tmp_dir / "gone"), "name": "g"},
            ]
        }
    )
    assert project_identities(config) == [("real", str(real)), ("g", str(tmp_dir / "gone"))]
    assert canonical_path("~") == os.path.realpath(Path.home())


# -- notify.machine (machine name spec §2, ADR 0023) ------------------------------------


def test_the_machine_name_is_unset_by_default() -> None:
    assert Config().notify.machine is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('notify:\n  machine: "Work MBP"\n', "Work MBP"),
        ('notify:\n  machine: ""\n', ""),
        ("notify:\n  machine: null\n", None),
        ("notify:\n  machine:\n", None),
        ("notify:\n  heartbeat: null\n", None),
        (f'notify:\n  machine: "  {"x" * 40}  "\n', f"  {'x' * 40}  "),  # 40 after strip
        ('notify:\n  machine: "Felix’s Mac"\n', "Felix’s Mac"),
    ],
)
def test_the_machine_name_is_read(tmp_dir: Path, text: str, expected: str | None) -> None:
    assert load_config(write(tmp_dir / "c.yaml", text)).notify.machine == expected


@pytest.mark.parametrize("bad", ["x" * 41, "Work\nMBP", "Work\tMBP", "Work​MBP", "a‮b"])
def test_an_invalid_machine_name_is_refused_not_cleaned(bad: str) -> None:
    with pytest.raises(ValidationError, match="40 characters"):
        Config.model_validate({"notify": {"machine": bad}})


def test_the_machine_rule_is_one_function() -> None:
    assert machine_name_ok("Work MBP") and machine_name_ok("") and machine_name_ok(" a ")
    assert not machine_name_ok("x" * 41) and not machine_name_ok("a\x00")


def test_the_default_config_documents_the_machine_name(tmp_dir: Path) -> None:
    path = tmp_dir / "config.yaml"
    write_default_config(path)
    text = path.read_text()
    assert '# machine: "Work MBP"' in text and "Computer Name" in text
    assert load_config(path).notify.machine is None
