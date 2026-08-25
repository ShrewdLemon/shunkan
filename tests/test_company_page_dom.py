"""Front-end behaviour that pytest can still guard.

Two real bugs in this repo have been invisible to a green suite and obvious in
a screenshot, so the parts of the UI that carry evidence get an actual DOM
test. tests/splc_dom_test.js extracts the shipped click handler out of app.js
and runs it under jsdom - it fails if someone edits the handler, which a
re-typed copy of the logic would not.

Skipped, never failed, when node or jsdom is absent: this must not turn a
Python-only checkout red.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_JS = ROOT / "tests" / "splc_dom_test.js"


def _env() -> dict:
    """Inherit the real environment - a hand-written PATH found no node at all
    on this machine, and the skip would then have hidden the whole test."""
    return {**os.environ, "NODE_PATH": str(ROOT / "node_modules")}


def _has_jsdom() -> bool:
    if not shutil.which("node"):
        return False
    r = subprocess.run(["node", "-e", "require('jsdom')"], cwd=ROOT,
                       capture_output=True,
                       env=_env())
    return r.returncode == 0


@pytest.mark.skipif(not _has_jsdom(),
                    reason="needs node + jsdom (npm install --no-save jsdom)")
def test_supply_map_tiles_expand_and_keep_full_evidence():
    r = subprocess.run(["node", str(TEST_JS)], cwd=ROOT, capture_output=True,
                       text=True,
                       env=_env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "0 failed" in r.stdout


def test_app_js_does_not_truncate_supply_chain_evidence():
    """The evidence sentence is the entire basis for a node existing. Clamping
    it in CSS is fine; slicing it in the template loses it for good - it stops
    being searchable with the browser find bar and copies out mangled."""
    src = (ROOT / "src/shunkan/server/static/app.js").read_text()
    body = src[src.index("async function drawSupplyMap"):]
    body = body[:body.index("\n}\n")]
    assert ".slice(0," not in body.replace("// slice(0,190)", ""), \
        "drawSupplyMap truncates evidence again"
    assert 'classList.toggle("open")' in body
