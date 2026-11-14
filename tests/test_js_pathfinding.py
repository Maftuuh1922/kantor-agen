"""Runs the Node test suite for the office pathfinding and walker modules.

The JS tests use Node's built-in runner (`node --test`), so no npm install is needed.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not on PATH; install Node.js to run the JS tests")
def test_node_test_suite_passes():
    result = subprocess.run(
        [NODE, "--test", "tests/js/"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, f"node --test failed:\n{result.stdout}\n{result.stderr}"
