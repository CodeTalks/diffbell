"""The web UI's HTTP API and pages (web UI spec §4, §5).

Every handler is ``async def`` on purpose: they run on the daemon's loop thread,
which owns the SQLite connection (``sqlite3`` refuses use from another thread, and
FastAPI would run a plain ``def`` handler in a worker thread). No handler ever
returns file contents, the bot token or the chat id, and this module has no access
to Watchman (it never imports pywatchman and never receives the WatchmanClient).
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import tzinfo
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Query, Request
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)
from starlette.exceptions import HTTPException as StarletteHTTPException

from diffbell import BRAND, __version__, machine
from diffbell.config import (
    ALWAYS_EXCLUDED_DIRS,
    BUILTIN_DEFAULTS,
    BUILTIN_REDACT,
    NOTIFY_KINDS,
    Config,
    Editor,
    MissingProject,
    NotifyKind,
    ResolvedProject,
    effective_lists,
    project_name,
    redact_patterns,
)
from diffbell.health import current as current_problems
from diffbell.models import Change
from diffbell.patterns import is_redacted
from diffbell.pausing import HELD_FOREVER, QuietHours, quiet_end
from diffbell.setup_cmd import SetupClient
from diffbell.store import (
    EventRow,
    Problem,
    ReportSummary,
    Snooze,
    Store,
    StoredReport,
    UnknownProject,
    WebSession,
    reports_state,
    stored_first_line,
)
from diffbell.telegram import ErrorKind, NotSetUp, TelegramError
from diffbell.web import auth, browse, config_edit
from diffbell.web.browse import BrowseError, BrowseRequest
from diffbell.web.config_edit import (
    KEEP,
    ConfigConflict,
    ConfigInvalid,
    NotifyInput,
    ProjectInput,
    ProjectNotFound,
    SettingsInput,
    describe_validation_error,
)
from diffbell.web.counts import WatchedCounts
from diffbell.web.preview import Previewer, PreviewError, PreviewRequest
from diffbell.web.settings_check import SettingsRaw, check_settings, settings_values

STATIC_DIR = Path(__file__).parent / "static"
MAX_PAGE = 200
MAX_BODY = 1024 * 1024  # bytes, for any /api request body
MAX_INT = 2**63 - 1  # SQLite INTEGER; a larger Python int raises OverflowError
MAX_ITEMS = 200  # entries in any list field
MAX_TEXT = 1024  # characters in any string field, query or path parameter
MAX_SNOOZE = 30 * 24 * 3600.0  # the longest timed snooze (spec §5.1)

log = logging.getLogger(__name__)

Text = Annotated[str, StringConstraints(max_length=MAX_TEXT)]
TextList = Annotated[list[Text], Field(max_length=MAX_ITEMS)]
QueryText = Annotated[str | None, Query(max_length=MAX_TEXT)]


@dataclass(frozen=True, slots=True)
class WebContext:
    """What the daemon hands the web layer. Deliberately no WatchmanClient."""

    store: Store
    config_path: Path
    port: int
    # Random per server start, known only to the daemon and to status.json (0600).
    # /healthz proves knowledge of it (HMAC of a challenge) and never returns it, so
    # `diffbell web` can tell our server from a process squatting the port.
    secret: bytes
    # The daemon's live status snapshot (the dict status.json holds).
    snapshot: Callable[[], dict[str, Any]]
    # The daemon's active config, resolved projects and missing projects.
    current: Callable[[], tuple[Config, list[ResolvedProject], list[MissingProject]]]
    # Sends the sample notification through the daemon's sender.
    send_test: Callable[[], Awaitable[None]]
    previewer: Previewer
    # Settings → Telegram (bot onboarding spec §2). {"configured", "bot"} from the
    # daemon's SenderSlot: no secret, no Bot API call.
    telegram_status: Callable[[], dict[str, Any]]
    # The slot holds a bot: notifications can be sent.
    configured: Callable[[], bool]
    # Swaps the slot to a saved bot (token, chat id, bot username), resolves
    # telegram.setup and releases what was held for setup.
    complete_setup: Callable[[str, str, str | None], Awaitable[None]]
    # A Bot API client for a draft's token: chat id None to probe, else to send the
    # draft's test message. Never the slot's client.
    make_probe: Callable[[str, str | None], SetupClient]
    # Writes the pair to the Keychain (marker, chat id, token, marker deleted).
    # Blocking: called through asyncio.to_thread.
    save_credentials: Callable[[str, str], None]
    # Why Telegram is not set up (secrets.Missing.what) while the slot is empty, else
    # None: the daemon's cache (set at start, "unfinished save" after a failed save,
    # cleared by complete_setup). Never a Keychain read (settings redesign spec §7).
    setup_reason: Callable[[], str | None]
    # The wizard's Keychain write failed: the save-pending marker may be left.
    note_unfinished_save: Callable[[], None]
    clock: Callable[[], float] = time.time
    # The activity strip's local days (Plan 7 spec §2.2); None: the Mac's local time.
    tz: tzinfo | None = None


class ApiError(Exception):
    def __init__(
        self, status: int, message: str, *, extra: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        # Keys beside "error" in the body: only the settings 422 (errors, issues).
        self.extra = dict(extra or {})


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _no_nul(value: str) -> str:
    if "\0" in value:
        raise ValueError("must not contain a NUL byte")
    return value


class ProjectBody(_Body):
    config_version: Text
    path: Text
    name: Text | None = None
    patterns: TextList = Field(default_factory=list)
    ignore: TextList = Field(default_factory=list)
    use_defaults: bool = True
    # None (or absent): unchanged. Emptiness and duplicates are the config's checks (422).
    notify_on: Annotated[list[NotifyKind], Field(max_length=MAX_ITEMS)] | None = None

    _path_has_no_nul = field_validator("path")(_no_nul)

    def to_input(self) -> ProjectInput:
        name = self.name.strip() if self.name else None
        notify_on = None if self.notify_on is None else [str(k) for k in self.notify_on]
        return ProjectInput(
            self.path, name or None, self.patterns, self.ignore, self.use_defaults, notify_on
        )


class PreviewBody(_Body):
    path: Text
    patterns: TextList = Field(default_factory=list)
    ignore: TextList = Field(default_factory=list)
    use_defaults: bool = True


class BrowseBody(PreviewBody):
    dir: Text = ""  # a folder relative to `path`; "" is the project root


# A number field (settings redesign spec §3): the text typed, which check_settings
# parses (empty or not a number is a 422, never 0), or a JSON number. A JSON bool, a
# non-finite literal, or 20.0 for the lines is neither (400).
RawSeconds = Annotated[float, Field(strict=True, allow_inf_nan=False)] | Text
RawLines = Annotated[int, Field(strict=True)] | Text


class DebounceBody(_Body):
    quiet_seconds: RawSeconds  # > 0, and max_wait >= quiet: check_settings (422)
    max_wait_seconds: RawSeconds


class DiffBody(_Body):
    max_lines: RawLines  # 0-200: check_settings (422)


class NotifyBody(_Body):
    quiet_hours: Text | None  # "23:00-08:00", or null for off; checked by the config
    # "09:00", or null for off; absent: unchanged (checked by the config).
    heartbeat: Text | None = None
    # This Mac's name (ADR 0023): text, "" for none, null for the Computer Name;
    # absent: unchanged (checked by settings_check).
    machine: Text | None = None


class WebBody(_Body):
    # Only the editor: the port is never editable here (extra keys are a 400).
    editor: Editor


class SettingsCheckBody(_Body):
    """PUT /api/settings's body without config_version: POST /api/settings/check."""

    debounce: DebounceBody
    diff: DiffBody
    defaults: TextList
    ignore: TextList
    redact: TextList
    notify: NotifyBody | None = None  # absent: the quiet hours are left as they are
    web: WebBody | None = None  # absent: the editor is left as it is

    def raw(self) -> SettingsRaw:
        notify = self.notify
        return SettingsRaw(
            quiet_seconds=self.debounce.quiet_seconds,
            max_wait_seconds=self.debounce.max_wait_seconds,
            max_lines=self.diff.max_lines,
            defaults=self.defaults,
            ignore=self.ignore,
            redact=self.redact,
            quiet_hours=None if notify is None else notify.quiet_hours,
            heartbeat=None if notify is None else notify.heartbeat,
            machine=None if notify is None else notify.machine,
        )


