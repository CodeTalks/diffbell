// The project editor (UI refine spec §4.4): watch mode, per-line validation, the
// live count, and saving with Plan 2's config_version semantics. Remove is
// immediate, with a 10 s Undo (§4.6).
import { api } from "./api.js";
import { allProjects } from "./dashboard.js";
import {
  $,
  baseName,
  debounce,
  h,
  icon,
  lines,
  reconcile,
  setAttr,
  setClass,
  setIcon,
  setMessage,
  setText,
  shown,
  TYPING_MS,
} from "./dom.js";
import { loadSettings } from "./settings.js";
import { state } from "./state.js";
import { projectStatus } from "./status.js";
import { showToast } from "./toast.js";
import { resetTree, syncTree, toggledText } from "./tree.js";

export const UNDO_MS = 10000;
const SAVE_POLLS = 3; // polls to wait for a saved project's new status before giving up
const EDITOR_LISTS = ["e-patterns", "e-ignore"];
// "Notify me about" (notify controls spec §5.2): a kind and its tick box.
export const NOTIFY_TICKS = [
  ["created", "e-notify-created"],
  ["modified", "e-notify-modified"],
  ["deleted", "e-notify-deleted"],
];
export const LAST_TICK_HINT = "To silence a project for a while, use snooze on the dashboard.";
export const QUOTES = "\"'‘’“”"; // config.check_pattern's straight and smart quotes

// -- per-line validation (also used by settings.js) -----------------------------------

// The cheap cases, checked as the person types; the server checks the rest.
export function precheck(line) {
  if (line.length >= 2 && QUOTES.includes(line[0]) && QUOTES.includes(line[line.length - 1])) {
    return "remove the quotes around it";
  }
  if (line.startsWith("/")) return "remove the leading / (patterns start at the folder root)";
  if (line.split("/").includes("..")) return "'..' is not allowed";
  if (line.includes("//")) return "remove the double /";
  if (line.endsWith("/")) return "remove the trailing / (use folder/** for a folder's files)";
  return null;
}

// Python's repr() of a string: the server's messages quote a pattern that way
// ("invalid pattern '<p>': …"), so this finds the line a message is about. As in
// Python, a character that isn't printable (Unicode categories C and Z, except
// the space) is escaped as \xNN, \uNNNN or \UNNNNNNNN.
export function pyRepr(s) {
  const quote = s.includes("'") && !s.includes('"') ? '"' : "'";
  let out = "";
  for (const ch of s) {
    const code = ch.codePointAt(0);
    if (ch === "\\") out += "\\\\";
    else if (ch === quote) out += `\\${quote}`;
    else if (ch === "\n") out += "\\n";
    else if (ch === "\r") out += "\\r";
    else if (ch === "\t") out += "\\t";
    else if (ch !== " " && /[\p{C}\p{Z}]/u.test(ch)) out += pyEscape(code);
    else out += ch;
  }
  return `${quote}${out}${quote}`;
}

function pyEscape(code) {
  const hex = code.toString(16);
  if (code < 0x100) return `\\x${hex.padStart(2, "0")}`;
  if (code < 0x10000) return `\\u${hex.padStart(4, "0")}`;
  return `\\U${hex.padStart(8, "0")}`;
}

// The problem of each raw line of `text` (index into text.split("\n")): the
// pre-check first, else the server's message for it. `other` keeps the server
// messages that name no line of this text.
export function lineIssues(text, serverMessage = "") {
  const raw = text.split("\n");
  const server = new Map();
  const other = [];
  for (const message of serverMessage ? serverMessage.split("\n") : []) {
    let matched = false;
    raw.forEach((line, index) => {
      const pattern = line.trim();
      const prefix = `invalid pattern ${pyRepr(pattern)}: `;
      if (pattern && message.startsWith(prefix)) {
        matched = true;
        if (!server.has(index)) server.set(index, message.slice(prefix.length));
      }
    });
    if (!matched && message) other.push(message);
  }
  const issues = [];
  raw.forEach((line, index) => {
    const pattern = line.trim();
    if (!pattern) return;
    const reason = precheck(pattern) || server.get(index);
    if (reason) issues.push({ index, line: index + 1, reason });
  });
  return { issues, other };
}

