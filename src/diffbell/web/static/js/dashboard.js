// The project cards with their snooze controls and problem badges, the header's
// status sentence and the first-run empty state (UI refine spec §4.2, §4.3; notify
// controls spec §5.2; health spec §6). Import-safe: main.js hands in navigation, the
// poll and the clipboard (initDashboard).
import { api, errorSentence } from "./api.js";
import { $, h, icon, plural, reconcile, setAttr, setClass, setHidden, setIcon, setMessage, setText, shown } from "./dom.js";
import { ATTENTION, setFilter } from "./feed.js";
import { state } from "./state.js";
import { footerVersion, projectStatus, staleText, statusSentence, undelivered } from "./status.js";
import { ago, dayTitle, untilText } from "./time.js";
import { showToast } from "./toast.js";

export const UNDO_SNOOZE_MS = 10000;

const deps = {
  now: () => Date.now(),
  showView: () => {},
  addProject: () => {},
  refresh: () => {},
  // navigator.clipboard.writeText, handed in by main.js.
  copy: async () => {
    throw new Error("no clipboard");
  },
};

export function initDashboard(options) {
  Object.assign(deps, options);
}

export function allProjects() {
  const d = state.dash;
  if (!d) return [];
  return [...d.projects, ...d.missing.map((m) => ({ ...m, status: "missing" }))];
}

// "12 files", "500+ files" (the walk stopped at a cap), "counting…", or "—" while
// the project's watch is lost (its `project:<name>` problem is open: the server
// forgets the count then, diff readability spec §4.5).
export function watchedText(p, problems = []) {
  if (p.status === "missing") return "path does not exist";
  if (watchLost(p, problems)) return "—";
  if (p.watched_count === null || p.watched_count === undefined) return "counting…";
  const n = p.watched_count;
  const exact = p.watched_exact !== false;
  return `${n}${exact ? "" : "+"} ${n === 1 && exact ? "file" : "files"}`; // never "1+ file"
}

export function watchLost(p, problems = []) {
  return problems.some((q) => q.key === `project:${p.name}`);
}

// The count's title when the walk stopped at its limit (spec §4.4), else "".
export function watchedTitle(p) {
  if (p.status === "missing" || p.watched_exact !== false || p.watched_count === null || p.watched_count === undefined) return "";
  return `At least ${p.watched_count} key files; counting stopped early in this large folder.`;
}

// The card's status line: "Watching", "Watching · snoozed until 14:00" or
// "Watching · snoozed" (spec §4.1).
export function cardStatus(p, now) {
  const word = shown(projectStatus(p).word);
  if (!p.snooze) return word;
  return `${word} · ${snoozedText(p.snooze, now).replace(/^Snoozed/, "snoozed")}`;
}

// -- snooze (notify controls spec §5.2) ------------------------------------------------

// The menu's choices; "tomorrow 9:00" is built from local date parts (DST-safe).
export function snoozeOptions(now) {
  const d = new Date(now);
  const tomorrow = new Date(d.getFullYear(), d.getMonth(), d.getDate() + 1, 9, 0, 0, 0);
  return [
    { label: "1 hour", until: now / 1000 + 3600 },
    { label: "4 hours", until: now / 1000 + 4 * 3600 },
    { label: "Until tomorrow 9:00", until: tomorrow.getTime() / 1000 },
    { label: "Until I turn it back on", until: null },
  ];
}

// "Snoozed until 16:00", "Snoozed until tomorrow 09:00", or "Snoozed".
export function snoozedText(snooze, now) {
  return snooze.until ? `Snoozed until ${untilText(snooze.until, now)}` : "Snoozed";
}

// A project's dashboard row; a missing one too (it may be snoozed).
function projectNamed(name) {
  if (!state.dash) return null;
  return [...state.dash.projects, ...(state.dash.missing || [])].find((p) => p.name === name) || null;
}

