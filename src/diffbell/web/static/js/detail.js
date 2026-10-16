// The detail pane: the stored, redacted diff of one event (UI refine spec §4.3),
// with held, batch and rule notes (notify controls spec §5.2). Every diff line is
// its own element; its pieces (word changes, syntax colour: diff readability spec §3)
// come from diffview.js and are set as text nodes and spans with h().
import { api } from "./api.js";
import { copyText } from "./dashboard.js";
import { countMatches, diffSegments, searchTerm, visible } from "./diffview.js";
import { $, h, icon, plural, setAttr, setText, shown } from "./dom.js";
import { feedNow, renderFeed, retryEvent, selectedId, syncFeedLayout, writeUrl } from "./feed.js";
import { gitLogCommand } from "./shell.js";
import { state } from "./state.js";
import { eventStatus, failureReason, heldReason, heldWord } from "./status.js";
import { fullTime } from "./time.js";

export const GONE_NOTE = "That event is no longer in history";
const KIND_ICON = { created: "created", modified: "modified", deleted: "deleted" };

export function lineClass(line) {
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "del";
  return "ctx";
}

export function lineCounts(entry) {
  const stored = entry.lines || [];
  return {
    added: stored.filter((l) => l.startsWith("+")).length,
    removed: stored.filter((l) => l.startsWith("-")).length,
  };
}

export const RULE_NOTE = "Not in the Telegram message (rule)";
export const NOT_RECORDED = "Diff not recorded (event predates diff history).";
export const NOT_KEPT = "Diff no longer kept (older than 30 days).";

// Which stored lines of file `index` the Telegram message showed: the first
// `telegram_files` files carried their first `telegram_lines` lines. `rest` is
// the stored excerpt beyond that (behind "Show all N lines"). A file a rule left
// out, and an event that was held (its own message never went out; a catch-up
// did), have no Telegram excerpt to mark.
export function diffPlan(entry, index, detail) {
  const stored = entry.lines || [];
  if (entry.rule_skipped || detail.status === "held" || detail.status === "skipped" || detail.batch_id) {
    return { head: stored, rest: [], divider: "", notListed: false };
  }
  if (index >= detail.telegram_files) {
    return { head: stored, rest: [], divider: "", notListed: true };
  }
  const budget = detail.telegram_lines;
  if (budget <= 0 || stored.length <= budget) {
    return { head: stored, rest: [], divider: "", notListed: false };
  }
  const divider = detail.status === "sent" ? "Telegram showed the lines above" : "The Telegram message has the lines above";
  return { head: stored.slice(0, budget), rest: stored.slice(budget), divider, notListed: false };
}

// -- quick actions on each file (Plan 7 spec §3.2) ----------------------------------------

export const EDITOR_NAMES = { cursor: "Cursor", vscode: "VS Code" };

// The "Open" link: cursor://file/<folder>/<path>:<line> (or vscode://), every path
// segment percent-encoded, so a space, "#", "?" or ":" in a name can't change the
// URL's meaning. The line is always there (1 without a first changed line). The
// editors decode the path and read a trailing ":<digits>" (or ":<digits>:<digits>")
// as line and column whatever follows, so a file whose name ends like that
// ("build:12") can't be opened safely: null, as for editor "none" (or an unknown
// one), without a folder, and for a name that can't be encoded (a lone surrogate).
const LINE_SUFFIX = /:\d+$/;

export function editorUrl(editor, folder, path, line) {
  if (!folder || !Object.hasOwn(EDITOR_NAMES, editor)) return null;
  if (LINE_SUFFIX.test(String(path).split("/").pop())) return null;
  const encode = (p) => String(p).split("/").map(encodeURIComponent).join("/");
  let file;
  try {
    file = `${encode(String(folder).replace(/\/+$/, ""))}/${encode(path)}`;
  } catch {
    return null; // URIError: one odd name never blanks the pane
  }
  const at = `:${Number.isInteger(line) && line >= 1 ? line : 1}`;
  return `${editor}://file${file.startsWith("/") ? "" : "/"}${file}${at}`;
}

// Why a file's diff can't be copied, in the words its section uses.
function noCopyReason(entry) {
  if (entry.redacted) return "contents redacted";
  if (entry.truncated) return "diff omitted (size limit)";
  return "no lines stored";
}

// The stored first changed line, or null (missing, not an integer, below 1).
export function firstLine(entry) {
  return Number.isInteger(entry.first_line) && entry.first_line >= 1 ? entry.first_line : null;
}

// The detail's project folder (from the dashboard) and the editor setting. An event
// of a project no longer configured has no folder: a configured project of the same
// name is another folder.
function actionContext(detail) {
  const dash = state.dash || {};
  const active = detail.project_active !== false;
  const row = active ? (dash.projects || []).find((p) => p.name === detail.project) : null;
  const editor = (dash.web && dash.web.editor) || "cursor";
  return { folder: (row && row.folder) || null, editor };
}