class SettingsBody(SettingsCheckBody):
    config_version: Text


class SnoozeBody(_Body):
    # Epoch seconds, or null: until turned back on.
    until: Annotated[float, Field(strict=True, allow_inf_nan=False)] | None


def _page(title: str, message: str) -> str:
    """A tiny static page (no inline script or style: the CSP forbids both)."""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title>"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<link rel="stylesheet" href="/static/app.css"></head>'
        f'<body><main class="notice"><h1>{html.escape(title)}</h1>'
        f"<p>{message}</p></main></body></html>"
    )


LOGIN_HINT = _page(BRAND, "Run <code>diffbell web</code> to log in.")
LINK_EXPIRED = _page(
    "Link expired",
    "This login link has expired or was already used. Run <code>diffbell web</code> again.",
)

NOT_SET_UP = "Telegram isn't set up yet."

# `/login?next=…` (bot onboarding spec §3): an allowlist, never a URL taken from the
# query. A missing or unknown value goes to the dashboard.
LOGIN_TARGETS = {"telegram": "/?view=settings&section=telegram"}


def _event_row(
    event: EventRow,
    changes: list[Change],
    summary: ReportSummary | None,
    redact: tuple[str, ...],
    now: float,
) -> dict[str, Any]:
    """One event as /api/events and /api/events/{id} return it. ``redacted`` is
    computed now, from the current redaction list. ``added``/``removed`` are the
    stored counts here (reading every row's JSON would make the list slow);
    /api/events/{id} recounts them after the current redaction is applied."""
    return {
        "id": event.id,
        "project": event.project,
        # False once its project is no longer configured: the name may then be a
        # configured project's (another folder), so the page offers no folder actions.
        "project_active": event.project_active,
        "created_at": event.created_at,
        "status": event.status,
        "attempts": event.attempts,
        "last_error": event.last_error,
        "next_attempt_at": event.next_attempt_at,
        # Held: when it is due (null for an indefinite snooze, and once it is due:
        # its catch-up is then retrying or about to be sent).
        "held_until": (
            event.next_attempt_at
            if event.status == "held" and now < event.next_attempt_at < HELD_FOREVER
            else None
        ),
        "held_reason": event.held_reason,
        "batch_id": event.batch_id,
        "branch": summary.branch if summary else None,
        "added": summary.added if summary else None,
        "removed": summary.removed if summary else None,
        "files": [
            {"path": c.path, "kind": c.kind.value, "redacted": is_redacted(c.path, redact)}
            for c in changes
        ],
    }


