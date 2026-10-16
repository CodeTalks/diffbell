"""The guide view under node (bot onboarding spec §3): guide.json rendered with h()
and text nodes, the requested step marked and scrolled to, unknown steps dropped."""

from __future__ import annotations

import json
from typing import Any

from diffbell.web.app import STATIC_DIR
from tests.web.node import needs_node, run_js

pytestmark = needs_node

GUIDE = json.loads((STATIC_DIR / "guide.json").read_text(encoding="utf-8"))

# A plain h(): the nodes as data, so the pure renderer is checked without a DOM.
PLAIN_H = """
const h = (tag, props = {}, ...children) => ({ tag, props, children: children.flat() });
"""


def nodes(step: str) -> Any:
    script = f"""
const guide = await import(js("guide.js"));
{PLAIN_H}
done(guide.guideNodes(h, {json.dumps(GUIDE)}, {json.dumps(step)}));
"""
    return run_js(script)


def test_every_section_is_rendered_in_order_as_text() -> None:
    out = nodes("")
    intro = [n for n in out if n["tag"] == "p"]
    sections = [n for n in out if n["tag"] == "section"]
    assert [n["props"]["text"] for n in intro] == GUIDE["intro"]
    assert [s["props"]["id"] for s in sections] == [f"guide-{s['id']}" for s in GUIDE["sections"]]
    first = sections[0]
    assert first["children"][0] == {
        "tag": "h3",
        "props": {"text": GUIDE["sections"][0]["title"]},
        "children": [],
    }
    texts = [c["props"]["text"] for c in first["children"] if c["tag"] == "p"]
    assert texts == GUIDE["sections"][0]["paragraphs"]


def test_links_open_telegram_in_a_new_tab() -> None:
    section = next(n for n in nodes("") if n["props"].get("id") == "guide-create")
    links = next(c for c in section["children"] if c["tag"] == "ul")
    anchor = links["children"][0]["children"][0]
    assert anchor == {
        "tag": "a",
        "props": {
            "href": "https://t.me/BotFather",
            "target": "_blank",
            "rel": "noopener noreferrer",
            "text": "Open @BotFather",
        },
        "children": [],
    }


def test_a_link_that_is_not_telegram_is_dropped() -> None:
    data = {
        "title": "T",
        "sections": [
            {
                "id": "a",
                "title": "A",
                "paragraphs": [],
                "links": [{"text": "x", "url": "javascript:alert(1)"}],
            }
        ],
    }
    script = f"""
const guide = await import(js("guide.js"));
{PLAIN_H}
done(guide.guideNodes(h, {json.dumps(data)}, ""));
"""
    assert run_js(script) == [
        {
            "tag": "section",
            "props": {"class": "guide-step", "id": "guide-a"},
            "children": [{"tag": "h3", "props": {"text": "A"}, "children": []}],
        }
    ]


def test_the_requested_step_is_marked_and_troubleshooting_items_are_headings() -> None:
    out = nodes("troubleshooting")
    classes = {n["props"]["id"]: n["props"]["class"] for n in out if n["tag"] == "section"}
    assert classes["guide-troubleshooting"] == "guide-step current"
    assert classes["guide-create"] == "guide-step"
    trouble = next(n for n in out if n["props"].get("id") == "guide-troubleshooting")
    headings = [c["props"]["text"] for c in trouble["children"] if c["tag"] == "h4"]
    assert headings == [i["title"] for i in GUIDE["sections"][-1]["items"]]


VIEW = f"""
const dom = await import(js("dom.js"));
const {{ state }} = await import(js("state.js"));
const api = await import(js("api.js"));
const feed = await import(js("feed.js"));
const guide = await import(js("guide.js"));
const doc = new FakeDocument();
dom.useDocument(doc);
const net = fakeFetch();
api.configureApi({{ fetch: net.fetch }});
const urls = [];
feed.initFeed({{ replaceUrl: (q) => urls.push(q) }});
const $ = (id) => doc.getElementById(id);
net.reply(200, {json.dumps(GUIDE)});
"""


def test_the_view_scrolls_to_its_step() -> None:
    script = (
        VIEW
        + """
state.view = "guide";
state.step = "start";
await guide.showGuide();
done([net.calls.map((c) => c.path), $("guide-title").textContent, $("guide-start").scrolled, $("guide-body").children.length, urls]);
"""
    )
    assert run_js(script) == [
        ["/static/guide.json"],
        GUIDE["title"],
        1,
        len(GUIDE["intro"]) + len(GUIDE["sections"]),
        [],
    ]


def test_an_unknown_step_goes_to_the_top_and_leaves_the_url() -> None:
    script = (
        VIEW
        + """
state.view = "guide";
state.step = "nowhere";
await guide.showGuide();
await guide.showGuide();  // loaded once
done([state.step, urls, net.calls.length]);
"""
    )
    assert run_js(script) == ["", ["view=guide"], 1]


def test_a_failed_load_says_so() -> None:
    script = """
const dom = await import(js("dom.js"));
const { state } = await import(js("state.js"));
const api = await import(js("api.js"));
const guide = await import(js("guide.js"));
const doc = new FakeDocument();
dom.useDocument(doc);
const net = fakeFetch();
api.configureApi({ fetch: net.fetch });
net.reply(404, { error: "Not Found" });
state.view = "guide";
await guide.showGuide();
done(doc.getElementById("guide-body").textContent);
"""
    assert run_js(script) == "The guide didn't load: Not found: Not Found"
