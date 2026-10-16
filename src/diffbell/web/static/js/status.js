// Plain words for the raw statuses (UI refine spec §3.3) and the header's status
// sentence (§4.2), with held and skipped events (notify controls spec §5.2) and the
// health registry's problems (health spec §6). Pure; the API keeps the raw values.
import { plural } from "./dom.js";
import { ago, clock, untilText } from "./time.js";

const REASON_MAX = 80;

export function projectStatus(p) {
  switch (p.status) {
    case "subscribed":
      return { word: "Watching", icon: "watch", tone: "ok" };
    case "pending":
      return { word: "Starting…", icon: "pending", tone: "warn" };
    case "missing":
      return { word: "Folder missing", icon: "warning", tone: "bad" };
    case "error":
      return { word: `Error: ${p.error || "unknown error"}`, icon: "failed", tone: "bad" };
    default:
      return { word: "Waiting for the daemon", icon: "pending", tone: "muted" };
  }
}

// Why a delivery failed, in a few words; `full` is the raw text (for a title).
export function failureReason(lastError) {
  const full = String(lastError || "unknown error");
  if (/blocked/i.test(full)) return { text: "bot was blocked", full };
  if (/\b429\b|too many requests/i.test(full)) return { text: "rate limited", full };
  if (/network error|unreachable|timed? ?out|unexpected reply|connect/i.test(full)) {
    return { text: "Telegram unreachable", full };
  }
  const text = full.length > REASON_MAX ? `${full.slice(0, REASON_MAX - 1)}…` : full;
  return { text, full };
}

// Why an event was held: "quiet hours", "Telegram not set up" (bot onboarding,
// held_reason "setup") or "snoozed".
const HELD_REASONS = { quiet: "quiet hours", setup: "Telegram not set up" };
export function heldReason(ev) {
  return HELD_REASONS[ev.held_reason] || "snoozed";
}

// "Held until 16:00", "Held until tomorrow 09:00", or "Held" (until turned back on).
export function heldWord(ev, now) {
  return ev.held_until ? `Held until ${untilText(ev.held_until, now)}` : "Held";
}

// `now` (ms) words a hold that ends on another day.
export function eventStatus(ev, now = 0) {
  if (ev.status === "sent") return { word: "Sent", icon: "sent", tone: "ok", title: "" };
  if (ev.status === "held") {
    // A catch-up that hit a retryable error is still held, but no longer "until" a
    // time: it says it is retrying and why (the raw error in its title).
    if (ev.attempts > 0 && ev.last_error) {
      const reason = failureReason(ev.last_error);
      return { word: `Catch-up retrying: ${reason.text}`, icon: "retry", tone: "warn", title: reason.full };
    }
    return { word: heldWord(ev, now), icon: "snooze", tone: "muted", title: `${heldWord(ev, now)} (${heldReason(ev)})` };
  }
  if (ev.status === "skipped") {
    return { word: "Not notified", icon: "skip", tone: "muted", title: "Nothing left to report (rules, or files created then deleted)" };
  }
  if (ev.status === "pending") {
    if (ev.attempts > 0 && ev.next_attempt_at) {
      const title = ev.last_error ? String(ev.last_error) : "";
      return { word: `Retrying at ${clock(ev.next_attempt_at)}`, icon: "retry", tone: "warn", title };
    }
    return { word: "Sending…", icon: "pending", tone: "warn", title: "" };
  }
  const reason = failureReason(ev.last_error);
  return { word: `Failed: ${reason.text}`, icon: "failed", tone: "bad", title: reason.full };
}

// "alert not delivered yet: rate limited" for a serious problem whose Telegram
// alert is still stuck, else "": the page never looks calm while it is.
export function undelivered(problem) {
  if (problem.severity !== "serious" || problem.notified || !(problem.attempts > 0)) return "";
  return `alert not delivered yet: ${failureReason(problem.last_error).text}`;
}

function allProjects(dash) {
  return [...(dash.projects || []), ...(dash.missing || []).map((m) => ({ ...m, status: "missing" }))];
}