def _stored_reports(reports: list[dict[str, Any]], redact: tuple[str, ...]) -> list[dict[str, Any]]:
    """The stored JSON, with any file the current redaction list covers emptied: a
    pattern added after the event still hides that file's stored lines and note (U11).
    ``first_line`` is returned only when it is valid (``stored_first_line``) and the
    file is not redacted; otherwise the key is left out."""
    out: list[dict[str, Any]] = []
    for entry in reports:
        first = stored_first_line(entry)
        entry = {key: value for key, value in entry.items() if key != "first_line"}
        if entry.get("redacted") or is_redacted(str(entry.get("path", "")), redact):
            # The note goes too: "symlink → /target" would name the link's target.
            entry = {**entry, "redacted": True, "note": None, "lines": []}
        elif first is not None:
            entry["first_line"] = first
        out.append(entry)
    return out


def _report_body(stored: StoredReport, redact: tuple[str, ...]) -> dict[str, Any]:
    """A stored report (an event's or a catch-up's) as the detail pane gets it: the
    current redaction applied and the line counts recounted from what is returned."""
    reports = _stored_reports(stored.reports, redact)
    lines = [str(line) for entry in reports for line in entry.get("lines", [])]
    return {
        "reports": reports,
        "added": sum(1 for line in lines if line.startswith("+")),
        "removed": sum(1 for line in lines if line.startswith("-")),
        "telegram_lines": stored.telegram_lines,
        "telegram_files": stored.telegram_files,
    }


def _problem_body(problem: Problem) -> dict[str, Any]:
    """A current problem for the page (health spec §6). ``notified`` false with
    ``attempts`` above 0: its alert has not reached Telegram yet."""
    return {
        "key": problem.key,
        "severity": problem.severity,
        "project": problem.project,
        "message": problem.message,
        "fix": problem.fix,
        "since": problem.opened_at,
        "notified": problem.notified_open,
        "attempts": problem.attempts,
        "last_error": problem.last_error,
    }


def _snooze_body(snooze: Snooze | None) -> dict[str, Any] | None:
    return None if snooze is None else {"since": snooze.since, "until": snooze.until}


# Shared with the wizard (telegram_setup.UNREACHABLE): one sentence for "can't reach".
TELEGRAM_UNREACHABLE = "Couldn't reach Telegram; try again."
TOKEN_REJECTED = (
    "Telegram rejected the bot token: it was probably revoked in @BotFather. Use Replace bot."
)


def _test_failure(exc: TelegramError) -> ApiError:
    """A failed test message, by kind: the card shows it, so the words say what to do.
    Only "can't reach" is an outage (503); a rejected token or a refused message is
    the request's answer (422)."""
    if exc.kind is ErrorKind.AUTH:
        return ApiError(422, TOKEN_REJECTED)
    if exc.kind is ErrorKind.RETRYABLE:
        return ApiError(503, TELEGRAM_UNREACHABLE)
    return ApiError(422, f"Telegram refused the message: {exc}")