// ✓ or ⚠ per line in the textarea's gutter, and the reasons below it. `issues` name
// the textarea's own lines ({index, line, reason}): the project editor's come from
// lineIssues, Settings' from the server's check (settings.js lineIssuesFor).
export function markLines(id, issues) {
  const area = $(id);
  state.lineIssues[id] = issues;
  const bad = new Map(issues.map((issue) => [issue.index, issue.reason]));
  const marks = area.value.split("\n").map((line, index) => {
    if (!line.trim()) return { index, mark: "" };
    return { index, mark: bad.has(index) ? "bad" : "ok" };
  });
  reconcile(
    $(`${id}-gutter`),
    marks,
    (item) => String(item.index),
    () => h("li", {}, icon("sent")),
    (li, item) => {
      setClass(li, item.mark ? `mark ${item.mark}` : "mark");
      setIcon(li.children[0], item.mark === "bad" ? "warning" : "sent");
      setAttr(li, "title", bad.get(item.index) || "");
    },
  );
  reconcile(
    $(`${id}-issues`),
    issues,
    (issue) => `${issue.index}:${issue.reason}`,
    (issue) => h("li", { text: `line ${issue.line}: ${issue.reason}` }),
    () => {},
  );
}

// The project editor's lines: the cheap checks at once, the server's message mapped
// to its line.
export function checkTextarea(id, serverMessage = "") {
  const result = lineIssues($(id).value, serverMessage);
  markLines(id, result.issues);
  return result;
}

export function hasIssues(ids) {
  return ids.some((id) => (state.lineIssues[id] || []).length > 0);
}

// -- config versions ---------------------------------------------------------------

// Projects and settings live in one config.yaml. After our own successful write,
// a form filled from the version we just replaced is still current, so it moves
// to the new version; a form filled from anything older keeps its version and
// gets a 409 (no silent overwrite of a change made on disk).
export function wrote(sentVersion, newVersion) {
  if (state.projectsVersion === sentVersion) state.projectsVersion = newVersion;
  if (state.settingsVersion === sentVersion) state.settingsVersion = newVersion;
  if (state.settings && state.settings.config_version === sentVersion) state.settings.config_version = newVersion;
}

// A write refused because the session ended (the project editor and Settings).
export const SESSION_ENDED = "Not saved: your session has ended. Run diffbell web to log in again.";

export function handleWriteError(err, prefix, message = err.message) {
  const out = $(`${prefix}-message`);
  if (err.status === 409) {
    $(`${prefix}-conflict`).hidden = false;
    setMessage(out, "");
  } else if (err.status === 404) {
    setMessage(out, `${err.message}. It may have been renamed or removed in config.yaml: Reload.`, "error");
    $(`${prefix}-conflict`).hidden = false;
  } else if (err.status === 401) {
    setMessage(out, SESSION_ENDED, "error");
  } else {
    setMessage(out, message, "error"); // 422 inline, 400/403/413/503 with their reason
  }
}

// Whether the dashboard reports the saved config version as applied or rejected.
// Applied is checked first: a rejection recorded for the same bytes earlier is
// stale once the daemon applied them.
export function saveOutcome(daemon, notice) {
  const applied = daemon.config_version === notice.version;
  const rejected = !applied && daemon.config_rejected_version === notice.version;
  return { applied, rejected };
}

// -- the project list and the form ---------------------------------------------------

// `auto` (a save report) never replaces what the person is doing: if another
// project was picked meanwhile, or the editor was typed into, only the strip
// follows (and only from the version the editor works from, as in refreshStrip).
// Reload and the other explicit callers always refill.
export async function loadProjects(select, { auto = false } = {}) {
  const editing = state.editing;
  let data;
  try {
    data = await api("GET", "/api/projects");
  } catch (err) {
    setMessage($("e-message"), err.message, "error");
    return;
  }
  if (auto && (state.editing !== editing || state.editorDirty)) {
    if (data.config_version === state.projectsVersion) state.projects = data.projects;
    if (state.editing === editing && state.editing) {
      // Not refilled, but the form now edits the saved project (e.g. one just
      // added): say so, and offer Remove.
      $("editor-title").textContent = `Edit ${shown(state.editing)}`;
      $("e-remove").hidden = false;
    }
    renderStrip();
    return;
  }
  state.projects = data.projects;
  state.projectsVersion = data.config_version;
  state.projectsLoaded = true;
  $("e-conflict").hidden = true;
  const keep = select !== undefined ? select : state.editing;
  const project = state.projects.find((p) => p.name === keep) || (keep === null ? null : state.projects[0] || null);
  fillEditor(project);
  if (!state.settings) loadSettings();
}

