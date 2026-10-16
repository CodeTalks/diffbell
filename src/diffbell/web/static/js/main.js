// diffbell web UI: boot, tabs, polling, the banner and the keyboard. The one
// module that touches the DOM and `window` at load time: it hands the document,
// fetch and the URL to the others. Vanilla ES modules, no build step, CSP-safe
// (no inline script or style; untrusted text only through textContent).
import { api, ApiError, configureApi } from "./api.js";
import {
  allProjects,
  clipboardCopy,
  closeSnoozeMenu,
  dismissSnoozeMenu,
  initDashboard,
  renderCards,
  renderDashEmpty,
  renderHeader,
  renderSetupBanner,
  wireDashboard,
} from "./dashboard.js";
import { syncDetail } from "./detail.js";
import { $, debounce, h, icon, plural, setText, useDocument } from "./dom.js";
import {
  checkTextarea,
  fillEditor,
  loadProjects,
  removeProject,
  renderCounts,
  renderStrip,
  reportSave,
  saveProject,
  schedulePreview,
  syncNotifyTicks,
} from "./editor.js";
import {
  clearFilters,
  DATE_INPUTS,
  dateField,
  eventQuery,
  initFeed,
  loadMore,
  MAX_PAGE,
  mergeNewest,
  moveSelection,
  PAGE,
  readUrl,
  renderFeed,
  renderProjectFilter,
  restoreUrl,
  SECTIONS,
  setDateEditing,
  setFilter,
  syncDateFields,
  syncFilterInputs,
  syncSearchHint,
  writeUrl,
} from "./feed.js";
import { showGuide } from "./guide.js";
import { installKeys } from "./keys.js";
import {
  askLogout,
  cancelLogout,
  discardSettings,
  focusSection,
  spySections,
  initSettings,
  loadSettings,
  retrySettingsLoad,
  logoutAll,
  renderSettings,
  reportSettingsSave,
  saveSettings,
  settingsDirty,
  settingsEdited,
} from "./settings.js";
import { state } from "./state.js";
import {
  initTelegram,
  refreshTelegram,
  renderTelegram,
  resumeTelegramPoll,
  showTelegram,
  syncTestButton,
  wireTelegram,
} from "./telegram.js";
import { currentTheme, nextTheme, themeCookie, themeIcon, themeLabel, THEMES } from "./theme.js";
import { closeToast } from "./toast.js";
import { setTreeFilter } from "./tree.js";

const POLL_MS = 5000;
const START_RETRY_MAX_MS = 30000;
const LINE_LISTS = ["e-patterns", "e-ignore", "s-defaults", "s-ignore", "s-redact"];

let win = null;
let pollNote = ""; // the feed note the last failed refresh set, cleared by a good one
let overlayReturn = null; // what had focus before the shortcut overlay opened

// -- tabs ----------------------------------------------------------------------------

function tabs() {
  return [...win.document.querySelectorAll("[role=tab]")];
}

// `section` (settings) and `step` (guide) belong to their view: any other view, a
// tab click among them, clears them. The guide is a fourth view without a tab: every
// tab is deselected (the first keeps the Tab stop) and #view-guide is shown.
function showView(view, { section = "", step = "" } = {}) {
  state.view = view;
  state.section = view === "settings" ? section : "";
  state.step = view === "guide" ? step : "";
  const all = tabs();
  for (const tab of all) {
    const selected = tab.dataset.view === view;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected || (view === "guide" && tab === all[0]) ? 0 : -1;
    $(`view-${tab.dataset.view}`).hidden = !selected;
  }
  $("view-guide").hidden = view !== "guide";
  writeUrl();
  if (state.loggedOut) return;
  // Fill a form only on its first visit: switching tabs (also with the arrow keys)
  // must not wipe unsaved edits. Reload fetches the file again.
  if (view === "projects") {
    if (!state.projectsLoaded) loadProjects();
    else renderStrip();
  }
  if (view === "settings") {
    if (!state.settings) loadSettings();
    showTelegram();
    focusSection(state.section); // ?section=: scrolled to, its heading focused
    if (!state.section) spy();
  }
  if (view === "guide") showGuide();
}

// The view, section and step that readUrl took from the address.
function showUrlView() {
  showView(state.view, { section: state.section, step: state.step });
}

