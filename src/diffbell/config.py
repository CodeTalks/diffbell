"""Load and validate ~/.config/diffbell/config.yaml (spec §4).

Every list is additive over the built-ins: `defaults` adds watch patterns and
`redact` adds redaction patterns. Coverage only shrinks through an explicit
`ignore` (global or per project) or `use_defaults: false`.
"""

from __future__ import annotations

import os
import re
import unicodedata
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from diffbell.globs import compile_glob

BUILTIN_DEFAULTS: tuple[str, ...] = (
    "package.json",
    "pyproject.toml",
    "*.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    ".env*",
    "Dockerfile*",
    "docker-compose*.yml",
    "Makefile",
    ".github/workflows/**",
    "CLAUDE.md",
    ".claude/settings*.json",
    "wrangler.toml",
    "*.entitlements",
    "Info.plist",
    "app.json",
    "eas.json",
)

BUILTIN_REDACT: tuple[str, ...] = (
    ".env*",
    ".dev.vars*",
    "*.pem",
    "*.key",
    "*.p8",
    "*.p12",
    "*.pfx",
    "*.keystore",
    "*.jks",
    "id_rsa*",
    "id_ed25519*",
    "*credentials*",
    "*secret*",
    "*.mobileprovision",
    "google-services.json",
    "GoogleService-Info.plist",
    ".npmrc",
    ".netrc",
    ".pypirc",
    "*.tfvars",
    "*.tfstate*",
    "*service-account*.json",
)

# Excluded at any depth, not configurable (§4). `.worktrees` prevents double
# reports from the worktree-per-feature workflow.
ALWAYS_EXCLUDED_DIRS: tuple[str, ...] = (
    ".git",
    "node_modules",
    ".venv",
    "dist",
    "build",
    ".next",
    ".wrangler",
    ".worktrees",
)


# The kinds of change a project can be notified about (`notify_on`), in this order.
NotifyKind = Literal["created", "modified", "deleted"]
NOTIFY_KINDS: tuple[NotifyKind, ...] = ("created", "modified", "deleted")
# "23:00-08:00": local time, 24-hour clock, the window may cross midnight.
# ASCII digits only: `\d` would also match other scripts' digits.
QUIET_HOURS = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]-([01][0-9]|2[0-3]):[0-5][0-9]")
# "09:00": the daily heartbeat's local time (health spec §5.2), validated like the
# quiet-hours times.
HEARTBEAT_TIME = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")
HEARTBEAT_DEFAULT = "09:00"
# The editor the dashboard's "Open" links use (Plan 7 spec §2.4). "none" (the
# string, not YAML null) hides them; an empty `editor:` is an error.
Editor = Literal["cursor", "vscode", "none"]
EDITORS: tuple[Editor, ...] = ("cursor", "vscode", "none")
EDITOR_DEFAULT: Editor = "cursor"


class ConfigError(Exception):
    """The config file is missing, unparsable or invalid. The message is user-facing."""


class MaxBelowQuiet(ValueError):
    """``max_wait_seconds < quiet_seconds``. Carries the validated quiet time, so the
    Settings message table (web/settings_check.py) names it without quoting input."""

    def __init__(self, quiet: float) -> None:
        super().__init__("max_wait_seconds must be >= quiet_seconds")
        self.quiet = quiet


class QuietHoursSame(ValueError):
    """A quiet-hours window that starts and ends at the same time."""

    def __init__(self) -> None:
        super().__init__("quiet hours start and end must differ")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DebounceConfig(_Strict):
    quiet_seconds: float = Field(10.0, gt=0)
    max_wait_seconds: float = Field(60.0, gt=0)

    @model_validator(mode="after")
    def _max_not_below_quiet(self) -> DebounceConfig:
        if self.max_wait_seconds < self.quiet_seconds:
            raise MaxBelowQuiet(self.quiet_seconds)
        return self


class DiffConfig(_Strict):
    max_lines: int = Field(20, ge=0, le=200)


