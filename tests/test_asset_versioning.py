"""Asset URLs must change when the asset changes, and only then.

index.html shipped hand-written cache busters - app.js?v=80, styles.css?v=51 -
that a human had to remember to bump. Across one session of edits nobody did,
so browsers kept serving the cached v=80 bundle. The symptoms all pointed at
the server, which was serving the correct file the whole time:

  a whole page section was missing, because that cached build predated it
  a screen hung forever on a spinner, because that build predated its error
  handling
  reloading changed nothing, because the URL had not changed

A number a human maintains is a number that goes stale.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from shunkan.server.api import create_app


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app())


def _versions(html: str) -> dict[str, str]:
    return dict(re.findall(r"/static/([\w.]+)\?v=([\w]+)", html))


def test_assets_are_versioned_by_content(client):
    from shunkan.server.api import STATIC_DIR

    import hashlib

    html = client.get("/").text
    got = _versions(html)
    assert "app.js" in got and "styles.css" in got, got
    for asset, ver in got.items():
        want = hashlib.md5((STATIC_DIR / asset).read_bytes()).hexdigest()[:10]
        assert ver == want, (
            f"{asset} is served as ?v={ver} but its content hashes to {want} - "
            f"browsers will keep the stale copy")


def test_no_hand_written_version_numbers_survive(client):
    """The literal ?v=80 in the file must not reach the browser."""
    html = client.get("/").text
    for asset, ver in _versions(html).items():
        assert not ver.isdigit(), (
            f"{asset} still carries a hand-maintained version (?v={ver}); "
            f"it will go stale the next time someone edits the file")


def test_version_changes_when_the_file_does(client, tmp_path):
    from shunkan.server.api import STATIC_DIR

    target = STATIC_DIR / "app.js"
    before = _versions(client.get("/").text)["app.js"]
    original = target.read_bytes()
    try:
        target.write_bytes(original + b"\n// touched by a test\n")
        after = _versions(client.get("/").text)["app.js"]
    finally:
        target.write_bytes(original)
    assert after != before, "editing the asset did not change its URL"
    assert _versions(client.get("/").text)["app.js"] == before, \
        "restoring the asset did not restore its URL - the hash is not stable"


def test_index_itself_is_never_cached(client):
    """The stamps live in index.html, so a cached index pins stale stamps."""
    r = client.get("/")
    cc = r.headers.get("cache-control", "")
    assert "no-store" in cc or "no-cache" in cc, \
        f"index.html is cacheable ({cc!r}); it would freeze the asset versions"


def test_ticker_search_puts_the_obvious_company_first(client):
    """NSE tickers are not guessable from the name - ICICI Bank is ICICIBANK,
    State Bank is SBIN - so typing "icici" must offer the bank.

    Ranking ties by ticker LENGTH put ICICIGI above ICICIBANK: correct by
    string length and useless to a reader. Size is the honest tiebreak.
    """
    for term, want in [("icici", "ICICIBANK"), ("hdfc", "HDFCBANK"),
                       ("sbi", "SBIN"), ("kotak", "KOTAKBANK"),
                       ("wip", "WIPRO"), ("reliance", "RELIANCE")]:
        r = client.get(f"/api/symbols/search?q={term}&limit=5").json()
        syms = [m["symbol"] for m in r["matches"]]
        assert syms, f"{term!r} matched nothing"
        assert syms[0] == want, f"{term!r} -> {syms}, expected {want} first"


def test_ticker_search_is_bounded_and_survives_junk(client):
    assert client.get("/api/symbols/search?q=").json()["matches"] == []
    assert client.get("/api/symbols/search?q=%20%20").json()["matches"] == []
    assert client.get("/api/symbols/search?q=zzzzzzzz").json()["matches"] == []
    many = client.get("/api/symbols/search?q=a&limit=999").json()["matches"]
    assert len(many) <= 25, "limit must be capped"
    # an exact ticker must win outright
    r = client.get("/api/symbols/search?q=TCS&limit=5").json()
    assert r["matches"][0]["symbol"] == "TCS"


def test_every_match_carries_a_name_to_disambiguate(client):
    """A list of bare tickers is a second guessing game."""
    for m in client.get("/api/symbols/search?q=tata&limit=6").json()["matches"]:
        assert m.get("name"), f"{m['symbol']} has no company name"


def test_periods_sort_by_date_not_by_text():
    """Sorting "Mar 2022"/"Sep 2022" as text puts every March before every
    September, so Reliance's six half-years read Mar 22, Mar 23, Mar 24,
    Sep 22, Sep 23, Sep 24.

    The bar chart drew them in that order, and the counterparty SPARKLINES
    plot against this list - so every "trend" was three March readings
    followed by three September readings. A line through points that are not
    in time order is not a trend; it is a shape that looks like one.

    Tested on the key function rather than on a live company: conftest points
    SHUNKAN_HOME at a temp directory, so the suite has no companies in it. The
    first version of this test looped over real symbols, got 404 for every
    one, skipped its whole body and passed a guard that only fired because I
    had written one - a test that asserts nothing is worse than no test.
    """
    from shunkan.server.api import _period_key

    scrambled = ["Sep 2024", "Mar 2022", "Sep 2022", "Mar 2024",
                 "Mar 2023", "Sep 2023"]
    assert sorted(scrambled, key=_period_key) == [
        "Mar 2022", "Sep 2022", "Mar 2023", "Sep 2023", "Mar 2024", "Sep 2024"]

    # text sorting would have produced this; make the difference explicit
    assert sorted(scrambled) != sorted(scrambled, key=_period_key)

    # a year boundary must beat the month
    assert sorted(["Jan 2025", "Dec 2024"], key=_period_key) == \
        ["Dec 2024", "Jan 2025"]
    # every month name parses
    for i, mon in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1):
        assert _period_key(f"{mon} 2024")[:3] == (0, 2024, i)


def test_period_ordering_survives_an_unknown_label():
    """A filer inventing a new period format must not take the endpoint down:
    it sorts last, under its own text, rather than raising."""
    from shunkan.server.api import _period_key

    for junk in ("Q3 FY24", "", "   ", "2024", "Marzo 2024", "Mar"):
        key = _period_key(junk)
        assert key[0] == 1, f"{junk!r} was parsed as a real date"
    mixed = ["Sep 2022", "Q3 FY24", "Mar 2022"]
    assert sorted(mixed, key=_period_key) == ["Mar 2022", "Sep 2022", "Q3 FY24"]


def test_the_entity_endpoint_uses_that_key():
    """The helper is only useful if the endpoint actually calls it."""
    import inspect

    from shunkan.server import api

    body = inspect.getsource(api.create_app)
    block = body[body.index("periods = sorted("):]
    assert "_period_key" in block[:200], \
        "the entity endpoint still sorts periods as text"