// The scroll-spy (spec §1): at most once per frame, and only while Settings shows.
let spyFrame = 0;

function spy() {
  const root = win.document.documentElement;
  const y = win.scrollY || 0;
  spySections({ y, atBottom: (win.innerHeight || 0) + y >= (root.scrollHeight || 0) - 2 });
}

function onScroll() {
  if (spyFrame || $("view-settings").hidden) return;
  spyFrame = win.requestAnimationFrame(() => {
    spyFrame = 0;
    spy();
  });
}

// The section index (spec §1): a link opens its section in place, and the address
// gains ?section=. A modified or middle click opens it as the browser does.
function onIndexClick(event) {
  const link = event.target?.closest?.("a[data-section]");
  const section = link ? link.dataset.section : undefined;
  if (!SECTIONS.includes(section)) return;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button > 0) return;
  event.preventDefault();
  state.section = section;
  writeUrl();
  focusSection(section);
}

// WAI-ARIA tabs: one tab in the Tab order (roving tabindex); the arrow keys, Home
// and End move between tabs and select the one they land on.
function onTabKey(event) {
  const all = tabs();
  const i = all.indexOf(event.target);
  if (i < 0) return;
  let next;
  if (event.key === "ArrowRight") next = (i + 1) % all.length;
  else if (event.key === "ArrowLeft") next = (i - 1 + all.length) % all.length;
  else if (event.key === "Home") next = 0;
  else if (event.key === "End") next = all.length - 1;
  else return;
  event.preventDefault();
  showView(all[next].dataset.view);
  all[next].focus();
}

function addProject() {
  showView("projects");
  fillEditor(null);
  $("e-path").focus();
}

// -- banner: failed deliveries, an outage (503), the end of the session ---------------

function renderBanner() {
  const banner = $("banner");
  const parts = [];
  if (state.loggedOut) {
    parts.push(h("span", {}, "Your session has ended. Run ", h("code", { text: "diffbell web" }), " to log in again."));
  } else {
    const failed = (state.dash && state.dash.counts && state.dash.counts.failed) || 0;
    if (failed) {
      parts.push(
        h("span", { text: `${plural(failed, "failed delivery", "failed deliveries")}.` }),
        h("button", {
          type: "button",
          text: "Show failed",
          onclick: () => {
            showView("dashboard");
            setFilter({ status: "failed" });
          },
        }),
      );
    }
    for (const [endpoint, message] of state.outages) {
      parts.push(
        h("span", { text: message }),
        h("button", {
          type: "button",
          text: "Dismiss",
          "aria-label": "Dismiss this message",
          onclick: () => {
            state.outages.delete(endpoint);
            renderBanner();
          },
        }),
      );
    }
  }
  // Re-render only on a change, so the alert isn't re-announced on every poll.
  const key = parts.map((p) => p.textContent).join("\u0000");
  if (key === state.bannerKey) return;
  state.bannerKey = key;
  banner.replaceChildren(...parts);
  banner.hidden = parts.length === 0;
}

function showLoggedOut() {
  state.loggedOut = true;
  clearTimeout(state.timer);
  renderBanner();
}

// -- polling -------------------------------------------------------------------------

