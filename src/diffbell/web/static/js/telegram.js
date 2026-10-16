// Settings → Telegram (bot onboarding spec §3, settings redesign spec §6): the card's
// rows (cardState) and the wizard. The token is sent once (Check) and then dropped:
// the page keeps only the draft handle and @bot, never the token or a chat id, and
// nothing of the draft in browser storage. Import-safe: main.js hands in visibility,
// navigation and the poll.
import { checkBotToken, enterChatId, errorSentence, findChats, saveDraft, sendDraftTest, telegramStatus } from "./api.js";
import { sendTest } from "./dashboard.js";
import { $, h, icon, setAttr, setClass, setHidden, setMessage, setText } from "./dom.js";
import { state } from "./state.js";
import { cardState } from "./telegram-card.js";

export const POLL_MS = 3000; // …/chats while waiting for /start
export const MAX_POLLS = 40; // 2 minutes, then "Send /start again" and the manual field

const deps = {
  visible: () => true,
  showView: () => {},
  showFailed: () => {}, // the dashboard, filtered to failed events
  refresh: () => {},
  now: () => Date.now(),
};

// The card's buttons by cardState's action names. All four stay in the page (hidden
// when unused), so getElementById always finds them.
const ACTION_IDS = { test: "tg-send-test", replace: "tg-replace", failed: "tg-show-failed", setup: "tg-setup-again" };

export function initTelegram(options) {
  Object.assign(deps, options);
}

let timer = null; // the next …/chats poll
let inFlight = null; // the wizard whose …/chats request is in flight
let chatsKey = ""; // the chat list as last rendered (rebuilt only when it changes)
let blocked = false; // syncTestButton disabled the card's test button
let actionsKey = ""; // the action order as last laid out (moved only when it changes)

// A wizard with nothing done yet. `open`: the wizard is shown over the card's state
// (Replace bot, Set up again); while not set up it is shown anyway.
export function freshWizard(open) {
  return { open, draft: null, bot: "", chats: [], choice: null, tested: null, polls: 0, polling: false, manual: false };
}

// Which card to show and how far the wizard is. Pure. `status` is GET /api/telegram
// (null until loaded), `draft` the wizard state above. Set up: "connected" or
// "failing" (its `failing`); an unfinished save: "unfinished", until Set up again
// opens the wizard (while the failed save's draft is kept, the wizard stays);
// otherwise the wizard. Steps unlock in order: 2 (the token) until
// Check made a draft, 3 (find the chat) until a chat is picked, then 4; Save only
// after a successful test to the picked chat.
export function wizardStep(status, draft) {
  if (!status) return { card: "loading", step: 0, canTest: false, canSave: false };
  if (!draft.open) {
    if (status.configured) return { card: status.failing ? "failing" : "connected", step: 0, canTest: false, canSave: false };
    // A failed save keeps its draft: the wizard stays and the token isn't asked again.
    if (status.reason === "unfinished save" && !draft.draft) return { card: "unfinished", step: 0, canTest: false, canSave: false };
  }
  const step = !draft.draft ? 2 : draft.choice ? 4 : 3;
  return { card: "wizard", step, canTest: step === 4, canSave: step === 4 && draft.tested === draft.choice };
}

// The line under step 3. Pure.
export function waitingText(draft) {
  if (!draft.draft) return "";
  if (draft.chats.length === 1) return "Found your chat.";
  if (draft.chats.length > 1) return "More than one person messaged the bot: pick yourself.";
  if (draft.polling) return "Waiting for your /start…";
  if (draft.manual) return "No /start from you yet.";
  return "";
}

// -- the card's presentations (settings redesign spec §6) ----------------------------
export function renderTelegram() {
  const status = state.telegram;
  const wiz = state.wizard;
  const view = wizardStep(status, wiz);
  const card = status ? cardState(status, state.dash && state.dash.counts, deps.now()) : null;
  const onCard = view.card === "connected" || view.card === "failing" || view.card === "unfinished";
  const wizard = view.card === "wizard";
  setHidden($("tg-loading"), view.card !== "loading");
  setHidden($("tg-state"), !onCard);
  setHidden($("tg-wizard"), !wizard);
  // Above step 1 while not set up (also after Set up again); never over Replace bot.
  const reason = wizard && card ? card.reason : "";
  const held = wizard && card ? card.held : "";
  setText($("tg-reason"), reason);
  setHidden($("tg-reason"), !reason);
  setText($("tg-held"), held);
  setHidden($("tg-held"), !held);
  if (onCard) renderCard(card);
  if (!wizard) return;
  for (let n = 1; n <= 4; n += 1) {
    const where = n < view.step ? "done" : n === view.step ? "current" : "locked";
    const li = $(`tg-step-${n}`);
    setClass(li, `wizard-step ${where}`);
    if (where === "current") setAttr(li, "aria-current", "step");
    else li.removeAttribute("aria-current");
  }
  setText($("tg-bot-name"), wiz.bot ? `@${wiz.bot}` : "your bot");
  setHidden($("tg-find"), view.step < 3);
  setText($("tg-waiting"), waitingText(wiz));
  setHidden($("tg-chats"), wiz.chats.length === 0);
  renderChats();
  setHidden($("tg-manual"), !wiz.manual);
  $("tg-test").disabled = !view.canTest;
  $("tg-save").disabled = !view.canSave;
  setHidden($("tg-cancel"), !wiz.open); // Replace bot or Set up again opened it
}

