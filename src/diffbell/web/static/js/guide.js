// The Telegram setup guide (bot onboarding spec §3): static/guide.json, the guide's
// single source (scripts/gen_guide.py renders the same file to Markdown), shown as
// the fourth view. Plain text only: built with h() and text nodes, never parsed as
// markup. Import-safe: guideNodes is pure; showGuide uses the page's api().
import { api } from "./api.js";
import { $, h, setText } from "./dom.js";
import { writeUrl } from "./feed.js";
import { state } from "./state.js";

export const GUIDE_URL = "/static/guide.json";
const SAFE_URL = /^https:\/\/t\.me\//; // the guide links only to Telegram

let guide = null; // the loaded guide.json

// Every section's id: what ?step= may name.
export function guideIds(data) {
  return (data.sections || []).map((s) => s.id);
}

// The guide's nodes, built with the given h(tag, props, ...children) (the page's
// dom.h, or a plain one in the tests). Pure. The section `step` is marked current.
export function guideNodes(h, data, step) {
  const nodes = (data.intro || []).map((text) => h("p", { class: "lead", text }));
  for (const section of data.sections || []) {
    const body = [h("h3", { text: section.title })];
    for (const text of section.paragraphs || []) body.push(h("p", { text }));
    const links = (section.links || []).filter((link) => SAFE_URL.test(link.url));
    if (links.length) {
      body.push(
        h(
          "ul",
          { class: "guide-links" },
          ...links.map((link) =>
            h("li", {}, h("a", { href: link.url, target: "_blank", rel: "noopener noreferrer", text: link.text })),
          ),
        ),
      );
    }
    for (const item of section.items || []) {
      body.push(h("h4", { text: item.title }));
      for (const text of item.paragraphs || []) body.push(h("p", { text }));
    }
    const current = section.id === step ? " current" : "";
    nodes.push(h("section", { class: `guide-step${current}`, id: `guide-${section.id}` }, ...body));
  }
  return nodes;
}

// The guide view was opened (main.js showView): load guide.json once, render it,
// and scroll to ?step=. A step the guide doesn't have is dropped from the URL.
export async function showGuide() {
  if (!guide) {
    try {
      guide = await api("GET", GUIDE_URL);
    } catch (err) {
      $("guide-body").replaceChildren(h("p", { class: "message error", text: `The guide didn't load: ${err.message}` }));
      return;
    }
  }
  if (state.view !== "guide") return; // left while it loaded
  if (state.step && !guideIds(guide).includes(state.step)) {
    state.step = "";
    writeUrl();
  }
  setText($("guide-title"), guide.title);
  $("guide-body").replaceChildren(...guideNodes(h, guide, state.step));
  if (state.step) $(`guide-${state.step}`).scrollIntoView();
}