async function refresh() {
  if (state.loggedOut) return;
  clearTimeout(state.timer);
  const seq = ++state.refreshSeq;
  // Re-fetch every loaded row (up to the server's max), so rows loaded with "Load
  // more" get their status updates too.
  const limit = Math.min(Math.max(PAGE, state.events.length), MAX_PAGE);
  try {
    const [dash, page] = await Promise.all([
      api("GET", "/api/dashboard"),
      api("GET", `/api/events?${eventQuery(0, limit)}`),
    ]);
    if (seq !== state.refreshSeq) return; // a newer refresh is on its way
    state.dash = dash;
    state.events = mergeNewest(page.events, limit);
    state.total = page.total;
    state.pollFailed = false;
    state.lastPollOk = Date.now();
    if (pollNote && state.feedNote === pollNote) state.feedNote = "";
    pollNote = "";
    renderHeader();
    renderBanner();
    renderSetupBanner();
    renderTelegram(); // the card's held count comes from this poll
    retrySettingsLoad(); // a config that couldn't be read: is it valid again?
    refreshTelegram(); // the card's state: a send failed, or works again
    syncTestButton();
    renderCards();
    renderDashEmpty();
    renderProjectFilter(allProjects().map((p) => p.name));
    renderFeed();
    renderStrip(); // its status icons follow the dashboard too
    syncDetail();
    await reportSave();
    reportSettingsSave();
    renderSettings(); // the quiet-hours preview follows the dashboard's quiet window
  } catch (err) {
    if (state.loggedOut || seq !== state.refreshSeq) return;
    // Only status 0 is "Can't reach the daemon" (spec §5); the last data stays on
    // screen either way.
    state.pollFailed = err instanceof ApiError && err.status === 0;
    if (!(err instanceof ApiError)) {
      console.error(err); // a bug in the page, not the daemon
    } else {
      // Status 0 is told by the status line and a 503 by the outage banner: the
      // feed only notes other errors, and drops an earlier poll's note either way.
      const note = err.status === 0 || err.status === 503 ? "" : err.message;
      if (pollNote && state.feedNote === pollNote) state.feedNote = "";
      if (note) state.feedNote = note;
      pollNote = note;
      renderFeed();
    }
    renderHeader();
  }
  schedule();
}

function schedule() {
  clearTimeout(state.timer);
  if (!state.loggedOut && win.document.visibilityState === "visible") {
    state.timer = setTimeout(refresh, POLL_MS);
  }
}

// -- keyboard ------------------------------------------------------------------------

function overlayOpen() {
  return !$("keys-overlay").hidden;
}

// A modal: focus moves in on open and back on close, Tab stays inside (its one
// control is Close), and a click on the backdrop closes it.
function showOverlay(open) {
  if (open === overlayOpen()) return;
  $("keys-overlay").hidden = !open;
  if (open) {
    overlayReturn = win.document.activeElement;
    $("keys-close").focus();
    return;
  }
  const back = overlayReturn;
  overlayReturn = null;
  // What had focus may have been rebuilt meanwhile: then the overlay's button.
  if (back && back.isConnected && typeof back.focus === "function") back.focus();
  else $("keys-open").focus();
}

// On the document, so Tab stays trapped even when focus has left the dialog.
function onOverlayKey(event) {
  if (event.key !== "Tab" || !overlayOpen()) return;
  event.preventDefault();
  $("keys-close").focus();
}

function escape(target) {
  if (overlayOpen()) {
    showOverlay(false);
    return true;
  }
  if (closeSnoozeMenu()) return true;
  if (closeToast()) return true;
  if (target === $("f-q") && target.value) {
    target.value = "";
    syncSearchHint();
    setFilter({ q: "" });
    return true;
  }
  if (target === $("tree-filter") && target.value) {
    target.value = "";
    setTreeFilter("");
    return true;
  }
  return false;
}

function runKey(action, event) {
  if (overlayOpen() && action !== "help" && action !== "escape") return false;
  switch (action) {
    case "save-project":
      if (!$("e-save").disabled) saveProject();
      return true;
    case "save-settings":
      saveSettings(); // it saves only changed fields the last check found valid
      return true;
    case "escape":
      return escape(event.target);
    case "focus-search":
      $("f-q").focus();
      return true;
    case "focus-tree-filter":
      $("tree-filter").focus();
      return true;
    case "next":
      return moveSelection(1);
    case "previous":
      return moveSelection(-1);
    case "help":
      showOverlay(!overlayOpen());
      return true;
    default:
      return false;
  }
}

// -- wiring --------------------------------------------------------------------------

// A click or focus anywhere but the open snooze menu's card tools closes the menu.
// Walks parentNode (not closest/contains) so the node tests' fake DOM can run it.
function dismissOutside(event) {
  const name = state.snoozeMenu;
  const target = event.target;
  if (name === null || !target) return;
  // Walk the path as dispatched: a handler may have detached the target (the snooze
  // button re-renders the tools as it opens the menu; a toast's close button removes
  // the toast). Only without any path information is a detached target left alone.
  let path = typeof event.composedPath === "function" ? event.composedPath() : [];
  if (path.length === 0) {
    if (target.isConnected === false) return;
    for (let n = target; n; n = n.parentNode) path.push(n);
  }
  for (let i = 0; i < path.length; i += 1) {
    const n = path[i];
    const cls = typeof n.getAttribute === "function" ? String(n.getAttribute("class") || "") : "";
    const wrap = path[i + 1];
    if (cls.split(" ").includes("card-tools") && wrap && wrap.dataset && wrap.dataset.key === `p:${name}`) return;
  }
  dismissSnoozeMenu();
}