export function toggleSnoozeMenu(name) {
  state.snoozeMenu = state.snoozeMenu === name ? null : name;
  renderCards();
  if (state.snoozeMenu === name) {
    const first = findFirst(cardOf(name), "menu-item");
    if (first) first.focus();
  }
}

// A click or focus outside the open menu closes it, leaving focus where it went.
export function dismissSnoozeMenu() {
  if (state.snoozeMenu === null) return false;
  state.snoozeMenu = null;
  renderCards();
  return true;
}

// The menu's keyboard (WAI-ARIA menu): the arrows move between items and wrap,
// Home and End jump; none reaches the page's shortcuts (↓ would move the feed's
// selection). Tab closes it with focus back on its button, so Tab goes on from there.
function onMenuKey(event) {
  const items = [...event.currentTarget.children];
  const i = items.indexOf(event.target);
  let next;
  if (event.key === "ArrowDown") next = (i + 1) % items.length;
  else if (event.key === "ArrowUp") next = (i - 1 + items.length) % items.length;
  else if (event.key === "Home") next = 0;
  else if (event.key === "End") next = items.length - 1;
  else {
    if (event.key === "Tab") closeSnoozeMenu();
    return;
  }
  event.preventDefault();
  event.stopPropagation();
  items[next].focus();
}

// True if a menu was open (Esc closes it, and focus goes back to its button).
export function closeSnoozeMenu() {
  const name = state.snoozeMenu;
  if (name === null) return false;
  state.snoozeMenu = null;
  renderCards();
  const button = findFirst(cardOf(name), "snooze");
  if (button) button.focus();
  return true;
}

// Projects with a snooze or resume request in flight: a second press is ignored.
const busy = new Set();

export async function snooze(name, until) {
  if (busy.has(name)) return false;
  state.snoozeMenu = null;
  renderCards();
  let result;
  busy.add(name);
  try {
    result = await api("POST", `/api/projects/${encodeURIComponent(name)}/snooze`, { until });
  } catch (err) {
    focusTool(name, "snooze"); // the menu item that had focus is gone
    showToast(`Snooze failed: ${err.message}`, { kind: "error" });
    return false;
  } finally {
    busy.delete(name);
  }
  const project = projectNamed(name);
  if (project) project.snooze = result.snooze;
  renderCards();
  focusTool(name, "resume"); // the menu (and its focused item) was replaced
  const text = until === null ? `Snoozed ${shown(name)}` : `Snoozed ${shown(name)} until ${untilText(until, deps.now())}`;
  showToast(text, { action: "Undo", onAction: () => resume(name), ms: UNDO_SNOOZE_MS });
  deps.refresh();
  return true;
}

// Resume (and the snooze toast's Undo): notifications back on; anything held is
// sent as one catch-up message.
export async function resume(name) {
  if (busy.has(name)) return false;
  busy.add(name);
  try {
    await api("DELETE", `/api/projects/${encodeURIComponent(name)}/snooze`);
  } catch (err) {
    showToast(`Resume failed: ${err.message}`, { kind: "error" });
    return false;
  } finally {
    busy.delete(name);
  }
  const project = projectNamed(name);
  if (project) project.snooze = null;
  renderCards();
  focusTool(name, "snooze"); // Resume (which had focus) was replaced
  showToast(`Notifications on for ${shown(name)}`);
  deps.refresh();
  return true;
}

// Focus a card's snooze or resume control once the tools were rebuilt.
function focusTool(name, cls) {
  const control = findFirst(cardOf(name), cls);
  if (control) control.focus();
}

function cardOf(name) {
  return [...$("cards").children].find((node) => node.dataset.key === `p:${name}`) || null;
}

function findFirst(root, cls) {
  if (!root) return null;
  for (const el of [...root.children]) {
    if (String(el.getAttribute("class") || "").split(" ").includes(cls)) return el;
    const inner = findFirst(el, cls);
    if (inner) return inner;
  }
  return null;
}

