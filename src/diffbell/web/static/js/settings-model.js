// The settings model (settings redesign spec §2-§4): pure, so node tests it without a
// page. What the save bar covers, the values as compared and sent, the bar's states
// and the two previews. settings.js reads the fields, calls the server and renders.
import { clock } from "./time.js";

// "Open files in" (Plan 7 spec §3.5): the editor the file headers' Open links use.
export const EDITORS = ["cursor", "vscode", "none"];

// The heartbeat time a switch shows when the config has none (spec §4).
export const HEARTBEAT_DEFAULT = "09:00";

// What the save bar covers (spec §2): exactly the fields PUT /api/settings writes.
// Quiet hours and the heartbeat are one field each ("HH:MM-HH:MM" / "HH:MM", or null
// when off), and so is this Mac's name (`machineValue`). Telegram, the theme and the
// sessions are never among them.
export const FIELDS = [
  "quiet_seconds",
  "max_wait_seconds",
  "max_lines",
  "quiet_hours",
  "heartbeat",
  "machine",
  "defaults",
  "ignore",
  "redact",
  "editor",
];

// A pattern list as compared (spec §2): its lines as typed, without the trailing
// empty ones.
export function listLines(text) {
  const out = String(text).split("\n");
  while (out.length && out[out.length - 1].trim() === "") out.pop();
  return out;
}

// GET /api/settings in the model: numbers as text, as the fields show them. A server
// without `heartbeat` reports undefined: the default time, on.
export function savedValues(s) {
  const notify = s.notify || {};
  const editor = s.web && s.web.editor;
  return {
    quiet_seconds: String(s.debounce.quiet_seconds),
    max_wait_seconds: String(s.debounce.max_wait_seconds),
    max_lines: String(s.diff.max_lines),
    quiet_hours: notify.quiet_hours || null,
    heartbeat: notify.heartbeat === undefined ? HEARTBEAT_DEFAULT : notify.heartbeat,
    machine: typeof notify.machine === "string" ? notify.machine : null,
    defaults: [...s.defaults],
    ignore: [...s.ignore],
    redact: [...s.redact],
    editor: EDITORS.includes(editor) ? editor : "cursor",
  };
}

function same(a, b) {
  if (Array.isArray(a) || Array.isArray(b)) {
    return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((line, i) => line === b[i]);
  }
  return a === b;
}

// The fields whose current value differs from the saved one, text as typed.
export function dirtyFields(saved, current) {
  if (!saved || !current) return [];
  return FIELDS.filter((field) => !same(saved[field], current[field]));
}

// The body of POST /api/settings/check and PUT /api/settings (without the version):
// numbers as the raw text typed (the server parses them; empty is never 0), each
// list's non-empty lines trimmed.
export function settingsBody(values) {
  const sent = (list) => list.map((line) => line.trim()).filter(Boolean);
  return {
    debounce: { quiet_seconds: values.quiet_seconds, max_wait_seconds: values.max_wait_seconds },
    diff: { max_lines: values.max_lines },
    defaults: sent(values.defaults),
    ignore: sent(values.ignore),
    redact: sent(values.redact),
    notify: { quiet_hours: values.quiet_hours, heartbeat: values.heartbeat, machine: values.machine },
    web: { editor: values.editor },
  };
}

// This Mac's name (ADR 0023) as notify.machine holds it: "" names no Mac (the switch
// off), null is the Computer Name (on with an empty field), else the text, trimmed.
export function machineValue(on, text) {
  if (!on) return "";
  return String(text).trim() || null;
}

// The switch and the field for a notify.machine value (machineValue's inverse).
export function machineFields(value) {
  return { on: value !== "", text: value || "" };
}

// The server numbers a list's issues by the lines it was sent (settingsBody: the
// non-empty ones, from 1): back to the textarea's own lines, for the gutter. An issue
// past the end (an answer about older text) is dropped.
export function lineIssuesFor(text, issues) {
  const rows = [];
  String(text)
    .split("\n")
    .forEach((line, index) => {
      if (line.trim()) rows.push(index);
    });
  return (issues || [])
    .filter((issue) => issue.line >= 1 && issue.line <= rows.length)
    .map((issue) => ({ index: rows[issue.line - 1], line: rows[issue.line - 1] + 1, reason: issue.reason }));
}

// Whether a check answer ({errors, issues}) names any problem.
export function hasErrors(check) {
  if (!check) return false;
  const lists = Object.values(check.issues || {});
  return Object.keys(check.errors || {}).length > 0 || lists.some((list) => list.length > 0);
}