// The theme (settings redesign spec §5): data-theme on <html> (the server sets it
// from the cookie on load), the header button's icon and label, and the radio of
// Settings → This page that is checked.
function showTheme(theme) {
  const root = win.document.documentElement;
  if (theme === "system") delete root.dataset.theme;
  else root.dataset.theme = theme;
  const button = $("theme-toggle");
  button.replaceChildren(icon(themeIcon(theme)));
  button.setAttribute("aria-label", themeLabel(theme));
  button.title = themeLabel(theme);
  for (const t of THEMES) $(`s-theme-${t}`).checked = t === theme;
}

// A choice applies at once and is kept in the cookie for the next load: this
// browser only, never the save bar or the daemon.
function chooseTheme(theme) {
  win.document.cookie = themeCookie(theme);
  showTheme(theme);
}

function wire() {
  const doc = win.document;
  for (const tab of tabs()) tab.addEventListener("click", () => showView(tab.dataset.view));
  doc.querySelector("[role=tablist]").addEventListener("keydown", onTabKey);
  $("f-project").addEventListener("change", (e) => setFilter({ project: e.target.value }));
  $("f-status").addEventListener("change", (e) => setFilter({ status: e.target.value }));
  // A date typed by hand replaces a strip bar's exact bounds.
  $("f-from").addEventListener("change", (e) => setFilter({ from: e.target.value, since: "", until: "" }));
  $("f-to").addEventListener("change", (e) => setFilter({ to: e.target.value, since: "", until: "" }));
  for (const id of DATE_INPUTS) {
    const input = $(id);
    const field = dateField(id);
    // "Any date" goes as soon as a date is picked or typed (before change commits it).
    input.addEventListener("input", syncDateFields);
    // Editing, from the press on: Safari may focus an inner segment or its picker.
    field.addEventListener("focusin", () => setDateEditing(id, true));
    field.addEventListener("pointerdown", () => setDateEditing(id, true));
    field.addEventListener("focusout", (e) => {
      if (e.relatedTarget && field.contains(e.relatedTarget)) return; // still inside
      setDateEditing(id, false);
      syncDateFields();
    });
    input.addEventListener("change", () => {
      if (doc.activeElement !== input) setDateEditing(id, false);
    });
  }
  $("f-q").addEventListener("input", syncSearchHint); // at once, not after the pause
  $("f-q").addEventListener("input", debounce((e) => setFilter({ q: e.target.value.trim() }), 300));
  $("f-clear").addEventListener("click", clearFilters);
  $("filters").addEventListener("submit", (e) => e.preventDefault());
  $("more").addEventListener("click", loadMore);
  wireDashboard();

  $("editor").addEventListener("submit", saveProject);
  $("editor").addEventListener("input", (e) => {
    state.editorDirty = true;
    if (e.target.id === "e-patterns" || e.target.id === "e-ignore") {
      checkTextarea(e.target.id); // the cheap checks at once, the server's after the pause
      renderCounts();
    }
    schedulePreview();
  });
  $("editor").addEventListener("change", () => {
    state.editorDirty = true;
    syncNotifyTicks();
    schedulePreview();
  });
  $("e-remove").addEventListener("click", removeProject);
  $("e-reload").addEventListener("click", () => loadProjects());
  $("tree-filter").addEventListener("input", (e) => setTreeFilter(e.target.value));

  $("settings").addEventListener("submit", saveSettings);
  $("settings").addEventListener("input", settingsEdited);
  $("settings").addEventListener("change", settingsEdited);
  $("s-reload").addEventListener("click", loadSettings);
  $("s-load-retry").addEventListener("click", loadSettings);
  $("s-discard").addEventListener("click", discardSettings);
  $("s-logout").addEventListener("click", askLogout);
  $("s-logout-yes").addEventListener("click", logoutAll);
  $("s-logout-no").addEventListener("click", cancelLogout);
  $("s-index").addEventListener("click", onIndexClick);
  win.addEventListener("scroll", onScroll, { passive: true });
  wireTelegram();
  $("guide-back").addEventListener("click", (e) => {
    // A modified or middle click opens the link (?view=settings&section=telegram)
    // in a new tab or window, as the browser does it.
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button > 0) return;
    e.preventDefault();
    showView("settings", { section: "telegram" }); // focus moves to the card's heading
  });

  for (const id of LINE_LISTS) {
    $(id).addEventListener("scroll", () => {
      $(`${id}-gutter`).scrollTop = $(id).scrollTop;
    });
  }
  showTheme(currentTheme(doc.documentElement.dataset.theme));
  for (const theme of THEMES) {
    $(`s-theme-${theme}`).addEventListener("change", () => chooseTheme(theme));
  }
  $("theme-toggle").addEventListener("click", () =>
    chooseTheme(nextTheme(currentTheme(doc.documentElement.dataset.theme))),
  );
  $("keys-open").addEventListener("click", () => showOverlay(true));
  $("keys-close").addEventListener("click", () => showOverlay(false));
  $("keys-overlay").addEventListener("click", (e) => {
    if (e.target === $("keys-overlay")) showOverlay(false);
  });
  doc.addEventListener("click", dismissOutside);
  doc.addEventListener("focusin", dismissOutside);
  installKeys(doc, () => state.view, runKey);
  doc.addEventListener("keydown", onOverlayKey);
  doc.addEventListener("visibilitychange", () => {
    // Before the session is known, start() is still retrying on its own.
    if (doc.visibilityState !== "visible") clearTimeout(state.timer);
    else if (state.csrf) {
      refresh();
      resumeTelegramPoll();
    }
  });
}