// What the actions depend on besides the event: a poll re-renders the pane when
// it changes (the setting was saved, or the project's folder appeared or moved).
function actionsKey(detail) {
  if (!detail) return "";
  const { folder, editor } = actionContext(detail);
  return `${editor}\u0000${folder}`;
}

// Open ↗ / Copy diff / git log, beside the header's collapse button (never inside
// it: pressing one never toggles the section). Nothing here runs anything.
function fileActions(detail, entry) {
  const { folder, editor } = actionContext(detail);
  const name = shown(entry.path);
  const parts = [];
  const line = firstLine(entry);
  // A deleted file is gone from the folder: nothing to open (its diff and history stay).
  const url = entry.kind === "deleted" ? null : editorUrl(editor, folder, entry.path, line);
  if (url) {
    const app = EDITOR_NAMES[editor];
    parts.push(
      h("a", { class: "file-action open-file", href: url, title: line ? `Open in ${app} at line ${line}` : `Open in ${app}`, "aria-label": `Open ${name} in ${app}` }, "Open ↗"),
    );
  }
  const lines = entry.lines || [];
  const copyable = !entry.redacted && !entry.truncated && lines.length > 0;
  const copy = h("button", {
    type: "button",
    class: "link file-action copy-diff",
    "aria-label": `Copy the diff of ${name}`,
    onclick: () => copyText(lines.join("\n"), "Diff copied"),
  }, "Copy diff");
  copy.disabled = !copyable; // nothing to copy from a redacted, truncated or empty file
  if (!copyable) copy.setAttribute("title", `Nothing to copy: ${noCopyReason(entry)}`);
  parts.push(copy);
  if (folder) {
    const command = gitLogCommand(folder, entry.path);
    parts.push(
      h("button", {
        type: "button",
        class: "link file-action git-log",
        title: shown(command),
        "aria-label": `Copy a git log command for ${name}`,
        onclick: () => copyText(command, "git log command copied"),
      }, "git log"),
    );
  }
  return h("span", { class: "file-actions" }, ...parts);
}

// A selected event that isn't in the loaded list (e.g. ?event= for an older one)
// has no status here, so a status change doesn't refetch it.
function selectionKey(id) {
  const ev = state.events.find((e) => e.id === id);
  return ev ? `${id}:${ev.status}:${ev.attempts}` : `${id}`;
}

// Fetch the selected event, but only when it changed (another event, or its
// status or attempts): a poll that changed nothing never refetches.
export async function syncDetail({ force = false } = {}) {
  const id = selectedId();
  if (id === null) {
    state.detail = null;
    state.detailKey = "";
    state.detailError = "";
    renderDetail();
    return;
  }
  const key = selectionKey(id);
  if (!force && key === state.detailKey) {
    // Same event, another search (only the marks and opened files change) or another
    // editor setting or folder (only the file actions change).
    if (state.detailQ !== state.filters.q || state.detailActions !== actionsKey(state.detail)) renderDetail();
    return;
  }
  state.detailKey = key;
  const seq = ++state.detailSeq;
  try {
    const detail = await api("GET", `/api/events/${encodeURIComponent(id)}`);
    if (seq !== state.detailSeq) return;
    state.detail = detail;
    state.detailError = "";
    // The pane itself is not a live region (a diff would be read out whole); this
    // short note is.
    setText($("detail-live"), `Loaded event: ${shown(detail.project)}, ${fullTime(detail.created_at)}`);
  } catch (err) {
    if (seq !== state.detailSeq) return;
    if (err.status === 404 && state.selected === id) {
      // ?event= is unknown or was pruned: show the newest instead, and say so.
      state.selected = null;
      state.detailNote = GONE_NOTE;
      state.detailKey = "";
      writeUrl();
      renderFeed();
      await syncDetail();
      return;
    }
    state.detail = null;
    state.detailError = err.status === 404 ? "" : "Couldn't load this event";
  }
  renderDetail();
}

// The event being retried from the pane: its Retry is aria-disabled (not
// disabled, which would drop its focus) until the request is answered, and this
// guard keeps a second click from sending it twice.
let retrying = null;

async function retryFromDetail(detail) {
  if (retrying !== null) return;
  retrying = detail.id;
  try {
    renderDetail();
    const ev = state.events.find((e) => e.id === detail.id) || detail;
    if (await retryEvent(ev)) await syncDetail({ force: true });
  } finally {
    retrying = null;
    renderDetail();
  }
}

function toggle(set, key) {
  if (set.has(key)) set.delete(key);
  else set.add(key);
  renderDetail();
}