// The snooze control in the card's corner (the bell, or Resume while snoozed: the
// status line says until when), rebuilt only when what it shows changes, so a poll
// never takes focus from a menu item.
function renderTools(tools, p, now) {
  const open = state.snoozeMenu === p.name;
  const snoozed = p.snooze ? snoozedText(p.snooze, now) : "";
  const missing = p.status === "missing";
  const key = `${missing ? "missing" : ""}|${snoozed}|${open}`;
  if (tools.dataset.state === key) return;
  tools.dataset.state = key;
  // A missing project has nothing to snooze, but one snoozed before its folder
  // went missing can still be resumed.
  if (missing && !p.snooze) {
    tools.replaceChildren();
    return;
  }
  if (p.snooze) {
    tools.replaceChildren(
      h(
        "button",
        { type: "button", class: "link resume", "aria-label": `Resume ${shown(p.name)}`, title: `${snoozed}: turn notifications back on`, onclick: () => resume(p.name) },
        "Resume",
      ),
    );
    return;
  }
  const button = h(
    "button",
    {
      type: "button",
      class: "icon-button snooze",
      "aria-label": `Snooze ${shown(p.name)}`,
      title: "Snooze notifications",
      "aria-haspopup": "menu",
      "aria-expanded": String(open),
      onclick: () => toggleSnoozeMenu(p.name),
    },
    icon("snooze"),
  );
  const parts = [button];
  if (open) {
    parts.push(
      h(
        "div",
        { class: "menu", role: "menu", "aria-label": `Snooze ${shown(p.name)}`, onkeydown: onMenuKey },
        snoozeOptions(now).map((option, i) =>
          h("button", {
            type: "button",
            class: "menu-item",
            role: "menuitem",
            text: option.label,
            // The time is computed at the click: the menu may have been open a while.
            onclick: () => snooze(p.name, snoozeOptions(deps.now())[i].until),
          }),
        ),
      ),
    );
  }
  tools.replaceChildren(...parts);
}

// -- problems (health spec §6) --------------------------------------------------------

export const COPIED = "Fix copied";

// The copy function for initDashboard: navigator.clipboard.writeText, or an error
// that says why there is none (an insecure origin, an old browser).
export function clipboardCopy(navigator) {
  return async (text) => {
    const clipboard = navigator && navigator.clipboard;
    if (!clipboard) throw new Error("the clipboard is not available here");
    await clipboard.writeText(text);
  };
}

// Copies text (a fix, a diff, a command: the page never runs it) and says so with
// `done`, or says why it could not.
export async function copyText(text, done) {
  try {
    await deps.copy(text);
  } catch (err) {
    showToast(`Copy failed: ${err.message || err}`, { kind: "error" });
    return false;
  }
  showToast(done);
  return true;
}

// Copies a suggested fix and says so.
export function copyFix(fix) {
  return copyText(fix, COPIED);
}

// One problem of a project: a ⚠️ (serious) or ⓘ (warning) badge whose details show
// on hover and on focus: the message, an undelivered alert, and the fix to copy.
export function problemBadge(problem, name) {
  const serious = problem.severity === "serious";
  const detail = [h("span", { class: "problem-message", text: shown(problem.message) })];
  const late = undelivered(problem);
  if (late) detail.push(h("span", { class: "problem-late", text: late }));
  if (problem.fix) {
    detail.push(
      h("code", { class: "problem-fix", text: shown(problem.fix) }),
      h("button", {
        type: "button",
        class: "link copy-fix",
        "aria-label": `Copy the fix for ${shown(name)}`,
        text: "Copy",
        onclick: () => copyFix(problem.fix),
      }),
    );
  }
  return h(
    "div",
    {
      class: `problem ${serious ? "serious" : "warning"}`,
      tabindex: "0",
      title: shown(problem.message),
      "aria-label": `${serious ? "Problem" : "Warning"}: ${shown(problem.message)}`,
    },
    h("span", { class: "badge", "aria-hidden": "true", text: serious ? "⚠️" : "ⓘ" }),
    h("span", { class: "problem-detail" }, ...detail),
  );
}