async function start() {
  try {
    state.csrf = (await api("GET", "/api/session")).csrf;
  } catch (err) {
    if (err.status === 401) return; // the banner already shows the login hint
    // The daemon may be starting or restarting: retry with a capped backoff.
    const delay = state.startDelay;
    state.startDelay = Math.min(delay * 2, START_RETRY_MAX_MS);
    state.pollFailed = true;
    renderHeader();
    setText($("refreshed"), `retrying in ${Math.round(delay / 1000)} s`);
    setTimeout(start, delay);
    return;
  }
  showUrlView();
  refresh();
}

function boot(w) {
  win = w;
  useDocument(w.document);
  configureApi({
    fetch: (path, options) => w.fetch(path, options),
    loggedOut: showLoggedOut,
    outagesChanged: renderBanner,
  });
  initFeed({
    refresh,
    replaceUrl: (query) => w.history.replaceState(null, "", query ? `?${query}` : w.location.pathname),
    pushUrl: (query) => w.history.pushState(null, "", query ? `?${query}` : w.location.pathname),
    now: () => Date.now(),
    selectionChanged: () => syncDetail(),
    filtersChanged: renderCards, // a card shows whether it is the project filter
  });
  initDashboard({
    now: () => Date.now(),
    showView,
    addProject,
    refresh,
    copy: clipboardCopy(w.navigator),
  });
  initSettings({ loggedOut: showLoggedOut, now: () => Date.now(), scrollY: () => w.scrollY || 0 });
  initTelegram({
    // The card is on screen: the wait for /start pauses on another view or tab.
    visible: () => w.document.visibilityState === "visible" && state.view === "settings",
    showView,
    // "Show failed changes": the dashboard banner's own "Show failed".
    showFailed: () => {
      showView("dashboard");
      setFilter({ status: "failed" });
    },
    refresh,
    now: () => Date.now(),
  });
  readUrl(w.location.search);
  wire();
  // Back after a strip click (a pushed entry): that URL's filters again.
  w.addEventListener("popstate", () => {
    restoreUrl(w.location.search);
    showUrlView();
  });
  // Unsaved settings (spec §2): the browser asks before the page goes, in its own
  // words; the page only sets returnValue.
  w.addEventListener("beforeunload", (event) => {
    if (!settingsDirty()) return;
    event.preventDefault();
    event.returnValue = "";
  });
  syncFilterInputs();
  start();
}

boot(window);
