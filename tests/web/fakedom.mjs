// A minimal DOM for the node tests: just what the page's modules use (elements,
// text nodes, attributes, dataset, listeners, focus, events bubbling to the document). getElementById creates a
// detached <div> for an id it has not seen, so a test needs no skeleton markup.

// Like the browser's HTMLCollection: indexable and iterable, but without the
// array methods (so `children.find(…)` fails here as it does in a browser).
class FakeCollection {
  constructor(items) {
    items.forEach((item, i) => {
      this[i] = item;
    });
    this.length = items.length;
  }

  item(i) {
    return this[i] ?? null;
  }

  *[Symbol.iterator]() {
    for (let i = 0; i < this.length; i += 1) yield this[i];
  }
}

class FakeNode {
  constructor(doc) {
    this.ownerDocument = doc;
    this.parentNode = null;
    this.childNodes = [];
  }

  get firstChild() {
    return this.childNodes[0] || null;
  }

  get children() {
    return new FakeCollection(this.elements());
  }

  elements() {
    return this.childNodes.filter((n) => n instanceof FakeElement);
  }

  get textContent() {
    return this.childNodes.map((n) => n.textContent).join("");
  }

  set textContent(value) {
    this.replaceChildren();
    if (value !== "" && value !== null && value !== undefined) this.append(String(value));
  }

  append(...nodes) {
    for (const n of nodes) this.insertBefore(typeof n === "object" ? n : this.ownerDocument.createTextNode(String(n)), null);
  }

  appendChild(node) {
    return this.insertBefore(node, null);
  }

  insertBefore(node, ref) {
    if (node.parentNode) node.remove();
    const i = ref ? this.childNodes.indexOf(ref) : -1;
    if (i < 0) this.childNodes.push(node);
    else this.childNodes.splice(i, 0, node);
    node.parentNode = this;
    return node;
  }

  remove() {
    if (!this.parentNode) return;
    // As in a browser, a focused node that leaves the document takes focus with it.
    const doc = this.ownerDocument;
    if (doc && doc.activeElement && this.contains(doc.activeElement)) doc.activeElement = null;
    const siblings = this.parentNode.childNodes;
    siblings.splice(siblings.indexOf(this), 1);
    this.parentNode = null;
  }

  replaceChildren(...nodes) {
    for (const n of [...this.childNodes]) n.remove();
    this.append(...nodes);
  }

  contains(node) {
    for (let n = node; n; n = n.parentNode) if (n === this) return true;
    return false;
  }
}

class FakeText extends FakeNode {
  constructor(doc, text) {
    super(doc);
    this.data = text;
  }

  get textContent() {
    return this.data;
  }

  set textContent(value) {
    this.data = String(value);
  }
}

const dataName = (prop) => `data-${String(prop).replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`;

function datasetOf(el) {
  return new Proxy(
    {},
    {
      get: (_, prop) => (typeof prop === "string" ? el.getAttribute(dataName(prop)) ?? undefined : undefined),
      set: (_, prop, value) => {
        el.setAttribute(dataName(prop), String(value));
        return true;
      },
      has: (_, prop) => el.hasAttribute(dataName(prop)),
      deleteProperty: (_, prop) => {
        el.removeAttribute(dataName(prop));
        return true;
      },
    },
  );
}

export class FakeElement extends FakeNode {
  constructor(doc, tag) {
    super(doc);
    this.tagName = tag.toUpperCase();
    this.attributes = new Map();
    // As in a browser: dataset is a view of the data-* attributes, so its
    // values are always strings.
    this.dataset = datasetOf(this);
    this.listeners = {};
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.checked = false;
    this.open = false;
    this.tabIndex = 0;
    this.scrolled = 0;
  }

  get id() {
    return this.getAttribute("id") || "";
  }

  get className() {
    return this.getAttribute("class") || "";
  }

  set className(value) {
    this.setAttribute("class", value);
  }

  get title() {
    return this.getAttribute("title") || "";
  }

