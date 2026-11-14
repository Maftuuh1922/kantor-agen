"""Structural checks for the performance work (task 8.8, Requirement 2.10).
String checks only: no browser. The helpers in perf.js are tested in tests/js/perf.test.mjs, and the
numbers in docs/performance.md were measured in headless Chromium with ?mock=1&agents=8&perf=1.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "skeleton" / "office" / "static"
DOC = ROOT / "docs" / "performance.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_perf_js_exists():
    assert (STATIC / "perf.js").is_file()


def test_probe_is_gated_by_perf_flag():
    js = read(STATIC / "perf.js")
    assert re.search(r"""get\(\s*["']perf["']\s*\)\s*===\s*["']1["']""", js)
    # The gate comes before anything is hooked, wrapped or drawn.
    start = js.index("export function startPerfProbe")
    body = js[start:]
    gate = body.index("if (!perfEnabled(search)) return null;")
    assert gate < body.index("renderer.render =")
    assert gate < body.index("document.createElement")
    assert gate < body.index("window.__perf")


def test_main_starts_the_probe_and_the_hint_is_never_automatic():
    main = read(STATIC / "main.js")
    assert "startPerfProbe(" in main
    # The only call that switches to low is behind a click on the hint's button.
    assert main.count('applyQuality("low")') == 1
    assert re.search(r'quality-hint-yes"\)\.addEventListener\("click",[^\n]*applyQuality\("low"\)', main)


def test_hidden_tab_pauses_the_loop():
    office = read(STATIC / "office.js")
    assert "visibilitychange" in office and "document.hidden" in office


def test_perf_files_make_no_external_requests():
    for rel in ["perf.js", "index.html", "main.js"]:
        text = read(STATIC / rel)
        assert not re.search(r"https?://", text), f"{rel} contains an http(s) URL"
        assert not re.search(r"""\bimport\s*\(\s*["']""", text), f"{rel} has a dynamic import"
    assert not re.search(r"\bfrom\s+[\"'](?!\./|three)", read(STATIC / "perf.js"))


def test_performance_doc_exists_and_covers_both_modes():
    assert DOC.is_file()
    text = read(DOC).lower()
    assert "normal" in text and "low" in text
    assert "?perf=1" in text
    assert "not verified" in text  # the 30 fps target is not measured on real hardware