// The save bar's copy (spec §2): "Saving…", the outcomes and the daemon's silence as
// settings.js said them before; the conflict sentence as index.html's #e-conflict says it.
export const BAR_TEXT = {
  saving: "Saving…",
  applied: "Saved and applied by the daemon.",
  saved: "Saved: the daemon reloads the config.",
  rejected: "Saved, but the daemon rejected the config (see the alert).",
  conflict: "Config changed on disk. Reload to load it (your unsaved changes here are discarded).",
};
const INVALID = " · fix the highlighted fields to save";

// Each state's classes, written out whole so the class-coverage test finds them.
const BAR_CLASS = {
  hidden: "s-bar s-bar-hidden",
  dirty: "s-bar s-bar-dirty",
  "dirty-invalid": "s-bar s-bar-dirty-invalid",
  saving: "s-bar s-bar-saving",
  applied: "s-bar s-bar-applied",
  saved: "s-bar s-bar-applied", // the daemon silent: the save still went through
  rejected: "s-bar s-bar-rejected",
  conflict: "s-bar s-bar-conflict",
};

const bar = (state, text, actions) => ({ state, class: BAR_CLASS[state], text, actions });

// The save bar (spec §2). Pure. `count`: the changed fields; `invalid`: the last check
// found problems; `outcome`: null, "saving" (the PUT in flight), "waiting" (saved, the
// daemon hasn't reported yet), "applied", "saved" (the daemon said nothing in time),
// "rejected" or "conflict" (a 409). Saving and a conflict win over edits; an edit wins
// over waiting and the three outcomes.
export function barState({ count, invalid, outcome }) {
  if (outcome === "saving") return bar("saving", BAR_TEXT.saving, []);
  if (outcome === "conflict") return bar("conflict", BAR_TEXT.conflict, ["reload", "discard"]);
  if (count > 0) {
    const text = count === 1 ? "1 unsaved change" : `${count} unsaved changes`;
    if (invalid) return bar("dirty-invalid", `${text}${INVALID}`, ["discard", "save"]);
    return bar("dirty", text, ["discard", "save"]);
  }
  if (outcome === "waiting") return bar("saving", BAR_TEXT.saving, []);
  if (["applied", "saved", "rejected"].includes(outcome)) return bar(outcome, BAR_TEXT[outcome], []);
  return bar("hidden", "", []);
}

// The scroll-spy (spec §1): the section being read is the last one whose top has
// reached `line` (just under the sticky header); at the end of the page, the last
// one, which may be too short to ever reach the line. `starts`: [section, y] pairs in
// page order, y relative to the viewport.
export function spySection(starts, line, atBottom) {
  if (!starts.length) return "";
  if (atBottom) return starts[starts.length - 1][0];
  let current = starts[0][0];
  for (const [section, y] of starts) if (y <= line) current = section;
  return current;
}

// The heartbeat's next send (spec §4), in the page's local time: the daemon runs on
// this Mac, in its time zone. `nowHHMM` and `at` are "HH:MM"; a null `at` says nothing.
export function heartbeatPreview(nowHHMM, at) {
  if (!at) return "";
  return at > nowHHMM ? `Next: today ${at}` : `Next: tomorrow ${at}`;
}

// Whether "HH:MM" `now` falls in the window from–to (to excluded); a window whose
// end is before its start runs past midnight.
function inWindow(now, from, to) {
  return from < to ? from <= now && now < to : now >= from || now < to;
}

// The quiet-hours line (spec §4), from the dashboard's quiet {window, active, ends_at}:
// what is in force. While the quiet-hours field is changed, that isn't what the
// fields show: the line previews the edited window (`draft`, "HH:MM-HH:MM", against
// `nowHHMM` in the page's local time, the daemon's) as it will be once saved. The
// caller passes a null `draft` while the server's check finds the times invalid.
export function quietPreview(dashQuiet, changed, draft = null, nowHHMM = null) {
  if (changed) {
    const [from, to] = typeof draft === "string" ? draft.split("-") : [];
    if (!from || !to || !nowHHMM) return "Takes effect when you save.";
    return inWindow(nowHHMM, from, to)
      ? `Once saved: active now · ends ${to}`
      : `Once saved: not active now · starts ${from}`;
  }
  // The payload's key, read by string: a bare `.window` would read as the browser global
  // (tests/web/test_static.py test_only_main_names_the_browser_globals).
  const range = dashQuiet ? dashQuiet["window"] : null;
  if (!range) return "";
  if (dashQuiet.active && dashQuiet.ends_at) return `Active now · ends ${clock(dashQuiet.ends_at)}`;
  return `Not active now · starts ${range.split("-")[0]}`;
}