// The status block: tone, icon, headline (bold parts as <strong>, all as text
// nodes), detail, the stored error (refused and legacy rows only), then the actions.
function renderCard(card) {
  setClass($("tg-status"), `tg-status ${card.tone}`.trim());
  const badge = $("tg-badge");
  if (badge.dataset.icon !== card.icon) {
    badge.dataset.icon = card.icon;
    badge.replaceChildren(icon(card.icon));
  }
  const headline = $("tg-headline");
  const key = JSON.stringify(card.headline);
  if (headline.dataset.key !== key) {
    headline.dataset.key = key;
    headline.replaceChildren(...card.headline.map(([text, bold]) => (bold ? h("strong", { text }) : text)));
  }
  setText($("tg-detail"), card.detail);
  setHidden($("tg-detail"), !card.detail);
  setText($("tg-error"), card.error);
  setHidden($("tg-error"), !card.error);
  renderActions(card);
}

// The row's buttons in its order, the unused ones hidden after them. Moved only
// when the order changes, so a poll never drops the focus of a button in use.
function renderActions(card) {
  const box = $("tg-actions");
  const key = card.actions.join(" ");
  if (key !== actionsKey) {
    actionsKey = key;
    const rest = Object.keys(ACTION_IDS).filter((name) => !card.actions.includes(name));
    for (const name of [...card.actions, ...rest]) box.append($(ACTION_IDS[name]));
  }
  for (const [name, id] of Object.entries(ACTION_IDS)) {
    const el = $(id);
    setHidden(el, !card.actions.includes(name));
    setClass(el, name === "failed" ? "tg-link" : name === card.primary ? "primary" : "");
  }
}

// One radio per chat, by name (a chat id entered by hand has none). Rebuilt only
// when the list changes, so picking one keeps the keyboard focus.
function renderChats() {
  const wiz = state.wizard;
  const key = wiz.chats.map((c) => `${c.choice}\u0000${c.name}`).join("\u0001");
  if (key !== chatsKey) {
    chatsKey = key;
    $("tg-chat-list").replaceChildren(
      ...wiz.chats.map((c) =>
        h(
          "label",
          { class: "radio" },
          h("input", { type: "radio", name: "tg-chat", value: c.choice, onchange: () => chooseChat(c.choice) }),
          c.name || "The chat id you entered",
        ),
      ),
    );
  }
  for (const input of $("tg-chat-list").children) {
    const radio = input.children[0];
    radio.checked = radio.getAttribute("value") === wiz.choice;
  }
}

// `quiet`: a failure keeps the message shown (after Save, "Saved" stays).
export async function loadTelegram({ quiet = false } = {}) {
  try {
    state.telegram = await telegramStatus();
  } catch (err) {
    if (!quiet) setMessage($("tg-message"), err.message, "error");
    return;
  }
  renderTelegram();
  syncTestButton();
}

// Every dashboard poll while Settings shows (main.js): the card follows a failure,
// or a recovery, without a reload. One request at a time; a failed one says nothing.
let refreshing = null;
export function refreshTelegram() {
  if (state.view !== "settings" || refreshing) return;
  const before = cardView();
  const said = $("tg-message").textContent;
  refreshing = loadTelegram({ quiet: true })
    .then(() => {
      // The poll moved the card on (connected → wizard, failing → connected, …): a
      // message about the old state ("Saved: …", a test's outcome) no longer
      // applies. One written meanwhile (a Save that just finished) stays.
      if (cardView() !== before && $("tg-message").textContent === said) setMessage($("tg-message"), "");
    })
    .finally(() => {
      refreshing = null;
    });
}

function cardView() {
  return state.telegram ? wizardStep(state.telegram, state.wizard).card : "loading";
}

// Settings was opened: refresh the card. ?section=telegram (a link, the login
// target, "Back to Settings") is settings.js focusSection's, as every section is.
// A wait for /start paused while Settings was left continues.
export function showTelegram() {
  loadTelegram();
  resumeTelegramPoll();
}

function focusHeading() {
  const heading = $("tg-title");
  setAttr(heading, "tabindex", "-1"); // focusable by script, not a Tab stop
  heading.focus();
}

