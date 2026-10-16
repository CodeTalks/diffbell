// Times in the viewer's local time zone. Pure: `now` (ms) is passed in. Days are
// built from date parts, never by adding 86400 s, so a 23 h or 25 h DST day is right.

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

const pad = (n) => String(n).padStart(2, "0");

// "14:05" (local).
export function clock(ts) {
  const d = new Date(ts * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// The full local timestamp, for a title attribute.
export function fullTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleString() : "";
}

// The feed's time column: "just now", "3 min ago", "2 h ago", then HH:MM past 24 h.
export function relativeTime(ts, now) {
  const s = Math.max(0, now / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return clock(ts);
}

// The feed's time per day group (diff readability spec §4.2): relative in Today's
// group, the clock time (HH:MM) in Yesterday's and older ones.
export function feedTime(ts, now) {
  return dayLabel(ts, now) === "Today" ? relativeTime(ts, now) : clock(ts);
}

// The cards' and header's "last change": like relativeTime, then "N d ago".
export function ago(ts, now) {
  if (!ts) return "no changes yet";
  const s = Math.max(0, now / 1000 - ts);
  if (s >= 86400) return `${Math.floor(s / 86400)} d ago`;
  return relativeTime(ts, now);
}

// "2026-10-09" (local): the key of the day a timestamp falls on.
export function dayKey(ts) {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function shiftedDayKey(now, days) {
  const d = new Date(now);
  const shifted = new Date(d.getFullYear(), d.getMonth(), d.getDate() + days, 12);
  return dayKey(shifted.getTime() / 1000);
}

// When a pause ends: "16:00" today, "tomorrow 09:00", else "Mon 6 Oct 09:00" (local).
export function untilText(ts, now) {
  const key = dayKey(ts);
  if (key === shiftedDayKey(now, 0)) return clock(ts);
  if (key === shiftedDayKey(now, 1)) return `tomorrow ${clock(ts)}`;
  return `${dayLabel(ts, now)} ${clock(ts)}`;
}

// When a failing backlog started (the Telegram card's "Since …"): "14:02" today,
// "yesterday 14:02", else "Mon 6 Oct 14:02" (local), like untilText.
export function sinceText(ts, now) {
  const label = dayLabel(ts, now);
  if (label === "Today") return clock(ts);
  if (label === "Yesterday") return `yesterday ${clock(ts)}`;
  return `${label} ${clock(ts)}`;
}

// "Today", "Yesterday", or "Mon 6 Oct" ("Mon 6 Oct 2025" in another year).
export function dayLabel(ts, now) {
  const key = dayKey(ts);
  if (key === shiftedDayKey(now, 0)) return "Today";
  if (key === shiftedDayKey(now, -1)) return "Yesterday";
  const d = new Date(ts * 1000);
  const label = `${DAYS[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]}`;
  return d.getFullYear() === new Date(now).getFullYear() ? label : `${label} ${d.getFullYear()}`;
}

// Local midnight at the start of the day `value` (YYYY-MM-DD) plus `days`, in
// epoch seconds (the date filters' since/until).
export function localMidnight(value, days = 0) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!m) return null;
  const [year, month, day] = [Number(m[1]), Number(m[2]) - 1, Number(m[3])];
  // new Date() rolls an impossible day over ("2026-02-31" is 3 March): the parts
  // must come back unchanged.
  const parsed = new Date(year, month, day);
  if (parsed.getFullYear() !== year || parsed.getMonth() !== month || parsed.getDate() !== day) return null;
  const t = new Date(year, month, day + days).getTime();
  return Number.isNaN(t) ? null : t / 1000;
}

// "Thu 8 Oct" for a day key "2026-10-08" (the activity strip's bars). Built from
// the date's own parts, so it names that day whatever the viewer's time zone; ""
// for anything that is not a date.
export function dayTitle(value) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value));
  if (!m) return "";
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), 12);
  if (Number.isNaN(d.getTime())) return "";
  return `${DAYS[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]}`;
}