// The search the pane marks (2+ characters, as on the server), or "". Toggles made
// during an earlier search are forgotten when it changes.
function currentTerm() {
  const term = searchTerm(state.filters.q);
  if (state.searchToggledQ !== term) {
    state.searchToggled.clear();
    state.searchToggledQ = term;
  }
  return term;
}

// A collapsed section holding a match opens for that search; once its header is
// toggled during the search, the person's choice wins (spec §2.3).
function sectionOpen(key, matched) {
  if (matched && !state.searchToggled.has(key)) return true;
  return !state.collapsed.has(key);
}

function toggleSection(key, open, term) {
  if (open) state.collapsed.add(key);
  else state.collapsed.delete(key);
  if (term) state.searchToggled.add(key);
  renderDetail();
}

// One piece of a line: a search match is a <mark> (its other classes on it), any
// other piece with classes a span, a plain one a text node. Never markup.
function piece(seg) {
  if (seg.classes.includes("mark")) {
    const rest = seg.classes.filter((c) => c !== "mark");
    return h("mark", { class: rest.length ? rest.join(" ") : null, text: seg.text });
  }
  return seg.classes.length ? h("span", { class: seg.classes.join(" "), text: seg.text }) : seg.text;
}

// One element per line, keeping its row class.
function diffLines(list, segments) {
  return list.map((line, i) => h("div", { class: `ln ${lineClass(line)}` }, (segments[i] || [{ text: visible(line), classes: [] }]).map(piece)));
}

// `part` names the list a file is in: "" for the event's own report, "b" for its
// catch-up message's, so their collapse and "Show all" state stay apart.
function fileSection(detail, entry, index, part = "") {
  const key = `${detail.id}:${part}${index}`;
  const term = currentTerm();
  const lines = entry.redacted || entry.truncated ? [] : entry.lines || [];
  const open = sectionOpen(key, Boolean(term) && countMatches(lines, term) > 0);
  const counts = lineCounts(entry);
  const head = h(
    "button",
    {
      type: "button",
      class: "file-head",
      "aria-expanded": String(open),
      "data-focus": `head:${key}`,
      onclick: () => toggleSection(key, open, term),
    },
    icon("chevron", open ? "open" : ""),
    icon(entry.redacted ? "lock" : KIND_ICON[entry.kind] || "file"),
    h("span", { class: "path", text: shown(entry.path) }),
    entry.lines && entry.lines.length ? h("span", { class: "add", text: `+${counts.added}` }) : null,
    entry.lines && entry.lines.length ? h("span", { class: "del", text: `−${counts.removed}` }) : null,
    entry.note && !entry.truncated ? h("span", { class: "note", text: entry.note }) : null,
  );
  const body = [];
  if (open) {
    const plan = diffPlan(entry, index, detail);
    if (entry.rule_skipped) body.push(h("p", { class: "muted", text: RULE_NOTE }));
    if (plan.notListed) body.push(h("p", { class: "muted", text: "Not listed in the Telegram message (too long)" }));
    if (entry.redacted) {
      body.push(h("p", { class: "muted" }, icon("lock"), " contents redacted"));
    } else if (entry.truncated) {
      body.push(h("p", { class: "muted", text: "diff omitted (size limit)" }));
    } else {
      // Pairs and syntax are worked out over all stored lines, then cut at the divider.
      const segments = diffSegments(entry.lines || [], entry.path, term);
      if (plan.head.length) body.push(h("div", { class: "diff" }, diffLines(plan.head, segments)));
      if (plan.divider) {
        body.push(h("p", { class: "divider", text: plan.divider }));
        if (state.showAll.has(key)) {
          body.push(h("div", { class: "diff" }, diffLines(plan.rest, segments.slice(plan.head.length))));
        } else {
          const hidden = countMatches(plan.rest, term);
          const matches = hidden ? ` (${hidden === 1 ? "1 match" : `${hidden} matches`})` : "";
          body.push(
            h("button", {
              type: "button",
              class: "link",
              text: `Show all ${entry.lines.length} lines${matches}`,
              "data-focus": `all:${key}`,
              onclick: () => toggle(state.showAll, key),
            }),
          );
        }
      }
    }
  }
  return h("section", { class: "file" }, h("div", { class: "file-bar" }, head, fileActions(detail, entry)), ...body);
}

