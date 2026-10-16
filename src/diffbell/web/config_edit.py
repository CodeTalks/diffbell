"""Every config edit made by the web UI and `diffbell web` (web UI spec §3, W9).

Round-trips config.yaml with ruamel so comments and layout survive, applies one typed
mutation, validates the result with the same models and ``resolve_projects`` the
daemon uses, then replaces the file atomically. ``config_version`` is the SHA-256 of
the file's bytes, so any change on disk (even a comment) is a conflict.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError
from ruamel.yaml.representer import RoundTripRepresenter
from ruamel.yaml.scalarstring import DoubleQuotedScalarString
from ruamel.yaml.tokens import CommentToken

from diffbell.config import (
    EDITOR_DEFAULT,
    HEARTBEAT_DEFAULT,
    NOTIFY_KINDS,
    Config,
    ConfigError,
    Editor,
    ProjectConfig,
    load_config,
    project_name,
    resolve_projects,
)
from diffbell.web import settings_check


class ConfigConflict(Exception):
    """The file changed on disk since the caller read it (HTTP 409)."""


class ConfigInvalid(Exception):
    """The edited config would be invalid; the file was not touched (HTTP 422)."""


class ProjectNotFound(Exception):
    """No configured project has that name (HTTP 404)."""


class ConfigUnreadable(Exception):
    """The config file exists but can't be read (e.g. EACCES); ``error`` is the cause."""

    def __init__(self, error: OSError) -> None:
        super().__init__(str(error))
        self.error = error


HOME_REFUSED = (
    "a project directory cannot be your home directory itself "
    "(watching all of it is too expensive); pick a directory inside it"
)


def describe_validation_error(exc: ValidationError) -> str:
    """Field + reason for each error, never the offending input (``str(exc)``
    includes ``input_value``, i.e. submitted values or config file contents). An
    error on a setting the Settings view edits is worded by its message table
    (settings_check.message_for), so the page, a save and a config-load rejection
    say the same sentence."""
    parts = []
    for error in exc.errors()[:5]:
        worded = settings_check.message_for(error)
        if worded is not None:
            parts.append(f"{worded[0]}: {worded[1]}")
            continue
        where = ".".join(str(part) for part in error.get("loc", ()))
        if error.get("type") == "extra_forbidden":
            parts.append(f"{where}: unknown setting" if where else "unknown setting")
            continue
        reason = str(error.get("msg", "invalid value")).removeprefix("Value error, ")
        parts.append(f"{where}: {reason}" if where else reason)
    return "; ".join(parts) or "invalid config"


def describe_yaml_error(exc: Exception) -> str:
    """Where and what, never the snippet of the file that ruamel quotes."""
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None) or "cannot parse"
    if mark is None:
        return f"invalid YAML: {problem}"
    return f"invalid YAML at line {mark.line + 1}, column {mark.column + 1}: {problem}"


def describe_config_error(exc: ConfigError) -> str:
    """A config-load rejection as the alert says it (Telegram, the page): the cause
    worded as a save's 422 is (field + reason, never the file's contents, its path or
    a library's URL); a ConfigError with no such cause keeps its own text."""
    cause = exc.__cause__
    if isinstance(cause, ValidationError | YAMLError):
        return _describe(cause)
    return str(exc)


