// Keyboard shortcuts (UI refine spec §4.7). `shortcut` is the pure decision;
// main.js installs the listener and runs the actions.

export function isTyping(target) {
  if (!target) return false;
  const tag = String(target.tagName || "").toUpperCase();
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || Boolean(target.isContentEditable);
}

// The action for a keydown on a view, or null. Keys other than ⌘S / Ctrl+S and
// Esc are ignored while typing in a field.
export function shortcut(event, view) {
  if (event.isComposing || event.keyCode === 229) return null; // the key belongs to an IME
  const mod = event.metaKey || event.ctrlKey;
  if (mod && !event.altKey && String(event.key).toLowerCase() === "s") {
    if (view === "projects") return "save-project";
    if (view === "settings") return "save-settings";
    return null; // the browser's own save stays on the dashboard
  }
  if (event.key === "Escape") return "escape";
  if (mod || event.altKey || isTyping(event.target)) return null;
  switch (event.key) {
    case "/":
      if (view === "dashboard") return "focus-search";
      return view === "projects" ? "focus-tree-filter" : null;
    case "?":
      return "help";
    case "ArrowDown":
    case "j":
      return view === "dashboard" ? "next" : null;
    case "ArrowUp":
    case "k":
      return view === "dashboard" ? "previous" : null;
    default:
      return null;
  }
}

// `run(action, event)` returns false when it did nothing (the key then keeps its
// default behaviour).
export function installKeys(target, currentView, run) {
  target.addEventListener("keydown", (event) => {
    const action = shortcut(event, currentView());
    if (action && run(action, event) !== false) event.preventDefault();
  });
}