export function renderStrip() {
  // Statuses come from the latest dashboard poll when it has the project.
  const live = new Map(allProjects().map((p) => [p.name, p]));
  reconcile(
    $("strip"),
    [...state.projects, null],
    (p) => (p ? `p:${p.name}` : "add"),
    (p) => {
      const name = p ? p.name : null;
      return h(
        "button",
        {
          type: "button",
          onclick: () => fillEditor(name === null ? null : state.projects.find((x) => x.name === name) || null),
        },
        icon("created"),
        h("span"),
      );
    },
    (button, p) => {
      setAttr(button, "aria-pressed", String(p ? state.editing === p.name : state.editing === null));
      if (!p) {
        setClass(button, "chip add");
        setText(button.children[1], "Add project");
        return;
      }
      const s = projectStatus(live.get(p.name) || p);
      setClass(button, `chip ${s.tone}`);
      setAttr(button, "title", shown(s.word));
      setIcon(button.children[0], s.icon);
      setText(button.children[1], shown(p.name));
    },
  );
}

// The ticked kinds, in the config's order.
export function notifyOn() {
  return NOTIFY_TICKS.filter(([, id]) => $(id).checked).map(([kind]) => kind);
}

// At least one kind stays ticked: the last ticked box can't be unticked.
export function syncNotifyTicks() {
  const ticked = NOTIFY_TICKS.filter(([, id]) => $(id).checked);
  for (const [, id] of NOTIFY_TICKS) {
    const last = ticked.length === 1 && $(id).checked;
    $(id).disabled = last;
    setAttr($(id), "title", last ? LAST_TICK_HINT : "");
  }
  $("e-notify-hint").hidden = ticked.length !== 1;
}

export function fillEditor(project) {
  state.editing = project ? project.name : null;
  state.editorDirty = false;
  state.preview = null;
  $("editor-title").textContent = project ? `Edit ${shown(project.name)}` : "Add a project";
  $("e-path").value = project ? project.path : "";
  $("e-name").value = project && project.name !== baseName(project.path) ? project.name : "";
  $("e-patterns").value = project ? project.patterns.join("\n") : "";
  $("e-ignore").value = project ? project.ignore.join("\n") : "";
  const defaults = project ? project.use_defaults : true;
  $("e-mode-defaults").checked = defaults;
  $("e-mode-only").checked = !defaults;
  const kinds = project && project.notify_on ? project.notify_on : NOTIFY_TICKS.map(([kind]) => kind);
  for (const [kind, id] of NOTIFY_TICKS) $(id).checked = kinds.includes(kind);
  syncNotifyTicks();
  $("e-ignore-box").open = Boolean(project && project.ignore.length);
  $("e-remove").hidden = !project;
  $("e-conflict").hidden = true;
  setText($("e-path-error"), "");
  setMessage($("e-message"), project && project.error ? `Error: ${project.error}` : "", project && project.error ? "error" : "");
  for (const id of EDITOR_LISTS) checkTextarea(id);
  resetTree(project ? project.path : "");
  renderStrip();
  renderCounts();
  schedulePreview();
}

export function editorBody() {
  return {
    config_version: state.projectsVersion,
    path: $("e-path").value.trim(),
    name: $("e-name").value.trim() || null,
    patterns: lines($("e-patterns").value),
    ignore: lines($("e-ignore").value),
    use_defaults: $("e-mode-defaults").checked,
    notify_on: notifyOn(),
  };
}