// Rebuilt only when what the badges say changes, so a poll keeps focus on them.
function renderProblems(box, problems, name) {
  const key = JSON.stringify(problems.map((q) => [q.key, q.severity, q.message, q.fix, undelivered(q)]));
  if (box.dataset.state === key) return;
  box.dataset.state = key;
  box.replaceChildren(...problems.map((q) => problemBadge(q, name)));
}

// -- the activity strip (Plan 7 spec §3.1) ----------------------------------------------

export const ACTIVITY_LABEL = "Activity, last 14 days";

// h0 (a 2 px stub) for a day without events; otherwise h1..h10 scaled to the
// busiest day of the window, never below h1: a 1-of-1000 day must not look empty.
export function barLevel(total, max) {
  if (!(total > 0) || !(max > 0)) return 0;
  return Math.min(10, Math.max(1, Math.round((total / max) * 10)));
}

// "Thu 8 Oct · 4 changes (1 failed)", "… · 1 change", "… · no changes".
export function barTitle(bucket) {
  const count = bucket.total ? plural(bucket.total, "change", "changes") : "no changes";
  const failed = bucket.failed ? ` (${bucket.failed} failed)` : "";
  return `${dayTitle(bucket.day)} · ${count}${failed}`;
}

function activityOf(name) {
  const activity = state.dash && state.dash.activity;
  return (activity && activity[name]) || [];
}

// The feed for that project and day, with the server's own since/until for the
// bucket (never re-derived from its label), and no status or search filter, so the
// list shows exactly what the bar counted; a new history entry, so Back undoes it.
export function showDay(name, bucket) {
  setFilter(
    { project: name, status: "", q: "", from: bucket.day, to: bucket.day, since: String(bucket.since), until: String(bucket.until) },
    { push: true },
  );
}

// One tab stop per strip (roving tabindex): ←/→ (and Home/End) move between days,
// Enter shows the focused one.
function onStripKey(event, name) {
  const bars = [...event.currentTarget.children];
  const i = bars.indexOf(event.target);
  if (i < 0) return;
  let next;
  if (event.key === "ArrowLeft") next = Math.max(0, i - 1);
  else if (event.key === "ArrowRight") next = Math.min(bars.length - 1, i + 1);
  else if (event.key === "Home") next = 0;
  else if (event.key === "End") next = bars.length - 1;
  else if (event.key === "Enter") {
    event.preventDefault(); // the button's own click would apply it a second time
    bars[i].click(); // the bucket this bar shows, as a mouse click would
    return;
  } else return;
  event.preventDefault();
  moveTabStop(bars, next);
  bars[next].focus();
}

function moveTabStop(bars, next) {
  bars.forEach((bar, j) => bar.setAttribute("tabindex", j === next ? "0" : "-1"));
}

// Rebuilt only when the buckets change (a poll with the same data keeps the nodes
// and focus); a rebuild keeps the tab stop and the focus on the same day (after
// midnight that is one bar to the left), or on the same place if the day is gone.
function renderActivity(box, name) {
  const buckets = activityOf(name);
  const key = JSON.stringify(buckets.map((b) => [b.day, b.since, b.until, b.total, b.failed]));
  if (box.dataset.state === key) return;
  box.dataset.state = key;
  const old = [...box.children];
  const last = buckets.length - 1;
  const sameDay = (i) => {
    if (i < 0) return -1;
    const at = buckets.findIndex((b) => b.day === old[i].dataset.day);
    return at >= 0 ? at : Math.min(i, last);
  };
  const focused = sameDay(old.indexOf(box.ownerDocument.activeElement));
  const stop = sameDay(old.findIndex((bar) => bar.getAttribute("tabindex") === "0"));
  const tabStop = stop >= 0 ? stop : last; // today's bar at first
  const max = Math.max(0, ...buckets.map((b) => b.total));
  box.replaceChildren(
    ...buckets.map((bucket, i) => {
      const title = barTitle(bucket);
      return h(
        "button",
        {
          type: "button",
          class: `bar h${barLevel(bucket.total, max)}${bucket.failed ? " failed" : ""}`,
          title,
          "aria-label": title, // a title alone is not announced
          tabindex: i === tabStop ? "0" : "-1",
          "data-day": bucket.day,
          onclick: () => {
            moveTabStop([...box.children], i); // a click moves the tab stop, as the keys do
            showDay(name, bucket);
          },
        },
        h("span", { class: "fill" }),
      );
    }),
  );
  if (focused >= 0 && box.children.length) box.children[focused].focus();
}