// -- the wizard --------------------------------------------------------------------

function message(text, kind = "") {
  setMessage($("tg-message"), text, kind);
}

// The draft expired, was replaced in another tab or the daemon restarted: back to
// step 2 with the server's reason.
function draftGone(err) {
  stopPolling(false);
  state.wizard = freshWizard(state.wizard.open);
  message(errorSentence(err), "error");
  renderTelegram();
  $("tg-token").focus();
}

export async function checkToken(event) {
  if (event) event.preventDefault();
  const input = $("tg-token");
  const token = input.value.trim();
  if (!token) {
    message("Paste the token from @BotFather first.", "error");
    return;
  }
  const wiz = state.wizard;
  $("tg-check").disabled = true;
  message("Checking…");
  let result = null;
  try {
    result = await checkBotToken(token);
  } catch (err) {
    if (state.wizard === wiz) message(errorSentence(err), "error");
  } finally {
    $("tg-check").disabled = false;
  }
  if (state.wizard !== wiz) return; // Cancel or Replace meanwhile: this answer is stale
  if (result) {
    input.value = ""; // from here on the page holds only the draft handle and @bot
    stopPolling(false);
    state.wizard = { ...freshWizard(wiz.open), draft: result.draft, bot: result.bot };
    message(`That's @${result.bot}. Now send /start to it in Telegram.`, "ok");
    startPolling();
  }
  renderTelegram();
}

export function startPolling() {
  const wiz = state.wizard;
  if (!wiz.draft) return;
  wiz.polling = true;
  wiz.polls = 0;
  renderTelegram();
  pollChats();
}

function stopPolling(manual) {
  clearTimeout(timer);
  timer = null;
  state.wizard.polling = false;
  if (manual) state.wizard.manual = true;
}

// Every POLL_MS while the card is on screen (deps.visible: the page is visible and
// Settings is the view), up to MAX_POLLS: then "Send /start again, then Check again"
// and the chat id field. Off screen the poll is skipped; resumeTelegramPoll picks
// it up again. At most one request per wizard: an older wizard's late answer is
// dropped and never holds up the current one.
export async function pollChats() {
  clearTimeout(timer);
  timer = null;
  const wiz = state.wizard;
  if (!wiz.draft || !wiz.polling || inFlight === wiz || !deps.visible()) return;
  inFlight = wiz;
  let chats = null;
  try {
    chats = (await findChats(wiz.draft)).chats;
  } catch (err) {
    if (state.wizard !== wiz) {
      resumeTelegramPoll();
      return;
    }
    if (err.status === 404) {
      draftGone(err);
      return;
    }
    if (err.status === 409) {
      // A webhook: updates can't be read; the chat id is the way left.
      stopPolling(true);
      message(errorSentence(err), "error");
      renderTelegram();
      return;
    }
    // Unreachable for now: the next poll tries again.
  } finally {
    if (inFlight === wiz) inFlight = null;
  }
  if (state.wizard !== wiz) {
    resumeTelegramPoll(); // the current wizard, if its wait has no poll scheduled
    return;
  }
  if (!wiz.polling) return;
  wiz.polls += 1;
  if (chats && chats.length) {
    wiz.chats = chats;
    if (!chats.some((c) => c.choice === wiz.choice)) wiz.choice = chats[0].choice;
    stopPolling(false);
  } else if (wiz.polls >= MAX_POLLS) {
    stopPolling(true);
  } else {
    timer = setTimeout(pollChats, POLL_MS);
  }
  renderTelegram();
}

// The card is on screen again (the page became visible, Settings was opened):
// continue a paused wait for /start.
export function resumeTelegramPoll() {
  if (state.wizard.polling && timer === null && inFlight !== state.wizard) pollChats();
}

export function chooseChat(choice) {
  state.wizard.choice = choice;
  state.wizard.tested = null; // Save needs a test to this chat
  renderTelegram();
}

export async function useManualChat(event) {
  if (event) event.preventDefault();
  const wiz = state.wizard;
  if (!wiz.draft) return;
  const input = $("tg-chat-id");
  const button = $("tg-use");
  button.disabled = true;
  let result = null;
  try {
    result = await enterChatId(wiz.draft, input.value);
  } catch (err) {
    if (state.wizard === wiz) {
      if (err.status === 404) draftGone(err);
      else message(errorSentence(err), "error");
    }
  } finally {
    button.disabled = false;
  }
  if (state.wizard !== wiz) return; // Cancel or Replace meanwhile: this answer is stale
  if (result) {
    input.value = "";
    stopPolling(false);
    if (!wiz.chats.some((c) => c.choice === result.choice)) wiz.chats = [...wiz.chats, { choice: result.choice, name: result.name }];
    wiz.choice = result.choice;
    wiz.tested = null;
    message("Chat set: send a test message.", "ok");
  }
  renderTelegram();
}

