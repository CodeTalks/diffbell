// The event list (UI refine spec §4.3): filters, day groups, paging, selection
// and the URL. Import-safe: main.js hands in the URL writer and the poll (initFeed).
import { api } from "./api.js";
import { copyFix, sendTest } from "./dashboard.js";
import { searchTerm } from "./diffview.js";
import { $, h, icon, plural, reconcile, setAttr, setClass, setHidden, setIcon, setText, shown } from "./dom.js";
import { state } from "./state.js";
import { eventStatus, undelivered } from "./status.js";
import { dayKey, dayLabel, feedTime, fullTime, localMidnight } from "./time.js";
import { showToast } from "./toast.js";

export const PAGE = 50;
export const MAX_PAGE = 200; // the server's largest page (/api/events limit)
export const MAX_FILTER = 1024; // the server's limit on the project and q filters
// "Needs attention" (Plan 7 spec §3.3): failed and held events, with the open
// problems listed above them.
export const ATTENTION = "attention";

const deps = {
  refresh: () => {},
  replaceUrl: () => {},
  pushUrl: () => {}, // a new history entry (the activity strip), so Back undoes it
  now: () => Date.now(),
  selectionChanged: () => {},
  filtersChanged: () => {}, // main.js re-renders the cards (they show the project filter)
};

export function initFeed(options) {
  Object.assign(deps, options);
}

// The page's clock (ms), for the detail pane's "Held until …".
export function feedNow() {
  return deps.now();
}

// -- URL ---------------------------------------------------------------------------

// Epoch seconds as the activity strip writes them (since/until in the URL).
const SECONDS = /^\d{1,12}(\.\d{1,6})?$/;
// The views besides the dashboard (?view=); the guide has no tab (bot onboarding
// spec §3). A settings section and a guide step, each allowlisted.
const VIEWS = ["projects", "settings", "guide"];
// Settings' sections, in page order (settings redesign spec §1).
export const SECTIONS = ["telegram", "notifications", "schedule", "patterns", "page"];
export const STEP = /^[a-z-]{1,32}$/;

export function readUrl(search) {
  const params = new URLSearchParams(search);
  for (const key of Object.keys(state.filters)) state.filters[key] = (params.get(key) || "").slice(0, MAX_FILTER);
  if (!["", "sent", "pending", "failed", "held", "skipped", ATTENTION].includes(state.filters.status)) state.filters.status = "";
  for (const key of ["since", "until"]) if (!SECONDS.test(state.filters[key])) state.filters[key] = "";
  const view = params.get("view");
  if (VIEWS.includes(view)) state.view = view;
  // Each only for its own view; a step unknown to the guide is dropped once it loads.
  const section = params.get("section") || "";
  state.section = state.view === "settings" && SECTIONS.includes(section) ? section : "";
  const step = params.get("step") || "";
  state.step = state.view === "guide" && STEP.test(step) ? step : "";
  const event = params.get("event") || "";
  state.selected = /^[1-9][0-9]{0,14}$/.test(event) ? Number(event) : null;
}

export function urlQuery() {
  const params = new URLSearchParams();
  if (state.view !== "dashboard") params.set("view", state.view);
  if (state.view === "settings" && state.section) params.set("section", state.section);
  if (state.view === "guide" && state.step) params.set("step", state.step);
  for (const [key, value] of Object.entries(state.filters)) if (value) params.set(key, value);
  if (state.selected !== null) params.set("event", String(state.selected));
  return params.toString();
}

export function writeUrl() {
  deps.replaceUrl(urlQuery());
}

// -- queries -------------------------------------------------------------------------

export function eventQuery(offset, limit, beforeId) {
  const f = state.filters;
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (beforeId) params.set("before_id", String(beforeId)); // keyset cursor: older rows only
  if (f.project) params.set("project", f.project);
  if (f.status) params.set("status", f.status);
  if (f.q) params.set("q", f.q);
  // A bucket's own bounds (from the server, so a strip bar and the feed it opens
  // count the same events), else the date inputs' local midnights.
  const fromDate = f.from ? localMidnight(f.from) : null;
  const toDate = f.to ? localMidnight(f.to, 1) : null; // through the end of that day
  const since = f.since ? Number(f.since) : fromDate;
  const until = f.until ? Number(f.until) : toDate;
  if (since !== null) params.set("since", String(since));
  if (until !== null) params.set("until", String(until));
  return params.toString();
}