function createCard(name) {
  return h(
    "div",
    { class: "card-wrap" },
    h(
      "button",
      {
        type: "button",
        class: "card",
        onclick: () => setFilter({ project: state.filters.project === name ? "" : name }),
      },
      h("span", { class: "card-title" }, icon("pending"), h("span", { class: "name" })),
      h("span", { class: "state" }),
      h("span", { class: "meta" }),
    ),
    // The corner (spec §4.1): the problem badges, then the snooze bell. Outside the
    // card's button (no control inside a control), positioned over its corner.
    h("div", { class: "card-tools" }, h("div", { class: "card-problems" }), h("div", { class: "card-snooze" })),
    // The 14-day strip (Plan 7 spec §3.1): outside the button too, drawn at its foot.
    h("div", { class: "activity", role: "group", "aria-label": ACTIVITY_LABEL, onkeydown: (e) => onStripKey(e, name) }),
  );
}

// How much room the card's title leaves for its corner (app.css .corner-*): the bell
// and each badge take one slot; Resume is wider than the bell and counts as two.
function cornerClass(p, badges) {
  if (p.snooze && badges === 0) return "corner-resume";
  const slots = badges + (p.snooze ? 2 : p.status === "missing" ? 0 : 1);
  return slots >= 3 ? "corner-3plus" : `corner-${slots}`;
}

function updateCard(wrap, p, now) {
  const [card, corner, activity] = wrap.children;
  const [problems, tools] = corner.children;
  renderActivity(activity, p.name);
  const all = (state.dash && state.dash.problems) || [];
  renderTools(tools, p, now);
  const own = all.filter((q) => q.project === p.name);
  renderProblems(problems, own, p.name);
  setClass(wrap, `card-wrap ${cornerClass(p, own.length)}`);
  const active = state.filters.project === p.name;
  const s = projectStatus(p);
  setClass(card, `card ${s.tone}${active ? " active" : ""}`);
  setAttr(card, "aria-pressed", String(active));
  setAttr(card, "title", active ? "Clear the project filter" : "Show this project's activity");
  const [title, status, meta] = card.children;
  setIcon(title.children[0], s.icon);
  setText(title.children[1], shown(p.name));
  // The name may end in an ellipsis: the title has it whole, with the folder.
  const folder = p.folder || p.path;
  setAttr(title.children[1], "title", folder ? `${shown(p.name)} — ${shown(folder)}` : shown(p.name));
  setText(status, cardStatus(p, now));
  const when = p.status === "missing" ? "" : ` · ${ago(p.last_change_at, deps.now())}`;
  setText(meta, `${watchedText(p, all)}${when}`);
  const counted = watchedTitle(p);
  if (counted) setAttr(meta, "title", counted);
  else if (meta.getAttribute("title") !== null) meta.removeAttribute("title");
}

export function renderCards() {
  // Sorted by name: the daemon's order may change on a reload, and reconcile moves
  // a card whose position changed (dropping its keyboard focus).
  const projects = allProjects().sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  const now = deps.now();
  reconcile(
    $("cards"),
    projects,
    (p) => `p:${p.name}`,
    (p) => createCard(p.name),
    (node, p) => updateCard(node, p, now),
  );
}

