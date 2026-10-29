"""Run the pure-function JavaScript tests in tests/js/ under node."""

import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_node(script):
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    result = subprocess.run(
        ["node", os.path.join("tests", "js", script)],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_aura_palette():
    _run_node("aura.test.js")


def test_motion_helpers():
    _run_node("motion.test.js")


def test_queue_eta_wording():
    _run_node("queue-eta.test.js")
