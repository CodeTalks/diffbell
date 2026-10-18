// Settings (settings redesign spec §1-§5): five sections with an index, the rows of
// Notifications and Schedule, the pattern lists and This page, and one save bar for
// everything PUT /api/settings writes. The server's check (POST /api/settings/check)
// is the only judge of what is valid: the page compares, sends and shows, and never
// re-implements a rule. Telegram saves through its own wizard (telegram.js); the
// theme applies at once (main.js).
import { api, postSettingsCheck } from "./api.js";
import { $, h, icon, setAttr, setClass, setHidden, setMessage, setText } from "./dom.js";
import { markLines, saveOutcome, SESSION_ENDED, wrote } from "./editor.js";
import {
  barState,
  dirtyFields,
  HEARTBEAT_DEFAULT,
  hasErrors,
  heartbeatPreview,
  lineIssuesFor,
  listLines,
  machineFields,
  machineValue,
  quietPreview,
  savedValues,
  spySection,
  settingsBody,
} from "./settings-model.js";
import { state } from "./state.js";
import { clock } from "./time.js";

const SAVE_POLLS = 3;
export const CHECK_MS = 300; // after the last edit, the server checks (spec §3)
const APPLIED_MS = 4000; // "applied" or "saved", then the bar hides (spec §2)

const deps = { loggedOut: () => {}, now: () => Date.now(), scrollY: () => 0 };

export function initSettings(options) {
  Object.assign(deps, options);
}

// -- sections (spec §1) ----------------------------------------------------------------

// ?section=<id>: the box to scroll to and the heading that takes focus (focusable by
// script, not a Tab stop), as the Telegram card's did before. The ids are in
// index.html (tests/web/test_static.py).
export const SECTION_TARGETS = {
  telegram: ["tg-card", "tg-title"],
  notifications: ["s-notifications", "s-notifications-title"],
  schedule: ["s-schedule", "s-schedule-title"],
  patterns: ["s-patterns", "s-patterns-title"],
  page: ["s-page", "s-page-title"],
};

export function focusSection(section) {
  const target = SECTION_TARGETS[section];
  if (!target) return;
  const [box, heading] = target.map((id) => $(id));
  box.scrollIntoView();
  setAttr(heading, "tabindex", "-1");
  heading.focus();
  // The clicked section stays marked until the page scrolls on: a short last
  // section can't reach the spy's line, and the scroll-spy must not overrule it.
  held = { section, y: deps.scrollY() };
  markSection(section);
}

// The index marks the section in view (aria-current; app.css styles it).
export function markSection(section) {
  for (const s of Object.keys(SECTION_TARGETS)) {
    setAttr($(`s-index-${s}`), "aria-current", s === section ? "true" : "false");
  }
}

// The spy's line, in px from the top of the viewport: under the sticky header
// (65 px), with a margin, so a section counts once its heading is in view.
export const SPY_LINE = 96;
let held = null;

// On scroll (main.js, once per frame) and when the view opens: mark the section in
// view. `y`: the page's scroll offset; `atBottom`: scrolled to the end.
export function spySections({ y, atBottom }) {
  if (held && Math.abs(y - held.y) < 4) {
    markSection(held.section);
    return;
  }
  held = null;
  // The rect's key, read by string: a bare `.top` would read as the browser global
  // (tests/web/test_static.py test_only_main_names_the_browser_globals).
  const starts = Object.entries(SECTION_TARGETS).map(([s, [box]]) => [s, $(box).getBoundingClientRect()["top"]]);
  markSection(spySection(starts, SPY_LINE, atBottom));
}

// -- the built-in lists (spec §5) --------------------------------------------------------

function chips(list) {
  return list.map((p) => h("li", {}, icon("lock"), p));
}