// "No projects yet" with the button that opens the add form.
export function renderDashEmpty() {
  const box = $("dash-empty");
  const empty = Boolean(state.dash) && allProjects().length === 0;
  if (box.hidden === !empty && box.dataset.ready === "1") return;
  box.dataset.ready = "1";
  setHidden(box, !empty);
  box.replaceChildren(
    ...(empty
      ? [
          h("h2", { text: "No projects yet" }),
          h("p", { class: "muted", text: "Pick a folder and the key files to watch in it." }),
          h("button", { type: "button", class: "primary", text: "Add your first project", onclick: () => deps.addProject() }),
        ]
      : []),
  );
}

// The header: one sentence, the first matching state (§4.2), plus "Updated N s
// ago" once the last good poll is more than 15 s old.
export function renderHeader() {
  const now = deps.now();
  const sentence = statusSentence({ pollFailed: state.pollFailed, dash: state.dash, now });
  setClass($("status-line"), `status-line ${sentence.tone}`);
  setIcon($("status-icon"), sentence.icon);
  const text = $("status-text");
  const link = $("status-attention");
  setHidden(text, sentence.link);
  setHidden(link, !sentence.link);
  setText(sentence.link ? link : text, sentence.text);
  setAttr(sentence.link ? link : text, "title", sentence.text); // the whole sentence, when cut
  setText($("status-live"), sentence.live); // written only when it changes, so announced once
  setText($("refreshed"), staleText(state.lastPollOk, now));
  setText($("foot-version"), footerVersion(state.dash));
}

// "Telegram isn't set up — N changes are waiting. Set up ▸" (bot onboarding spec
// §3), while the dashboard poll says Telegram isn't configured. N counts every held
// event, snoozed or not. Pure: "" when there is nothing to say.
export function setupBannerText(dash) {
  if (!dash || !dash.telegram || dash.telegram.configured !== false) return "";
  const held = (dash.counts && dash.counts.held) || 0;
  if (!held) return "Telegram isn't set up — nothing is sent until it is.";
  return `Telegram isn't set up — ${plural(held, "change is", "changes are")} waiting.`;
}

export function renderSetupBanner() {
  const text = setupBannerText(state.dash);
  setText($("setup-text"), text);
  setHidden($("setup-banner"), !text);
}

// The header's "N projects need attention" (or a serious problem) opens the
// "Needs attention" view: failed and held events, the open problems above them,
// with every other filter cleared (a project or day filter would hide some). A new
// history entry, so Back brings the cleared filters (and the tab) back: it is
// pushed before showView writes the URL, which would otherwise replace the entry
// of the tab the link was clicked on.
export function wireDashboard() {
  $("setup-open").addEventListener("click", () => deps.showView("settings", { section: "telegram" }));
  $("status-attention").addEventListener("click", () => {
    state.view = "dashboard"; // the pushed URL carries no ?view=
    setFilter({ project: "", status: ATTENTION, from: "", to: "", since: "", until: "", q: "" }, { push: true });
    deps.showView("dashboard");
  });
}

// "Send test notification" (the empty feed and Settings → Tools). The daemon
// answers once Telegram did, retries included, so it gets longer than a poll.
export const TEST_NOTIFICATION_TIMEOUT_MS = 60000;

export async function sendTest(button, message) {
  button.disabled = true;
  setMessage(message, "Sending…");
  try {
    // banner: false: the message next to the button says what went wrong.
    await api("POST", "/api/test-notification", undefined, { timeout: TEST_NOTIFICATION_TIMEOUT_MS, banner: false });
    setMessage(message, "Sample notification sent.", "ok");
  } catch (err) {
    // The server's sentence says what went wrong and what to do: no status label.
    setMessage(message, errorSentence(err), "error");
  } finally {
    button.disabled = false;
  }
}
