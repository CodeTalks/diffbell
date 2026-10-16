// Diff readability (diff readability spec §3): per stored diff line, the segments
// {text, classes} that detail.js turns into spans and <mark>s with h(). Pure and
// import-safe: no DOM, no fetch. Word changes (wadd/wdel), light syntax colour by
// the file's extension (sk key, ss string, sn number, sc comment, sp list marker)
// and search marks ("mark") are merged into one list of segments per line, which
// always covers the whole line: any failure falls back to the plain line. Control
// and line-separator characters are shown visibly, after the ranges are worked out.

export const WORD_MAX = 400; // longer lines get whole-line colour only
export const WORD_SHARE = 0.4; // fewer shared tokens than this: whole-line colour only
export const SEARCH_MIN = 2; // the server's minimum for a diff search (code points)

// The search the server runs on diff lines, so the page marks the same thing:
// q (2+ code points after trimming) or "" when it is too short.
export function searchTerm(q) {
  const term = String(q || "").trim();
  return [...term].length >= SEARCH_MIN ? term : "";
}

// SQLite's LIKE folds ASCII letters only; one code unit in, one out, so offsets in
// the folded text are offsets in the original.
function asciiLower(text) {
  return text.replace(/[A-Z]/g, (c) => String.fromCharCode(c.charCodeAt(0) + 32));
}

// [start, end) of every non-overlapping occurrence of `term` in `line` (a non-string
// line counts as its text, null or undefined as "", as in diffSegments).
export function markRanges(line, term) {
  if (!term) return [];
  const hay = asciiLower(String(line ?? ""));
  const needle = asciiLower(term);
  const out = [];
  let at = hay.indexOf(needle);
  while (at >= 0) {
    out.push([at, at + needle.length]);
    at = hay.indexOf(needle, at + needle.length);
  }
  return out;
}

export function countMatches(lines, term) {
  return lines.reduce((n, line) => n + markRanges(line, term).length, 0);
}

// "json", "yaml", "toml" or "" from the path's extension (any case).
export function syntaxOf(path) {
  const m = /\.([A-Za-z]+)$/.exec(String(path || ""));
  const ext = m ? m[1].toLowerCase() : "";
  if (ext === "json") return "json";
  if (ext === "yaml" || ext === "yml") return "yaml";
  if (ext === "toml") return "toml";
  return "";
}

function marker(line) {
  return line.startsWith("+") || line.startsWith("-") ? line[0] : "";
}

// Pairs within each run of "-" lines followed directly by a run of "+" lines, by
// position: partner[i] is the index of line i's partner, or -1. Stored reports keep
// only the +/- lines of a diff (no "@@" headers, no context), so a run can span two
// hunks; an "@@" line, should one ever be stored, still ends a run.
export function pairRuns(lines) {
  const partner = lines.map(() => -1);
  let i = 0;
  while (i < lines.length) {
    if (marker(lines[i]) !== "-") {
      i += 1;
      continue;
    }
    const dels = i;
    while (i < lines.length && marker(lines[i]) === "-") i += 1;
    const adds = i;
    while (i < lines.length && marker(lines[i]) === "+") i += 1;
    const n = Math.min(adds - dels, i - adds);
    for (let k = 0; k < n; k += 1) {
      partner[dels + k] = adds + k;
      partner[adds + k] = dels + k;
    }
  }
  return partner;
}

// Runs of letters, digits and underscore; single whitespace; single other characters.
export function tokens(text) {
  return text.match(/[\p{L}\p{N}_]+|\s|[^\p{L}\p{N}_\s]/gu) || [];
}

// The changed [start, end) ranges of two paired lines' contents (markers removed):
// {del, add}, or null when the pair gets whole-line colour only (a line over
// WORD_MAX characters, or shared tokens under WORD_SHARE of the longer line's).
export function wordRanges(oldText, newText) {
  if (oldText.length + 1 > WORD_MAX || newText.length + 1 > WORD_MAX) return null;
  const a = tokens(oldText);
  const b = tokens(newText);
  // Longest common subsequence, by table.
  const table = Array.from({ length: a.length + 1 }, () => new Array(b.length + 1).fill(0));
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      table[i][j] = a[i] === b[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
    }
  }
  const shared = table[0][0];
  if (shared < WORD_SHARE * Math.max(a.length, b.length)) return null;
  const keepA = new Array(a.length).fill(false);
  const keepB = new Array(b.length).fill(false);
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      keepA[i] = true;
      keepB[j] = true;
      i += 1;
      j += 1;
    } else if (table[i + 1][j] >= table[i][j + 1]) i += 1;
    else j += 1;
  }
  return { del: changed(a, keepA), add: changed(b, keepB) };
}

// The ranges of the tokens not kept, adjacent ones joined.
function changed(toks, keep) {
  const out = [];
  let at = 0;
  toks.forEach((tok, k) => {
    const end = at + tok.length;
    if (!keep[k]) {
      const last = out[out.length - 1];
      if (last && last[1] === at) last[1] = end;
      else out.push([at, end]);
    }
    at = end;
  });
  return out;
}