// "Will be watched: N files" and "Save · watching N files" ("N+" when the walk
// stopped at a cap); Save is disabled while any line is marked.
export function renderCounts() {
  const p = state.preview;
  const n = p ? `${p.total}${p.exact ? "" : "+"}` : "";
  const files = p && p.total === 1 && p.exact ? "file" : "files";
  setText($("tree-title"), p ? `Will be watched: ${n} ${files}` : "Will be watched: …");
  const save = $("e-save");
  save.disabled = Boolean(state.saving) || hasIssues(EDITOR_LISTS);
  setText(save, p ? `Save · watching ${n} ${files}` : "Save");
}

export async function runPreview() {
  const seq = ++state.previewSeq;
  const body = editorBody();
  for (const id of EDITOR_LISTS) checkTextarea(id);
  if (!body.path.startsWith("/") && !body.path.startsWith("~")) {
    state.preview = null;
    setText($("e-path-error"), "");
    renderCounts();
    resetTree("");
    return;
  }
  try {
    const result = await api("POST", "/api/preview", {
      path: body.path,
      patterns: body.patterns,
      ignore: body.ignore,
      use_defaults: body.use_defaults,
    });
    if (seq !== state.previewSeq) return; // a newer preview is on its way
    state.preview = { total: result.total, exact: result.total_exact !== false };
    setText($("e-path-error"), "");
  } catch (err) {
    if (seq !== state.previewSeq) return;
    state.preview = null;
    if (err.status === 422) {
      // Pattern messages go to their lines; the rest (the folder, "nothing to
      // watch") goes under the folder field.
      const [patterns, ignore] = EDITOR_LISTS.map((id) => checkTextarea(id, err.message));
      if ((state.lineIssues["e-ignore"] || []).length) $("e-ignore-box").open = true;
      setText($("e-path-error"), patterns.other.filter((m) => ignore.other.includes(m)).join("\n"));
    } else {
      setText($("e-path-error"), err.message);
    }
  }
  renderCounts();
  await syncTree(body);
}

export const schedulePreview = debounce(() => runPreview(), TYPING_MS);

// A tick in the tree adds or removes the file's exact path as a line.
export function applyTick(rel, checked) {
  const area = $("e-patterns");
  area.value = toggledText(area.value, rel, checked);
  state.editorDirty = true;
  checkTextarea("e-patterns");
  renderCounts();
  schedulePreview();
}

function projectSignature(p) {
  return JSON.stringify([p.status, p.error || null, p.note || null, p.path]);
}

export async function saveProject(event) {
  if (event) event.preventDefault();
  if (state.saving || hasIssues(EDITOR_LISTS)) return;
  const body = editorBody();
  const adding = state.editing === null || state.editing === undefined;
  const path = adding ? "/api/projects" : `/api/projects/${encodeURIComponent(state.editing)}`;
  state.saving = true;
  renderCounts();
  $("e-conflict").hidden = true;
  setMessage($("e-message"), "Saving…");
  state.editorDirty = false; // the form is what gets saved; typing from now on counts
  try {
    const result = await api(adding ? "POST" : "PUT", path, body);
    wrote(body.config_version, result.config_version);
    state.editing = body.name || baseName(body.path);
    const before = allProjects().find((p) => p.name === state.editing);
    state.saveNotice = {
      name: state.editing,
      version: result.config_version, // the daemon reports it once it applied (or rejected) it
      polls: 0,
      appliedPolls: 0,
      before: before ? projectSignature(before) : null,
    };
    setMessage($("e-message"), "Saved: reloading…", "ok");
    showToast("Saved");
  } catch (err) {
    state.editorDirty = true; // not saved: the edits are still only in the form
    let message = err.message;
    if (err.status === 422) {
      const [patterns, ignore] = EDITOR_LISTS.map((id) => checkTextarea(id, err.message));
      const other = patterns.other.filter((m) => ignore.other.includes(m));
      message = other.length ? other.join("\n") : "Not saved: fix the marked lines.";
    }
    handleWriteError(err, "e", message);
  } finally {
    state.saving = false;
    renderCounts();
  }
}

// Remove at once; the toast's Undo re-adds the entry exactly as /api/projects
// returned it, against the config_version the remove returned.
export async function removeProject() {
  const name = state.editing;
  if (typeof name !== "string" || state.saving) return;
  // In flight (as a save is): a second click sends nothing, and Save waits.
  state.saving = true;
  renderCounts();
  try {
    await removeNow(name);
  } finally {
    state.saving = false;
    renderCounts();
  }
}

