// One toast at a time in the aria-live region (UI refine spec §4.6); a new toast
// replaces the current one. Import-safe: the region is looked up when shown.
import { $, h, icon } from "./dom.js";

let timer = null;
let current = null;

export function showToast(text, { action = "", onAction = null, ms = 5000, kind = "" } = {}) {
  clearTimeout(timer);
  const parts = [h("span", { class: "toast-text", text })];
  if (action && onAction) {
    parts.push(
      h("button", {
        type: "button",
        class: "link",
        onclick: () => {
          closeToast();
          onAction();
        },
      }, icon("undo"), action),
    );
  }
  parts.push(h("button", { type: "button", class: "close", "aria-label": "Dismiss", onclick: closeToast }, icon("close")));
  const toast = h("div", { class: `toast ${kind}`.trim() }, ...parts);
  $("toasts").replaceChildren(toast);
  current = toast;
  timer = setTimeout(closeToast, ms);
  return toast;
}

// True if a toast was open.
export function closeToast() {
  clearTimeout(timer);
  timer = null;
  if (!current) return false;
  current.remove();
  current = null;
  return true;
}