export async function testDraft() {
  const wiz = state.wizard;
  if (!wiz.draft || !wiz.choice) return;
  const choice = wiz.choice;
  $("tg-test").disabled = true;
  message("Sending…");
  try {
    await sendDraftTest(wiz.draft, choice);
    if (state.wizard !== wiz) return; // Cancel or Replace meanwhile: stale
    if (wiz.choice === choice) wiz.tested = choice;
    message("Test message sent: check Telegram, then Save.", "ok");
  } catch (err) {
    if (state.wizard !== wiz) return;
    if (err.status === 404) {
      draftGone(err);
      return;
    }
    message(errorSentence(err), "error");
  }
  renderTelegram();
}

export async function saveWizard() {
  const wiz = state.wizard;
  if (!wiz.draft || !wiz.choice) return;
  $("tg-save").disabled = true;
  message("Saving…");
  const choice = wiz.choice;
  try {
    const result = await saveDraft(wiz.draft, choice);
    message(`Saved: Diffbell now sends to @${result.bot}.`, "ok");
    if (state.wizard === wiz) {
      // The card shows the saved bot at once, also if the reload below fails.
      const chat = wiz.chats.find((c) => c.choice === choice);
      state.telegram = {
        ...state.telegram,
        configured: true,
        bot: result.bot,
        chat_name: chat ? chat.name : null,
        reason: null,
        failing: null,
      };
      stopPolling(false);
      state.wizard = freshWizard(false);
      renderTelegram();
      focusHeading();
    }
    // A save that lands after Cancel or Replace still changed the bot: the status
    // is reloaded, the wizard now shown is left alone.
    await loadTelegram({ quiet: true });
    deps.refresh(); // the dashboard's banner and held counts
  } catch (err) {
    if (state.wizard !== wiz) return;
    if (err.status === 404) {
      draftGone(err);
      return;
    }
    message(errorSentence(err), "error"); // the draft stays: Save can be tried again
    renderTelegram();
  }
}

// Replace bot (Connected or Failing) and Set up again (an unfinished save) → the
// wizard; a current bot keeps sending until Save.
export function replaceBot() {
  stopPolling(false);
  state.wizard = freshWizard(true);
  message("");
  renderTelegram();
  $("tg-token").focus();
}

export function cancelWizard() {
  stopPolling(false);
  state.wizard = freshWizard(false);
  message("");
  renderTelegram();
  focusHeading();
}

// -- the card's Send test message -------------------------------------------------------

// Whether Telegram is set up: the dashboard poll's answer, else the card's; null
// while neither has loaded.
export function telegramConfigured() {
  const t = state.dash && state.dash.telegram;
  if (t && typeof t.configured === "boolean") return t.configured;
  return state.telegram ? Boolean(state.telegram.configured) : null;
}

// "Send test message" is disabled while Telegram isn't set up (the server would
// answer 409). Only re-enabled here when this disabled it: a send in flight keeps
// its own disabled state.
export function syncTestButton() {
  const off = telegramConfigured() === false;
  const button = $("tg-send-test");
  if (off) button.disabled = true;
  else if (blocked) button.disabled = false;
  blocked = off;
}

// POST /api/test-notification (the "try now" of state 4); the result in the card's
// message line. Disabled again afterwards while Telegram isn't set up.
export async function sendTestMessage() {
  await sendTest($("tg-send-test"), $("tg-message"));
  syncTestButton();
}

// A link the page follows itself; a modified or middle click opens it in a new tab
// or window, as the browser does it.
function inPage(go) {
  return (event) => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button > 0) return;
    event.preventDefault();
    go();
  };
}

export function wireTelegram() {
  $("tg-token-form").addEventListener("submit", checkToken);
  $("tg-manual-form").addEventListener("submit", useManualChat);
  $("tg-again").addEventListener("click", startPolling);
  $("tg-test").addEventListener("click", testDraft);
  $("tg-save").addEventListener("click", saveWizard);
  $("tg-replace").addEventListener("click", replaceBot);
  $("tg-setup-again").addEventListener("click", replaceBot);
  $("tg-send-test").addEventListener("click", sendTestMessage);
  $("tg-show-failed").addEventListener("click", inPage(() => deps.showFailed()));
  $("tg-guide").addEventListener("click", inPage(() => deps.showView("guide", {})));
  $("tg-cancel").addEventListener("click", cancelWizard);
  // "How? ▸": the guide at that step, inside the page (?view=guide&step=…).
  for (const link of [$("tg-how-create"), $("tg-how-token"), $("tg-how-start")]) {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      deps.showView("guide", { step: link.dataset.step });
    });
  }
}
