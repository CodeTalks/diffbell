// The editor's file tree (UI refine spec §4.4, U7): one folder per /api/browse
// request. Ticks and badges come from the server's matched_by / ignored_by, so
// the page never matches globs itself.
import { api } from "./api.js";
import { $, h, icon, lines, reconcile, setAttr, setClass, setHidden, setText, shown } from "./dom.js";
import { applyTick, QUOTES } from "./editor.js";
import { state } from "./state.js";

const BUILTIN = "built-in: ";

function relOf(dir, name) {
  return dir ? `${dir}/${name}` : name;
}

// The pattern that matches this path and (almost) nothing else, with the
// server's glob rules: "*" and "?" become one-character classes ("[*]", "[?]");
// "[" and "]" become "?" (any one character), because check_pattern rejects
// a class that would start with "[" or "]". So does every character a pattern
// line can't hold or check_pattern rejects: a control character (a newline
// would split the line), "\", whitespace at either end (trimmed), and the
// first of a pair of quotes around the whole name. A name without these
// characters is its own pattern.
export function patternForPath(rel) {
  let out = rel
    .replace(/[*?[\]\\\u0000-\u001f\u007f-\u009f]/g, (c) => (c === "*" ? "[*]" : c === "?" ? "[?]" : "?"))
    .replace(/^\s+|\s+$/g, (run) => "?".repeat(run.length));
  if (out.length >= 2 && QUOTES.includes(out[0]) && QUOTES.includes(out[out.length - 1])) out = `?${out.slice(1)}`;
  return out;
}

function isLine(rel, patternLines) {
  return patternLines.includes(rel) || patternLines.includes(patternForPath(rel));
}

// The checkbox of a file row. Checked means "watched". A file whose path is a
// line (as typed, or as patternForPath wrote it) can be unticked (the line goes);
// one matched only by another pattern is ticked and disabled; an ignored one is
// unticked and disabled. A line that is the path but matches nothing (a name the
// glob rules read differently) is flagged rather than shown as watched.
export function tickState(entry, rel, patternLines) {
  if (isLine(rel, patternLines)) {
    if (!entry.matched_by && !entry.ignored_by) {
      return { checked: true, disabled: false, badge: "line doesn't match this name", title: "untick to remove this line", warn: true };
    }
    const badge = entry.ignored_by ? `ignored by ${entry.ignored_by}` : "watching";
    return { checked: true, disabled: false, badge, title: "untick to remove this line", warn: false };
  }
  if (entry.ignored_by) {
    const title = `ignored by ${entry.ignored_by}; edit the ignore list to change this`;
    return { checked: false, disabled: true, badge: `ignored by ${entry.ignored_by}`, title, warn: false };
  }
  if (entry.matched_by) {
    const pattern = entry.matched_by.startsWith(BUILTIN) ? entry.matched_by.slice(BUILTIN.length) : entry.matched_by;
    const exact = pattern === rel || pattern === patternForPath(rel);
    const badge = exact ? "watching" : `matched by ${entry.matched_by}`;
    const title = `matched by ${entry.matched_by}; edit the pattern to change this`;
    return { checked: true, disabled: true, badge, title, warn: false };
  }
  return { checked: false, disabled: false, badge: "", title: "tick to watch this file", warn: false };
}

// The patterns text after ticking (append patternForPath(rel)) or unticking
// (remove every line that is the path, as typed or as written by a tick).
export function toggledText(text, rel, checked) {
  if (checked) {
    if (isLine(rel, lines(text))) return text;
    const separator = text === "" || text.endsWith("\n") ? "" : "\n";
    return `${text}${separator}${patternForPath(rel)}`;
  }
  const own = new Set([rel, patternForPath(rel)]);
  return text
    .split("\n")
    .filter((line) => !own.has(line.trim()))
    .join("\n");
}

export function resetTree(path) {
  const t = state.tree;
  t.path = path;
  t.open = new Set();
  t.listings = new Map();
  t.seq += 1;
  renderTree();
}

function browse(dir) {
  const t = state.tree;
  return api("POST", "/api/browse", { path: t.path, dir, ...t.lists });
}

// Re-list the root and every open folder for the editor's current lists (after
// each preview), so the badges follow the patterns being typed.
export async function syncTree(body) {
  const t = state.tree;
  if (body.path !== t.path) resetTree(body.path);
  // A closed folder's listing is for the old lists: dropped, so reopening it
  // lists it afresh.
  for (const d of [...t.listings.keys()]) if (d && !t.open.has(d)) t.listings.delete(d);
  t.lists = { patterns: body.patterns, ignore: body.ignore, use_defaults: body.use_defaults };
  const seq = ++t.seq;
  for (const dir of ["", ...[...t.open].sort()]) {
    let listing;
    try {
      listing = await browse(dir);
    } catch {
      break; // a bad pattern or path is shown by the editor; the tree keeps its rows
    }
    if (seq !== t.seq) return; // the path or the lists changed meanwhile
    t.listings.set(dir, listing);
  }
  renderTree();
}