class WebConfig(_Strict):
    enabled: bool = False
    port: int = Field(8765, ge=1024, le=65535)
    editor: Editor = EDITOR_DEFAULT

    @field_validator("editor", mode="before")
    @classmethod
    def _known_editor(cls, value: object) -> object:
        if not isinstance(value, str) or value not in EDITORS:
            raise ValueError(
                'editor must be "cursor", "vscode" or "none" (the string "none" hides Open)'
            )
        return value


class NotifyConfig(_Strict):
    # One daily window for every project; None: off (notify controls spec §3.1).
    quiet_hours: str | None = None
    # The daily silent heartbeat's local time; None (null): off (health spec §5.2).
    heartbeat: str | None = HEARTBEAT_DEFAULT

    @field_validator("heartbeat")
    @classmethod
    def _heartbeat_time(cls, value: str | None) -> str | None:
        if value is not None and not HEARTBEAT_TIME.fullmatch(value):
            raise ValueError('heartbeat must look like "09:00" (24-hour clock), or null for off')
        return value

    @field_validator("quiet_hours")
    @classmethod
    def _window(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not QUIET_HOURS.fullmatch(value):
            raise ValueError('quiet hours must look like "23:00-08:00" (24-hour clock)')
        start, end = value.split("-")
        if start == end:
            raise QuietHoursSame()
        return value


def _invisible(ch: str) -> bool:
    """Control, format (zero-width, bidi overrides, BOM), surrogate, private-use and
    unassigned characters, and line/paragraph separators: a name containing one can
    look like another name or reorder the text around it."""
    category = unicodedata.category(ch)
    return category.startswith("C") or category in ("Zl", "Zp")


class ProjectConfig(_Strict):
    path: str
    name: str | None = None
    patterns: list[str] = Field(default_factory=list)
    ignore: list[str] = Field(default_factory=list)
    use_defaults: bool = True
    notify_on: list[NotifyKind] = Field(default_factory=lambda: list(NOTIFY_KINDS))

    @field_validator("notify_on")
    @classmethod
    def _some_kinds_once(cls, value: list[NotifyKind]) -> list[NotifyKind]:
        if not value:
            raise ValueError("notify_on needs at least one kind; use snooze to silence a project")
        if len(set(value)) != len(value):
            raise ValueError("notify_on lists a kind more than once")
        return value

    @field_validator("name")
    @classmethod
    def _name_is_sane(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("project name must not be empty")
        if value.strip() in (".", ".."):
            # It is a path segment of /api/projects/<name>, which URLs collapse.
            raise ValueError("project name must not be '.' or '..'")
        if "/" in value or any(_invisible(ch) for ch in value):
            raise ValueError("project name must not contain '/' or control/invisible characters")
        return value


class Config(_Strict):
    debounce: DebounceConfig = Field(default_factory=DebounceConfig)
    diff: DiffConfig = Field(default_factory=DiffConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    redact: list[str] = Field(default_factory=list)
    defaults: list[str] = Field(default_factory=list)
    ignore: list[str] = Field(default_factory=list)
    projects: list[ProjectConfig] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ResolvedProject:
    """A validated project with its effective pattern lists.

    ``excluded`` holds the user's ignore patterns (global first, then project);
    the always-excluded directories are added by ``patterns.build_expression``.
    ``notify_on`` holds the kinds of change that notify (NOTIFY_KINDS values).
    """

    name: str
    path: Path
    watched: tuple[str, ...]
    excluded: tuple[str, ...]
    notify_on: tuple[str, ...] = NOTIFY_KINDS


def _dedupe(items: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


_QUOTES = "\"'‘’“”"  # straight and macOS "smart" quotes


def check_pattern(pattern: str) -> str | None:
    """Return an error message for an unusable glob, or None if it is fine."""
    if not pattern or pattern != pattern.strip():
        return f"invalid pattern {pattern!r}: empty or surrounding whitespace"
    if pattern.startswith("/"):
        return f"invalid pattern {pattern!r}: patterns are relative to the project root"
    if ".." in pattern.split("/"):
        return f"invalid pattern {pattern!r}: '..' segments are not allowed"
    if "\0" in pattern:
        return f"invalid pattern {pattern!r}: NUL byte"
    # Quotes typed around a pattern (e.g. in the web form) would be part of the
    # glob and match nothing. Rejected, not stripped: a name may contain quotes.
    if len(pattern) >= 2 and pattern[0] in _QUOTES and pattern[-1] in _QUOTES:
        return f"invalid pattern {pattern!r}: remove the quotes around it"
    # Watchman collapses "//" in names it reports and never reports a trailing
    # "/", so such a pattern would match nothing locally (and redact nothing).
    if "//" in pattern or pattern.endswith("/"):
        return f"invalid pattern {pattern!r}: empty path segment"
    # The local matcher (used for redaction, fail-closed) cannot honor these
    # constructs that Watchman accepts, so a pattern using them would silently
    # redact nothing. Reject them instead of emulating them.
    if "\\" in pattern:
        return f"invalid pattern {pattern!r}: backslash escapes are not supported"
    if "[:" in pattern:
        return f"invalid pattern {pattern!r}: POSIX character classes are not supported"
    if any(bad in pattern for bad in ("[]", "[!]", "[^]")):
        return f"invalid pattern {pattern!r}: a character class cannot start with ']'"
    if pattern.count("[") and pattern.rfind("[") > pattern.rfind("]"):
        return f"invalid pattern {pattern!r}: unclosed '['"
    # Compile exactly as the matcher will: a bad range like "[z-a]" would otherwise
    # raise on every report, and "[[", "&&", "--", "||", "~~" inside a class carry
    # future set-operation semantics (FutureWarning), so reject both.
    # patterns.matches also tries the NFD form, so validate NFC and NFD.
    forms = (
        (unicodedata.normalize("NFC", pattern), ""),
        (unicodedata.normalize("NFD", pattern), " (range is invalid after Unicode decomposition)"),
    )
    for normalized, hint in forms:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                compile_glob(normalized)
        except (re.error, Warning) as exc:
            return f"invalid pattern {pattern!r}: unsupported character class ({exc}){hint}"
    return None


@dataclass(frozen=True, slots=True)
class MissingProject:
    """A configured project whose directory does not exist (spec §8).

    Not a config error: the project is skipped until the directory reappears,
    and every other project keeps being watched. ``path`` is the expanded path.
    """

    name: str
    path: Path


def project_name(project: ProjectConfig) -> str:
    """The configured name, or the basename of the project's realpath."""
    return project.name or Path(os.path.realpath(Path(project.path).expanduser())).name


def canonical_path(path: str) -> str:
    """A project's folder as its stable id keys it (stable project ids spec §1):
    ``~`` expanded and symlinks resolved, without a trailing slash, as Watchman's
    roots are. Only meaningful for an absolute or ``~`` path (resolve_projects
    rejects any other)."""
    return os.path.realpath(os.path.expanduser(path))


def project_identities(config: Config) -> list[tuple[str, str]]:
    """(name, canonical folder) of every configured project, a missing folder
    included: what Store.sync_projects takes."""
    return [(project_name(p), canonical_path(p.path)) for p in config.projects]


def effective_lists(
    config: Config, project: ProjectConfig
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(watched, excluded) as the daemon uses them: built-in + extra defaults (when
    ``use_defaults``) + project patterns; global ignore + project ignore."""
    defaults = BUILTIN_DEFAULTS + tuple(config.defaults) if project.use_defaults else ()
    watched = _dedupe(defaults + tuple(project.patterns))
    excluded = _dedupe(tuple(config.ignore) + tuple(project.ignore))
    return watched, excluded


def resolve_projects(config: Config) -> tuple[list[ResolvedProject], list[MissingProject]]:
    """Validate the config. Returns the watchable projects and the ones whose path
    does not exist (or is not a directory). Raises ConfigError for anything else."""
    errors: list[str] = []
    lists = [config.redact, config.defaults, config.ignore]
    lists += [p.patterns for p in config.projects] + [p.ignore for p in config.projects]
    for patterns in lists:
        errors += [msg for pat in patterns if (msg := check_pattern(pat))]

    resolved: list[ResolvedProject] = []
    missing: list[MissingProject] = []
    # Messages name projects, never paths: the web API returns them as 422 bodies.
    seen_paths: dict[Path, tuple[int, str]] = {}
    seen_names: set[str] = set()
    for index, project in enumerate(config.projects, start=1):
        if not project.path.startswith(("/", "~")):
            # Not project_name(): its realpath of a relative path depends on the cwd.
            base = Path(project.path).name
            label = project.name or (base if base not in ("", ".", "..") else "(unnamed)")
            errors.append(f"project {label}: path must be absolute or start with ~")
            continue
        name = project_name(project)
        raw = Path(project.path).expanduser()
        # realpath also works for a path that does not exist (yet).
        real = Path(os.path.realpath(raw))
        if real in seen_paths:
            first, first_name = seen_paths[real]
            errors.append(
                f"duplicate project path: entries {first} and {index}"
                f" (projects {first_name} and {name})"
            )
            continue
        if name in seen_names:
            errors.append(f"duplicate project name: {name}")
            continue
        seen_paths[real] = (index, name)
        seen_names.add(name)
        watched, excluded = effective_lists(config, project)
        if not watched:
            errors.append(
                f"project {name}: nothing to watch (use_defaults is false and patterns is empty)"
            )
            continue
        try:
            is_dir = raw.is_dir()
        except OSError:  # e.g. EACCES on a parent: treat like a missing path
            is_dir = False
        if not is_dir:
            missing.append(MissingProject(name, raw))
            continue
        resolved.append(ResolvedProject(name, real, watched, excluded, tuple(project.notify_on)))

    if errors:
        raise ConfigError("\n".join(errors))
    return resolved, missing


def redact_patterns(config: Config) -> tuple[str, ...]:
    """Built-in redaction list ∪ config `redact`. Built-ins can never be removed."""
    return _dedupe(BUILTIN_REDACT + tuple(config.redact))


def load_config(path: Path) -> Config:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path} (run `diffbell setup`)") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: cannot read config: {exc}") from exc
    try:
        data: Any = YAML(typ="safe").load(text)
    except YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: the top level must be a mapping")
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def load_and_resolve(
    path: Path,
) -> tuple[Config, list[ResolvedProject], list[MissingProject]]:
    config = load_config(path)
    return config, *resolve_projects(config)


DEFAULT_CONFIG_TEXT = """\
# diffbell configuration. See README.md for the full reference.
# Lists are additive: `defaults` and `redact` add to the built-in sets,
# which can never be removed. Use `ignore` to exclude patterns explicitly.

debounce:
  quiet_seconds: 10        # flush a project's batch after 10s with no new changes
  max_wait_seconds: 60     # ...or after 60s even if changes keep coming

diff:
  max_lines: 20            # per file, in the Telegram message

web:
  # enabled: false         # install turns the UI on while Telegram isn't set up
  port: 8765

notify:
  quiet_hours: null        # e.g. "23:00-08:00": hold, then one catch-up message per project
  heartbeat: "09:00"       # a silent daily "all OK" message; null turns it off

redact: []                 # extra redaction patterns
defaults: []               # extra watch patterns for every project
ignore: []                 # patterns excluded in every project

projects: []
# projects:
#   - path: ~/projects/myapp
#     name: myapp
#     patterns: ["migrations/**"]
#     ignore: ["examples/**"]
#     notify_on: [created, deleted]   # default: created, modified and deleted
"""


def write_default_config(path: Path) -> bool:
    """Create the config with commented defaults. Returns False if it already exists."""
    if path.exists():
        return False
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(DEFAULT_CONFIG_TEXT)
    return True
