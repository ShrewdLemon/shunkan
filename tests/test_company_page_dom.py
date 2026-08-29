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


def test_every_analyse_code_is_typeable_in_the_command_bar():
    """The hub and the command bar are two doors to the same rooms.

    NET and ADM were added to ANL_GROUPS and not to CODE_ALIAS, so both views
    existed, rendered, and could only be reached by clicking through the
    Analyse hub - typing the code did nothing. A screen listed in the hub but
    not typeable is a screen the muscle memory cannot find.
    """
    import re

    src = (ROOT / "src/shunkan/server/static/app.js").read_text()
    groups = src[src.index("const ANL_GROUPS"):src.index("const ANL_VIEWS")]
    hub_codes = set(re.findall(r'\["[a-z]+", "([A-Z]{2,3})"', groups))
    alias = src[src.index("const CODE_ALIAS"):src.index("function wireCmdline")]
    typeable = set(re.findall(r'\b([A-Z]{2,3}):\s*"', alias))
    missing = hub_codes - typeable
    assert not missing, (
        f"listed in the Analyse hub but not typeable: {sorted(missing)}. "
        f"Add them to CODE_ALIAS.")


def test_company_page_carries_the_related_party_block():
    """Related parties are ABOUT the company, so they belong on the company
    page beside ownership and the supply chain.

    Building them only into NET meant a reader who wanted to know who Reliance
    sells to had to know a second screen existed and know its three-letter
    code. The deep view keeps what needs a whole screen - the multi-hop walk,
    the canvas map, drilling into a counterparty - and the company page
    answers the direct question.
    """
    src = (ROOT / "src/shunkan/server/static/app.js").read_text()
    body = src[src.index("async function renderCompany"):]
    body = body[:body.index("\nasync function drawSegments")]
    assert "drawRelatedParties" in body, \
        "the company page never calls drawRelatedParties"
    assert 'id="cmp-rpt"' in body, "no mount point for the related-party block"

    draw = src[src.index("async function drawRelatedParties"):]
    draw = draw[:draw.index("\nasync function renderHeatmap")]
    assert "/api/entity/" in draw, "must read the entity endpoint"
    # The same shipped renderers as NET, not a re-typed copy that can drift.
    # netFlowBlock is the flow diagram's wrapper - accept it, but then prove
    # it really does delegate, or this test passes on an empty shim.
    for fn in ("netTable", "netStructure", "netCr"):
        assert fn in draw, f"{fn} not reused on the company page"
    assert "netFlowBlock" in draw or "netSankey" in draw, \
        "the company page draws no flow diagram"
    if "netSankey" not in draw:
        wrapper = src[src.index("function netFlowBlock"):]
        wrapper = wrapper[:wrapper.index("\n}")]
        assert "netSankey" in wrapper, \
            "netFlowBlock does not delegate to netSankey - the company page " \
            "has its own copy of the diagram and they will drift"
    assert 'show("network"' in draw, \
        "no route through to the full network for the multi-hop walk"
    assert "no related-party filing" in draw, \
        "a company with nothing filed must get a named reason, not a blank"


def test_net_sankey_does_not_depend_on_the_net_view_being_open():
    """netSankey read NET.d for its hub label, which made it a NET-only
    function. The company page calls it too, so the label comes from the
    payload it was handed."""
    src = (ROOT / "src/shunkan/server/static/app.js").read_text()
    fn = src[src.index("function netSankey"):]
    fn = fn[:fn.index("\n}\n")]
    assert "NET.d" not in fn, \
        "netSankey still reaches for the NET view's global state"


def test_every_fetch_has_a_deadline():
    """A request that never settles renders as a spinner that never stops.

    That is the worst refusal this app can make: it is visually identical to
    "still working" and it lasts forever. The entity graph showed "walking the
    graph" indefinitely because getJSON awaited a fetch with no timeout and
    openNode had no catch, so a server restart mid-session produced a screen
    with nothing to read and nothing to click.
    """
    src = (ROOT / "src/shunkan/server/static/app.js").read_text()
    fn = src[src.index("async function getJSON"):]
    fn = fn[:fn.index("\nasync function postJSON")]
    assert "AbortController" in fn, "getJSON can hang forever"
    assert "setTimeout" in fn and "abort()" in fn
    assert "clearTimeout" in fn, "the timer must be cleared or it leaks"
    assert "did not respond within" in fn, \
        "a timeout must say so, not surface as a bare TypeError"


def test_a_failed_request_cannot_leave_a_spinner_on_screen():
    """The backstop, because views are hand-written and some forget to catch."""
    src = (ROOT / "src/shunkan/server/static/app.js").read_text()
    assert "unhandledrejection" in src, "no global failure net"
    net = src[src.index("function wireFailureNet"):]
    net = net[:net.index("\nfunction bootChrome")]
    assert 'querySelectorAll(".loading")' in net, \
        "the net must actually replace visible spinners"
    boot = src[src.index("function bootChrome"):]
    boot = boot[:boot.index("\n}")]
    assert "wireFailureNet()" in boot, "the net is defined but never armed"


def test_entity_graph_handles_its_own_failures():
    """openNode awaited three fetches with no error path at all."""
    src = (ROOT / "src/shunkan/server/static/app.js").read_text()
    # start at the fail() helper, not openNode - the retry button lives there
    blk = src[src.index("const fail = (b, what"):src.index('$("#gph-go").onclick')]
    # Brace-match each `await getJSON` back to see whether a `try {` opened
    # before it and had not yet closed. Counting keywords in the preceding
    # text cannot tell an enclosing try from one that already ended - the
    # first version of this test got that wrong and passed a real hole.
    for idx in [i for i in range(len(blk)) if blk.startswith("await getJSON", i)]:
        depth, guarded, j = 0, False, idx
        while j > 0:
            j -= 1
            if blk[j] == "}":
                depth += 1
            elif blk[j] == "{":
                if depth == 0:
                    if blk[max(0, j - 6):j].rstrip().endswith("try"):
                        guarded = True
                    break
                depth -= 1
        line = blk[idx:blk.index("\n", idx)].strip()
        assert guarded, f"unguarded await in the entity graph: {line[:70]}"
    assert "RETRY" in blk, "a failure with no way to retry is a dead end"