def _describe(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return describe_validation_error(exc)
    if isinstance(exc, YAMLError):
        return describe_yaml_error(exc)
    return str(exc)


@dataclass(frozen=True, slots=True)
class ProjectInput:
    path: str
    name: str | None
    patterns: list[str]
    ignore: list[str]
    use_defaults: bool
    notify_on: list[str] | None = None  # None: leave the project's notify_on as it is


class Keep(Enum):
    """A field the request did not send: the config keeps its value."""

    KEEP = "keep"


KEEP = Keep.KEEP


@dataclass(frozen=True, slots=True)
class NotifyInput:
    quiet_hours: str | None  # None: quiet hours off
    heartbeat: str | Keep | None = KEEP  # None: the heartbeat off (written as null)


@dataclass(frozen=True, slots=True)
class SettingsInput:
    quiet_seconds: float
    max_wait_seconds: float
    max_lines: int
    defaults: list[str]
    ignore: list[str]
    redact: list[str]
    notify: NotifyInput | None = None  # None: leave the notify block as it is
    editor: Editor | Keep = KEEP  # web.editor (Plan 7 spec §2.4); KEEP: leave it as it is


def version_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def config_version(path: Path) -> str:
    try:
        return version_of(path.read_bytes())
    except FileNotFoundError:
        return version_of(b"")


NOT_FOUND = "config file not found (run `diffbell setup`)"


def _load_error(exc: ConfigError, path: Path) -> str:
    """load_config's reason without the config's absolute path (API bodies never
    carry it) or the pydantic/ruamel text (input values, file snippets)."""
    cause = exc.__cause__
    if isinstance(cause, ValidationError | YAMLError):
        return _describe(cause)
    if isinstance(cause, UnicodeDecodeError):
        return f"cannot read config file: not valid UTF-8 ({cause.reason})"
    if isinstance(cause, OSError):
        return f"cannot read config file: {cause.strerror or type(cause).__name__}"
    text = str(exc)
    if text.startswith("config file not found"):
        return NOT_FOUND
    return text.replace(f"{path}: ", "config file: ").replace(str(path), "config file")


def read(path: Path) -> tuple[str, Config]:
    """(config_version, parsed config). Raises ConfigInvalid for an invalid file."""
    version = config_version(path)
    try:
        return version, load_config(path)
    except ConfigError as exc:
        raise ConfigInvalid(_load_error(exc, path)) from None


class _VisibleNull:
    """A value written as a visible ``null``. ruamel writes None as an empty value,
    which a reader may take for a forgotten one; other None values (the default
    config's placeholders) keep their look."""


class _Representer(RoundTripRepresenter):
    """A subclass, so ruamel's shared representer is left as it is."""


def _null(representer: RoundTripRepresenter, _: _VisibleNull) -> Any:
    return representer.represent_scalar("tag:yaml.org,2002:null", "null")


_Representer.add_representer(_VisibleNull, _null)


def _yaml() -> YAML:
    yaml = YAML()
    yaml.Representer = _Representer
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.preserve_quotes = True
    yaml.width = 4096  # never re-wrap long lines
    return yaml


def _flow(items: list[str]) -> CommentedSeq:
    seq = CommentedSeq(items)
    seq.fa.set_flow_style()
    return seq


def _number(value: float) -> float | int:
    """10.0 → 10, so an untouched integer setting keeps its spelling."""
    return int(value) if float(value).is_integer() else value


def _set_or_drop(target: CommentedMap, key: str, value: Any) -> None:
    if value is None:
        target.pop(key, None)
    else:
        target[key] = value


def _section(data: CommentedMap, key: str, before: str | None = None) -> CommentedMap:
    """A new section goes before `before` when that key exists: a document's trailing
    comments (e.g. the commented `projects` example) stay at the end of the file."""
    section = data.get(key)
    if not isinstance(section, CommentedMap):
        section = CommentedMap()
        if before is not None and before in data:
            data.insert(list(data).index(before), key, section)
        else:
            data[key] = section
    return section


def _projects(data: CommentedMap) -> CommentedSeq:
    projects = data.get("projects")
    if not isinstance(projects, CommentedSeq):
        projects = CommentedSeq()
        data["projects"] = projects
    projects.fa.set_block_style()  # `projects: []` would otherwise stay a flow list
    return projects


def _index_of(data: CommentedMap, name: str) -> int:
    for i, item in enumerate(_projects(data)):
        try:
            if project_name(ProjectConfig.model_validate(dict(item))) == name:
                return i
        except (ValidationError, TypeError, ValueError):
            continue
    raise ProjectNotFound(name)


def _fill_project(target: CommentedMap, project: ProjectInput) -> None:
    """Write only what differs from the defaults, so the YAML stays minimal."""
    target["path"] = project.path
    _set_or_drop(target, "name", project.name or None)
    _set_or_drop(target, "patterns", _flow(project.patterns) if project.patterns else None)
    _set_or_drop(target, "ignore", _flow(project.ignore) if project.ignore else None)
    _set_or_drop(target, "use_defaults", None if project.use_defaults else False)
    if project.notify_on is not None:
        every = sorted(project.notify_on) == sorted(NOTIFY_KINDS)  # the default: no key
        _set_or_drop(target, "notify_on", None if every else _flow(project.notify_on))


def _validate(text: str, must_exist: str | None) -> None:
    """Raise ConfigInvalid unless the text is a config the daemon would accept.
    ``must_exist`` names a project whose directory must exist (an add or edit)."""
    try:
        data = YAML(typ="safe").load(text)
        config = Config.model_validate(data or {})
        resolved, missing = resolve_projects(config)
    except (YAMLError, ValidationError, ConfigError) as exc:
        raise ConfigInvalid(_describe(exc)) from None
    for gone in missing:
        if gone.name == must_exist:
            raise ConfigInvalid(
                "project directory does not exist (or is not a directory); "
                "fix the path or remove the project"
            )
    home = Path(os.path.realpath(Path.home()))
    for project in resolved:
        # Same rule as the preview (preview.check_path): never the home itself.
        if project.name == must_exist and project.path == home:
            raise ConfigInvalid(HOME_REFUSED)


def _atomic_write(path: Path, data: bytes) -> None:
    # A symlinked config (dotfiles) stays a symlink: write to the resolved target.
    path = path.resolve()
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _edit(
    path: Path,
    expected_version: str | None,
    mutate: Callable[[CommentedMap], str | None],
) -> str:
    """Load, mutate, validate, write. ``mutate`` returns the name of a project whose
    directory must exist, or None. Returns the new config_version."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise ConfigInvalid(NOT_FOUND) from None
    except OSError as exc:
        raise ConfigUnreadable(exc) from None
    if expected_version is not None and version_of(raw) != expected_version:
        raise ConfigConflict("config changed on disk")
    yaml = _yaml()
    try:
        data = yaml.load(raw.decode("utf-8"))
    except (YAMLError, UnicodeDecodeError) as exc:
        raise ConfigInvalid(f"cannot parse the current config: {_describe(exc)}") from None
    if data is None:
        data = CommentedMap()
    if not isinstance(data, CommentedMap):
        raise ConfigInvalid("config file: the top level must be a mapping")
    must_exist = mutate(data)
    out = io.StringIO()
    yaml.dump(data, out)
    text = out.getvalue()
    _validate(text, must_exist)
    if path.read_bytes() != raw:  # changed while we were editing: never overwrite it
        raise ConfigConflict("config changed on disk")
    new = text.encode("utf-8")
    _atomic_write(path, new)
    return version_of(new)


def add_project(path: Path, expected_version: str, project: ProjectInput) -> str:
    def mutate(data: CommentedMap) -> str:
        entry = CommentedMap()
        _fill_project(entry, project)
        _projects(data).append(entry)
        return project_name(ProjectConfig(path=project.path, name=project.name))

    return _edit(path, expected_version, mutate)


def edit_project(path: Path, expected_version: str, name: str, project: ProjectInput) -> str:
    def mutate(data: CommentedMap) -> str:
        entry = _projects(data)[_index_of(data, name)]
        _fill_project(entry, project)
        return project_name(ProjectConfig(path=project.path, name=project.name))

    return _edit(path, expected_version, mutate)


def remove_project(path: Path, expected_version: str, name: str) -> str:
    def mutate(data: CommentedMap) -> None:
        del _projects(data)[_index_of(data, name)]

    return _edit(path, expected_version, mutate)


class _Unchanged(Exception):
    pass


def _as_written(raw: str, new: str) -> str:
    """``new`` in the style of ``raw``: under the home folder as ~/... when raw was."""
    home = os.path.realpath(Path.home())
    if raw.startswith("~") and (new == home or new.startswith(home + os.sep)):
        return "~" + new[len(home) :]
    return new


def move_project_path(path: Path, old: str, new: str) -> bool:
    """Point every project entry whose folder is ``old`` (canonical:
    realpath(expanduser)) at ``new``, through the one config writer (round-trip,
    validate, atomic replace). True if an entry changed; nothing is written when
    none matches. Used only by rename_install (rename spec section 3, step 5)."""

    def mutate(data: CommentedMap) -> str | None:
        changed = False
        for entry in data.get("projects") or []:
            raw = entry.get("path") if isinstance(entry, CommentedMap) else None
            if isinstance(raw, str) and os.path.realpath(os.path.expanduser(raw)) == old:
                entry["path"] = _as_written(raw, new)
                changed = True
        if not changed:
            raise _Unchanged
        return None

    try:
        _edit(path, None, mutate)
    except _Unchanged:
        return False
    return True


def update_settings(path: Path, expected_version: str, settings: SettingsInput) -> str:
    """Only the additive lists, debounce/diff, the notify block and ``web.editor``:
    built-in lists and the web port can't be changed here (each field is whitelisted
    here explicitly; ``test_settings_cannot_change_the_port_or_builtins`` guards it)."""

    def mutate(data: CommentedMap) -> None:
        debounce = _section(data, "debounce")
        debounce["quiet_seconds"] = _number(settings.quiet_seconds)
        debounce["max_wait_seconds"] = _number(settings.max_wait_seconds)
        _section(data, "diff")["max_lines"] = settings.max_lines
        data["defaults"] = _flow(settings.defaults)
        data["ignore"] = _flow(settings.ignore)
        data["redact"] = _flow(settings.redact)
        if settings.notify is not None:
            quiet = settings.notify.quiet_hours
            notify = data.get("notify")
            if quiet is not None:
                _section(data, "notify", before="projects")["quiet_hours"] = quiet
            # Turned off: drop the key, then the block if that emptied it. Already off
            # (absent, or the default config's commented `null`): leave it as it is.
            elif isinstance(notify, CommentedMap) and notify.get("quiet_hours") is not None:
                del notify["quiet_hours"]
                if not notify:
                    del data["notify"]
            heartbeat = settings.notify.heartbeat
            if not isinstance(heartbeat, Keep):
                _set_heartbeat(data, heartbeat)
        if not isinstance(settings.editor, Keep):
            _set_editor(data, settings.editor)

    return _edit(path, expected_version, mutate)


def _set_editor(data: CommentedMap, value: Editor) -> None:
    """Write ``web.editor`` only when it changes what is in force (the default is
    cursor), double-quoted, so "none" reads as the string it is. An unknown value
    is written and then refused by ``_validate`` (the file is left untouched)."""
    web = data.get("web")
    current = web.get("editor", EDITOR_DEFAULT) if isinstance(web, CommentedMap) else EDITOR_DEFAULT
    if value == current:
        return
    section = _section(data, "web", before="projects")
    keys = list(section)
    section["editor"] = DoubleQuotedScalarString(value)
    if keys and "editor" not in keys:  # a new last key: it takes the block's blank lines
        _move_blank_lines(section, keys[-1], "editor")


def _move_blank_lines(section: CommentedMap, old_last: str, new_last: str) -> None:
    """ruamel keeps the blank lines after a block in its last key's end-of-line
    comment: a key appended after it would land below them, outside the block's
    look. Keep the old key's own comment there, and any comment lines below it, and
    move only the trailing blank lines (a flow map has no such token: left alone)."""
    item = section.ca.items.get(old_last)
    token = item[2] if item and len(item) > 2 else None
    if not isinstance(token, CommentToken):
        return
    head, newline, rest = str(token.value).partition("\n")
    below = rest.rstrip("\n")  # comment lines under the key ("  # note"), if any
    blank = rest[len(below) :]
    if below:
        below, blank = below + "\n", blank[1:]  # the first newline ends the last comment
    if not blank:
        return  # no blank lines after the block
    token.value = head + newline + below
    moved = CommentToken("\n" + blank, token.start_mark)
    section.ca.items[new_last] = [None, None, moved, None]


def _set_heartbeat(data: CommentedMap, value: str | None) -> None:
    """Write ``notify.heartbeat`` only when it changes what is in force. Off is a
    visible ``null``: the heartbeat is on by default, so removing the key would turn
    it back on at 09:00. A time is written double-quoted, like the default config's."""
    notify = data.get("notify")
    if isinstance(notify, CommentedMap) and "heartbeat" in notify:
        current = notify["heartbeat"]
    else:
        current = HEARTBEAT_DEFAULT
    if value == current:
        return
    written = _VisibleNull() if value is None else DoubleQuotedScalarString(value)
    _section(data, "notify", before="projects")["heartbeat"] = written