// The disclosure under each pattern list (spec §5) and the editor's "What to watch":
// entries and counts only from GET /api/settings, never from the page.
export function renderBuiltins(s) {
  const excluded = s.always_excluded || [];
  $("b-defaults").replaceChildren(...chips(s.builtin_defaults));
  $("b-redact").replaceChildren(...chips(s.builtin_redact));
  $("b-excluded").replaceChildren(...chips(excluded));
  setText($("b-defaults-title"), `Always watched (built in, ${s.builtin_defaults.length})`);
  setText($("b-excluded-title"), `Always excluded folders (built in, ${excluded.length})`);
  setText($("b-redact-title"), `Always redacted (built in, ${s.builtin_redact.length})`);
  $("e-builtin-list").replaceChildren(...chips(s.builtin_defaults));
  setText($("e-mode-defaults-label"), `Key files (${s.builtin_defaults.length} built-in) + my additions`);
}

// The times a switch shows when the config has none (spec §4).
const QUIET_DEFAULT = ["23:00", "08:00"];

// The server's check (spec §3): {errors, issues}, both empty when valid.
export async function checkSettings(body) {
  const result = await postSettingsCheck(body);
  return { errors: (result && result.errors) || {}, issues: (result && result.issues) || {} };
}

// -- the form ----------------------------------------------------------------------------

// Each field the server's check names (spec §3) and its control; the message goes to
// `<control>-error`, which the control's aria-describedby names. In this order the
// first problem takes focus.
const FIELD_INPUTS = {
  quiet_seconds: "s-quiet",
  max_wait_seconds: "s-maxwait",
  max_lines: "s-lines",
  quiet_from: "s-qh-from",
  quiet_to: "s-qh-to",
  heartbeat: "s-hb-at",
  machine: "s-machine-name",
};
const LIST_AREAS = { defaults: "s-defaults", ignore: "s-ignore", redact: "s-redact" };

// The fields as shown, in the model. A time input can report seconds ("22:30:15"):
// the config takes HH:MM. A switch that is off keeps its times on the page (spec §4).
export function readValues() {
  const time = (id) => $(id).value.slice(0, 5);
  return {
    quiet_seconds: $("s-quiet").value,
    max_wait_seconds: $("s-maxwait").value,
    max_lines: $("s-lines").value,
    quiet_hours: $("s-qh-on").checked ? `${time("s-qh-from")}-${time("s-qh-to")}` : null,
    heartbeat: $("s-hb-on").checked ? time("s-hb-at") : null,
    machine: machineValue($("s-machine-on").checked, $("s-machine-name").value),
    defaults: listLines($("s-defaults").value),
    ignore: listLines($("s-ignore").value),
    redact: listLines($("s-redact").value),
    editor: $("s-editor").value,
  };
}

function fillValues(values) {
  const [from, to] = values.quiet_hours ? values.quiet_hours.split("-") : QUIET_DEFAULT;
  $("s-quiet").value = values.quiet_seconds;
  $("s-maxwait").value = values.max_wait_seconds;
  $("s-lines").value = values.max_lines;
  $("s-qh-on").checked = values.quiet_hours !== null;
  $("s-qh-from").value = from;
  $("s-qh-to").value = to;
  $("s-hb-on").checked = values.heartbeat !== null;
  $("s-hb-at").value = values.heartbeat || HEARTBEAT_DEFAULT;
  const named = machineFields(values.machine);
  $("s-machine-on").checked = named.on;
  $("s-machine-name").value = named.text;
  $("s-defaults").value = values.defaults.join("\n");
  $("s-ignore").value = values.ignore.join("\n");
  $("s-redact").value = values.redact.join("\n");
  $("s-editor").value = values.editor;
  syncSwitches();
}

// A switch that is on reveals its times and preview (spec §4); off hides them.
function syncSwitches() {
  setHidden($("s-qh-times"), !$("s-qh-on").checked);
  setHidden($("s-hb-times"), !$("s-hb-on").checked);
  setHidden($("s-machine-field"), !$("s-machine-on").checked);
}