export async function toggleFolder(dir) {
  const t = state.tree;
  if (t.open.has(dir)) {
    t.open.delete(dir);
    renderTree();
    return;
  }
  t.open.add(dir);
  renderTree();
  if (t.listings.has(dir)) return;
  const seq = t.seq; // a sync or a new path since makes this listing stale
  let listing;
  try {
    listing = await browse(dir);
  } catch (err) {
    listing = { dir, entries: [], truncated: false, error: err.status === 0 ? "unreachable" : "unreadable" };
  }
  if (t.seq !== seq) return;
  t.listings.set(dir, listing);
  renderTree();
}

function visible(entry, rel) {
  const t = state.tree;
  const filter = t.filter.trim().toLowerCase();
  if (!filter || entry.name.toLowerCase().includes(filter)) return true;
  const listing = entry.type === "dir" ? t.listings.get(rel) : undefined;
  return Boolean(listing) && listing.entries.some((child) => visible(child, relOf(rel, child.name)));
}

function createRow(rel, entry) {
  const isDir = entry.type === "dir";
  const row = h(
    "div",
    { class: "trow" },
    isDir && !entry.excluded
      ? h("button", { type: "button", class: "disclose", "aria-label": `Open ${shown(entry.name)}`, "aria-expanded": "false", onclick: () => toggleFolder(rel) }, icon("chevron"))
      : h("span", { class: "disclose" }),
    isDir
      ? h("span", { class: "check" })
      : h("input", { type: "checkbox", class: "check", "aria-label": `Watch ${shown(entry.name)}`, onchange: (e) => applyTick(rel, e.target.checked) }),
    icon(isDir ? "folder" : "file"),
    h("span", { class: "name", text: shown(entry.name) }),
    h("span", { class: "badge" }),
    icon("lock", "lock off"),
  );
  // A plain nested list: role=tree would promise arrow-key navigation.
  return h("li", {}, row, isDir ? h("ul", { class: "tree" }) : null);
}

function updateRow(li, rel, entry, patternLines) {
  const [row, children] = li.children;
  const [, check, , , badge, lock] = row.children;
  setClass(lock, entry.redacted ? "icon lock" : "icon lock off"); // an <svg> has no `hidden`
  if (entry.type === "dir") {
    setClass(li, entry.excluded ? "excluded" : "");
    setText(badge, entry.excluded ? "excluded" : "");
    if (entry.excluded) return;
    const open = state.tree.open.has(rel);
    setAttr(row.children[0], "aria-expanded", String(open));
    setClass(row.children[0], open ? "disclose open" : "disclose");
    setHidden(children, !open);
    if (open) renderLevel(children, rel, patternLines);
    else children.replaceChildren();
    return;
  }
  const tick = tickState(entry, rel, patternLines);
  if (check.checked !== tick.checked) check.checked = tick.checked;
  if (check.disabled !== tick.disabled) check.disabled = tick.disabled;
  setAttr(check, "title", tick.title);
  setText(badge, tick.badge);
  setClass(li, tick.warn ? "watched mismatch" : tick.checked ? "watched" : "");
}

function renderLevel(container, dir, patternLines) {
  const listing = state.tree.listings.get(dir);
  const items = [];
  if (listing) {
    for (const entry of listing.entries) {
      const rel = relOf(dir, entry.name);
      if (visible(entry, rel)) items.push({ rel, entry });
    }
    if (listing.error) items.push({ note: "Can't read this folder", rel: dir });
    if (listing.truncated) items.push({ note: "Showing the first 2,000", rel: dir });
  }
  reconcile(
    container,
    items,
    (item) => (item.note ? `note:${item.note}:${item.rel}` : `${item.entry.type}:${item.rel}`),
    (item) => (item.note ? h("li", { class: "tree-note", text: item.note }) : createRow(item.rel, item.entry)),
    (li, item) => {
      if (!item.note) updateRow(li, item.rel, item.entry, patternLines);
    },
  );
}

export function renderTree() {
  const t = state.tree;
  renderLevel($("tree"), "", lines($("e-patterns").value));
  setText($("tree-status"), t.path ? "" : "Enter a folder to see its files.");
}

export function setTreeFilter(value) {
  state.tree.filter = value;
  renderTree();
}
