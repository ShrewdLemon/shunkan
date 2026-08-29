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
    # Only the EVIDENCE must not be sliced. Slicing a timestamp to 16 chars is
    # fine, and an earlier version of this test failed on exactly that.
    for line in body.splitlines():
        if ".slice(0," not in line:
            continue
        assert "quote" not in line and "evidence" not in line, \
            f"drawSupplyMap truncates evidence again: {line.strip()}"
    assert 'classList.toggle("open")' in body
    assert "/extract" in body, "supply map must read the validated extraction"


NET_JS = ROOT / "tests" / "net_render_test.js"


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_entity_network_renders_without_junk_or_zeroed_money():
    """The NET view's own render pass, over payloads shaped like the API's.

    Needs no jsdom: these functions build strings, and building the string
    wrong is how the money bug shipped - HDFC Bank's whole related-party book,
    every figure real, every one displayed as "0 Cr".
    """
    r = subprocess.run(["node", str(NET_JS)], cwd=ROOT, capture_output=True,
                       text=True, env=_env())
    assert r.returncode == 0, r.stdout + r.stderr


def test_net_flow_uses_one_scale_for_both_directions():
    """Independently scaling each side is the lie this picture tells well: it
    would draw a Rs 8,603 Cr purchase at the same height as a Rs 617,086 Cr
    sale. One rupees-per-pixel figure, computed from the larger side, and
    printed on the diagram so the reader can check it."""
    src = (ROOT / "src/shunkan/server/static/app.js").read_text()
    body = src[src.index("function netSankey"):]
    body = body[:body.index("\n}\n")]
    assert body.count("pxPerRs") >= 3, "the shared scale is gone"
    assert "Math.max(sTot, bTot)" in body, \
        "the scale must come from the LARGER side, or the small side overflows"
    assert "per pixel" in body, "the scale must be stated on the diagram"
    assert "smaller counterparties" in body, \
        "counterparties past the cut must be summed into a labelled band, " \
        "never dropped"


def test_net_never_formats_a_real_amount_as_zero():
    """A filed number displayed as 0 is the insider-dealing bug again."""
    src = (ROOT / "src/shunkan/server/static/app.js").read_text()
    body = src[src.index("function netCr"):]
    body = body[:body.index("\n}\n")]
    assert "maximumFractionDigits: 0 }" not in body, \
        "fixed 0-decimal crore formatting zeroes out every sub-crore amount"
    assert "<0.01" in body, "small non-zero amounts need a display floor"


def test_extract_button_hidden_when_there_is_nothing_to_extract():
    """A control that cannot succeed reads as a broken app. Emmvee and
    PhysicsWallah listed into NIFTY 500 weeks ago and have filed no annual
    report on either exchange - the page must say the SOURCE is missing, not
    offer a button that will fail."""
    src = (ROOT / "src/shunkan/server/static/app.js").read_text()
    body = src[src.index("async function drawSupplyMap"):]
    body = body[:body.index("\n}\n")]
    assert "d.runnable === false" in body, \
        "supply map ignores the API's runnable flag"
    gate = body[body.index("d.runnable === false"):]
    gate = gate[:gate.index("EXTRACT NOW")]
    assert "missing SOURCE" in gate, \
        "the un-runnable branch must explain that no filing exists"
