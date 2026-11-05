"""Web terminal guards that pytest can still see: the command bar reaches every
hub screen, and no request can leave a spinner on screen forever."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_every_analyse_code_is_typeable_in_the_command_bar():
    """The hub and the command bar are two doors to the same rooms.

    Two codes were once added to ANL_GROUPS and not to CODE_ALIAS, so both
    views existed, rendered, and could only be reached by clicking through the
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


def test_every_fetch_has_a_deadline():
    """A request that never settles renders as a spinner that never stops.

    That is the worst refusal this app can make: it is visually identical to
    "still working" and it lasts forever. One view spun indefinitely because
    getJSON awaited a fetch with no timeout and the caller had no catch, so a
    server restart mid-session produced a screen with nothing to read and
    nothing to click.
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


def test_a_toast_shows_its_message_as_text():
    """Alert text comes from rules, servers and filings, so it is data.

    The toast once set it as innerHTML, so an alert carrying markup would
    have run in the terminal page.
    """
    src = (ROOT / "src/shunkan/server/static/app.js").read_text()
    body = src[src.index("function toast("):src.index("/* ---------- panel")]
    assert "t.textContent = msg" in body
    assert "innerHTML" not in body and ", msg)" not in body