export function renderDetail() {
  const pane = $("detail");
  syncFeedLayout(); // an empty pane beside an empty list is not drawn
  state.detailQ = state.filters.q;
  state.detailActions = actionsKey(state.detail);
  const parts = [];
  if (state.detailNote) parts.push(h("p", { class: "note", text: state.detailNote }));
  if (state.detailError) {
    parts.push(
      h(
        "p",
        { class: "error" },
        `${state.detailError} · `,
        h("button", { type: "button", class: "link", text: "Retry", "data-focus": "reload", onclick: () => syncDetail({ force: true }) }),
      ),
    );
    replacePane(pane, parts);
    return;
  }
  const d = state.detail;
  if (!d) {
    replacePane(pane, parts);
    return;
  }
  const s = eventStatus(d, feedNow());
  parts.push(
    h(
      "header",
      { class: "detail-head" },
      h("h2", {}, h("span", { text: shown(d.project) }), d.branch ? h("code", { class: "branch", text: shown(d.branch) }) : null),
      h(
        "p",
        { class: "meta" },
        h("span", { text: fullTime(d.created_at) }),
        h("span", { class: `state ${s.tone}`, title: s.title || null }, icon(s.icon), s.word),
        d.status === "failed" ? retryButton(d) : null,
      ),
    ),
  );
  const note = deliveryNote(d);
  if (note) parts.push(h("p", { class: "note delivery", text: note }));
  if (d.batch_report) {
    // The catch-up message's own copy: its divider marks what Telegram showed.
    const batch = { ...d.batch_report, id: d.id, status: d.status, batch_id: null };
    parts.push(h("h3", { class: "part", text: "Catch-up message" }));
    d.batch_report.reports.forEach((entry, index) => parts.push(fileSection(batch, entry, index, "b")));
    parts.push(h("h3", { class: "part", text: "This change (when it was held)" }));
  }
  if (d.reports === null) {
    parts.push(h("p", { class: "muted", text: d.reports_state === "pruned" ? NOT_KEPT : NOT_RECORDED }));
    parts.push(
      h(
        "ul",
        { class: "files" },
        d.files.map((f) => h("li", {}, icon(f.redacted ? "lock" : KIND_ICON[f.kind] || "file"), shown(f.path))),
      ),
    );
  } else {
    d.reports.forEach((entry, index) => parts.push(fileSection(d, entry, index)));
  }
  replacePane(pane, parts);
}

// One line on how this event was (or will be) delivered, or "".
export function deliveryNote(d, now = feedNow()) {
  if (d.status === "held") {
    // No held_until: snoozed until turned back on, or already due (the API sends
    // none for a time in the past), when the catch-up is retrying or about to go.
    let until = "Held until you turn notifications back on";
    if (d.attempts > 0 && d.last_error) until = `Catch-up retrying: ${failureReason(d.last_error).text}`;
    else if (d.held_reason === "setup") return "Held until Telegram is set up (Settings → Telegram)";
    else if (d.held_until) until = heldWord(d, now);
    else if (d.next_attempt_at * 1000 <= now) until = "Catch-up about to be sent";
    return `${until} (${heldReason(d)})`;
  }
  if (!d.batch_id) return "";
  const others = Math.max(0, (d.batch_size || 1) - 1);
  if (d.status === "failed") return `Catch-up failed: ${failureReason(d.last_error).text}`;
  if (d.status === "sent") {
    // "Sent in catch-up #12 with 3 other changes", or alone in its batch "… #12".
    const rest = others ? ` with ${plural(others, "other change", "other changes")}` : "";
    return `Sent in catch-up #${d.batch_id}${rest}`;
  }
  return "";
}

function retryButton(d) {
  const busy = retrying === d.id ? "true" : null;
  return h("button", { type: "button", "data-focus": "retry", "aria-disabled": busy, onclick: () => retryFromDetail(d) }, icon("retry"), "Retry");
}

// The first descendant (depth first) that passes `test`.
function find(root, test) {
  for (const el of [...root.children]) {
    if (test(el)) return el;
    const inner = find(el, test);
    if (inner) return inner;
  }
  return null;
}

function findFocus(root, name) {
  return find(root, (el) => Boolean(el.dataset) && el.dataset.focus === name);
}

// The pane is rebuilt on every change (collapse, Show all, Retry, a refetch), so
// a focused control is found again by its data-focus and refocused; a gone
// "Show all" hands focus to its file's header, and any other gone control (Retry
// once the event is no longer failed) to the first file header, else the pane.
function replacePane(pane, parts) {
  const active = pane.ownerDocument.activeElement;
  const name = active && active !== pane && pane.contains(active) && active.dataset ? active.dataset.focus : undefined;
  pane.replaceChildren(...parts);
  if (!name) return;
  const target =
    findFocus(pane, name) ||
    (name.startsWith("all:") ? findFocus(pane, `head:${name.slice(4)}`) : null) ||
    find(pane, (el) => Boolean(el.dataset) && String(el.dataset.focus).startsWith("head:"));
  if (target) {
    target.focus();
    return;
  }
  setAttr(pane, "tabindex", "-1"); // focusable from script only, not a tab stop
  pane.focus();
}