  set title(value) {
    this.setAttribute("title", value);
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name === "id") this.ownerDocument.ids.set(String(value), this);
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  hasAttribute(name) {
    return this.attributes.has(name);
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  // A minimal Element.closest: "tag", "[attr]" or "tag[attr]" (what main.js uses).
  closest(selector) {
    const m = /^([a-z0-9]*)(?:\[([\w-]+)\])?$/i.exec(selector);
    if (!m) throw new Error(`fake closest: unsupported selector ${selector}`);
    for (let n = this; n && n.tagName; n = n.parentNode) {
      if (m[1] && n.tagName !== m[1].toUpperCase()) continue;
      if (m[2] && !n.hasAttribute(m[2])) continue;
      return n;
    }
    return null;
  }

  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  }

  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
  }

  dispatch(type, init = {}) {
    // As in a browser, the path is fixed when the event is dispatched (a handler
    // that replaces its own node doesn't cut it short), and it ends at the
    // document when the node is in it. composedPath() returns it, as in a browser.
    const path = [];
    for (let n = this; n; n = n.parentNode) path.push(n);
    if (this.isConnected) path.push(this.ownerDocument);
    const event = {
      type,
      target: this,
      defaultPrevented: false,
      stopped: false,
      preventDefault() {
        this.defaultPrevented = true;
      },
      stopPropagation() {
        this.stopped = true;
      },
      composedPath() {
        return [...path];
      },
      ...init,
    };
    for (const n of path) {
      if (event.stopped) break;
      event.currentTarget = n;
      for (const fn of n.listeners?.[type] || []) fn.call(n, event);
    }
    return event;
  }

  click() {
    if (!this.disabled) this.dispatch("click");
  }

  // As in a browser, a disabled control or a node outside the document can't
  // take focus: the call does nothing.
  focus() {
    if (this.disabled || !this.isConnected) return;
    if (this.ownerDocument.activeElement === this) return;
    this.ownerDocument.activeElement = this;
    this.dispatch("focusin"); // it bubbles, as in a browser
  }

  // In the document: its topmost ancestor is an element the document knows by id
  // (getElementById's roots stand in for the page's markup).
  get isConnected() {
    let top = this;
    while (top.parentNode) top = top.parentNode;
    return top.ownerDocument.ids.get(top.id) === top;
  }

  blur() {
    if (this.ownerDocument.activeElement === this) this.ownerDocument.activeElement = null;
  }

  scrollIntoView() {
    this.scrolled += 1;
  }

  // A test sets `top` (px from the viewport's top) for the scroll-spy.
  getBoundingClientRect() {
    const top = this.top ?? 0;
    return { top, bottom: top, left: 0, right: 0, width: 0, height: 0 };
  }

  select() {}

  // Every descendant element, depth first (a test helper, not a DOM API).
  all() {
    return this.elements().flatMap((c) => [c, ...c.all()]);
  }

  byClass(name) {
    return this.all().filter((e) => e.className.split(" ").includes(name));
  }
}

export class FakeDocument {
  constructor() {
    this.ids = new Map();
    this.activeElement = null;
    this.visibilityState = "visible";
    this.listeners = {};
    this.documentElement = new FakeElement(this, "html");
    this.cookie = "";
  }

  createElement(tag) {
    return new FakeElement(this, tag);
  }

  createElementNS(ns, tag) {
    return new FakeElement(this, tag);
  }

  createTextNode(text) {
    return new FakeText(this, text);
  }

  getElementById(id) {
    if (!this.ids.has(id)) new FakeElement(this, "div").setAttribute("id", id);
    return this.ids.get(id);
  }

  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  }
}

// A fetch whose replies the test controls: `calls` records each request, and
// `reply(status, body)` answers the oldest open one (or queue them up front).
export function fakeFetch() {
  const calls = [];
  const waiting = [];
  const queued = [];
  const answer = (resolve, [status, body]) =>
    resolve({ ok: status >= 200 && status < 300, status, json: async () => body });
  const fetch = (path, options) => {
    calls.push({
      path,
      signal: options.signal,
      method: options.method,
      headers: options.headers,
      body: options.body ? JSON.parse(options.body) : undefined,
    });
    return new Promise((resolve) => {
      if (queued.length) answer(resolve, queued.shift());
      else waiting.push(resolve);
    });
  };
  const reply = (status, body) => {
    if (waiting.length) answer(waiting.shift(), [status, body]);
    else queued.push([status, body]);
  };
  return { fetch, calls, reply };
}

// Let pending promise callbacks run.
export const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
