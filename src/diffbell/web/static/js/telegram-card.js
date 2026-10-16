// Settings → Telegram's card, as pure presentation (settings redesign spec §6): which
// row to show and what it says. No DOM, no fetch: telegram.js renders it. Import-safe.
import { plural } from "./dom.js";
import { ago, sinceText } from "./time.js";

// "Show failed changes": the feed filter that exists today, with per-event Retry. The
// dashboard is the default view, so the link names only the filter.
export const FAILED_HREF = "?status=failed";

// One entry per failing kind (GET /api/telegram's failing.kind). Every failing row
// offers Replace bot: a refused message is often the chat (deleted, or the bot
// removed from it), and picking the chat again is the wizard. `count`: the field
// whose number goes in the count clause, `what` the clause's verb, `tail` the
// sentence after it. A legacy failed row (no stored kind) and an unknown kind read
// as the generic "Some changes couldn't be sent".
const FAILING = {
  auth: {
    tone: "tg-bad",
    icon: "failed",
    title: "Telegram rejected the bot token",
    count: "failed",
    what: "couldn't be sent",
    tail: "The token was probably revoked in @BotFather.",
    actions: ["replace", "failed"],
    primary: "replace",
    error: false,
  },
  retryable: {
    tone: "tg-warn",
    icon: "retry",
    title: "Can't reach Telegram",
    count: "waiting",
    what: "waiting",
    tail: "Diffbell keeps retrying on its own; nothing is lost.",
    actions: ["test", "replace"],
    primary: null,
    error: false,
  },
  bad_request: {
    tone: "tg-bad",
    icon: "failed",
    title: "Telegram refused a message",
    count: "failed",
    what: "couldn't be sent",
    tail: "",
    actions: ["failed", "test", "replace"],
    primary: null,
    error: true,
  },
  legacy_failed: {
    tone: "tg-bad",
    icon: "failed",
    title: "Some changes couldn't be sent",
    count: "failed",
    what: "couldn't be sent",
    tail: "",
    actions: ["failed", "test", "replace"],
    primary: null,
    error: true,
  },
};

// "Since 14:02 · 3 changes couldn't be sent. <tail>". One change is one event (as
// the dashboard banner counts); a count of 0 drops its clause, an unknown start
// drops "Since".
function failingDetail(failing, f, now) {
  const parts = [];
  if (failing.since) parts.push(`Since ${sinceText(failing.since, now)}`);
  const n = Number(failing[f.count]) || 0;
  if (n) parts.push(`${plural(n, "change", "changes")} ${f.what}`);
  const head = parts.join(" · ");
  if (!head) return f.tail;
  return f.tail ? `${head}. ${f.tail}` : `${head}.`;
}

// What the card shows. Pure: `status` is GET /api/telegram, `dashCounts` the
// dashboard poll's `counts` (held comes only from there, as on the dashboard
// banner), `now` in ms. `row` is one of "connected", "connected-noname", "auth",
// "retryable", "bad_request", "legacy_failed", "not_set_up", "unfinished".
// `headline` is [text, bold] pairs; `actions` name buttons in their order ("test",
// "replace", "failed", "setup"); the not-set-up rows show the wizard under `reason`
// and `held`. The chat is named only by its name: the page never has its id.
export function cardState(status, dashCounts, now) {
  const card = {
    row: "",
    tone: "",
    icon: "",
    headline: [],
    detail: "",
    error: "",
    actions: [],
    primary: null,
    reason: "",
    held: "",
  };
  if (!status.configured) {
    const n = (dashCounts && dashCounts.held) || 0;
    const held = n ? `${plural(n, "change is", "changes are")} waiting and will arrive as one message per project.` : "";
    if (status.reason === "unfinished save") {
      return {
        ...card,
        row: "unfinished",
        tone: "tg-notice",
        icon: "warning",
        headline: [["An earlier save didn't finish", true]],
        detail: "Diffbell won't trust a half-saved bot. Changes are held until you set Telegram up again.",
        actions: ["setup"],
        primary: "setup",
        held,
      };
    }
    const missing = status.reason === "bot token" || status.reason === "chat id";
    return { ...card, row: "not_set_up", reason: missing ? `Telegram not set up: the ${status.reason} is missing` : "", held };
  }
  const failing = status.failing;
  if (!failing) {
    // `bot` is null while the background getMe hasn't answered (Telegram unreachable):
    // then the "via @…" part is left out, and with no name either it says "your bot".
    const to = [["Notifications go to ", false]];
    if (status.chat_name) to.push([status.chat_name, true]);
    if (status.chat_name && status.bot) to.push([" via ", false]);
    if (status.bot) to.push([`@${status.bot}`, true]);
    const headline = status.chat_name || status.bot ? to : [...to, ["your bot", true]];
    return {
      ...card,
      row: status.chat_name ? "connected" : "connected-noname",
      tone: "tg-ok",
      icon: "sent",
      headline,
      detail: status.last_ok ? `Last sent ${ago(status.last_ok, now)}` : "Nothing sent yet",
      actions: ["test", "replace"],
    };
  }
  const row = Object.hasOwn(FAILING, failing.kind) ? failing.kind : "legacy_failed";
  const f = FAILING[row];
  return {
    ...card,
    row,
    tone: f.tone,
    icon: f.icon,
    headline: [[f.title, true]],
    detail: failingDetail(failing, f, now),
    error: f.error ? failing.error || "" : "",
    actions: f.actions.filter((a) => a !== "failed" || Number(failing.failed) > 0),
    primary: f.primary,
  };
}