// Under the search box while it holds exactly one character (diff readability spec
// §2.3): the server searches diff lines only from two, so a one-character search
// that finds only file names doesn't look like a bug.
export const SEARCH_HINT = "2+ characters search inside diffs";

export function searchHint(value) {
  const text = String(value || "").trim();
  return text && !searchTerm(text) ? SEARCH_HINT : "";
}

export function syncSearchHint() {
  const hint = searchHint($("f-q").value);
  setText($("f-q-hint"), hint);
  setHidden($("f-q-hint"), !hint);
}

// How many filters are on, for the Clear button: the project, the status, the
// dates (From/To and a strip bar's since/until together count as one) and the
// search.
export function activeFilterCount(f = state.filters) {
  return [f.project, f.status, f.from || f.to || f.since || f.until, f.q].filter(Boolean).length;
}

export function filtersActive() {
  return activeFilterCount() > 0;
}

// "2 filters" beside Clear, and "Clear 2 filters" as its name; nothing while no
// filter is on.
export function syncFilterCount() {
  const n = activeFilterCount();
  const text = n ? plural(n, "filter", "filters") : "";
  const badge = $("f-clear-count");
  setText(badge, text);
  setHidden(badge, !n);
  const button = $("f-clear");
  if (n) setAttr(button, "aria-label", `Clear ${text}`);
  else if (button.getAttribute("aria-label") !== null) button.removeAttribute("aria-label");
}

// An empty date input: Safari draws today's date in grey, which reads like a
// filter. While empty it gets .date-empty (its text transparent) and the CSS
// shows the "Any date" overlay instead, unless the field is being edited.
export const DATE_INPUTS = ["f-from", "f-to"];

export function syncDateFields() {
  for (const id of DATE_INPUTS) setClass($(id), $(id).value ? "" : "date-empty");
}

// The input's wrapper (span.date-field#f-from-field), which holds the overlay.
export function dateField(id) {
  return $(`${id}-field`);
}

// While a date field is being edited (.date-editing, besides its :focus-within)
// the overlay is hidden and the input's text drawn: Safari keeps focus on an
// inner segment or its picker, where input:focus alone doesn't hold.
export function setDateEditing(id, editing) {
  setClass(dateField(id), editing ? "date-field date-editing" : "date-field");
}

// A poll fetches only the newest page. It replaces the rows it covers (so a row
// that changed or left the filter is updated or dropped) and keeps the older rows
// loaded with "Load more". Ids only grow and the feed is ordered by id, newest
// first, so everything older than the page's last id lies beyond it. When more
// than a page arrived since the last poll (a hidden tab, a daemon restart), the
// page doesn't reach back to the loaded rows: keeping them would leave a gap that
// "Load more" never fills, so they are dropped instead.
export function mergeNewest(page, limit) {
  if (page.length < limit) return page; // the page is the whole result
  const cutoff = page[page.length - 1].id;
  const newestLoaded = state.events.length ? state.events[0].id : 0;
  if (cutoff > newestLoaded) return page; // a gap would open: drop the older rows
  return page.concat(state.events.filter((ev) => ev.id < cutoff));
}

// -- selection -----------------------------------------------------------------------

// The event in the detail pane: the one in ?event=, else the newest.
export function selectedId() {
  if (state.selected !== null) return state.selected;
  return state.events.length ? state.events[0].id : null;
}

export function select(id) {
  state.selected = id;
  state.detailNote = "";
  writeUrl();
  renderFeed();
  deps.selectionChanged();
}

function rowFor(id) {
  return [...$("feed").children].find((node) => node.dataset.key === `e:${id}`) || null;
}