def web_enabled_set(path: Path) -> bool:
    """True if the config states ``web.enabled`` itself, whatever the value. Only an
    absent key lets `install` turn the web UI on (bot onboarding spec §1): a present
    one is the user's own edit, or `diffbell web` / `--disable`. False for a
    missing file. Raises ConfigUnreadable / ConfigInvalid like an edit would."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ConfigUnreadable(exc) from None
    try:
        data = _yaml().load(raw.decode("utf-8"))
    except (YAMLError, UnicodeDecodeError) as exc:
        raise ConfigInvalid(f"cannot parse the current config: {_describe(exc)}") from None
    web = data.get("web") if isinstance(data, CommentedMap) else None
    return isinstance(web, CommentedMap) and "enabled" in web


def set_web_enabled(path: Path, enabled: bool) -> bool:
    """Set ``web.enabled`` as an explicit key. Returns whether the value in force
    changed. The file is untouched only when the key already states ``enabled``; an
    absent key is written even when the default already matches, so the user's
    choice sticks (`install` never overrides a stated key)."""
    _, config = read(path)
    if config.web.enabled is enabled and web_enabled_set(path):
        return False

    def mutate(data: CommentedMap) -> None:
        section = _section(data, "web", before="projects")
        keys = list(section)
        section["enabled"] = enabled
        if keys and "enabled" not in keys:  # a new last key: it takes the block's blank lines
            _move_blank_lines(section, keys[-1], "enabled")

    _edit(path, None, mutate)
    return config.web.enabled is not enabled