// Also what Reload runs (the conflict state): the file as it is now.
// A config the server can't read or validate: the form's fields would be empty or
// stale, so it is hidden and the notice at the top says why (Try again; the poll
// retries too, retrySettingsLoad).
function showLoadError(message) {
  state.settingsLoadError = message;
  setText($("s-load-error-text"), message ? `Settings can't be shown. ${message}` : "");
  setHidden($("s-load-error"), !message);
  setHidden($("settings"), Boolean(message));
  // The index links only to what shows: the sections in the form go with it.
  for (const section of Object.keys(SECTION_TARGETS)) {
    if (section !== "telegram") setHidden($(`s-index-${section}`), Boolean(message));
  }
}

let loading = null;

export function loadSettings() {
  loading ||= fetchSettings().finally(() => {
    loading = null;
  });
  return loading;
}

// Every dashboard poll, while the last load failed and Settings shows.
export function retrySettingsLoad() {
  if (state.settingsLoadError && state.view === "settings" && !loading) loadSettings();
}

async function fetchSettings() {
  let s;
  try {
    s = await api("GET", "/api/settings");
  } catch (err) {
    if (err.status !== 401) showLoadError(err.message); // a 401 ends the session instead
    return;
  }
  showLoadError("");
  state.settings = s;
  state.settingsVersion = s.config_version;
  state.settingsSaved = savedValues(s);
  state.settingsCheck = null;
  state.settingsCheckSeq += 1; // an answer about the fields before this load is dropped
  setOutcome(null);
  $("s-port").value = String(s.web.port);
  // The detected Computer Name: what an empty field stands for (ADR 0023).
  const detected = s.notify && typeof s.notify.machine_default === "string" ? s.notify.machine_default : "";
  setAttr($("s-machine-name"), "placeholder", detected);
  fillValues(state.settingsSaved);
  setMessage($("s-message"), "");
  renderBuiltins(s);
  renderChecked();
  renderSettings();
}

// The controls of the fields PUT /api/settings writes (FIELDS, readValues). Only an
// edit in one of them is the form's: input and change bubble to #settings from
// anything inside it, and the theme radios (applied at once, spec §2) and the
// read-only port are never checked, counted or saved.
const SAVED_CONTROLS = [
  "s-quiet",
  "s-maxwait",
  "s-lines",
  "s-qh-on",
  "s-qh-from",
  "s-qh-to",
  "s-hb-on",
  "s-hb-at",
  "s-machine-on",
  "s-machine-name",
  "s-defaults",
  "s-ignore",
  "s-redact",
  "s-editor",
];

// Any edit in the form (main.js: input and change on #settings). An edit hides
// "applied" / "saved" / "rejected" and stops waiting for them; "saving" and a conflict stay.
export function settingsEdited(event) {
  const target = event && event.target;
  if (!target || !SAVED_CONTROLS.includes(target.id)) return;
  if (!state.settingsSaved) return;
  if (["waiting", "applied", "saved", "rejected"].includes(state.settingsOutcome)) setOutcome(null);
  syncSwitches();
  renderChecked();
  renderSettings();
  scheduleCheck();
}

// Unsaved edits (what beforeunload asks about).
export function settingsDirty() {
  return state.settingsSaved !== null && dirtyFields(state.settingsSaved, readValues()).length > 0;
}

// The last check's messages: under each field (with aria-invalid), the gutter marks
// and issue lists of the three lists, and a message about a field the page has no
// control for, so nothing the server says is lost.
function renderChecked() {
  const check = state.settingsCheck || { errors: {}, issues: {} };
  const errors = check.errors || {};
  for (const [field, id] of Object.entries(FIELD_INPUTS)) {
    const message = errors[field] || "";
    setText($(`${id}-error`), message);
    setAttr($(id), "aria-invalid", message ? "true" : "false");
  }
  for (const [list, id] of Object.entries(LIST_AREAS)) {
    const issues = lineIssuesFor($(id).value, (check.issues || {})[list]);
    markLines(id, issues);
    setAttr($(id), "aria-invalid", issues.length ? "true" : "false");
  }
  const other = Object.entries(errors).filter(([field]) => !(field in FIELD_INPUTS));
  setText($("s-other-error"), other.map(([, message]) => message).join("\n"));
}

