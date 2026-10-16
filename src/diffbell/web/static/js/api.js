// fetch, CSRF and the error envelope. Import-safe: main.js hands in fetch and
// the page's reactions to a 401 or a 503 (configureApi).
import { state } from "./state.js";

// `data`: the error's parsed body, or null. Settings reads the 422's `errors` and
// `issues` from it (settings redesign spec §3); everyone else reads the message.
export class ApiError extends Error {
  constructor(status, message, data = null) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

export const UNREACHABLE = "Couldn't reach the daemon";

// A request the daemon never answers is given up (and reads as unreachable), so
// a hung daemon can't stop the polling for good.
export const TIMEOUT_MS = 15000;

// What each error status means to the person in front of the page; the server's
// own message (the `error` field of the envelope) follows it.
const STATUS_TEXT = {
  400: "The request was rejected",
  401: "Not logged in",
  403: "Refused by the server",
  404: "Not found",
  409: "Conflict",
  411: "The request was rejected",
  413: "Too much input",
  422: "Invalid",
  500: "Internal error in the daemon",
  503: "Unavailable",
};

// What to show next to the control that failed: the server's own sentence when it
// sent one (it says what went wrong and what to do), else the labelled text.
export function errorSentence(err) {
  return (err && err.data && typeof err.data.error === "string" && err.data.error) || (err && err.message) || "";
}

export function errorText(status, data) {
  const detail = data && typeof data.error === "string" ? data.error : "";
  const label = STATUS_TEXT[status] || `HTTP ${status}`;
  if (status === 422 && detail) return detail; // validation messages stand on their own
  return detail ? `${label}: ${detail}` : label;
}

const hooks = {
  fetch: null,
  loggedOut: () => {},
  outagesChanged: () => {},
};

export function configureApi(options) {
  Object.assign(hooks, options);
}

// `timeout` (ms) overrides TIMEOUT_MS for a call that may take longer. `banner: false`
// keeps a 503 out of the page banner, for a caller that shows its own errors: its
// endpoint (a draft's) may never be called again, so its outage would never clear.
export async function api(method, path, body, { timeout = TIMEOUT_MS, banner = true } = {}) {
  // A JSON string body, so fetch sends a Content-Length (the server answers 411
  // to a chunked body). Mutations carry the CSRF token.
  const options = { method, headers: {}, credentials: "same-origin", cache: "no-store" };
  options.signal = AbortSignal.timeout(timeout); // made per call: the module stays import-safe
  if (method !== "GET") options.headers["X-Diffbell-Token"] = state.csrf;
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  // One banner line per endpoint, not per event: /api/events/7 and /8 are one.
  const endpoint = `${method} ${path.split("?")[0].replace(/\/\d+(?=\/|$)/g, "/:id")}`;
  let response;
  try {
    response = await hooks.fetch(path, options);
  } catch {
    // A network error or the timeout's abort.
    throw new ApiError(0, UNREACHABLE);
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (response.status === 401) hooks.loggedOut();
  if (response.status === 503 && banner) {
    state.outages.set(endpoint, errorText(503, data));
    hooks.outagesChanged();
  } else if (state.outages.delete(endpoint)) {
    hooks.outagesChanged();
  }
  if (!response.ok) throw new ApiError(response.status, errorText(response.status, data), data);
  return data;
}

// -- Settings (settings redesign spec §3) ---------------------------------------------
// The server's check of a settings body (PUT's body without config_version): it runs
// the same validation as the save and writes nothing.
export const postSettingsCheck = (body) => api("POST", "/api/settings/check", body);

// -- Settings → Telegram (bot onboarding spec §2) -------------------------------------
// The token is sent once, by checkBotToken; every later call names the draft and a
// chat only by their handles. Sending and saving wait on Telegram (and the Keychain),
// so they get longer than a poll.
export const TELEGRAM_TIMEOUT_MS = 60000;

const draftPath = (draft, rest) => `/api/telegram/draft/${encodeURIComponent(draft)}/${rest}`;

export const telegramStatus = () => api("GET", "/api/telegram");
// The wizard shows every draft call's error in the card itself (banner: false).
const WIZARD = { banner: false };
const WIZARD_SLOW = { banner: false, timeout: TELEGRAM_TIMEOUT_MS };
export const checkBotToken = (token) => api("POST", "/api/telegram/draft", { token }, WIZARD);
export const findChats = (draft) => api("GET", draftPath(draft, "chats"), undefined, WIZARD);
export const enterChatId = (draft, chatId) => api("POST", draftPath(draft, "chats"), { chat_id: chatId }, WIZARD);
export const sendDraftTest = (draft, choice) => api("POST", draftPath(draft, "test"), { choice }, WIZARD_SLOW);
export const saveDraft = (draft, choice) => api("POST", draftPath(draft, "save"), { choice }, WIZARD_SLOW);
