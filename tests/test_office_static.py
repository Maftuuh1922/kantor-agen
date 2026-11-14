"""Smoke check for the office front end: files exist, offline, expected exports.

String checks only. It needs no Node, no browser, and no network.
"""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "skeleton" / "office" / "static"


def read(rel: str) -> str:
    return (STATIC / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "rel",
    [
        "index.html",
        "office.js",
        "layout.js",
        "vendor/three.module.js",
        "vendor/three.core.js",
        "vendor/LICENSE-three.txt",
        "vendor/addons/loaders/GLTFLoader.js",
        "vendor/addons/utils/SkeletonUtils.js",
        "vendor/addons/utils/BufferGeometryUtils.js",
    ],
)
def test_static_file_exists_and_is_not_empty(rel):
    path = STATIC / rel
    assert path.is_file(), f"missing {rel}"
    assert path.stat().st_size > 0


def test_three_license_is_mit_text():
    text = read("vendor/LICENSE-three.txt")
    assert "MIT License" in text
    assert "three.js authors" in text


def test_index_has_importmap_for_vendored_three():
    html = read("index.html")
    match = re.search(r'<script\s+type="importmap"\s*>(.*?)</script>', html, re.S)
    assert match, "index.html needs an importmap"
    body = match.group(1)
    assert '"three": "./vendor/three.module.js"' in body
    assert '"three/addons/": "./vendor/addons/"' in body


def test_index_loads_nothing_from_the_network():
    html = read("index.html")
    # No external script, stylesheet, or other resource URLs (CDNs).
    for attr in re.findall(r'\b(?:src|href)\s*=\s*"([^"]*)"', html):
        assert not re.match(r"(?i)(https?:)?//", attr), f"external URL in index.html: {attr}"
    for tag in re.findall(r"<script\b[^>]*>", html, re.I):
        assert not re.search(r'src\s*=\s*"(https?:)?//', tag, re.I), tag
    # The importmap and inline code must not point at a CDN either.
    assert not re.search(r"https?://", html), "index.html must not contain http(s) URLs"


def test_index_has_hud_panel_and_loading_bar():
    html = read("index.html")
    for needle in ("<canvas", 'id="business-name"', 'id="pause-btn"', 'id="quality-btn"',
                   'id="side-panel"', 'id="loading"', "data-fill"):
        assert needle in html, needle


def test_vendored_code_has_no_http_imports():
    for rel in ("office.js", "layout.js", "main.js"):
        assert not re.search(r"""from\s+["']https?:""", read(rel)), rel
        assert not re.search(r"""import\(\s*["']https?:""", read(rel)), rel


def test_office_js_exports_public_api():
    js = read("office.js")
    assert re.search(r"export\s+function\s+createOffice\s*\(", js)
    assert re.search(r"export\s*\{[^}]*\blayout\b[^}]*\}", js)
    assert re.search(r"export\s*\{[^}]*\bGRID_SIZE\b[^}]*\}", js)
    for name in ("start", "resize", "setQuality", "addAgentDesk", "scene", "camera", "renderer"):
        assert re.search(rf"\b{name}\b", js), name
    assert '"normal"' in js and '"low"' in js
    # Shadow map 1024 px and the pixel ratio cap of 2 for normal quality.
    assert "mapSize.set(1024, 1024)" in js
    assert "Math.min(dpr, 2)" in js


def test_layout_js_exports_grid_and_helpers():
    js = read("layout.js")
    assert re.search(r"export\s+const\s+GRID_SIZE\s*=\s*0\.5\b", js)
    assert re.search(r"export\s+function\s+deskForAgent\s*\(", js)
    assert re.search(r"export\s+function\s+isBlocked\s*\(", js)
    assert re.search(r"export\s+const\s+layout\b", js)
    for name in ("pantrySpot", "meetingSpots", "isBlocked", "deskForAgent",
                 "standSpot", "seat", "rotation", "position"):
        assert re.search(rf"\b{name}\b", js), name