// ↑/↓ and j/k: false when there is nothing to move through.
export function moveSelection(delta) {
  if (!state.events.length) return false;
  const current = selectedId();
  const at = state.events.findIndex((ev) => ev.id === current);
  const next = at < 0 ? 0 : Math.min(state.events.length - 1, Math.max(0, at + delta));
  select(state.events[next].id);
  const row = rowFor(state.events[next].id);
  if (row) {
    row.scrollIntoView({ block: "nearest" });
    row.focus();
  }
  return true;
}

// -- rendering ---------------------------------------------------------------------

// Day headings ("Today", "Yesterday", "Mon 6 Oct") between the rows, in local time.
export function feedItems(events, now) {
  const items = [];
  let day = null;
  for (const ev of events) {
    const key = dayKey(ev.created_at);
    if (key !== day) {
      day = key;
      items.push({ kind: "day", key, first: ev.id, label: dayLabel(ev.created_at, now) });
    }
    items.push({ kind: "event", ev });
  }
  return items;
}

function createRow(id) {
  return h(
    "button",
    { type: "button", class: "row", onclick: () => select(id) },
    h("span", { class: "when" }),
    h("span", { class: "what" }, h("span", { class: "project" }), h("span", { class: "branch" })),
    h(
      "span",
      { class: "size" },
      h("span", { class: "files" }),
      h("span", { class: "in-diff", text: "in diff", title: "The search matched a line of this change's diff" }),
      h("span", { class: "add" }),
      h("span", { class: "del" }),
    ),
    h("span", { class: "state" }, icon("pending"), h("span", { class: "word" })),
  );
}

function updateRow(row, ev, selected, now) {
  setClass(row, selected ? "row selected" : "row");
  setAttr(row, "aria-current", selected ? "true" : "false");
  const [when, what, size, status] = row.children;
  setText(when, feedTime(ev.created_at, now));
  setAttr(when, "title", fullTime(ev.created_at));
  setText(what.children[0], shown(ev.project));
  setText(what.children[1], ev.branch ? shown(ev.branch) : "");
  setText(size.children[0], plural(ev.files.length, "file", "files"));
  setHidden(size.children[1], !ev.q_in_diff);
  setText(size.children[2], ev.added === null || ev.added === undefined ? "" : `+${ev.added}`);
  setText(size.children[3], ev.removed === null || ev.removed === undefined ? "" : `−${ev.removed}`);
  const s = eventStatus(ev, now);
  setClass(status, `state ${s.tone}`);
  setAttr(status, "title", s.title);
  setIcon(status.children[0], s.icon);
  setText(status.children[1], s.word);
}

export function renderFeed() {
  const now = deps.now();
  const selected = selectedId();
  reconcile(
    $("feed"),
    feedItems(state.events, now),
    (item) => (item.kind === "day" ? `d:${item.key}:${item.first}` : `e:${item.ev.id}`), // a day may recur
    (item) => (item.kind === "day" ? h("h3", { class: "day" }) : createRow(item.ev.id)),
    (node, item) => {
      if (item.kind === "day") setText(node, item.label);
      else updateRow(node, item.ev, item.ev.id === selected, now);
    },
  );
  // A row may still be loaded for a moment after it left the total (min).
  const open = renderAttention();
  let count = state.total ? `${Math.min(state.events.length, state.total)} of ${plural(state.total, "event", "events")}` : "";
  // Needs attention without events: the open problems are what the view holds.
  if (!count && open) count = plural(open, "open problem", "open problems");
  setText($("feed-status"), [count, state.feedNote].filter(Boolean).join(" · "));
  setHidden($("more"), state.events.length >= state.total);
  renderFeedEmpty(open);
  syncFeedLayout();
}

// One message and nothing else when the list is empty (Plan 7 spec §3.4): the
// empty list box, and the detail pane while it has nothing to show, are not drawn.
export function syncFeedLayout() {
  const nothing = Boolean(state.dash) && state.total === 0 && state.events.length === 0;
  const noDetail = !state.detail && !state.detailNote && !state.detailError;
  setHidden($("feed"), nothing);
  setHidden($("detail"), nothing && noDetail);
  setClass($("feed-split"), nothing && noDetail ? "split-feed single" : "split-feed");
}