// -- light syntax colour: per line, no state across lines --------------------------

const NUMBER = /^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/;
const TOML_NUMBER = /^[+-]?\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?/;
const WORD = /[\p{L}\p{N}_]/u;

// The end of a quoted string starting at `start` (the index after its closing quote),
// or -1 when it does not close on this line (a value cut across lines: left plain).
function stringEnd(text, start, escapes) {
  const quote = text[start];
  for (let k = start + 1; k < text.length; k += 1) {
    if (escapes && text[k] === "\\") k += 1;
    else if (text[k] === quote) return k + 1;
  }
  return -1;
}

// A number or keyword at `k`, standing alone (not inside a word): its end, else -1.
function literalEnd(text, k, number, words) {
  if (k > 0 && WORD.test(text[k - 1])) return -1;
  const rest = text.slice(k);
  const m = number.exec(rest);
  let end = m ? k + m[0].length : -1;
  for (const word of words) if (rest.startsWith(word)) end = Math.max(end, k + word.length);
  if (end < 0 || (end < text.length && WORD.test(text[end]))) return -1;
  return end;
}

function jsonRanges(text) {
  const out = [];
  let k = 0;
  while (k < text.length) {
    const c = text[k];
    if (c === '"') {
      const end = stringEnd(text, k, true);
      if (end < 0) break; // unterminated: the rest stays plain
      let after = end;
      while (after < text.length && /\s/.test(text[after])) after += 1;
      out.push([k, end, text[after] === ":" ? "sk" : "ss"]);
      k = end;
      continue;
    }
    const end = literalEnd(text, k, NUMBER, ["true", "false", "null"]);
    if (end > 0) {
      out.push([k, end, "sn"]);
      k = end;
      continue;
    }
    k += 1;
  }
  return out;
}

// The index of a "#" comment outside quotes (at the start or after whitespace), or -1.
function commentAt(text, from) {
  let quote = "";
  for (let k = from; k < text.length; k += 1) {
    const c = text[k];
    if (quote) {
      if (c === "\\" && quote === '"') k += 1;
      else if (c === quote) quote = "";
    } else if (c === '"' || c === "'") quote = c;
    else if (c === "#" && (k === 0 || /\s/.test(text[k - 1]))) return k;
  }
  return -1;
}

function yamlRanges(text) {
  const out = [];
  const m = /^(\s*)(-\s+)?/.exec(text);
  let k = m[0].length;
  if (m[2]) out.push([m[1].length, m[1].length + 1, "sp"]);
  const key = /^("[^"]*"|'[^']*'|[^\s#'"\-][^:#]*?|-[^\s:#][^:#]*?)\s*:(?=\s|$)/.exec(text.slice(k));
  if (key) {
    out.push([k, k + key[1].length, "sk"]);
    k += key[0].length;
  }
  const hash = commentAt(text, k);
  if (hash >= 0) out.push([hash, text.length, "sc"]);
  return out;
}