// What follows the fields, the check and the dashboard poll: the two previews and
// the save bar.
export function renderSettings() {
  if (!state.settingsSaved) return;
  const values = readValues();
  const changed = dirtyFields(state.settingsSaved, values);
  const errors = (state.settingsCheck && state.settingsCheck.errors) || {};
  const quiet = state.dash && state.dash.quiet;
  const now = clock(deps.now() / 1000);
  const qh = values.quiet_hours;
  const draft = errors.quiet_from || errors.quiet_to ? null : qh;
  setText($("s-qh-preview"), qh === null ? "" : quietPreview(quiet, changed.includes("quiet_hours"), draft, now));
  const at = errors.heartbeat ? null : values.heartbeat;
  setText($("s-hb-preview"), heartbeatPreview(now, at));
  renderBar(changed.length);
}

// A save that can't go: focus moves to the first field with a problem.
export function focusFirstError() {
  const check = state.settingsCheck || { errors: {}, issues: {} };
  const errors = check.errors || {};
  const issues = check.issues || {};
  const field = Object.keys(FIELD_INPUTS).find((f) => errors[f]);
  if (field) {
    $(FIELD_INPUTS[field]).focus();
    return;
  }
  const list = Object.keys(LIST_AREAS).find((l) => (issues[l] || []).length > 0);
  if (list) $(LIST_AREAS[list]).focus();
}

// -- the server's check (spec §3) ---------------------------------------------------------

let checkTimer = null;
let checking = false; // one check in flight at a time
let again = false; // an edit's check came due meanwhile: run it when this one returns

// CHECK_MS after the last edit. Every edit bumps the sequence number, so an answer
// about older fields is dropped.
export function scheduleCheck() {
  state.settingsCheckSeq += 1;
  clearTimeout(checkTimer);
  checkTimer = setTimeout(runCheck, CHECK_MS);
}

export async function runCheck() {
  if (checking) {
    again = true;
    return;
  }
  checking = true;
  const seq = state.settingsCheckSeq;
  let result = null;
  try {
    result = await checkSettings(settingsBody(readValues()));
  } catch {
    // Unreachable or refused: no answer. The last one was about older fields, and a
    // problem fixed since must not block Save, so it goes: the PUT's own check (a 422)
    // decides.
    result = null;
  }
  checking = false;
  if (again) {
    again = false;
    runCheck();
    return;
  }
  if (seq === state.settingsCheckSeq) applyCheck(result);
}

export function applyCheck(result) {
  state.settingsCheck = result;
  renderChecked();
  renderSettings();
}

// -- the save bar (spec §2) -------------------------------------------------------------------

let appliedTimer = null;

const BAR_BUTTONS = { reload: "s-reload", discard: "s-discard", save: "s-save" };

function renderBar(count) {
  const bar = barState({ count, invalid: hasErrors(state.settingsCheck), outcome: state.settingsOutcome });
  const box = $("s-bar");
  setHidden(box, bar.state === "hidden");
  setClass(box, bar.class);
  setText($("s-bar-text"), bar.text);
  for (const [action, id] of Object.entries(BAR_BUTTONS)) setHidden($(id), !bar.actions.includes(action));
  // aria-disabled, not disabled: a click still answers, by focusing the first problem.
  setAttr($("s-save"), "aria-disabled", bar.state === "dirty-invalid" ? "true" : "false");
}

// "applied" and "saved" hide by themselves after APPLIED_MS; every other outcome waits
// for an edit, a save, Discard or Reload.
function setOutcome(outcome) {
  clearTimeout(appliedTimer);
  state.settingsOutcome = outcome;
  if (outcome !== "applied" && outcome !== "saved") return;
  appliedTimer = setTimeout(() => {
    if (state.settingsOutcome === outcome) state.settingsOutcome = null;
    renderSettings();
  }, APPLIED_MS);
}