// The open problems above the results while "Needs attention" is selected: the
// same problems the card badges and the header show, as compact rows, plus each
// missing project without its own missing: problem (the header counts it, so the
// view must show it). Rebuilt only when they change, so a poll keeps focus on a
// Copy button. Returns how many rows it lists.
export function renderAttention() {
  const box = $("attention");
  const problems = state.filters.status === ATTENTION && state.dash ? attentionProblems(state.dash) : [];
  setHidden(box, problems.length === 0);
  const key = JSON.stringify(problems.map((q) => [q.key, q.severity, q.message, q.fix, undelivered(q)]));
  if (box.dataset.state !== key) {
    box.dataset.state = key;
    box.replaceChildren(...problems.map(attentionRow));
  }
  return problems.length;
}

function attentionProblems(dash) {
  const problems = dash.problems || [];
  const listed = new Set(problems.map((q) => q.key));
  const missing = (dash.missing || [])
    .filter((m) => !listed.has(`missing:${m.name}`))
    .map((m) => ({ key: `missing:${m.name}`, severity: "warning", message: `project ${m.name}: path does not exist`, fix: null }));
  return [...problems, ...missing];
}

function attentionRow(problem) {
  const serious = problem.severity === "serious";
  const late = undelivered(problem);
  return h(
    "div",
    { class: `attention-row ${serious ? "serious" : "warning"}` },
    h("span", { class: "badge", "aria-hidden": "true", text: serious ? "⚠️" : "ⓘ" }),
    h("span", { class: "attention-message", text: shown(problem.message) }),
    late ? h("span", { class: "problem-late", text: late }) : null,
    problem.fix ? h("code", { class: "attention-fix", text: shown(problem.fix) }) : null,
    problem.fix
      ? h("button", {
          type: "button",
          class: "link copy-fix",
          text: "Copy",
          "aria-label": `Copy the fix: ${shown(problem.message)}`,
          onclick: () => copyFix(problem.fix),
        })
      : null,
  );
}

// "No changes yet", "No events match these filters" (§4.3) or "Nothing needs
// attention"; rebuilt only when the case changes, so a poll doesn't steal focus
// from its button. Under "Needs attention" with open problems, the problems are
// the content: no message.
function renderFeedEmpty(open) {
  const box = $("feed-empty");
  const noProjects = Boolean(state.dash) && !state.dash.projects.length && !state.dash.missing.length;
  let kind = "";
  if (state.dash && !noProjects && state.total === 0) {
    if (state.filters.status === ATTENTION) kind = open ? "" : "attention";
    else kind = filtersActive() ? "filtered" : "quiet";
  }
  if (box.dataset.kind === kind) return;
  box.dataset.kind = kind;
  setHidden(box, kind === "");
  if (kind === "filtered" || kind === "attention") {
    box.replaceChildren(
      h("p", { text: kind === "attention" ? "Nothing needs attention." : "No events match these filters." }),
      h("button", { type: "button", text: "Clear filters", onclick: clearFilters }),
    );
  } else if (kind === "quiet") {
    const message = h("p", { class: "message" });
    box.replaceChildren(
      h("p", { text: "No changes yet. When a watched file changes, it shows up here and in Telegram." }),
      h("button", { type: "button", text: "Send test notification", onclick: (e) => sendTest(e.currentTarget || e.target, message) }),
      message,
    );
  } else {
    box.replaceChildren();
  }
}

export function renderProjectFilter(names) {
  const select = $("f-project");
  const all = [...names];
  if (state.filters.project) all.push(state.filters.project);
  reconcile(
    select,
    ["", ...new Set(all)],
    (n) => `o:${n}`,
    (n) => h("option", { value: n }),
    (option, n) => setText(option, n ? shown(n) : "All projects"),
  );
  if (select.value !== state.filters.project) select.value = state.filters.project;
}

// -- actions -----------------------------------------------------------------------

