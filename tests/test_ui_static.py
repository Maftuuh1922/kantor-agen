"""Structural checks for the office UI layer (task 8.7): files, offline, safe DOM writes, element ids.

String checks only. No Node, browser or network. The behavior of the formatting helpers is tested in
tests/js/ui-format.test.mjs, and the rendering was checked in a real browser with ?mock=1.
"""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "skeleton" / "office" / "static"
UI_MODULES = [
    "ui-text.js", "ui-format.js", "ui-api.js", "ui-mock.js", "ui-overlay.js", "ui-panel.js",
    "ui-hud.js", "ui-policy.js", "ui-feed.js", "ui-onboarding.js",
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


@pytest.mark.parametrize("rel", ["ui-hud.js", "ui-panel.js", "ui-policy.js", "ui-feed.js", "ui-overlay.js", "ui-onboarding.js"])
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
        # first-run screen (task 10.5)
        "onboarding", "onboarding-title", "onboarding-form", "onboarding-name", "onboarding-name-help",
        "onboarding-name-error", "onboarding-types", "onboarding-type-error", "onboarding-preview-title",
        "onboarding-preview-hint", "onboarding-preview-list", "onboarding-error", "onboarding-submit",
    ]
    for element_id in ids:
        assert re.search(rf'\bid="{re.escape(element_id)}"', html), f"index.html has no #{element_id}"
    # The global pause button is wired now: it starts disabled only until /api/office answers.
    assert 'aria-label="Jeda semua staf"' in html
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
    assert "aria-label" in js and "TEXT.panel.close" in js
    overlay = read("ui-overlay.js")
    assert 'el("button", "agent-label")' in overlay, "name labels must be buttons (focusable)"
    assert "aria-label" in overlay


def test_ui_format_has_no_dom_or_three_dependency():
    code = strip_js_comments(read("ui-format.js"))
    # The only import is the pure dictionary of Indonesian words (task 14.1).
    assert [i.strip() for i in re.findall(r"^\s*import\b[^;]*;", code, re.M)] == ['import { TEXT, fmt } from "./ui-text.js";']
    ui_text = strip_js_comments(read("ui-text.js"))
    assert not re.search(r"^\s*import\b", ui_text, re.M), "ui-text.js must not import anything"
    for name in ("document", "window", "navigator", "THREE", "fetch(", "localStorage"):
        assert name not in ui_text, f"ui-text.js uses {name}"
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
    for module in ("ui-api.js", "ui-mock.js", "ui-overlay.js", "ui-panel.js", "ui-hud.js", "ui-policy.js", "ui-feed.js",
                   "ui-onboarding.js"):
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


# ---- first-run onboarding screen (task 10.5) ----------------------------------------------------
def test_onboarding_screen_is_accessible():
    html = read("index.html")
    screen = html[html.index('id="onboarding"'):html.index('id="policy-modal"')]
    assert re.search(r'<label[^>]+for="onboarding-name"', screen), "the name input needs a label"
    assert "<legend" in screen, "the business types are a labelled group"
    assert 'aria-labelledby="onboarding-title"' in screen and 'lang="id"' in screen
    assert len(re.findall(r'role="alert"', screen)) >= 3, "validation errors are announced"
    assert 'aria-describedby="onboarding-name-help onboarding-name-error"' in screen
    assert '<form id="onboarding-form" novalidate>' in screen and 'type="submit"' in screen
    assert 'aria-live="polite"' in screen, "the staff preview is announced when it changes"
    js = read("ui-onboarding.js")
    assert 'type = "radio"' in js and "htmlFor" in js, "business types are real radio inputs with labels (arrow keys work)"
    assert "aria-invalid" in js and "inert" in js
    assert "focus()" in js


def test_onboarding_screen_comes_before_the_policy_dialog_in_the_page_and_above_it_in_css():
    html = read("index.html")
    assert html.index('id="onboarding"') < html.index('id="policy-modal"')
    css = read("style.css")
    z = lambda selector: int(re.search(rf"\n{re.escape(selector)}\s*\{{[^}}]*z-index:\s*(\d+)", css).group(1))
    assert z(".onboarding") > z(".modal") and z(".onboarding") > z(".loading") and z(".onboarding") < z(".fatal")
    for selector in (".onboarding-card", ".type-grid", ".type-card", ".staff-list", ".staff-item", ".onboarding-preview"):
        assert selector in css, selector


def test_onboarding_text_is_indonesian_and_writes_only_text():
    js = strip_js_comments(read("ui-onboarding.js"))
    for phrase in ("Nama usaha", "Pilih jenis usaha.", "Buka kantor", "Staf AI"):
        assert phrase in js, phrase
    # Server data (business types, staff names, error bodies) is only ever set as text.
    assert "textContent" in js and "createTextNode" not in js
    assert not re.search(r"\.(inner|outer)HTML", js) and "insertAdjacentHTML" not in js