// Save changes (the bar's button, Enter in a field, the keyboard's save): only changed
// fields the last check found no problem in. Telegram and the theme are never part of it.
export async function saveSettings(event) {
  if (event) event.preventDefault();
  if (!state.settingsSaved || state.settingsOutcome === "saving" || state.settingsOutcome === "conflict") return;
  const values = readValues();
  if (dirtyFields(state.settingsSaved, values).length === 0) return;
  if (hasErrors(state.settingsCheck)) {
    focusFirstError();
    return;
  }
  const body = { config_version: state.settingsVersion, ...settingsBody(values) };
  setMessage($("s-message"), "");
  setOutcome("saving");
  renderSettings();
  let result;
  try {
    result = await api("PUT", "/api/settings", body);
  } catch (err) {
    saveFailed(err);
    renderSettings();
    return;
  }
  wrote(body.config_version, result.config_version);
  state.settingsSaved = values;
  state.settingsNotice = { version: result.config_version, polls: 0 };
  // Only while still "saving": a Discard or Reload meanwhile has the last word.
  if (state.settingsOutcome === "saving") setOutcome("waiting");
  renderSettings();
}

// 409: the file changed on disk (the conflict state). 422: the server's messages under
// their fields. Anything else (the session ended, the daemon unavailable) is said in
// the line above the bar; the edits stay.
function saveFailed(err) {
  if (err.status === 409) {
    setOutcome("conflict");
    return;
  }
  setOutcome(null);
  if (err.status === 422 && err.data && (err.data.errors || err.data.issues)) {
    applyCheck({ errors: err.data.errors || {}, issues: err.data.issues || {} });
    focusFirstError();
  } else if (err.status === 401) {
    setMessage($("s-message"), SESSION_ENDED, "error");
  } else {
    setMessage($("s-message"), err.message, "error");
  }
}

// After a save, on each dashboard poll: the saved config_version applied or rejected
// (saveOutcome, as for a project). Past SAVE_POLLS without either, the save still
// went through: the bar says so ("saved").
export function reportSettingsSave() {
  const notice = state.settingsNotice;
  if (!notice) return;
  notice.polls += 1;
  const { applied, rejected } = saveOutcome(state.dash.daemon || {}, notice);
  if (!applied && !rejected && notice.polls < SAVE_POLLS) return;
  state.settingsNotice = null;
  if (state.settingsOutcome === "waiting") setOutcome(applied ? "applied" : rejected ? "rejected" : "saved");
  renderSettings();
}

// Discard while dirty: the fields as last loaded or saved, no messages. In a conflict
// the saved values and their version are stale: Discard reloads, as Reload does.
export function discardSettings() {
  if (state.settingsOutcome === "conflict") return loadSettings();
  if (!state.settingsSaved) return;
  clearTimeout(checkTimer);
  fillValues(state.settingsSaved);
  state.settingsCheck = null;
  state.settingsCheckSeq += 1;
  setOutcome(null);
  setMessage($("s-message"), "");
  renderChecked();
  renderSettings();
}

// -- the sessions ------------------------------------------------------------------------------

// "Log out all sessions" asks inline first (U9): it ends this browser's session
// too, so there would be nothing left to click Undo in.
export function askLogout() {
  setHidden($("s-logout"), true);
  setHidden($("s-logout-confirm"), false);
  $("s-logout-yes").focus();
}

export function cancelLogout() {
  setHidden($("s-logout-confirm"), true);
  setHidden($("s-logout"), false);
  $("s-logout").focus();
}

export async function logoutAll() {
  try {
    await api("POST", "/api/logout?all=true");
  } catch (err) {
    if (err.status !== 401) setMessage($("s-page-message"), err.message, "error");
    cancelLogout();
    return;
  }
  deps.loggedOut();
}