// The header's one sentence; the first matching state wins. `link`: the sentence
// is a link to the "Needs attention" view (Plan 7 spec §3.3): projects in trouble,
// or a serious problem outside any project. `live` is what a screen reader hears: the sentence
// without its "last change" clause, which changes every minute.
export function statusSentence({ pollFailed, dash, now }) {
  if (pollFailed || !dash) {
    const text = "Can't reach the daemon · retrying";
    return { text, live: text, icon: "warning", tone: "bad", link: false };
  }
  if (!dash.daemon || !dash.daemon.connected) {
    // The snapshot can't tell "still connecting" from "down for good" (it has no
    // time of the last connection), so this stays a warning; the daemon's own
    // alert after 5 minutes (watcher.WATCHMAN_GRACE) says when it is really down.
    const text = "Watchman not running";
    return { text, live: text, icon: "warning", tone: "warn", link: false };
  }
  if (dash.telegram && dash.telegram.last_error) {
    const queued = (dash.counts && dash.counts.pending) || 0;
    const reason = failureReason(dash.telegram.last_error).text;
    const what = reason === "Telegram unreachable" ? reason : `Telegram failing: ${reason}`;
    const text = `${what} · ${plural(queued, "notification", "notifications")} queued`;
    return { text, live: text, icon: "failed", tone: "bad", link: false };
  }
  const projects = allProjects(dash);
  const problems = dash.problems || [];
  const serious = problems.filter((q) => q.severity === "serious");
  const hurt = new Set(serious.filter((q) => q.project).map((q) => q.project));
  const troubled = projects.filter((p) => p.status === "missing" || p.status === "error" || hurt.has(p.name));
  // A serious problem outside any project (the config, its watch, the web UI).
  // Watchman's own problem is the "Watchman not running" state above.
  const other = serious.filter((q) => !q.project && q.key !== "watchman");
  if (troubled.length) {
    const count = troubled.length === 1 ? "1 project needs attention" : `${troubled.length} projects need attention`;
    // It has no card to show it: it must not disappear behind the projects.
    const text = [count, other[0]?.message, other.length > 1 ? `${other.length - 1} more` : ""].filter(Boolean).join(" · ");
    return { text, live: text, icon: "warning", tone: "warn", link: true };
  }
  if (other.length) {
    const late = undelivered(other[0]);
    const text = [other[0].message, other.length > 1 ? `${other.length - 1} more` : "", late].filter(Boolean).join(" · ");
    return { text, live: text, icon: "warning", tone: "warn", link: true };
  }
  const quiet = dash.quiet;
  if (quiet && quiet.active && quiet.ends_at) {
    const held = (dash.counts && dash.counts.held) || 0;
    const text = `Quiet hours until ${clock(quiet.ends_at)} · ${plural(held, "notification", "notifications")} held`;
    return { text, live: text, icon: "snooze", tone: "ok", link: false };
  }
  const last = Math.max(0, ...projects.map((p) => p.last_change_at || 0));
  const change = last ? `last change ${ago(last, now)}` : "no changes yet";
  const snoozed = projects.filter((p) => p.snooze).length;
  const warnings = problems.filter((q) => q.severity === "warning").length;
  const live = [
    "All good",
    warnings ? plural(warnings, "warning", "warnings") : "",
    `watching ${plural(projects.length, "project", "projects")}`,
    snoozed ? `${snoozed} snoozed` : "",
  ]
    .filter(Boolean)
    .join(" · ");
  return { text: `${live} · ${change}`, live, icon: "watch", tone: "ok", link: false };
}

// "Updated 20 s ago", only once the last good poll is more than 15 s old.
export function staleText(lastOk, now) {
  if (!lastOk) return "";
  const s = Math.floor((now - lastOk) / 1000);
  if (s <= 15) return "";
  return s < 120 ? `Updated ${s} s ago` : `Updated ${Math.floor(s / 60)} min ago`;
}