def test_onboarding_module_has_no_dom_work_at_import_time():
    # tests/js/ui-onboarding.test.mjs imports it in Node, so the top level must not touch the DOM.
    code = strip_js_comments(read("ui-onboarding.js"))
    top_level = re.sub(r"(?m)^(export )?(async )?function [\s\S]*?\n}\n", "", code)  # drop function bodies
    assert not re.search(r"(?m)^(const|let|var)\s+\w+\s*=\s*(document|window|localStorage)", top_level)
    assert re.search(r"export function validateBusinessName\(", code)
    assert "document." in code  # only inside createOnboarding


def test_onboarding_talks_to_the_backend_endpoint_and_is_wired_into_main():
    js = read("ui-onboarding.js")
    assert '"/api/onboarding"' in js and '"POST"' in js and "JSON.stringify" in js
    assert "onboarding_required" in read("main.js"), "main.js must open the first-run screen when the server asks"
    main = read("main.js")
    assert "createOnboarding(" in main and "createOnboardingApi" in main and "createMockOnboardingApi" in main
    assert "onboarding=1" in main, "the ?mock=1&onboarding=1 preview is documented and wired"
    # The first-run screen is followed by the policy dialog (Req 1.3): data.policy.is_set is still checked.
    assert main.index("createOnboarding(") < main.index("needsPolicy")


def test_onboarding_files_have_no_external_urls_and_no_unsafe_calls():
    for rel in ("ui-onboarding.js", "index.html", "style.css", "main.js"):
        text = read(rel)
        assert not re.search(r"https?://", text), rel
        assert not re.search(r"""\bsrc\s*=\s*["']?(https?:)?//""", text, re.I), rel


# ---- task 14.1: Indonesian UI, bubbles, "Ubah nama" ------------------------------------------------------
def test_the_page_language_is_indonesian():
    html = read("index.html")
    assert '<html lang="id">' in html and 'lang="en"' not in html


def test_bubbles_never_take_a_click():
    # A bubble can sit on a neighbour's name label or on a character; clicks must pass through it.
    css = read("style.css")
    block = re.search(r"\n\.bubble\s*\{([^}]*)\}", css).group(1)
    assert re.search(r"pointer-events:\s*none", block)
    assert not re.search(r"pointer-events:\s*auto", block)
    # the name label stays clickable
    assert re.search(r"\.agent-label\s*\{[^}]*pointer-events:\s*auto", css, re.S)
    # and the overlay container itself lets clicks through to the 3D scene
    assert re.search(r"\.overlay\s*\{[^}]*pointer-events:\s*none", css, re.S)


def test_the_rename_control_is_wired_to_the_patch_endpoint():
    api, panel, main, fmt = read("ui-api.js"), read("ui-panel.js"), read("main.js"), read("ui-format.js")
    assert "patchAgent" in api and '"PATCH"' in api
    # the panel: button, form, two labelled inputs, Save and Cancel, validation, Escape cancels
    assert "TEXT.rename.open" in panel and "TEXT.rename.save" in panel and "TEXT.rename.cancel" in panel
    assert 'el("form", "rename-form")' in panel and "noValidate" in panel
    assert panel.count("lab.htmlFor = id") == 1 and "renameField(" in panel
    assert "validateRename(" in panel and "renameChanges(" in panel and "api.patchAgent(" in panel
    assert 'aria-describedby' in panel and 'setAttribute("role", "alert")' in panel and "aria-invalid" in panel
    assert 'e.key !== "Escape"' in panel and "e.stopPropagation()" in panel
    assert "onRenamed?.(" in panel and "aria-expanded" in panel
    # labels follow live: main.js reloads /api/office after a rename
    assert "onRenamed: (agent) =>" in main and "info.name = agent.name" in main and "hud.refresh()" in main
    assert re.search(r"export function validateRename\(", fmt) and "MAX_NAME_CHARS = 80" in fmt
    css = read("style.css")
    assert ".rename-form" in css and ".panel-link" in css


def test_the_policy_dialog_has_no_blank_gap_above_its_error():
    css = read("style.css")
    assert re.search(r"#policy-error\s*\{[^}]*margin", css, re.S), "the form-level error sits tight under the fields"


def test_the_loading_and_fatal_messages_come_from_the_dictionary():
    for rel in ("office.js", "characters.js", "main.js"):
        code = strip_js_comments(read(rel))
        assert '"Loading' not in code and "`Loading" not in code and '"Ready"' not in code, rel
        assert "ui-text.js" in code, rel
