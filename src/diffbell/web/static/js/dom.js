// DOM helpers. Import-safe: main.js hands the document in (useDocument), so the
// node tests import every module without a browser. Untrusted text is only ever
// set with textContent or as an attribute value, never parsed as markup.

import { visibleName } from "./diffview.js";

let doc = null;

export function useDocument(d) {
  doc = d;
}

export const ICONS = "/static/icons.svg";
const SVG_NS = "http://www.w3.org/2000/svg";

export const $ = (id) => doc.getElementById(id);

export function h(tag, props = {}, ...children) {
  const el = doc.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (/^on/i.test(key)) {
      // A handler is always a function: a string would be code (and an "ONCLICK"
      // attribute is still one), so it is refused rather than set.
      if (typeof value !== "function") throw new TypeError(key);
      el.addEventListener(key.slice(2).toLowerCase(), value);
    }
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(typeof child === "object" ? child : doc.createTextNode(String(child)));
  }
  return el;
}

// One symbol of the sprite (U10): <svg class="icon"><use href="/static/icons.svg#name">.
export function icon(name, extra = "") {
  const svg = doc.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", `icon ${extra}`.trim());
  svg.setAttribute("aria-hidden", "true");
  const use = doc.createElementNS(SVG_NS, "use");
  use.setAttribute("href", `${ICONS}#${name}`);
  svg.append(use);
  return svg;
}

export function setIcon(svg, name) {
  const use = svg.children[0];
  const href = `${ICONS}#${name}`;
  if (use.getAttribute("href") !== href) use.setAttribute("href", href);
}

// Writes that skip unchanged values: an aria-live region isn't re-announced, and
// a node the person is using (focus, an open <select>) is left alone.
export function setText(el, text) {
  if (el.textContent !== text) el.textContent = text;
}

export function setAttr(el, name, value) {
  if (el.getAttribute(name) !== value) el.setAttribute(name, value);
}

// Through the attribute, so it works for SVG elements too.
export function setClass(el, name) {
  if ((el.getAttribute("class") || "") !== name) el.setAttribute("class", name);
}

export function setHidden(el, hidden) {
  if (el.hidden !== hidden) el.hidden = hidden;
}

// Make `container`'s children match `items`, in order, keyed by `key(item)` (kept
// in data-key). A node whose key survives is reused and only `update`d, and moved
// only when the order really changed, so polling never drops keyboard focus or
// closes an open <select>. `create` builds the node for a new key.
export function reconcile(container, items, key, create, update) {
  const keys = items.map((item) => String(key(item))); // data-key holds a string
  const wanted = new Set(keys);
  const existing = new Map();
  for (const node of [...container.children]) {
    const k = node.dataset.key;
    if (k === undefined || !wanted.has(k) || existing.has(k)) node.remove();
    else existing.set(k, node);
  }
  items.forEach((item, i) => {
    let node = existing.get(keys[i]);
    if (!node) {
      node = create(item);
      node.dataset.key = keys[i];
      existing.set(keys[i], node);
    }
    update(node, item);
    const at = container.children[i];
    if (at !== node) container.insertBefore(node, at || null);
  });
}

// The pause after typing before a field is checked (pattern lines, the preview).
export const TYPING_MS = 150;

export function debounce(fn, ms) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

// The non-empty, trimmed lines of a textarea's value.
export function lines(text) {
  return text.split("\n").map((s) => s.trim()).filter(Boolean);
}

// A file name may contain newlines or other control characters: show them as
// \uXXXX (diffview.js's code-point set, render.py's name mapping) so one name stays
// on one line and can't pose as several, and "\u202egpj.exe" can't read as "exe.jpg".
// Display only: links, commands and storage keep the raw name.
export function shown(name) {
  return visibleName(name);
}

export function plural(n, one, many) {
  return `${n} ${n === 1 ? one : many}`;
}

export function setMessage(el, text, kind = "") {
  setText(el, text);
  setClass(el, `message ${kind}`.trim());
}

export function baseName(path) {
  return String(path).split("/").filter(Boolean).pop() || "";
}