async function removeNow(name) {
  const entry = state.projects.find((p) => p.name === name) || null;
  const sent = state.projectsVersion;
  const query = new URLSearchParams({ config_version: sent });
  let result;
  try {
    result = await api("DELETE", `/api/projects/${encodeURIComponent(name)}?${query}`);
  } catch (err) {
    handleWriteError(err, "e");
    return;
  }
  wrote(sent, result.config_version);
  state.removed = entry ? { entry, version: result.config_version } : null;
  state.editing = undefined;
  await loadProjects();
  setMessage($("e-message"), `Removed ${shown(name)}.`, "ok");
  showToast(`Removed ${shown(name)}`, { action: "Undo", onAction: undoRemove, ms: UNDO_MS });
}

export async function undoRemove() {
  const removed = state.removed;
  state.removed = null;
  if (!removed) return;
  const p = removed.entry;
  const body = {
    config_version: removed.version,
    path: p.path,
    name: p.name !== baseName(p.path) ? p.name : null,
    patterns: p.patterns,
    ignore: p.ignore,
    use_defaults: p.use_defaults,
    notify_on: p.notify_on,
  };
  try {
    const result = await api("POST", "/api/projects", body);
    wrote(removed.version, result.config_version);
    await loadProjects(p.name);
    showToast(`Restored ${shown(p.name)}`);
  } catch (err) {
    if (err.status === 409) {
      // The config changed since the remove: nothing is written.
      $("e-conflict").hidden = false;
      showToast(`Not restored: config.yaml changed on disk`, { kind: "error" });
    } else {
      showToast(`Undo failed: ${err.message}`, { kind: "error" });
    }
  }
}

// "Saved: reloading…" stays until the dashboard shows the daemon applied (or
// rejected) the exact config version the save returned, then reports the project's
// status; a project still "pending" (subscribing) gets up to SAVE_POLLS more polls.
// An older daemon that doesn't report the version falls back to waiting for the
// project to appear or its status to change, for at most SAVE_POLLS polls. The
// report goes to the editor only while it still shows the saved project.
export async function reportSave() {
  const notice = state.saveNotice;
  if (!notice) return;
  notice.polls += 1;
  const { applied, rejected } = saveOutcome(state.dash.daemon || {}, notice);
  const project = allProjects().find((p) => p.name === notice.name);
  const changed = Boolean(project) && projectSignature(project) !== notice.before;
  if (!rejected) {
    if (applied) {
      if (project && project.status === "pending" && notice.appliedPolls < SAVE_POLLS) {
        notice.appliedPolls += 1;
        return; // applied; the subscription is still being set up
      }
    } else if (!changed && notice.polls < SAVE_POLLS) {
      return; // the daemon has not reloaded yet
    }
  }
  state.saveNotice = null;
  if (state.editing !== notice.name) {
    await refreshStrip(); // the person moved on: only the strip follows
    return;
  }
  await loadProjects(project ? project.name : undefined, { auto: true });
  if (state.editing !== notice.name) return;
  const message = $("e-message");
  if (rejected) {
    setMessage(message, "Saved, but the daemon rejected the config (see the alert).", "error");
  } else if (!project) {
    const what = applied ? "Saved and applied" : "Saved";
    setMessage(message, `${what}. The daemon has not reported this project's status yet.`, "ok");
  } else if (!applied && !changed) {
    setMessage(message, `Saved. ${shown(project.name)} is still ${projectStatus(project).word} (no new status reported yet).`, "ok");
  } else {
    setMessage(message, `Saved: ${shown(project.name)} · ${projectStatus(project).word}.`, project.error ? "error" : "ok");
  }
}

// Re-read the project list for the strip without touching the editor. Only taken
// when it is the version the editor already works from (after our own write).
async function refreshStrip() {
  try {
    const data = await api("GET", "/api/projects");
    if (data.config_version === state.projectsVersion) state.projects = data.projects;
  } catch {
    // the strip keeps its rows; the next save or Reload tries again
  }
  renderStrip();
}