export async function loadMore() {
  const button = $("more");
  const gen = state.filterGen;
  button.disabled = true;
  try {
    // Keyset cursor: the rows older than the oldest loaded one. An offset would
    // shift (skip or repeat rows) as new events arrive at the top.
    const oldest = state.events.length ? state.events[state.events.length - 1].id : undefined;
    const page = await api("GET", `/api/events?${eventQuery(0, PAGE, oldest)}`);
    // Dropped if the filters changed, or if a poll replaced the rows meanwhile (it
    // dropped the older ones to avoid a gap): rows older than a cursor that is no
    // longer the tail would re-open that gap.
    const tail = state.events.length ? state.events[state.events.length - 1].id : undefined;
    if (gen !== state.filterGen || tail !== oldest) return;
    const seen = new Set(state.events.map((ev) => ev.id));
    state.events = state.events.concat(page.events.filter((ev) => !seen.has(ev.id))).sort((a, b) => b.id - a.id);
    state.total = page.total;
    state.feedNote = "";
    renderFeed();
  } catch (err) {
    if (gen === state.filterGen) {
      // Kept in state, so the next poll's renderFeed doesn't erase it.
      state.feedNote = `Load more failed: ${err.message}`;
      renderFeed();
    }
  } finally {
    if (gen === state.filterGen) button.disabled = false;
  }
}

// Retry keeps Plan 2's semantics: only a failed event, re-queued for the daemon.
export async function retryEvent(ev) {
  // Show "Sending…" straight away; put it back if the server refuses.
  const before = { status: ev.status, attempts: ev.attempts, last_error: ev.last_error, next_attempt_at: ev.next_attempt_at };
  Object.assign(ev, { status: "pending", attempts: 0, last_error: null, next_attempt_at: null });
  renderFeed();
  try {
    await api("POST", `/api/events/${encodeURIComponent(ev.id)}/retry`);
    state.feedNote = "";
    showToast("Retry queued");
    deps.refresh(); // drops a poll already in flight, which may still say "failed"
    return true;
  } catch (err) {
    if (err.status === 409) {
      // Not failed any more (sent or already retried elsewhere): show the truth.
      state.feedNote = `Retry not needed: ${err.message}`;
      deps.refresh();
      return false;
    }
    Object.assign(ev, before);
    state.feedNote = `Retry failed: ${err.message}`;
    showToast(state.feedNote, { kind: "error" });
    renderFeed();
    return false;
  }
}

// `push`: a new history entry instead of replacing the current one (the activity
// strip), so the browser's Back returns to the filters before.
export function setFilter(change, { push = false } = {}) {
  Object.assign(state.filters, change);
  state.selected = null;
  if (push) deps.pushUrl(urlQuery());
  else writeUrl();
  filtersApplied();
}

// The list starts over under the current filters.
function filtersApplied() {
  state.filterGen += 1;
  $("more").disabled = false; // a load begun under the old filters is dropped
  syncFilterInputs();
  state.events = [];
  state.feedNote = "";
  state.detailNote = "";
  deps.filtersChanged();
  deps.refresh();
}

// Back or Forward (popstate): the filters, view and selection of that URL. A URL
// without ?view= is the dashboard's (readUrl only sets the other views).
export function restoreUrl(search) {
  state.view = "dashboard";
  readUrl(search);
  filtersApplied();
}

export function clearFilters() {
  setFilter({ project: "", status: "", from: "", to: "", since: "", until: "", q: "" });
}

export function syncFilterInputs() {
  const f = state.filters;
  const projectSelect = $("f-project");
  if (f.project && ![...projectSelect.children].some((o) => o.value === f.project)) {
    projectSelect.append(h("option", { value: f.project, "data-key": `o:${f.project}`, text: shown(f.project) }));
  }
  for (const [id, value] of [["f-project", f.project], ["f-status", f.status], ["f-from", f.from], ["f-to", f.to]]) {
    if ($(id).value !== value) $(id).value = value;
  }
  // The search box holds f.q untrimmed while typing: don't eat a trailing space.
  if ($("f-q").value.trim() !== f.q) $("f-q").value = f.q;
  syncSearchHint();
  syncDateFields();
  syncFilterCount();
}
