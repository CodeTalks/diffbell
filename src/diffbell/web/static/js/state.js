// The page's state, shared by every module (the only shared mutable export).
// Import-safe: plain data, no DOM, no fetch.

export const state = {
  csrf: "",
  view: "dashboard",
  section: "", // ?section= of the settings view: "telegram" scrolls to its card
  step: "", // ?step= of the guide view: a section id of guide.json
  // since/until: a bucket's exact bounds (epoch seconds) from the activity strip;
  // they win over from/to, which then only show its day in the date inputs.
  filters: { project: "", status: "", from: "", to: "", since: "", until: "", q: "" },
  events: [],
  total: 0,
  selected: null, // the event id in ?event=, or null for the newest one
  detail: null, // GET /api/events/{id} of the event in the detail pane
  detailKey: "", // "id:status:attempts" of that fetch: refetched only when it changes
  detailNote: "", // "That event is no longer in history"
  detailError: "", // the last failed detail fetch
  detailSeq: 0,
  collapsed: new Set(), // "eventId:fileIndex" of collapsed file sections
  searchToggled: new Set(), // sections toggled by hand during the search in searchToggledQ
  searchToggledQ: "", // the search those toggles belong to
  detailQ: "", // the search the detail pane was last rendered for
  detailActions: "", // the editor and folder its file actions were built with
  showAll: new Set(), // "eventId:fileIndex" whose lines past the Telegram excerpt are shown
  dash: null,
  timer: null,
  loggedOut: false,
  pollFailed: false, // the last poll could not reach the daemon
  lastPollOk: 0, // ms of the last successful poll
  outages: new Map(), // endpoint -> message of its last 503, shown in the banner
  bannerKey: null,
  projects: [],
  projectsVersion: "", // config_version the editor was filled from
  settingsVersion: "", // config_version the settings form was filled from
  editing: undefined, // the project in the editor: a name, null for a new one, undefined at first
  saveNotice: null, // {name, version, polls, appliedPolls, before}
  settingsNotice: null, // {version, polls}
  settingsLoadError: "", // why GET /api/settings failed: the form is hidden, the notice says why
  settingsSaved: null, // the save bar's fields as last loaded or saved (settings.js savedValues)
  settingsCheck: null, // {errors, issues}: the last check answer about the current fields
  settingsCheckSeq: 0, // bumped by every edit and load: an older check answer is dropped
  settingsOutcome: null, // the save bar: "saving", "waiting", "applied", "saved", "rejected", "conflict"
  projectsLoaded: false, // the editor was filled once: a tab switch doesn't refill it
  editorDirty: false, // typed into since it was filled or saved
  feedNote: "", // a Load more / Retry error, kept until the next successful load
  previewSeq: 0,
  preview: null, // {total, exact} of the last preview, null while unknown
  lineIssues: {}, // textarea id -> [{index, reason}] (index into its raw lines)
  refreshSeq: 0,
  filterGen: 0,
  settings: null,
  startDelay: 1000,
  tree: { path: "", open: new Set(), listings: new Map(), seq: 0, filter: "" },
  removed: null, // {entry, version} of the last removed project, for Undo
  saving: false, // a project save is in flight
  snoozeMenu: null, // the project whose snooze menu is open
  telegram: null, // GET /api/telegram for the Settings card, null until loaded
  // Settings → Telegram's wizard (telegram.js freshWizard): the draft's handle and
  // @bot, never the token or a chat id; chats and the pick by their opaque handles.
  wizard: { open: false, draft: null, bot: "", chats: [], choice: null, tested: null, polls: 0, polling: false, manual: false },
};