function tomlRanges(text) {
  const out = [];
  const lead = /^\s*/.exec(text)[0].length;
  const hash = commentAt(text, lead);
  const stop = hash >= 0 ? hash : text.length;
  const section = /^\[\[?[^\]#]*\]\]?/.exec(text.slice(lead));
  let k = lead;
  if (section) {
    out.push([lead, lead + section[0].length, "sk"]);
    k = lead + section[0].length;
  } else {
    const key = /^([A-Za-z0-9_.\-]+|"[^"]*"|'[^']*')(?:\s*\.\s*(?:[A-Za-z0-9_\-]+|"[^"]*"|'[^']*'))*\s*(?==)/.exec(text.slice(lead));
    if (key) {
      out.push([lead, lead + key[0].trimEnd().length, "sk"]);
      k = lead + key[0].length + 1; // past the "="
    }
  }
  while (k < stop) {
    const c = text[k];
    if (c === '"' || c === "'") {
      const end = stringEnd(text, k, c === '"');
      if (end < 0 || end > stop) break; // unterminated (or a multi-line string): plain
      out.push([k, end, "ss"]);
      k = end;
      continue;
    }
    const end = literalEnd(text, k, TOML_NUMBER, ["true", "false"]);
    if (end > 0) {
      out.push([k, end, "sn"]);
      k = end;
      continue;
    }
    k += 1;
  }
  if (hash >= 0) out.push([hash, text.length, "sc"]);
  return out;
}

// [start, end, class] over a line's content (without its marker).
export function syntaxRanges(text, kind) {
  if (kind === "json") return jsonRanges(text);
  if (kind === "yaml") return yamlRanges(text);
  if (kind === "toml") return tomlRanges(text);
  return [];
}

// -- merging ------------------------------------------------------------------------

// Cut the line at every range boundary; each piece carries the classes of the
// ranges covering it. Adjacent pieces with the same classes are joined.
export function merge(line, ranges) {
  const cuts = new Set([0, line.length]);
  for (const [start, end] of ranges) {
    cuts.add(Math.max(0, Math.min(line.length, start)));
    cuts.add(Math.max(0, Math.min(line.length, end)));
  }
  const points = [...cuts].sort((x, y) => x - y);
  const out = [];
  for (let k = 0; k + 1 < points.length; k += 1) {
    const [from, to] = [points[k], points[k + 1]];
    const classes = [];
    for (const [start, end, cls] of ranges) if (start <= from && to <= end && !classes.includes(cls)) classes.push(cls);
    classes.sort();
    const last = out[out.length - 1];
    if (last && last.classes.join(" ") === classes.join(" ")) last.text += line.slice(from, to);
    else out.push({ text: line.slice(from, to), classes });
  }
  return out.length ? out : [{ text: line, classes: [] }];
}

// One line's segments. `other` is its paired line (or null), `kind` the syntax.
function lineSegments(line, other, kind, term) {
  const ranges = [];
  const sign = marker(line);
  const offset = sign ? 1 : 0;
  const content = line.slice(offset);
  if (!line.startsWith("@@")) {
    if (other !== null) {
      const words = sign === "-" ? wordRanges(content, other.slice(1)) : wordRanges(other.slice(1), content);
      if (words) {
        const own = sign === "-" ? words.del : words.add;
        for (const [start, end] of own) ranges.push([start + offset, end + offset, sign === "-" ? "wdel" : "wadd"]);
      }
    }
    for (const [start, end, cls] of syntaxRanges(content, kind)) ranges.push([start + offset, end + offset, cls]);
  }
  // Marks cover the whole line, marker included: the server searched it whole.
  for (const [start, end] of markRanges(line, term)) ranges.push([start, end, "mark"]);
  return merge(line, ranges);
}

// -- display: control characters are shown, never obeyed ---------------------------

// C0 (but "\t"), DEL, C1, U+2028/U+2029 and the bidi controls (ALM, LRM, RLM, the
// embeddings/overrides, the isolates): drawn as a line break, a space, a box or not
// at all, or reordering what is shown (Trojan Source). Each becomes ONE visible
// character, one UTF-16 unit for one: C0 its Control Picture (U+2400 + code, "\r"
// -> U+240D), DEL U+2421, the rest OTHER_MARK. render.py's `visible` is the same
// mapping (tests/web/test_js_visible.py; ADR 0017, "Display of control characters").
export const OTHER_MARK = "\u2370";
const CONTROLS = /[\u0000-\u0008\u000a-\u001f\u007f-\u009f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]/g;

export function visible(text) {
  return String(text ?? "").replace(CONTROLS, (c) => {
    const code = c.charCodeAt(0);
    if (code < 0x20) return String.fromCharCode(0x2400 + code);
    return code === 0x7f ? "\u2421" : OTHER_MARK;
  });
}

// A file name (or note) as displayed: the same characters as `visible`, but written
// "\uXXXX" (no offsets to keep), as render.py's name mapping does. Display only: the
// raw name stays in storage, URLs, editor links and copied commands.
export function visibleName(text) {
  return String(text ?? "").replace(CONTROLS, (c) => `\\u${c.charCodeAt(0).toString(16).toUpperCase().padStart(4, "0")}`);
}

// Every line ends in "\r" (a CRLF file): its display drops that "\r", so an edit in
// a Windows file has no noise; with mixed endings each "\r" stays (shown as U+240D).
export function uniformCrlf(lines) {
  return lines.length > 0 && lines.every((line) => String(line ?? "").endsWith("\r"));
}

// A line's segments as displayed, worked out on the raw line first so word changes
// and marks keep their offsets: the trailing "\r" cut from the last segments (when
// `crlf`), then every segment's text mapped one unit for one.
function display(segments, crlf) {
  let out = segments.map((s) => ({ text: s.text, classes: s.classes }));
  if (crlf) {
    const last = out[out.length - 1];
    if (last && last.text.endsWith("\r")) last.text = last.text.slice(0, -1);
    out = out.filter((s) => s.text !== "");
    if (!out.length) out = [{ text: "", classes: [] }];
  }
  return out.map((s) => ({ text: visible(s.text), classes: s.classes }));
}

// The segments of every stored line of one file: pairing runs over the whole list,
// so the caller slices it at the Telegram divider afterwards.
export function diffSegments(lines, path, q = "") {
  if (!Array.isArray(lines)) return [];
  const kind = syntaxOf(path);
  const term = searchTerm(q);
  const crlf = uniformCrlf(lines);
  let partner = [];
  try {
    partner = pairRuns(lines);
  } catch {
    partner = [];
  }
  return lines.map((raw, i) => {
    const line = String(raw ?? "");
    let segments;
    try {
      const at = partner[i] ?? -1;
      segments = lineSegments(line, at >= 0 ? String(lines[at]) : null, kind, term);
    } catch {
      segments = [{ text: line, classes: [] }];
    }
    return display(segments, crlf);
  });
}