def create_app(ctx: WebContext) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    counts = WatchedCounts(ctx.previewer, clock=ctx.clock)
    app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")

    # -- envelope, headers, host check --------------------------------------

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> Response:
        # "error" last: every ApiError body keeps it, whatever `extra` holds.
        return JSONResponse({**exc.extra, "error": exc.message}, status_code=exc.status)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        # Keep the exception's headers (e.g. Allow on a 405).
        return JSONResponse(
            {"error": str(exc.detail)}, status_code=exc.status_code, headers=exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError) -> Response:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(part) for part in first.get("loc", ()))
        return JSONResponse(
            {"error": f"invalid request: {where}: {first.get('msg', 'bad input')}"},
            status_code=400,
        )

    def refuse(status: int, message: str) -> Response:
        return JSONResponse({"error": message}, status_code=status)

    def body_size(request: Request) -> Response | None:
        """Refuse an /api body over MAX_BODY before anything reads it. The server
        enforces the declared Content-Length framing, so a body can't outgrow it;
        a body without one (chunked) has no size to check and is refused."""
        if "transfer-encoding" in request.headers:
            return refuse(411, "request body must have a Content-Length")
        length = request.headers.get("content-length")
        if length is None:
            return None
        # isdigit() alone accepts "²" (int() then raises); 20 digits bound int().
        if not (length.isascii() and length.isdigit() and len(length) <= 20):
            return refuse(400, "invalid Content-Length")
        if int(length) > MAX_BODY:
            return refuse(413, "request body too large (limit 1 MiB)")
        return None

    def check(request: Request) -> Response | None:
        """Host for every request; for /api/* the body size, a session, and for
        mutations also the Origin and CSRF checks. Runs before routing, so before
        any body is parsed."""
        if not auth.host_ok(request.headers.get("host"), ctx.port):
            return refuse(403, "forbidden host")
        if not request.url.path.startswith("/api/"):
            return None
        if (refused := body_size(request)) is not None:
            return refused
        session = auth.session_for(ctx.store, request.cookies.get(auth.SESSION_COOKIE), ctx.clock())
        if session is None:
            return refuse(401, "not logged in: run `diffbell web`")
        if request.method not in ("GET", "HEAD"):
            if not auth.origin_ok(request.headers.get("origin"), ctx.port):
                return refuse(403, "cross-origin request refused")
            if not auth.csrf_ok(session, request.headers.get(auth.CSRF_HEADER)):
                return refuse(403, "missing or wrong CSRF token")
        request.state.session = session
        return None

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        try:
            response = check(request) or await call_next(request)
        except Exception:
            # Otherwise Starlette's ServerErrorMiddleware, outside this middleware,
            # answers with a bare text 500 that skips the headers below.
            log.exception("unhandled error in %s %s", request.method, request.url.path)
            response = refuse(500, "internal error")
        response.headers.update(auth.SECURITY_HEADERS)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path.startswith("/static/"):
            # Revalidate on every load, so an upgraded page never mixes old and new files.
            response.headers["Cache-Control"] = "no-cache"
        return response

    def session_of(request: Request) -> WebSession:
        session = getattr(request.state, "session", None)
        if not isinstance(session, WebSession):  # unreachable behind `guard`
            raise ApiError(401, "not logged in: run `diffbell web`")
        return session

    def os_reason(exc: OSError) -> str:
        """The short reason ("Permission denied"), never the path."""
        return exc.strerror or type(exc).__name__

    def read_config() -> tuple[str, Config]:
        try:
            return config_edit.read(ctx.config_path)
        except ConfigInvalid as exc:
            raise ApiError(422, f"config.yaml is invalid: {exc}") from None
        except OSError as exc:
            raise ApiError(503, f"cannot read config: {os_reason(exc)}") from None

    def write_config(action: Callable[[], str]) -> dict[str, Any]:
        try:
            return {"config_version": action()}
        except ConfigConflict:
            raise ApiError(409, "config changed on disk") from None
        except ConfigInvalid as exc:
            raise ApiError(422, str(exc)) from None
        except ProjectNotFound as exc:
            raise ApiError(404, f"unknown project: {exc}") from None
        except ValidationError as exc:  # e.g. a project name with "/" in it
            raise ApiError(422, describe_validation_error(exc)) from None
        except config_edit.ConfigUnreadable as exc:
            raise ApiError(503, f"cannot read config: {os_reason(exc.error)}") from None
        except OSError as exc:
            raise ApiError(503, f"cannot write config: {os_reason(exc)}") from None

    # -- pages -----------------------------------------------------------------

    @app.get("/healthz")
    async def healthz(challenge: str | None = None) -> dict[str, Any]:
        if challenge is None:
            return {"ok": True}
        if not auth.valid_nonce(challenge):
            raise ApiError(400, "challenge must be 16-64 URL-safe characters")
        return {"ok": True, "proof": auth.prove(ctx.secret, challenge)}

    @app.get("/login", response_model=None)
    async def login(t: str = "", next_: Annotated[str, Query(alias="next")] = "") -> Response:
        redeemed = auth.redeem_login(ctx.store, t, ctx.clock(), ctx.secret)
        if redeemed is None:
            return HTMLResponse(LINK_EXPIRED, status_code=400)
        response = RedirectResponse(LOGIN_TARGETS.get(next_, "/"), status_code=303)
        response.set_cookie(
            auth.SESSION_COOKIE,
            redeemed[0],
            max_age=int(auth.SESSION_TTL),
            path="/",
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get("/", response_model=None)
    async def index(request: Request) -> Response:
        cookie = request.cookies.get(auth.SESSION_COOKIE)
        if auth.session_for(ctx.store, cookie, ctx.clock()) is None:
            return HTMLResponse(LOGIN_HINT, status_code=401)
        page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        theme = request.cookies.get(auth.THEME_COOKIE)
        if theme in auth.FORCED_THEMES:
            # Written by the server so the first paint is right: the CSP allows no
            # inline script to do it before main.js runs.
            page = page.replace('<html lang="en">', f'<html lang="en" data-theme="{theme}">', 1)
        # The page varies by cookie: never reuse a stored copy.
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    # -- session ---------------------------------------------------------------

    @app.get("/api/session")
    async def session_info(request: Request) -> dict[str, Any]:
        return {"csrf": session_of(request).csrf}

    @app.post("/api/logout", response_model=None)
    async def logout(
        request: Request, all_sessions: Annotated[bool, Query(alias="all")] = False
    ) -> Response:
        """End this session, or with ``?all=true`` every session (Settings)."""
        if all_sessions:
            ctx.store.delete_sessions()
        else:
            auth.end_session(ctx.store, request.cookies.get(auth.SESSION_COOKIE))
        response = JSONResponse({"ok": True})
        response.delete_cookie(auth.SESSION_COOKIE, path="/")
        return response

    # -- dashboard and activity --------------------------------------------------

    @app.get("/api/dashboard")
    async def dashboard() -> dict[str, Any]:
        snap = ctx.snapshot()
        web = snap.get("web") or {}
        rows = [r for r in snap.get("projects", []) if isinstance(r, dict)]
        names = {str(r.get("name")) for r in rows}  # watched and missing projects
        last_ok, last_error = ctx.store.telegram_health()
        last_change = ctx.store.last_event_times()
        config, resolved, _ = ctx.current()
        by_name = {p.name: p for p in resolved}
        redact = redact_patterns(config)
        now = ctx.clock()
        snoozes = ctx.store.active_snoozes(now)
        window = config.notify.quiet_hours
        quiet_ends = quiet_end(QuietHours.parse(window), now) if window is not None else None

        counts.retain(set(by_name))
        problems = current_problems(ctx.store.all_problems())
        # A project whose watch is lost (its `project:<name>` problem is open) shows no
        # count, and none is started until the problem is resolved (spec §4.5): a
        # cached number, or one from a walk in flight, would be stale.
        lost = {p.key.partition(":")[2] for p in problems if p.key.startswith("project:")}
        configured = {project_name(p): tuple(p.notify_on) for p in config.projects}

        def notify_on(name: object) -> list[str]:
            """The resolved project's rules; while its folder is missing (it is then
            not resolved) the configured ones; every kind for an unknown name."""
            project = by_name.get(str(name))
            if project is not None:
                return list(project.notify_on)
            return list(configured.get(str(name), NOTIFY_KINDS))

        def folder(name: object) -> str | None:
            """The project's absolute folder (the page builds editor links and git
            commands from it), or None while it is not resolved."""
            project = by_name.get(str(name))
            return None if project is None else str(project.path)

        configured_names = [project_name(p) for p in config.projects]
        activity = ctx.store.activity(now, configured_names, tz=ctx.tz)

        def watched(name: object) -> dict[str, Any]:
            project = by_name.get(str(name))
            if str(name) in lost:
                counts.forget(str(name))
                project = None
            count = None if project is None else counts.get(project, redact)
            return {
                "watched_count": None if count is None else count.total,
                "watched_exact": None if count is None else count.exact,
            }

        return {
            "daemon": {
                "pid": snap.get("pid"),
                "connected": bool(snap.get("connected")),
                # sha256 of the config bytes the daemon last applied / rejected: the
                # editor compares them with the version its save returned.
                "config_version": snap.get("config_version"),
                "config_rejected_version": snap.get("config_rejected_version"),
                # The web runs in the daemon: the version serving this page (the footer).
                "version": __version__,
            },
            "watchman": {"version": snap.get("watchman_version")},
            "web": {
                "running": bool(web.get("running")),
                "port": web.get("port"),
                "last_error": web.get("last_error"),
                # The "Open" links' editor (Plan 7 spec §2.4): the page needs it
                # without a visit to Settings.
                "editor": config.web.editor,
            },
            # `configured` drives the page's setup banner (bot onboarding spec §3).
            "telegram": {
                "configured": ctx.configured(),
                "last_ok": last_ok,
                "last_error": last_error,
            },
            "projects": [
                {
                    "name": r.get("name"),
                    "path": r.get("path"),
                    "status": r.get("status"),
                    "error": r.get("error"),
                    "note": r.get("note"),
                    "last_change_at": last_change.get(str(r.get("name"))),
                    "snooze": _snooze_body(snoozes.get(str(r.get("name")))),
                    "notify_on": notify_on(r.get("name")),
                    "folder": folder(r.get("name")),
                    **watched(r.get("name")),
                }
                for r in rows
                if r.get("status") != "missing"
            ],
            "missing": [
                {
                    "name": r.get("name"),
                    "path": r.get("path"),
                    # A snoozed project whose folder went missing can still be resumed.
                    "snooze": _snooze_body(snoozes.get(str(r.get("name")))),
                    "notify_on": notify_on(r.get("name")),
                }
                for r in rows
                if r.get("status") == "missing"
            ],
            "counts": snap.get("counts", {}),
            "quiet": {"window": window, "active": quiet_ends is not None, "ends_at": quiet_ends},
            # A removed project's rows stay until the daemon's next reload forgets
            # them: the page ignores them already.
            "problems": [
                _problem_body(p) for p in problems if p.project is None or p.project in names
            ],
            # 14 local days per configured project, oldest first (Plan 7 spec §2.2).
            "activity": {
                name: [
                    {
                        "day": d.day.isoformat(),
                        "since": d.since,
                        "until": d.until,
                        "total": d.total,
                        "failed": d.failed,
                    }
                    for d in days
                ]
                for name, days in activity.items()
            },
        }

    @app.get("/api/events")
    async def events(
        project: QueryText = None,
        # "attention": failed and held (Plan 7 spec §2.3).
        status: Literal["pending", "sent", "failed", "held", "skipped", "attention"] | None = None,
        since: float | None = None,
        until: float | None = None,
        q: QueryText = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
        offset: Annotated[int, Query(ge=0, le=MAX_INT)] = 0,
        before_id: Annotated[int | None, Query(ge=1, le=MAX_INT)] = None,
    ) -> dict[str, Any]:
        # Today's redaction list: the same tuple /api/events/{id} applies, so the diff
        # search never matches a file whose lines the detail would hide.
        redact = redact_patterns(ctx.current()[0])
        rows, total = ctx.store.query_events(
            project=project or None,
            status=status,
            since=since,
            until=until,
            q=q or None,
            redact=redact,
            limit=limit,
            offset=offset,
            before_id=before_id,
        )
        now = ctx.clock()
        return {
            "events": [
                {
                    **_event_row(r.event, r.changes, r.summary, redact, now),
                    "q_in_diff": r.q_in_diff,
                }
                for r in rows
            ],
            "total": total,
        }

    @app.get("/api/events/{event_id}")
    async def event_detail(
        event_id: Annotated[int, PathParam(ge=1, le=MAX_INT)],
    ) -> dict[str, Any]:
        """One event with its stored report: the redacted diff the notification was
        built from (null when there is none: ``reports_state`` says why). An event
        sent in a catch-up also carries that message's merged report."""
        try:
            event = ctx.store.get_event(event_id)
        except KeyError:
            raise ApiError(404, f"unknown event {event_id}") from None
        stored = ctx.store.get_event_report(event_id)
        redact = redact_patterns(ctx.current()[0])
        body = None if stored is None else _report_body(stored, redact)
        summary = None
        if stored is not None and body is not None:
            # Counted from what is returned: lines hidden since the event don't count.
            summary = ReportSummary(stored.branch, body["added"], body["removed"])
        now = ctx.clock()
        row = _event_row(event, ctx.store.event_changes(event_id), summary, redact, now)
        row["reports"] = None if body is None else body["reports"]
        row["telegram_lines"] = None if stored is None else stored.telegram_lines
        row["telegram_files"] = None if stored is None else stored.telegram_files
        row["reports_state"] = reports_state(stored is not None, event.created_at, now)
        batch = None if event.batch_id is None else ctx.store.get_batch_report(event.batch_id)
        row["batch_size"] = None if event.batch_id is None else ctx.store.batch_size(event.batch_id)
        row["batch_report"] = None if batch is None else _report_body(batch, redact)
        return row

    @app.post("/api/events/{event_id}/retry")
    async def retry(event_id: Annotated[int, PathParam(ge=1, le=MAX_INT)]) -> dict[str, Any]:
        try:
            ctx.store.get_event(event_id)
        except KeyError:
            raise ApiError(404, f"unknown event {event_id}") from None
        if not ctx.store.retry_event(event_id, ctx.clock()):
            raise ApiError(409, f"event {event_id} is not failed")
        return {"ok": True}

    # -- snooze (spec §5.1) ----------------------------------------------------

    @app.post("/api/projects/{name}/snooze")
    async def snooze(
        name: Annotated[str, PathParam(max_length=MAX_TEXT)], body: SnoozeBody
    ) -> dict[str, Any]:
        """Pause one project's notifications until ``until`` (null: until turned
        back on). Changes made meanwhile are held and sent as one catch-up."""
        if name not in {project_name(p) for p in ctx.current()[0].projects}:
            raise ApiError(404, "unknown project")
        now = ctx.clock()
        if body.until is not None and body.until <= now:
            raise ApiError(422, "the snooze must end in the future")
        if body.until is not None and body.until > now + MAX_SNOOZE:
            raise ApiError(422, "a snooze can last at most 30 days")
        try:
            snooze = ctx.store.set_snooze(name, now, body.until)
        except UnknownProject:  # configured, but the daemon's reload hasn't synced it yet
            raise ApiError(404, "unknown project") from None
        return {"snooze": _snooze_body(snooze)}

    @app.delete("/api/projects/{name}/snooze", status_code=204, response_model=None)
    async def unsnooze(name: Annotated[str, PathParam(max_length=MAX_TEXT)]) -> Response:
        """Turn notifications back on; what was held is sent at the next check.
        Idempotent, and also for a name no longer configured."""
        ctx.store.delete_snooze(name, ctx.clock())
        return Response(status_code=204)

    @app.post("/api/test-notification")
    async def test_notification() -> dict[str, Any]:
        if not ctx.configured():
            raise ApiError(409, NOT_SET_UP)
        try:
            await ctx.send_test()
        except NotSetUp:  # the slot was emptied between the check and the send
            raise ApiError(409, NOT_SET_UP) from None
        except TelegramError as exc:  # its message never contains the token
            raise _test_failure(exc) from None
        except Exception as exc:
            raise ApiError(503, f"test notification failed: {type(exc).__name__}") from None
        return {"ok": True}

    # -- projects ----------------------------------------------------------------

    @app.get("/api/projects")
    async def projects() -> dict[str, Any]:
        version, config = read_config()
        statuses = {
            str(r.get("name")): r for r in ctx.snapshot().get("projects", []) if isinstance(r, dict)
        }
        out: list[dict[str, Any]] = []
        for p in config.projects:
            name = project_name(p)
            watched, excluded = effective_lists(config, p)
            row = statuses.get(name, {})
            out.append(
                {
                    "name": name,
                    "path": p.path,
                    "patterns": p.patterns,
                    "ignore": p.ignore,
                    "use_defaults": p.use_defaults,
                    "notify_on": list(p.notify_on),
                    "watched": list(watched),
                    "excluded": list(excluded),
                    "status": row.get("status", "unknown"),
                    "error": row.get("error"),
                }
            )
        return {"config_version": version, "projects": out}

    @app.post("/api/projects")
    async def add_project(body: ProjectBody) -> dict[str, Any]:
        return write_config(
            lambda: config_edit.add_project(ctx.config_path, body.config_version, body.to_input())
        )

    @app.put("/api/projects/{name}")
    async def edit_project(
        name: Annotated[str, PathParam(max_length=MAX_TEXT)], body: ProjectBody
    ) -> dict[str, Any]:
        return write_config(
            lambda: config_edit.edit_project(
                ctx.config_path, body.config_version, name, body.to_input()
            )
        )

    @app.delete("/api/projects/{name}")
    async def remove_project(
        name: Annotated[str, PathParam(max_length=MAX_TEXT)],
        config_version: Annotated[str, Query(max_length=MAX_TEXT)],
    ) -> dict[str, Any]:
        return write_config(
            lambda: config_edit.remove_project(ctx.config_path, config_version, name)
        )

    @app.post("/api/preview")
    async def preview(body: PreviewBody) -> dict[str, Any]:
        req = PreviewRequest(body.path, body.patterns, body.ignore, body.use_defaults)
        try:
            result = await ctx.previewer.preview(req, ctx.current()[0])
        except PreviewError as exc:
            raise ApiError(422, str(exc)) from None
        return {
            "files": [{"path": f.path, "redacted": f.redacted} for f in result.files],
            "total": result.total,
            "truncated": result.truncated,
            "total_exact": result.total_exact,
        }

    @app.post("/api/browse")
    async def browse_folder(body: BrowseBody) -> dict[str, Any]:
        """One folder of the editor's file tree: names and match badges, never contents."""
        req = BrowseRequest(body.path, body.dir, body.patterns, body.ignore, body.use_defaults)
        previewer = ctx.previewer
        try:
            result = await previewer.in_slot(browse.run, req, ctx.current()[0], previewer.home)
        except PreviewError as exc:  # the path or a pattern, as in the preview
            raise ApiError(422, str(exc)) from None
        except BrowseError as exc:
            raise ApiError(400, str(exc)) from None
        return {
            "dir": result.dir,
            "entries": [
                {
                    "name": e.name,
                    "type": e.type,
                    "redacted": e.redacted,
                    "excluded": e.excluded,
                    "matched_by": e.matched_by,
                    "ignored_by": e.ignored_by,
                }
                for e in result.entries
            ],
            "truncated": result.truncated,
            "error": result.error,
        }

    # -- settings ----------------------------------------------------------------

    @app.get("/api/settings")
    async def settings() -> dict[str, Any]:
        version, config = read_config()
        # The Settings field's placeholder: scutil may take a moment, off the loop.
        detected = await asyncio.to_thread(machine.default_name)
        return {
            "config_version": version,
            "debounce": {
                "quiet_seconds": config.debounce.quiet_seconds,
                "max_wait_seconds": config.debounce.max_wait_seconds,
            },
            "diff": {"max_lines": config.diff.max_lines},
            "defaults": config.defaults,
            "ignore": config.ignore,
            "redact": config.redact,
            "builtin_defaults": list(BUILTIN_DEFAULTS),
            "builtin_redact": list(BUILTIN_REDACT),
            "always_excluded": list(ALWAYS_EXCLUDED_DIRS),
            "web": {"port": config.web.port, "editor": config.web.editor},
            "notify": {
                "quiet_hours": config.notify.quiet_hours,
                "heartbeat": config.notify.heartbeat,
                "machine": config.notify.machine,
                "machine_default": detected,
            },
        }

    @app.post("/api/settings/check")
    async def check_settings_body(body: SettingsCheckBody) -> dict[str, Any]:
        """The same validation as PUT /api/settings, writing nothing (settings
        redesign spec §3): {"errors", "issues"}, both empty when valid."""
        return check_settings(body.raw()).body()

    @app.put("/api/settings")
    async def update_settings(body: SettingsBody) -> dict[str, Any]:
        raw = body.raw()
        result = check_settings(raw)
        if not result.ok:
            # Additive: "error" as in every ApiError body, plus what the view reads.
            raise ApiError(422, result.summary(), extra=result.body())
        quiet_seconds, max_wait_seconds, max_lines = settings_values(raw)
        new = SettingsInput(
            quiet_seconds=quiet_seconds,
            max_wait_seconds=max_wait_seconds,
            max_lines=max_lines,
            defaults=body.defaults,
            ignore=body.ignore,
            redact=body.redact,
            notify=None
            if body.notify is None
            else NotifyInput(
                body.notify.quiet_hours,
                body.notify.heartbeat if "heartbeat" in body.notify.model_fields_set else KEEP,
                body.notify.machine if "machine" in body.notify.model_fields_set else KEEP,
            ),
            editor=KEEP if body.web is None else body.web.editor,
        )
        return write_config(
            lambda: config_edit.update_settings(ctx.config_path, body.config_version, new)
        )

    # -- Settings → Telegram (bot onboarding spec §2) ----------------------------

    # Imported here: telegram_setup imports ApiError and WebContext from this module.
    from diffbell.web import telegram_setup

    telegram_setup.register(app, ctx, telegram_setup.Drafts(clock=ctx.clock))
    return app
