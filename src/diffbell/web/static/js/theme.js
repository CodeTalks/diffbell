// The theme (theme switch spec §3): System (follow macOS), Light or Dark, chosen with
// the header button (a cycle, on every view) or in Settings → This page (settings
// redesign spec §5); the two always agree. Pure: main.js applies it to
// <html> and writes the cookie the server reads (diffbell_theme, web/auth.py
// THEME_COOKIE) to set data-theme before the first paint.
export const THEMES = ["system", "light", "dark"];

const NAMES = { system: "System", light: "Light", dark: "Dark" };
const YEAR = 365 * 24 * 60 * 60;

// The theme an attribute value stands for: anything but light or dark is System.
export function currentTheme(attribute) {
  return attribute === "light" || attribute === "dark" ? attribute : "system";
}

export function nextTheme(theme) {
  const i = THEMES.indexOf(currentTheme(theme));
  return THEMES[(i + 1) % THEMES.length];
}

export function themeLabel(theme) {
  const t = currentTheme(theme);
  return `Theme: ${NAMES[t]} (click for ${NAMES[nextTheme(t)]})`;
}

export function themeIcon(theme) {
  return `theme-${currentTheme(theme)}`;
}

// The document.cookie assignment for a choice: System deletes the cookie.
export function themeCookie(theme) {
  const t = currentTheme(theme);
  return t === "system"
    ? "diffbell_theme=; Path=/; Max-Age=0; SameSite=Strict"
    : `diffbell_theme=${t}; Path=/; Max-Age=${YEAR}; SameSite=Strict`;
}
