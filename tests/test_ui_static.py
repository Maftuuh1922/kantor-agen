"""Structural checks for the office UI layer (task 8.7): files, offline, safe DOM writes, element ids.

String checks only. No Node, browser or network. The behavior of the formatting helpers is tested in
tests/js/ui-format.test.mjs, and the rendering was checked in a real browser with ?mock=1.
"""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "skeleton" / "office" / "static"
UI_MODULES = [
    "ui-format.js", "ui-api.js", "ui-mock.js", "ui-overlay.js", "ui-panel.js",
    "ui-hud.js", "ui-policy.js", "ui-feed.js",
]
JS_FILES = [*UI_MODULES, "main.js"]


def read(rel: str) -> str:
    return (STATIC / rel).read_text(encoding="utf-8")


def strip_js_comments(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(^|[^:\"'`])//[^\n]*", r"\1", js)


@pytest.mark.parametrize("rel", [*UI_MODULES, "index.html", "style.css", "main.js"])
def test_ui_file_exists_and_is_not_empty(rel):
    path = STATIC / rel
    assert path.is_file(), f"missing {rel}"
    assert path.stat().st_size > 0


@pytest.mark.parametrize("rel", [*JS_FILES, "index.html", "style.css"])
def test_ui_files_make_no_external_requests(rel):
    text = read(rel)
    assert not re.search(r"https?://", text), f"{rel} contains an http(s) URL"
    assert not re.search(r"""(?<![\w.])//[a-z0-9.-]+\.[a-z]{2,}/""", strip_js_comments(text) if rel.endswith(".js") else "", re.I)
    assert not re.search(r"""@import\s+url\(\s*["']?(https?:)?//""", text, re.I)
    assert not re.search(r"""\bimport\s*\(\s*["']https?:""", text)
    assert not re.search(r"""\bfrom\s+["']https?:""", text)
    for url in re.findall(r"""\b(?:src|href)\s*=\s*["']([^"']*)["']""", text):
        assert not re.match(r"(?i)(https?:)?//", url), f"{rel}: external url {url}"


@pytest.mark.parametrize("rel", JS_FILES)
def test_ui_code_never_writes_html_from_strings(rel):
    code = strip_js_comments(read(rel))
    for banned in (r"\.innerHTML\b", r"\.outerHTML\b", r"insertAdjacentHTML", r"document\.write", r"\beval\s*\(", r"new\s+Function\b",
                   r"\.srcdoc\b", r"createContextualFragment", r"DOMParser"):
        assert not re.search(banned, code), f"{rel} uses {banned}"


@pytest.mark.parametrize("rel", ["ui-hud.js", "ui-panel.js", "ui-policy.js", "ui-feed.js", "ui-overlay.js"])
def test_server_text_goes_in_with_text_content(rel):
    code = read(rel)
    assert "textContent" in code or "createTextNode" in code, f"{rel} sets no text"


def test_index_has_the_ui_elements():
    html = read("index.html")
    ids = [
        # top bar
        "hud", "business-name", "spend-info", "spend-value", "paused-badge", "demo-badge", "pause-btn", "quality-btn", "hud-message",
        # scene overlay and agent panel
        "overlay", "side-panel",
        # activity feed
        "activity-feed", "feed-toggle", "feed-list", "feed-empty",
        # policy dialog
        "policy-modal", "policy-form", "policy-limit", "policy-limit-preview", "policy-limit-error",
        "policy-blocked", "policy-blocked-error", "policy-error", "policy-submit",
    ]
    for element_id in ids:
        assert re.search(rf'\bid="{re.escape(element_id)}"', html), f"index.html has no #{element_id}"
    # The global pause button is wired now: it starts disabled only until /api/office answers.
    assert 'aria-label="Pause all agents"' in html
    assert "coming soon" not in html


def test_index_ids_are_unique():
    html = read("index.html")
    ids = re.findall(r'\bid="([^"]+)"', html)
    assert len(ids) == len(set(ids)), sorted({i for i in ids if ids.count(i) > 1})


def test_policy_dialog_is_accessible():
    html = read("index.html")
    assert 'role="dialog"' in html and 'aria-modal="true"' in html
    assert 'aria-labelledby="policy-title"' in html
    assert re.search(r'<label[^>]+for="policy-limit"', html)
    assert 'role="alert"' in html
    js = read("ui-policy.js")
    assert "Escape" in js and "Tab" in js, "the dialog must trap focus and not close on Escape"
    assert "inert" in js


def test_panel_is_keyboard_accessible():
    js = read("ui-panel.js")
    assert "Escape" in js, "Escape must close the panel"
    assert "aria-label" in js and "Close agent panel" in js
    overlay = read("ui-overlay.js")
    assert 'el("button", "agent-label")' in overlay, "name labels must be buttons (focusable)"
    assert "aria-label" in overlay


def test_ui_format_has_no_dom_or_three_dependency():
    code = strip_js_comments(read("ui-format.js"))
    assert not re.search(r"^\s*import\b", code, re.M), "ui-format.js must not import anything"
    for name in ("document", "window", "navigator", "THREE", "fetch(", "localStorage"):
        assert name not in code, f"ui-format.js uses {name}"


def test_ui_format_exports_the_tested_helpers():
    js = read("ui-format.js")
    for name in ("formatRupiah", "parseRupiah", "truncate", "bubbleText", "formatJakartaTime", "stateColor",
                 "actionStatusColor", "validatePolicyInput", "spendSummary", "errorMessage"):
        assert re.search(rf"export\s+function\s+{name}\s*\(", js), name
    assert "Asia/Jakarta" in js
    assert re.search(r"BUBBLE_MAX_CHARS\s*=\s*60\b", js)


def test_main_wires_all_ui_modules():
    js = read("main.js")
    for module in ("ui-api.js", "ui-mock.js", "ui-overlay.js", "ui-panel.js", "ui-hud.js", "ui-policy.js", "ui-feed.js"):
        assert f'"./{module}"' in js, f"main.js does not import {module}"
    assert "is_set" in js, "main.js must open the policy dialog when the policy is not set"
    assert "kantor:activity" in js


def test_endpoints_used_match_the_backend():
    api = read("ui-api.js")
    for needle in ('"/api/office"', "/api/agents/", '"/api/office/pause"', '"/api/policy"', "/pause", "/api/activity"):
        assert needle in api, needle


def test_css_has_markers_pulse_and_reduced_motion():
    css = read("style.css")
    for color in ("green", "blue", "amber", "grey"):
        assert f".marker-{color}" in css, color
    assert "@keyframes marker-pulse" in css
    assert re.search(r"\.marker-amber::after\s*\{[^}]*animation:\s*marker-pulse", css, re.S)
    assert "prefers-reduced-motion" in css
    assert ".bubble" in css and ".agent-label" in css and ".modal" in css and ".activity-feed" in css


def test_overlay_click_has_no_press_duration_limit():
    # Found in the browser check: with slow frames (software GL) a normal click took over 1 s from press to
    # release and was dropped by an old 600 ms limit. A drag is told apart by distance and `controls.dragged`.
    code = strip_js_comments(read("ui-overlay.js"))
    assert "CLICK_MAX_MS" not in code
    assert "CLICK_MOVE_PX" in code and "controls?.dragged" in code
